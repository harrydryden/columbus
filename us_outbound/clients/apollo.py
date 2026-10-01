"""Apollo API client (SPEC 1.2, 7): READ ONLY.

Apollo is the universe (organization and people search), verified email reveal,
organization fields and job titles, and company-level site visits. This system never
writes to it: there is no method here that saves contacts, accounts, lists, labels or
sequences, every call goes through _read() with a write=False Op whose action is one of
guard.APOLLO_READ_ACTIONS, and _read() only knows the endpoints in READ_ENDPOINTS.

Search filters are sent as a JSON body (keys without the "[]" suffix; "x[min]" becomes
{"x": {"min": ...}}). Slicing a search under the 50,000-result cap (state x industry x
size band, SPEC 7) is the caller's job; this client just pages.

Shapes come from Apollo's published OpenAPI documents (mirrored Aug 2026) and its MCP
tool schemas. Auth is the master API key in the X-Api-Key header.

Credits (Apollo's API docs, 1 Oct 2026): an organization search costs 1 credit for a page
that returns at least one company and 0 for an empty one; job postings cost 1 credit per
request. The jobs record what they spend in credit_ledger (budget.py); this client only
says which reads are paid (paid_reads), so none is resent after a timeout.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from datetime import date, datetime
from typing import Any

from us_outbound.clients.guard import Op
from us_outbound.clients.http import ApiError, HttpClient

MAX_PER_PAGE = 100
MAX_PAGE = 500  # the display limit: 50,000 records = 500 pages of 100
BULK_MATCH_MAX = 10  # people/bulk_match takes up to 10 details per call
MAX_POSTINGS_PER_PAGE = 10_000  # job postings: a display limit of 10,000 records
_PATH_SEGMENT = re.compile(r"[A-Za-z0-9_-]+")  # an Apollo id in a URL path

# action -> (method, path). The only endpoints this client can reach.
READ_ENDPOINTS: dict[str, tuple[str, str]] = {
    "usage.credits": ("POST", "/usage_stats/credit_usage_stats"),  # 0 credits
    "organizations.search": ("POST", "/mixed_companies/search"),  # 1 credit per page with results
    "organizations.enrich": ("GET", "/organizations/enrich"),  # 1 credit per organization
    # PHASE0-CONFIRM: REST path of Organization Job Postings (the MCP tool
    # apollo_organizations_job_postings takes the organization id, page and per_page; 1 credit a request).
    "organizations.job_postings": ("GET", "/organizations/{organization_id}/job_postings"),
    "people.search": ("POST", "/mixed_people/api_search"),  # 0 credits; no emails returned
    "people.bulk_match": ("POST", "/people/bulk_match"),  # credits per revealed email
    # PHASE0-CONFIRM: REST path of Apollo's website-visitor domain aggregates (the MCP tool
    # apollo_website_visitors_domain_aggregates takes organization_id, domain, from, to).
    "website_visitors.domain_aggregates": ("GET", "/website_visitors/domain_aggregates"),
}

# bulk_match query flags: never personal emails (SPEC 1.4), never phones, and no waterfall,
# whose results only arrive by webhook (there is no public endpoint, SPEC 2).
BULK_MATCH_FLAGS = {
    "reveal_personal_emails": "false",
    "reveal_phone_number": "false",
    "run_waterfall_email": "false",
    "run_waterfall_phone": "false",
}


def normalize_filters(filters: Mapping[str, Any]) -> dict[str, Any]:
    """Documented query-style keys to JSON-body keys.

    "organization_locations[]" -> "organization_locations";
    "organization_num_jobs_range[min]" -> {"organization_num_jobs_range": {"min": ...}}.
    PHASE0-CONFIRM: the docs list these as query parameters; the JSON body form is the one
    Apollo's own tools send, and it keeps 1,000-domain lists out of the URL.
    """
    out: dict[str, Any] = {}
    for key, value in filters.items():
        k = str(key)
        if k.endswith("[]"):
            k = k[:-2]
        if k.endswith("]") and "[" in k:
            base, sub = k[:-1].split("[", 1)
            out.setdefault(base, {})[sub] = value
            continue
        if isinstance(value, Mapping) and isinstance(out.get(k), dict):
            out[k].update(value)
        else:
            out[k] = value
    for k in ("page", "per_page"):
        out.pop(k, None)
    return out


def organizations_in(page: Mapping[str, Any]) -> list[dict]:
    """Both result buckets of an organization search, each with organization_id and domain set.

    `organizations` rows carry the organization id as `id` and `primary_domain`; `accounts`
    rows (companies someone saved in Apollo) carry an account `id`, a separate
    `organization_id` and `domain`.
    """
    out: list[dict] = []
    for org in page.get("organizations") or []:
        out.append({**org, "organization_id": org.get("id"), "domain": org.get("primary_domain") or org.get("domain")})
    for acct in page.get("accounts") or []:
        out.append({**acct, "organization_id": acct.get("organization_id"), "domain": acct.get("domain") or acct.get("primary_domain")})
    return out


def postings_in(page: Mapping[str, Any]) -> list[dict]:
    """The postings on one job_postings() page: {id, title, url, city, state, country, posted_at, ...}.

    PHASE0-CONFIRM: the list's key; Apollo's docs show organization_job_postings.
    """
    for key in ("organization_job_postings", "job_postings"):
        rows = page.get(key)
        if isinstance(rows, list):
            return [dict(r) for r in rows if isinstance(r, Mapping)]
    return []


def total_entries(page: Mapping[str, Any]) -> int | None:
    """pagination.total_entries of a search or postings page, if Apollo gave it."""
    value = (page.get("pagination") or {}).get("total_entries")
    try:
        return None if value is None else int(value)
    except (TypeError, ValueError):
        return None


def _segment(value: str) -> str:
    v = str(value or "").strip()
    if not _PATH_SEGMENT.fullmatch(v):
        raise ValueError(f"not an Apollo id: {v[:40]!r}")
    return v


def credits_left(usage: Mapping[str, Any], credit_type: str = "lead_credit") -> float | None:
    """left_over for one credit type from credit_usage().

    PHASE0-CONFIRM: which credit type holds the 30,078 credits of SPEC 4 (lead_credit funds
    email reveals and enrichment).
    """
    stats = (usage.get("credit_usage_stats") or {}).get(credit_type)
    if not stats or stats.get("left_over") is None:
        return None
    return float(stats["left_over"])


def _yyyymmdd(value: date | datetime | str) -> str:
    if isinstance(value, str):
        return value.replace("-", "")
    return value.strftime("%Y%m%d")


class Apollo(HttpClient):
    system = "apollo"
    base_url = "https://api.apollo.io/api/v1"
    paid_reads = frozenset({"organizations.search", "organizations.enrich", "organizations.job_postings", "people.bulk_match"})

    def headers(self) -> dict[str, str]:
        return {
            "X-Api-Key": self.token,
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Cache-Control": "no-cache",
        }

    def _read(
        self,
        action: str,
        *,
        target: str = "",
        params: dict[str, Any] | None = None,
        json: Any = None,
        detail: Mapping[str, Any] | None = None,
        path_args: Mapping[str, str] | None = None,
    ) -> Any:
        method, path = READ_ENDPOINTS[action]
        if path_args:
            path = path.format(**{k: _segment(v) for k, v in path_args.items()})
        op = Op(action, target=target or path.strip("/"), write=False, detail=dict(detail or {}))
        return self.request(method, path, op, params=params, json=json)

    @staticmethod
    def _page_args(page: int, per_page: int) -> dict[str, int]:
        if not 1 <= per_page <= MAX_PER_PAGE:
            raise ValueError(f"per_page must be 1..{MAX_PER_PAGE}")
        if not 1 <= page <= MAX_PAGE:
            raise ValueError(f"Apollo shows at most {MAX_PAGE} pages; slice the search further (SPEC 7)")
        return {"page": page, "per_page": per_page}

    def credit_usage(self) -> dict:
        """Team credit balances for the current cycle, keyed by credit type (0 credits)."""
        return self._read("usage.credits") or {}

    def search_organizations(self, filters: Mapping[str, Any], page: int = 1, per_page: int = 100) -> dict:
        """One page of organization search (1 credit per page). See organizations_in()."""
        body = {**normalize_filters(filters), **self._page_args(page, per_page)}
        return self._read("organizations.search", json=body, detail={"page": page, "filters": sorted(body)}) or {}

    def enrich_organization(self, domain: str) -> dict:
        """Organization enrichment by root domain (1 credit)."""
        d = domain.strip().lower().removeprefix("www.")
        return self._read("organizations.enrich", target=d, params={"domain": d}) or {}

    def job_postings(self, organization_id: str, page: int = 1, per_page: int = 100) -> dict:
        """One page of an organization's current job postings (1 credit per request). See postings_in()."""
        if not 1 <= per_page <= MAX_POSTINGS_PER_PAGE:
            raise ValueError(f"per_page must be 1..{MAX_POSTINGS_PER_PAGE}")
        if page < 1:
            raise ValueError("page starts at 1")
        org = _segment(organization_id)
        return self._read(
            "organizations.job_postings", target=org, path_args={"organization_id": org},
            params={"page": page, "per_page": per_page}, detail={"organization_id": org},
        ) or {}

    def search_people(self, filters: Mapping[str, Any], page: int = 1, per_page: int = 100) -> dict:
        """One page of People API Search (0 credits; returns no emails)."""
        body = {**normalize_filters(filters), **self._page_args(page, per_page)}
        return self._read("people.search", json=body, detail={"page": page, "filters": sorted(body)}) or {}

    def bulk_match(self, details: Iterable[Mapping[str, Any]], reveal_personal_emails: bool = False) -> dict:
        """Bulk People Enrichment, 10 details per call; the pages are merged into one response."""
        if reveal_personal_emails:
            raise ValueError("personal emails are never revealed: they are never emailed (SPEC 1.4)")
        items = [{k: v for k, v in d.items() if v not in (None, "")} for d in details]
        merged: dict[str, Any] = {
            "matches": [],
            "total_requested_enrichments": 0,
            "unique_enriched_records": 0,
            "missing_records": 0,
            "credits_consumed": 0.0,
        }
        for i in range(0, len(items), BULK_MATCH_MAX):
            chunk = items[i : i + BULK_MATCH_MAX]
            body = self._read(
                "people.bulk_match", params=dict(BULK_MATCH_FLAGS), json={"details": chunk}, detail={"count": len(chunk)}
            ) or {}
            merged["matches"].extend(body.get("matches") or [])
            for key in ("total_requested_enrichments", "unique_enriched_records", "missing_records"):
                merged[key] += int(body.get(key) or 0)
            merged["credits_consumed"] += float(body.get("credits_consumed") or 0)
        return merged

    def website_visitor_aggregates(
        self,
        domain: str,
        organization_ids: Iterable[str],
        *,
        since: date | datetime | str | None = None,
        until: date | datetime | str | None = None,
    ) -> dict:
        """{organization_id: aggregates} for visits to OUR tracked domain (spill.chat) by each company.

        A company with no visits ("stats not found") maps to {}. Filtering paths to /us is
        the caller's job (SPEC 7).
        """
        out: dict[str, Any] = {}
        for org_id in dict.fromkeys(str(o) for o in organization_ids if o):
            params: dict[str, Any] = {"domain": domain, "organization_id": org_id}
            if since:
                params["from"] = _yyyymmdd(since)
            if until:
                params["to"] = _yyyymmdd(until)
            try:
                out[org_id] = self._read(
                    "website_visitors.domain_aggregates", target=domain, params=params, detail={"organization_id": org_id}
                ) or {}
            except ApiError as exc:
                if exc.status in (404, 422):  # PHASE0-CONFIRM: the status Apollo uses for "stats not found"
                    out[org_id] = {}
                else:
                    raise
        return out
