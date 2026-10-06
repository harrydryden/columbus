"""Clay client (SPEC 1.2, 8): the US Outbound functions, called through the Routines API.

Only "US Outbound – Accounts" and "US Outbound – Contacts" may be run, and, while General
clay_email_fallback is yes, the existing workspace function Work Email (WORK_EMAIL_FUNCTION_ID),
called as it is and never modified (SPEC 1.2; Harry, 2 Oct 2026: Clay narrowed to the email
waterfall for contacts Apollo can't verify, contacts/pick.py). Work Email may also run from
`us-outbound clay check-email` (CHECK_EMAIL_JOB), the one lookup that confirms it before the switch
goes on. The guard refuses any other function id (context.boundaries_for). Clay runs in dry-run
too; budgets are checked by the caller before each batch (SPEC 8).

Routines API (Clay Public HTTP API, beta, per Clay University docs Sep 2026):
  POST {base}/routines/{routine_id}/run   body {"items": [{"id", "inputs"}]}  -> a run id
  GET  {base}/routines/run/{run_id}/results                                 -> poll
Auth: workspace API key in the clay-api-key header. Routine ids look like
"function:t_…"; a bare table id "t_…" is prefixed. The function needs "API & CLI"
ticked in its Integrations settings, or calls return 403.

If the plan does not allow the API, SPEC 8's CSV fallback applies: write_import_csv()
for the import, read_export_csv() for Clay's export.

Work Email takes its own input names (WORK_EMAIL_INPUTS: "Full Name", "Company Domain", ...), as
Clay's function list gives them (Clay's MCP list_subroutines, 6 Oct 2026); work_email_inputs()
builds them. The "US Outbound – Contacts" function takes SPEC 8's names as they are.

parse_accounts_output() and parse_contacts_output() validate the strict JSON of SPEC 8;
parse_work_email_output() reads Work Email's own output; parse_cross_check_output() reads the
narrowed "US Outbound – Accounts" (HQ state and headcount only; docs/pipeline.md, Harry, 6 Oct 2026)
that verify's cross-check asks (us_outbound/clay_cross_check.py). After each run, Clay.last_run
holds what the routines endpoint said (run id, statuses, the raw result rows), for the operator
check to describe; nothing logs it.
"""

from __future__ import annotations

import csv
import json
import re
import time
from collections.abc import Callable, Iterable, Mapping
from datetime import date
from pathlib import Path
from typing import Any

from us_outbound.clean.people import state_code
from us_outbound.clients.guard import Op
from us_outbound.clients.http import HttpClient

DEFAULT_BASE_URL = "https://api.clay.com/public/v0"  # PHASE0-CONFIRM: our workspace has the Public API beta
RUN_ITEMS_MAX = 100  # inline runs take up to 100 items
QUOTE_LIMIT = 300  # SPEC 6: signal_events.quote is at most 300 characters

READ_STATUSES = frozenset({"read", "no_pages_found", "blocked", "error"})
PROVISION_TYPES = frozenset(
    {"eap", "carrier_eap", "named_vendor", "therapy_stipend", "general_support", "mental_health_days"}
)
EMAIL_STATUSES = frozenset({"valid", "catch_all_valid", "invalid", "not_found"})
# The existing workspace function "Work Email" (docs/phase0-facts.md), which charges only when it finds an email.
WORK_EMAIL_FUNCTION_ID = "t_0tk0v4lhJ895hhhhTHJ"
# Work Email's inputs under its own names, as Clay's function list gives them (6 Oct 2026; it also takes
# "Company Social Profile URL" and "Personal Email", which we never send). PHASE0-CONFIRM: that the Routines
# API takes inputs by these names (`us-outbound clay check-email --live` shows it: a run that fails
# validation means it does not).
WORK_EMAIL_INPUTS = {"full_name": "Full Name", "domain": "Company Domain", "linkedin_url": "Social Profile URL",
                     "company_name": "Company Name"}
CHECK_EMAIL_JOB = "clay_check_email"  # `us-outbound clay check-email`: may run Work Email while the fallback is off
UNVERIFIED = "unverified"  # Work Email found an address but said nothing of its validity: never used
# PHASE0-CONFIRM: Work Email's output fields. Its waterfall result is read under any of these names, and
# its validation status mapped onto SPEC 8's statuses; anything else that came with an email is UNVERIFIED.
WORK_EMAIL_KEYS = ("email", "work_email", "Work Email", "workEmail")
WORK_EMAIL_STATUS_KEYS = ("status", "email_status", "validation_status", "verification_status", "result")
WORK_EMAIL_STATUS = {
    "valid": "valid", "verified": "valid", "deliverable": "valid",
    "catch_all_valid": "catch_all_valid", "catch_all": "catch_all_valid", "catch-all": "catch_all_valid",
    "catchall": "catch_all_valid", "accept_all": "catch_all_valid", "accept-all": "catch_all_valid",
    "invalid": "invalid", "undeliverable": "invalid", "not_found": "not_found", "not found": "not_found",
}

# PHASE0-CONFIRM: status values in the results response. Run-level terminal statuses are
# documented (complete, validation_failed, processing_failed); item-level ones are not.
RUN_DONE = frozenset({"complete", "completed", "success", "succeeded", "done", "finished"})
RUN_FAILED = frozenset({"validation_failed", "processing_failed", "failed", "error", "cancelled"})
ITEM_FAILED = frozenset({"failed", "error", "validation_failed", "processing_failed", "timed_out", "cancelled"})

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class ClayError(Exception):
    """A Clay run failed, or its output does not have the SPEC 8 shape."""


def routine_id(function_id: str) -> str:
    return function_id if function_id.startswith("function:") else f"function:{function_id}"


def work_email_inputs(full_name: str | None, domain: str | None, linkedin_url: str | None = None,
                      company_name: str | None = None) -> dict[str, str]:
    """Work Email's inputs under its own names (WORK_EMAIL_INPUTS); blanks left out."""
    given = {"full_name": full_name, "domain": domain, "linkedin_url": linkedin_url, "company_name": company_name}
    return {WORK_EMAIL_INPUTS[k]: v.strip() for k, v in given.items() if isinstance(v, str) and v.strip()}


def _clean_inputs(inputs: Mapping[str, Any]) -> dict[str, Any]:
    """Drop None (Clay treats null as missing) and NUL characters (Clay refuses them)."""
    out: dict[str, Any] = {}
    for k, v in inputs.items():
        if v is None:
            continue
        out[str(k)] = v.replace("\x00", "") if isinstance(v, str) else v
    return out


def _output_of(row: Mapping[str, Any]) -> Any:
    return next((row[k] for k in ("output", "outputs", "result", "data") if row.get(k) is not None), None)


def _as_obj(obj: Any, what: str) -> dict:
    if isinstance(obj, (str, bytes)):
        try:
            obj = json.loads(obj)
        except ValueError as exc:
            raise ClayError(f"{what} output is not JSON: {exc}") from None
    if not isinstance(obj, dict):
        raise ClayError(f"{what} output must be a JSON object, got {type(obj).__name__}")
    return obj


class Clay(HttpClient):
    system = "clay"
    base_url = DEFAULT_BASE_URL
    paid_reads = frozenset({"function.run"})

    def __init__(
        self,
        guard,
        transport,
        token: str = "",
        *,
        base_url: str = DEFAULT_BASE_URL,
        poll_interval: float = 5.0,
        timeout: float = 900.0,
        sleep: Callable[[float], None] = time.sleep,
    ):
        super().__init__(guard, transport, token)
        self.api_base = base_url.rstrip("/")
        self.poll_interval, self.timeout, self._sleep = poll_interval, timeout, sleep
        self.last_run: dict[str, Any] = {}  # the latest run as the endpoint gave it (`us-outbound clay check-email`)

    def headers(self) -> dict[str, str]:
        return {"clay-api-key": self.token, "Content-Type": "application/json", "Accept": "application/json"}

    # -- the function API --------------------------------------------------------

    def run_function(self, function_id: str, inputs: Mapping[str, Any]) -> dict:
        """Run one record through a US Outbound function and return its output object.

        PHASE0-CONFIRM: the function API (plan, beta access, response shapes).
        """
        [result] = self.run_function_batch(function_id, {"1": inputs}).values()
        if isinstance(result, ClayError):
            raise result
        return result

    def run_function_batch(self, function_id: str, inputs_by_id: Mapping[str, Mapping[str, Any]]) -> dict[str, dict | ClayError]:
        """Run up to 100 records in one run. Returns {item id: output object, or the ClayError for that item}."""
        if not inputs_by_id:
            return {}
        if len(inputs_by_id) > RUN_ITEMS_MAX:
            raise ValueError(f"a Clay run takes at most {RUN_ITEMS_MAX} items")
        items = [{"id": str(i), "inputs": _clean_inputs(v)} for i, v in inputs_by_id.items()]
        self.last_run = {"function_id": function_id, "items": len(items)}
        body = self.request(
            "POST",
            f"/routines/{routine_id(function_id)}/run",
            Op("function.run", target=function_id, detail={"items": len(items)}),
            json={"items": items},
            base_url=self.api_base,
        ) or {}
        self.last_run["started"] = body if isinstance(body, dict) else {"body": body}
        run_id = body.get("routine_run_id") or body.get("run_id") or body.get("id") if isinstance(body, dict) else None
        if not run_id:
            raise ClayError(f"Clay run of {function_id} returned no run id")
        self.last_run["run_id"] = str(run_id)
        rows = self._wait(function_id, str(run_id), len(items))
        out: dict[str, dict | ClayError] = {}
        for item in items:
            row = rows.get(item["id"])
            if row is None:
                out[item["id"]] = ClayError(f"Clay run {run_id} returned no result for item {item['id']}")
                continue
            status = str(row.get("status") or "").lower()
            if status in ITEM_FAILED:
                out[item["id"]] = ClayError(f"Clay item {item['id']} {status}: {str(row.get('error') or '')[:200]}")
                continue
            output = _output_of(row)
            if output is None and status in RUN_DONE:
                # The item finished and gave nothing back: read as an empty output (Work Email: no email
                # found, which it does not charge for), not as a failure counted at the reserve.
                output = {}
            try:
                out[item["id"]] = _as_obj(output, "Clay function")
            except ClayError as exc:
                out[item["id"]] = exc
        return out

    def _wait(self, function_id: str, run_id: str, expected: int) -> dict[str, dict]:
        """Poll the results until the run finishes; {item id: result row}."""
        polls = max(1, int(self.timeout / self.poll_interval) if self.poll_interval else 1)
        for attempt in range(polls):
            rows, done = self._results(function_id, run_id, expected)
            if done:
                return rows
            if attempt < polls - 1:
                self._sleep(self.poll_interval)
        raise ClayError(f"Clay run {run_id} did not finish within {self.timeout:.0f}s")

    def _results(self, function_id: str, run_id: str, expected: int) -> tuple[dict[str, dict], bool]:
        # PHASE0-CONFIRM: results shape. Best known: {routine_run_id, status, total, finished,
        # cursor, results: [{id, status, output}]}; pages follow the cursor.
        rows: dict[str, dict] = {}
        cursor = ""
        status, total, finished = "", expected, None
        while True:
            body = self.request(
                "GET",
                f"/routines/run/{run_id}/results",
                Op("run.get", target=function_id, detail={"run_id": run_id}),
                params={"cursor": cursor} if cursor else None,
                base_url=self.api_base,
            ) or {}
            status = str(body.get("status") or status).lower()
            total = int(body.get("total") or total)
            if body.get("finished") is not None:
                finished = int(body["finished"])
            for row in next((body[k] for k in ("results", "items", "data") if isinstance(body.get(k), list)), []):
                if isinstance(row, dict) and row.get("id") is not None:
                    rows[str(row["id"])] = row
            next_cursor = body.get("cursor") or body.get("next_cursor") or ""
            if not next_cursor or next_cursor == cursor:
                break
            cursor = str(next_cursor)
        self.last_run.update(status=status, total=total, finished=finished, rows=rows)
        if status in RUN_FAILED:
            raise ClayError(f"Clay run {run_id} {status}")
        settled = sum(1 for r in rows.values()
                      if _output_of(r) is not None or str(r.get("status") or "").lower() in ITEM_FAILED | RUN_DONE)
        done = status in RUN_DONE or (finished is not None and finished >= total) or settled >= total
        return rows, done

    # -- CSV fallback (SPEC 8) -----------------------------------------------------

    @staticmethod
    def write_import_csv(rows: Iterable[Mapping[str, Any]], path: str | Path) -> None:
        """UTF-8 CSV for a Clay table import. Columns in first-seen order; dicts and lists JSON-encoded."""
        rows = list(rows)
        columns: dict[str, None] = {}
        for row in rows:
            for k in row:
                columns.setdefault(str(k), None)

        def cell(v: Any) -> str:
            if v is None:
                return ""
            if isinstance(v, (dict, list, tuple, bool)):
                return json.dumps(list(v) if isinstance(v, tuple) else v, ensure_ascii=False)
            return str(v)

        with open(path, "w", encoding="utf-8", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(list(columns))
            for row in rows:
                writer.writerow([cell(row.get(c)) for c in columns])

    @staticmethod
    def read_export_csv(path: str | Path) -> list[dict]:
        """Rows of a Clay CSV export. Cells holding a JSON object or array are decoded; blank rows skipped."""
        out: list[dict] = []
        with open(path, encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                if not any((v or "").strip() for v in row.values()):
                    continue
                parsed: dict[str, Any] = {}
                for k, v in row.items():
                    v = v or ""
                    s = v.strip()
                    if s[:1] in "{[" and s[-1:] in "}]":
                        try:
                            parsed[k] = json.loads(s)
                            continue
                        except ValueError:
                            pass
                    parsed[k] = v
                out.append(parsed)
        return out


# -- output validators (SPEC 8) ----------------------------------------------------


def _clip_quote(text: Any) -> str:
    s = "" if text is None else str(text).strip()
    return s if len(s) <= QUOTE_LIMIT else s[: QUOTE_LIMIT - 1] + "…"


def _opt_str(obj: Mapping[str, Any], key: str) -> str | None:
    v = obj.get(key)
    if v is None:
        return None
    if not isinstance(v, str):
        raise ClayError(f"{key} must be a string or null")
    return v.strip() or None


def _opt_int(obj: Mapping[str, Any], key: str, lo: int, hi: int) -> int | None:
    v = obj.get(key)
    if v is None or v == "":
        return None
    if isinstance(v, bool):
        raise ClayError(f"{key} must be a number")
    try:
        n = float(str(v).replace(",", "")) if isinstance(v, str) else float(v)
    except ValueError:
        raise ClayError(f"{key} must be a number, got {v!r}") from None
    if n != int(n) or not lo <= n <= hi:
        raise ClayError(f"{key} out of range: {v!r}")
    return int(n)


def _credits(obj: Mapping[str, Any]) -> float:
    if "credits_used" not in obj:
        raise ClayError("credits_used is missing")
    v = obj["credits_used"]
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0:
        raise ClayError(f"credits_used must be a non-negative number, got {v!r}")
    return float(v)


def _list_of(obj: Mapping[str, Any], key: str) -> list[dict]:
    v = obj.get(key)
    if v is None:
        return []
    if not isinstance(v, list) or not all(isinstance(x, dict) for x in v):
        raise ClayError(f"{key} must be a list of objects")
    return v


def parse_accounts_output(obj: Any) -> dict:
    """Validate and normalize the "US Outbound – Accounts" output (SPEC 8). Raises ClayError."""
    o = _as_obj(obj, "US Outbound – Accounts")
    for key in ("clean_name", "domain_confirmed", "read_status"):
        if key not in o:
            raise ClayError(f"{key} is missing")
    read_status = o["read_status"]
    if read_status not in READ_STATUSES:
        raise ClayError(f"read_status {read_status!r} is not one of {sorted(READ_STATUSES)}")

    pages_in = o.get("pages") or {}
    if not isinstance(pages_in, dict):
        raise ClayError("pages must be an object")
    pages = {k: _opt_str(pages_in, k) for k in ("careers", "benefits", "values")}

    benefits = [
        {"item": _opt_str(b, "item") or "", "quote": _clip_quote(b.get("quote")), "url": _opt_str(b, "url")}
        for b in _list_of(o, "benefits")
    ]
    provision = []
    for p in _list_of(o, "mental_health_provision"):
        if p.get("type") not in PROVISION_TYPES:
            raise ClayError(f"mental_health_provision type {p.get('type')!r} is not one of {sorted(PROVISION_TYPES)}")
        provision.append(
            {"type": p["type"], "provider": _opt_str(p, "provider"), "quote": _clip_quote(p.get("quote")), "url": _opt_str(p, "url")}
        )
    culture = [{"quote": _clip_quote(c.get("quote")), "url": _opt_str(c, "url")} for c in _list_of(o, "culture_statements")]

    values_page = o.get("values_page")
    if values_page is not None and not isinstance(values_page, bool):
        raise ClayError("values_page must be true, false or null")

    funding = o.get("funding")
    if funding is not None:
        if not isinstance(funding, dict):
            raise ClayError("funding must be an object or null")
        fdate = _opt_str(funding, "date")
        if fdate is not None:
            if not _DATE_RE.match(fdate):
                raise ClayError(f"funding.date must be YYYY-MM-DD, got {fdate!r}")
            try:
                date.fromisoformat(fdate)
            except ValueError:
                raise ClayError(f"funding.date is not a date: {fdate!r}") from None
        amount = funding.get("amount_usd")
        if amount is not None and (isinstance(amount, bool) or not isinstance(amount, (int, float)) or amount < 0):
            raise ClayError(f"funding.amount_usd must be a non-negative number, got {amount!r}")
        funding = {
            "stage": _opt_str(funding, "stage"),
            "amount_usd": amount,
            "date": fdate,
            "source": _opt_str(funding, "source"),
        }

    return {
        "clean_name": _opt_str(o, "clean_name") or "",
        "legal_name": _opt_str(o, "legal_name"),
        "domain_confirmed": (_opt_str(o, "domain_confirmed") or "").lower() or None,
        "hq_city": _opt_str(o, "hq_city"),
        "hq_state": _opt_str(o, "hq_state"),
        "employees": _opt_int(o, "employees", 0, 10_000_000),
        "employees_source": _opt_str(o, "employees_source"),
        "industry": _opt_str(o, "industry"),
        "founded_year": _opt_int(o, "founded_year", 1600, 2100),
        "pages": pages,
        "read_status": read_status,
        "benefits": benefits,
        "mental_health_provision": provision,
        "culture_statements": culture,
        "values_page": values_page,
        "funding": funding,
        "credits_used": _credits(o),
    }


def parse_contacts_output(obj: Any) -> dict:
    """Validate and normalize the "US Outbound – Contacts" output (SPEC 8). Raises ClayError."""
    o = _as_obj(obj, "US Outbound – Contacts")
    status = o.get("status")
    if status not in EMAIL_STATUSES:
        raise ClayError(f"status {status!r} is not one of {sorted(EMAIL_STATUSES)}")
    email = (_opt_str(o, "email") or "").lower() or None
    if status in {"valid", "catch_all_valid"}:
        if not email or email.count("@") != 1 or "." not in email.split("@")[1]:
            raise ClayError(f"status {status} needs an email address")
    return {"email": email, "status": status, "provider": _opt_str(o, "provider"), "credits_used": _credits(o)}


def parse_work_email_output(obj: Any) -> dict:
    """Work Email's output as {email, status, provider, credits_used}; credits_used None when it gives none.

    The SPEC 8 Contacts shape is read as it is. Otherwise status is SPEC 8's (valid,
    catch_all_valid, invalid, not_found) when Work Email names one, UNVERIFIED when it gave an
    email and no status, and not_found when it gave no email. Raises ClayError on a malformed email.
    """
    o = _as_obj(obj, "Work Email")
    if o.get("status") in EMAIL_STATUSES and "credits_used" in o:
        return parse_contacts_output(o)
    email = next((v.strip().lower() for k in WORK_EMAIL_KEYS if isinstance(v := o.get(k), str) and v.strip()), None)
    raw = next((str(v).strip().lower() for k in WORK_EMAIL_STATUS_KEYS if isinstance(v := o.get(k), str) and v.strip()), "")
    status = WORK_EMAIL_STATUS.get(raw, UNVERIFIED if email else "not_found")
    if email and (email.count("@") != 1 or "." not in email.split("@")[1]):
        raise ClayError(f"Work Email returned a malformed address ({len(email)} characters)")
    if status in {"valid", "catch_all_valid"} and not email:
        status = "not_found"
    credits = o.get("credits_used")
    if isinstance(credits, bool) or not isinstance(credits, (int, float)) or credits < 0:
        credits = None
    provider = next((v.strip() for k in ("provider", "source") if isinstance(v := o.get(k), str) and v.strip()), None)
    return {"email": email, "status": status, "provider": provider,
            "credits_used": None if credits is None else float(credits)}



CROSS_CHECK_KEYS = ("hq_state", "employees", "source")  # the narrowed Accounts function's output (docs/pipeline.md)


def parse_cross_check_output(obj: Any) -> dict:
    """The narrowed "US Outbound – Accounts" output that verify's cross-check reads (docs/pipeline.md; Harry, 6 Oct
    2026): {hq_state, hq_state_given, employees, source, credits_used}. Raises ClayError.

    hq_state, employees and source must be present (each may be null). hq_state is read as a USPS code (a state
    name is read too); a place that is no US state gives None, with what Clay said in hq_state_given. employees is
    a whole number of staff; 0 reads as none, as providers use it for unknown. source is Clay's note of where the
    facts came from ("" when none). credits_used is optional: None when Clay reports none.
    """
    o = _as_obj(obj, "US Outbound – Accounts")
    missing = [k for k in CROSS_CHECK_KEYS if k not in o]
    if missing:
        raise ClayError(f"{', '.join(missing)} {'is' if len(missing) == 1 else 'are'} missing")
    given = _opt_str(o, "hq_state")
    credits = _credits(o) if o.get("credits_used") is not None else None
    return {
        "hq_state": state_code(given) if given else None,
        "hq_state_given": given,
        "employees": _opt_int(o, "employees", 0, 10_000_000) or None,
        "source": _clip_quote(_opt_str(o, "source")),
        "credits_used": credits,
    }
