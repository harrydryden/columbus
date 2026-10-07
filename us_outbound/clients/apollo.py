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
that returns at least one company and 0 for an empty one (the website-visitor search is one, with
visitor filters); job postings cost 1 credit per
request; organization enrichment, single or bulk, 1 credit per company found and 0 for one
not found. The jobs record what they spend in credit_ledger (budget.py); this client only
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
BULK_ENRICH_MAX = 10  # organizations/bulk_enrich takes up to 10 domains per call
LOOKALIKE_SEEDS_MAX = 5  # organization search takes up to 5 lookalike_organization_ids (sources/lookalike_leads.py)
MAX_POSTINGS_PER_PAGE = 10_000  # job postings: a display limit of 10,000 records
_PATH_SEGMENT = re.compile(r"[A-Za-z0-9_-]+")  # an Apollo id in a URL path

# action -> (method, path). The only endpoints this client can reach.
READ_ENDPOINTS: dict[str, tuple[str, str]] = {
    "usage.credits": ("POST", "/usage_stats/credit_usage_stats"),  # 0 credits
    "organizations.search": ("POST", "/mixed_companies/search"),  # 1 credit per page with results
    "organizations.enrich": ("GET", "/organizations/enrich"),  # 1 credit per organization found
    # PHASE0-CONFIRM: Bulk Organization Enrichment's body. Apollo's docs list domains[] as query
    # parameters; the JSON body {"domains": [...]} is what its own tools send (1 credit per company found).
    "organizations.bulk_enrich": ("POST", "/organizations/bulk_enrich"),
    # PHASE0-CONFIRM: REST path of Organization Job Postings (the MCP tool
    # apollo_organizations_job_postings takes the organization id, page and per_page; 1 credit a request).
    "organizations.job_postings": ("GET", "/organizations/{organization_id}/job_postings"),
    "people.search": ("POST", "/mixed_people/api_search"),  # 0 credits; no emails returned
    "people.bulk_match": ("POST", "/people/bulk_match"),  # credits per revealed email
    # PHASE0-CONFIRM: REST path of Apollo's website-visitor domain aggregates (the MCP tool
    # apollo_website_visitors_domain_aggregates takes organization_id, domain, from, to).
    "website_visitors.domain_aggregates": ("GET", "/website_visitors/domain_aggregates"),
    # Organization search filtered to the companies that visited our tracked domain (sources/site_visits.py):
    # the same endpoint and price as organizations.search, its own action so the guard and the logs show it.
    "website_visitors.search": ("POST", "/mixed_companies/search"),
}
# website_visitors_from_past takes only these windows, in days (Apollo's MCP tool docs, 5 Oct 2026).
VISITOR_WINDOWS = frozenset({1, 7, 15, 30, 60, 90})

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


def enriched_in(body: Mapping[str, Any]) -> list[dict]:
    """The organizations of an enrich() or bulk_enrich() answer, each with organization_id and domain set.

    PHASE0-CONFIRM: organizations/enrich answers {"organization": {...}}, and bulk_enrich
    {"organizations": [...]}, with a company Apollo does not know left out (or null).
    """
    rows = [body.get("organization")] if isinstance(body.get("organization"), Mapping) else []
    rows += [o for o in body.get("organizations") or () if isinstance(o, Mapping)]
    return [{**o, "organization_id": o.get("id") or o.get("organization_id"),
             "domain": o.get("primary_domain") or o.get("domain")} for o in rows if o]


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
    """The total_entries of a search or postings page, if Apollo gave it.

    People API Search gives it at the top level ({"total_entries": 2, "people": [...]}, Apollo's MCP
    tool on 5 Oct 2026; PHASE0-CONFIRM over REST); the organization search and job postings under
    pagination.
    """
    value = page.get("total_entries")
    if value is None:
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


VISITOR_CREDIT = "inbound_website_visitor_credit"  # what Apollo spends to name a company that visits our site


def credit_stats(usage: Mapping[str, Any], credit_type: str) -> dict[str, float] | None:
    """{limit, consumed, left_over} for one credit type from credit_usage(), as numbers; None when Apollo gave
    no limit or no left_over for it (learn/spend.py reads the website-visitor credits this way).

    PHASE0-CONFIRM: the website-visitor credits are inbound_website_visitor_credit, with the same three
    fields as lead_credit (Apollo's usage tool showed 1,200 of them on 1 Oct 2026; docs/gtm-review/04).
    """
    stats = (usage.get("credit_usage_stats") or {}).get(credit_type)
    if not isinstance(stats, Mapping):
        return None
    try:
        out = {k: float(stats[k]) for k in ("limit", "left_over")}
        out["consumed"] = float(stats.get("consumed") or 0)
    except (KeyError, TypeError, ValueError):
        return None
    return out


def _enrich_domain(domain: str) -> str:
    return str(domain or "").strip().lower().removeprefix("www.")


def _yyyymmdd(value: date | datetime | str) -> str:
    if isinstance(value, str):
        return value.replace("-", "")
    return value.strftime("%Y%m%d")


class Apollo(HttpClient):
    system = "apollo"
    base_url = "https://api.apollo.io/api/v1"
    paid_reads = frozenset({"organizations.search", "organizations.enrich", "organizations.bulk_enrich",
                            "organizations.job_postings", "people.bulk_match", "website_visitors.search"})

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

    def search_website_visitors(
        self, domains: Iterable[str], *, days: int, pages: Iterable[str] = (), page: int = 1, per_page: int = 100,
    ) -> dict:
        """One page of the companies that visited our tracked domains (spill.chat) in the last `days` days.

        An organization search (1 credit per page that returns a company, 0 for an empty one) with
        Apollo's website-visitor filters: website_visitors_from_domains, website_visitors_from_past and,
        when pages are given, website_visitors_domain_pages (a page whose path CONTAINS any of them).
        It reads the visitor list only. The tracker's own settings are never read or changed: Apollo's
        tracker endpoint creates a tracker when there is none, which is a write.
        PHASE0-CONFIRM: the REST body takes these keys as Apollo's MCP tool does, and the answer comes in
        the usual two buckets (organizations_in). A filter Apollo ignored would return its whole
        database, which sources/site_visits.py refuses to use.
        """
        if days not in VISITOR_WINDOWS:
            raise ValueError(f"website_visitors_from_past is one of {sorted(VISITOR_WINDOWS)} days, not {days}")
        tracked = [d for d in (str(x).strip().lower() for x in domains) if d]
        if not tracked:
            raise ValueError("no tracked domain")
        body: dict[str, Any] = {"website_visitors_from_domains": tracked, "website_visitors_from_past": days}
        paths = [p for p in (str(x).strip() for x in pages) if p]
        if paths:
            body["website_visitors_domain_pages"] = paths
        body.update(self._page_args(page, per_page))
        return self._read("website_visitors.search", target=tracked[0], json=body,
                          detail={"page": page, "days": days, "pages": paths}) or {}

    def search_lookalike_organizations(self, seed_ids: Iterable[str], filters: Mapping[str, Any], page: int = 1,
                                       per_page: int = 100) -> dict:
        """One page of organization search ranked by likeness to up to LOOKALIKE_SEEDS_MAX seed organizations
        (1 credit per page with results, as any search). See organizations_in().

        Apollo leaves the seeds themselves out, and a seed it has no lookalike data for makes the
        whole search return nothing rather than fall back to the other filters, so the caller tries
        a group of seeds that comes back empty one seed at a time (an empty page costs nothing).
        PHASE0-CONFIRM: the REST body key lookalike_organization_ids (the docs' query form
        lookalike_organization_ids[], which normalize_filters reads the same way), its limit of 5,
        and that a page of lookalikes is charged like any search page.
        """
        ids = list(dict.fromkeys(_segment(str(i)) for i in seed_ids if i))
        if not 1 <= len(ids) <= LOOKALIKE_SEEDS_MAX:
            raise ValueError(f"a lookalike search takes 1 to {LOOKALIKE_SEEDS_MAX} seed organizations, not {len(ids)}")
        body = {**normalize_filters(filters), "lookalike_organization_ids": ids, **self._page_args(page, per_page)}
        return self._read("organizations.search", json=body,
                          detail={"page": page, "filters": sorted(body), "seeds": len(ids)}) or {}

    def enrich_organization(self, domain: str) -> dict:
        """Organization enrichment by root domain (1 credit if found). See enriched_in()."""
        d = _enrich_domain(domain)
        return self._read("organizations.enrich", target=d, params={"domain": d}) or {}

    def bulk_enrich_organizations(self, domains: Iterable[str]) -> dict:
        """Bulk Organization Enrichment: up to BULK_ENRICH_MAX root domains in one call (1 credit per company found).

        One call, so the caller reserves its credits before it; see enriched_in().
        """
        ds = list(dict.fromkeys(d for d in (_enrich_domain(x) for x in domains) if d))
        if not 1 <= len(ds) <= BULK_ENRICH_MAX:
            raise ValueError(f"bulk_enrich takes 1 to {BULK_ENRICH_MAX} domains, not {len(ds)}")
        return self._read("organizations.bulk_enrich", json={"domains": ds}, detail={"count": len(ds)}) or {}

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
