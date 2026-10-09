"""`us-outbound clay check-email`: one Work Email lookup through Clay's Routines API, to confirm the email
fallback before General clay_email_fallback goes on (clients/clay.py PHASE0-CONFIRM: the endpoint, Work
Email's inputs, its output fields and what a lookup costs; docs/roadmap.md §4, weeks 2–4, item 1).

Harry, 6 Oct 2026: "push ahead with building these". The fallback stays off until one Work Email call is
confirmed live; this command makes that one call, safely:
  * for a Spill colleague's own name, never a prospect: the domain must be one of Spill's own (spill.chat,
    the Mailboxes tab's domains, escalation_email's) and no account we hold;
  * without --live it prints what it would send and calls nothing;
  * with --live it makes exactly one Work Email call through the guarded Clay client (the guard allows Work
    Email for this command while clay_email_fallback is no: context.boundaries_for), with the inputs
    pick_contacts sends, within the month's Clay budget, recorded in credit_ledger as pick_contacts records
    its lookups (reserved before the call, settled after it);
  * it prints whether the routines endpoint answered, the output's keys and the shape of each value (never a
    value that could be personal), the status parse_work_email_output reads, the credits Clay reports,
    whether that is what the parser expects, and what to do next. It exits 0 when the fallback can go on.
Logs carry hashed emails only (logs.py); the address found is printed masked.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from us_outbound import budget, ledger
from us_outbound.clean.domains import root_domain
from us_outbound.clients.clay import (
    DEFAULT_BASE_URL,
    EMAIL_STATUSES,
    UNVERIFIED,
    WORK_EMAIL_FUNCTION_ID,
    WORK_EMAIL_KEYS,
    WORK_EMAIL_STATUS_KEYS,
    ClayError,
    parse_work_email_output,
    routine_id,
    work_email_inputs,
)
from us_outbound.clients.http import ApiError
from us_outbound.contacts.pick import CLAY_RESERVE
from us_outbound.context import Context
from us_outbound.logs import hash_email, log

SPILL_DOMAINS = frozenset({"spill.chat"})
LEDGER_NOTE = "Work Email check (`us-outbound clay check-email`)"
READY, NOT_READY, FAILED = "ready", "not ready", "failed"
SWITCH_ON = "Next: set clay_email_fallback = yes on the General tab, then run `us-outbound sync`."
TEXT_LIMIT = 40  # a status or provider value this short is printed; longer text is described, never shown


def own_domains(ctx: Context) -> set[str]:
    """Spill's own domains: spill.chat, the Mailboxes tab's and escalation_email's."""
    s = ctx.settings
    found = {root_domain(m.domain or m.address) for m in s.mailboxes} | {root_domain(s.general.escalation_email)}
    return set(SPILL_DOMAINS) | {d for d in found if d}


def plan(ctx: Context, first: str, last: str, domain: str) -> dict:
    """The lookup this command would make: {domain, inputs, url}. ValueError, in plain words, when it may not."""
    first, last = " ".join(str(first or "").split()), " ".join(str(last or "").split())
    if not first or not last:
        raise ValueError("--first and --last are your own first and last names")
    root = root_domain(domain)
    if not root:
        raise ValueError(f"--domain {domain!r} is not a domain like spill.chat")
    own = own_domains(ctx)
    if root not in own:
        raise ValueError(f"check-email looks up a Spill colleague, never a prospect: --domain must be one of "
                         f"Spill's own ({', '.join(sorted(own))}), not {root}")
    if ctx.store.select("accounts", {"domain": root}):
        raise ValueError(f"{root} is an account we hold, so a prospect's domain; check-email is for Spill's own")
    inputs = work_email_inputs(f"{first} {last}", root)
    url = f"{DEFAULT_BASE_URL}/routines/{routine_id(WORK_EMAIL_FUNCTION_ID)}/run"
    return {"domain": root, "inputs": inputs, "url": url}


def shape(value: Any) -> str:
    """What kind of value this is, without showing it."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true or false"
    if isinstance(value, (int, float)):
        return "a number"
    if isinstance(value, str):
        v = value.strip()
        if "@" in v and "." in v.rsplit("@", 1)[-1] and " " not in v:
            return "an email address"
        return f"text ({len(v)} characters)" if v else "empty text"
    if isinstance(value, list):
        return f"a list of {len(value)}"
    if isinstance(value, Mapping):
        return f"an object with keys {', '.join(map(str, value)) or '(none)'}"
    return type(value).__name__


def mask(email: str) -> str:
    local, _, domain = email.partition("@")
    return f"{local[:1]}…@{domain}"


def _credits_reported(*objs: Any) -> dict[str, Any]:
    """Every number under a key naming credits or cost, at the top of the output and the result row."""
    out: dict[str, Any] = {}
    for where, obj in zip(("output", "result row"), objs):
        if isinstance(obj, Mapping):
            for k, v in obj.items():
                if any(w in str(k).lower() for w in ("credit", "cost")) and isinstance(v, (int, float)):
                    out[f"{where}.{k}"] = v
    return out


def assess(output: Mapping[str, Any]) -> dict:
    """What parse_work_email_output makes of Work Email's output, and whether that is what it expects."""
    email_key = next((k for k in WORK_EMAIL_KEYS if isinstance(output.get(k), str) and output[k].strip()), None)
    status_key = next((k for k in WORK_EMAIL_STATUS_KEYS if isinstance(output.get(k), str) and output[k].strip()), None)
    raw_status = str(output[status_key]).strip() if status_key else None
    out: dict[str, Any] = {"email_key": email_key, "status_key": status_key,
                           "status_given": raw_status if raw_status and len(raw_status) <= TEXT_LIMIT else None}
    try:
        parsed = parse_work_email_output(output)
    except ClayError as exc:
        return {**out, "parsed": None, "verdict": NOT_READY,
                "why": f"Work Email's email field is not an address ({exc}); the parser would refuse every result"}
    out.update(parsed={k: v for k, v in parsed.items() if k != "email"}, email=parsed["email"])
    status = parsed["status"]
    if status in EMAIL_STATUSES - {"not_found"} and parsed["email"]:
        out.update(verdict=READY, why=f"Work Email gave an address under {email_key!r} and a status the parser reads "
                                      f"({status_key!r} {raw_status!r} -> {status})")
    elif status == UNVERIFIED:
        why = (f"Work Email gave an address under {email_key!r} but no status the parser reads"
               + (f" ({status_key!r} is {raw_status!r}, which it does not know)" if status_key else
                  f" (none of {', '.join(WORK_EMAIL_STATUS_KEYS)})")
               + ", so pick_contacts would never use what it finds")
        out.update(verdict=NOT_READY, why=why)
    else:
        named = [k for k in output if k not in WORK_EMAIL_STATUS_KEYS and k not in ("credits_used", "provider", "source")]
        why = ("Work Email found no email for this person, so its email field is not confirmed"
               + (f"; the output has other keys ({', '.join(map(str, named))}), so if it did find one, it is under a "
                  f"name the parser does not read (it reads {', '.join(WORK_EMAIL_KEYS)})" if named else ""))
        out.update(verdict=NOT_READY, why=why)
    return out


def failure(exc: Exception, started: bool) -> str:
    """What failed, in plain words, and what to do about it."""
    if isinstance(exc, ApiError):
        s = exc.status
        if s == 401:
            return ("Clay refused the key (HTTP 401): US_OUTBOUND_CLAY_API_KEY must be the workspace's API key "
                    "(Clay: Settings, API), set as a sealed Railway variable.")
        if s == 403:
            return ("Clay refused the call (HTTP 403): tick \"API & CLI\" in Work Email's Integrations settings in "
                    "Clay, or the plan does not allow API calls (then SPEC 8's CSV fallback applies; tell the build).")
        if s == 404:
            return ("Clay has no such endpoint or function (HTTP 404): the Routines API path or base URL in "
                    "clients/clay.py is wrong for this workspace, or the Public API beta is not on. Tell the build.")
        if s in (400, 422):
            return (f"Clay refused the request (HTTP {s}): most likely Work Email's input names differ from what we "
                    f"send. Clay said: {str(exc.body)[:200]}. Tell the build.")
        if s == 429 or s >= 500:
            return f"Clay is busy or down (HTTP {s}). Try again later."
        return f"Clay answered HTTP {s}: {str(exc.body)[:200]}. Tell the build."
    text = str(exc)
    if "validation_failed" in text:
        return ("Clay refused the inputs (validation_failed): Work Email's input names differ from what we send "
                "(clients/clay.WORK_EMAIL_INPUTS). Tell the build, with Work Email's inputs as Clay lists them.")
    if "processing_failed" in text:
        return "The run failed inside Clay (processing_failed): see Work Email's run history in Clay, then try again."
    if "did not finish" in text:
        return "Clay accepted the run but it did not finish in time: try again; if it repeats, tell the build."
    if "no run id" in text:
        return ("The endpoint answered, but without a run id: its response differs from what clients/clay.py "
                "expects (the keys are above). Tell the build.")
    if started:
        return f"The run's result did not have the expected shape ({text[:200]}). Tell the build, with the keys above."
    return f"The call failed ({text[:200]}). Tell the build."


def check_email(ctx: Context, first: str, last: str, domain: str) -> dict:
    """Make the lookup (with --live) or say what it would be. Returns the report lines() prints; no address in it."""
    p = plan(ctx, first, last, domain)
    report: dict[str, Any] = {"dry_run": ctx.dry_run, "function_id": WORK_EMAIL_FUNCTION_ID, "url": p["url"],
                              "inputs": p["inputs"]}
    if ctx.dry_run:
        return {**report, "verdict": None}
    month = budget.monthly(ctx.store, ctx.settings, "clay", ctx.now)
    if month.budget <= 0:
        raise ValueError("clay_monthly_credits is 0, so no Clay call is made")
    if month.remaining < CLAY_RESERVE:
        raise ValueError(f"the month's Clay budget is used ({month.describe()})")
    clay = ctx.clients.clay  # a missing key stops here, before anything is reserved
    paid = ledger.Charge(ctx, ledger.reserve(ctx, "clay", ctx.job, CLAY_RESERVE, note=f"{LEDGER_NOTE}, reserved"))
    error: Exception | None = None
    output: Any = None
    try:
        output = clay.run_function(WORK_EMAIL_FUNCTION_ID, p["inputs"])
    except (ApiError, ClayError) as exc:
        error = exc
    trace = clay.last_run or {}
    rows = trace.get("rows") or {}
    row = next(iter(rows.values()), None) if isinstance(rows, Mapping) else None
    report.update(
        endpoint_answered="run_id" in trace,
        run_id=trace.get("run_id"),
        run_status=trace.get("status") or None,
        started_keys=sorted(map(str, trace.get("started") or {})),
        row_keys=sorted(map(str, row)) if isinstance(row, Mapping) else [],
        item_status=str(row.get("status") or "") or None if isinstance(row, Mapping) else None,
    )
    if error is not None:
        # A run Clay never started cost nothing; one that started may have been charged, so counts its reserve.
        counted = paid.settle(CLAY_RESERVE if report["endpoint_answered"] else 0.0, note=f"{LEDGER_NOTE} failed" + (
            "; counted in case Clay charged it" if report["endpoint_answered"] else "; no run started"))
        report.update(verdict=FAILED, why=failure(error, report["endpoint_answered"]), credits_counted=counted,
                      error=str(error)[:300])
        log("clay_check_email", run_id=ctx.run_id, verdict=FAILED, endpoint_answered=report["endpoint_answered"],
            error=report["error"])
        return report
    found = assess(output)
    email = found.pop("email", None)
    parsed = found.get("parsed") or {}
    reported = parsed.get("credits_used")
    credits = reported if reported is not None else (CLAY_RESERVE if email else 0.0)
    paid.settle(credits, note=LEDGER_NOTE)
    report.update(
        output_keys={str(k): shape(v) for k, v in output.items()},
        credits_reported=_credits_reported(output, row),
        credits_counted=credits,
        email_masked=mask(email) if email else None,
        **found,
    )
    log("clay_check_email", run_id=ctx.run_id, verdict=report["verdict"], status=parsed.get("status"),
        email_sha256=hash_email(email) if email else None, credits=credits, output_keys=sorted(report["output_keys"]))
    return report


def lines(report: Mapping[str, Any]) -> list[str]:
    """The report in plain words."""
    inputs = ", ".join(f"{k} = {v!r}" for k, v in report["inputs"].items())
    if report.get("dry_run"):
        return [
            "Dry-run: one Work Email lookup through Clay's Routines API would be made (the workspace's own "
            f"function {report['function_id']}, called as it is):",
            f"  POST {report['url']}",
            f"  inputs: {inputs}",
            f"  It costs Clay credits only if Work Email finds an address (pick_contacts reserves {CLAY_RESERVE:g}).",
            "Use your own name, never a prospect's. Nothing was called. Add --live to make the call.",
        ]
    out = [f"Work Email lookup through Clay's Routines API ({report['url']}), inputs: {inputs}"]
    if report.get("endpoint_answered"):
        out.append(f"The routines endpoint answered: run {report.get('run_id')}"
                   + (f", status {report['run_status']}" if report.get("run_status") else "")
                   + (f"; the item {report['item_status']}" if report.get("item_status") else "") + ".")
        if report.get("row_keys"):
            out.append(f"  Result row keys: {', '.join(report['row_keys'])}.")
    else:
        out.append("The routines endpoint did not start a run"
                   + (f" (it answered with keys {', '.join(report['started_keys'])})" if report.get("started_keys") else "")
                   + ".")
    if report["verdict"] == FAILED:
        spent = (f"{report['credits_counted']:g} Clay credits are counted in case Clay charged them."
                 if report["credits_counted"] else "No run started, so no Clay credits were spent.")
        return out + [f"FAILED: {report['why']}", f"{spent} clay_email_fallback stays no."]
    out.append("Output keys and shapes:")
    out += [f"  {k}: {v}" for k, v in (report.get("output_keys") or {}).items()] or ["  (none)"]
    parsed = report.get("parsed") or {}
    if parsed:
        out.append(f"parse_work_email_output reads: status {parsed.get('status')}"
                   + (f" (from {report['status_key']!r} = {report['status_given']!r})" if report.get("status_key") else "")
                   + (f", address {report['email_masked']} (under {report['email_key']!r})" if report.get("email_masked") else "")
                   + (f", provider {parsed['provider']!r}" if parsed.get("provider") and len(parsed["provider"]) <= TEXT_LIMIT else "")
                   + ".")
    credits = report.get("credits_reported") or {}
    out.append("Credits Clay reports: " + (", ".join(f"{k} = {v:g}" for k, v in credits.items()) if credits else
                                           f"none (pick_contacts counts {CLAY_RESERVE:g} for each address found; "
                                           "check the true cost on Clay's usage page and tell the build)")
               + f". Counted in credit_ledger: {report['credits_counted']:g}.")
    if report["verdict"] == READY:
        return out + [f"MATCHES what the parser expects: {report['why']}.", SWITCH_ON]
    then = ("Try again with a colleague Clay is likely to find." if parsed.get("status") == "not_found"
            else "Send the build the keys above, so the parser reads them, then run this again.")
    return out + [f"NOT READY: {report['why']}.", f"clay_email_fallback stays no. {then}"]
