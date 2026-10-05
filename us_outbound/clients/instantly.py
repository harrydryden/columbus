"""Instantly API v2 client (SPEC 1.2, 9, 11, 13): sending, warmup, blocklist, replies.

The workspace is shared with other (European) campaigns, so this client touches only:

  * campaigns named "US Outbound – {owner}". Every Op names the campaign by NAME (the
    guard checks the prefix); ids are resolved here from list_campaigns() and cached,
    and a name outside the prefix is refused before any request;
  * the mailbox-registry accounts. Every read of emails or accounts is filtered by
    registry addresses (one request per account), passes them to the guard as
    detail["accounts"] (the guard logs every account id read), and results for any
    other account are dropped client-side as well.

Writes return their dry_result in dry-run and send nothing. create_campaign never
activates: new campaigns stay in Instantly's Draft state until `start` (SPEC 13).

Endpoint shapes come from Instantly's published v2 OpenAPI document (api.instantly.ai
/openapi/api_v2.json, mirrored Aug 2026). Anything it leaves open is marked PHASE0-CONFIRM.
"""

from __future__ import annotations

import html
from collections.abc import Iterable, Iterator, Mapping, Sequence
from datetime import date, datetime, time
from typing import Any

from us_outbound.clients.guard import CLI_APPROVER, REPLIES_CLI_JOB, US_CAMPAIGN_PREFIX, GuardViolation, Op
from us_outbound.clients.http import ApiError, HttpClient
from us_outbound.logs import log
from us_outbound.settings.model import SendWindow

CAMPAIGN_SEARCH = US_CAMPAIGN_PREFIX.strip()  # "US Outbound –": the guard requires this search prefix
# PHASE0-CONFIRM: that Instantly's name search matches the en dash in "US Outbound –".

PAGE = 100  # list page size (leads/list documents a maximum of 100)
LEADS_PER_ADD = 1000  # POST /leads/add accepts up to 1000 leads
ACCOUNTS_PER_CALL = 100  # warmup enable / analytics accept up to 100 emails
BLOCKLIST_PER_CALL = 1000  # bulk-create accepts up to 1000 values

# SPEC 9 campaign settings, as Instantly field names. Registry code checks drift against these.
CAMPAIGN_SETTINGS: dict[str, Any] = {
    "stop_on_reply": True,
    "stop_for_company": True,  # "stop the campaign for the entire company (domain) when a lead replies"
    "stop_on_auto_reply": False,  # the jobs handle out-of-office replies
    "open_tracking": False,  # SPEC 1.8: open and link tracking stay off
    "link_tracking": False,
    "insert_unsubscribe_header": True,
    # Harry, 5 Oct 2026: the seed send showed Instantly sending email 1 as text only (text/plain, no HTML
    # part) in a campaign with text_only off. The HTML step was flattened: the paragraphs ran together, the
    # links became written-out addresses and the unsubscribe line lost its link. Email 1 is HTML like the
    # rest (email_format), so the step-1 text-only option is off, and drift puts it back if it is turned on.
    "first_email_text_only": False,
    "allow_risky_contacts": False,
    "is_evergreen": True,  # PHASE0-CONFIRM: that is_evergreen keeps the campaign open to new leads indefinitely
}
TRACKING_FIELDS = frozenset({"open_tracking", "link_tracking"})
# Instantly's GET /campaigns/{id} (read 2 Oct 2026) leaves out a setting that is at its default, so a
# setting we want false and that is missing is false. is_evergreen is never returned, so it cannot be
# checked (PHASE0-CONFIRM above).
NOT_RETURNED = frozenset({"is_evergreen"})
# The opt-out every email carries (Harry, 1 Oct 2026): Instantly's own unsubscribe link, not a page
# of ours. It goes in the campaign's step template after the lead's rendered body, since Instantly
# fills its merge tags in the template, not inside a custom variable's value. A click stops the
# lead's sequence and adds the address to the workspace's unsubscribe list, which every campaign
# honors; insert_unsubscribe_header also gives mail clients their one-click unsubscribe button.
# PHASE0-CONFIRM: that the tag is {{unsubscribe}} and becomes the link's URL, by a test send to a seed
# inbox in both formats (Instantly's editor offers it as "Insert unsubscribe link").
UNSUBSCRIBE_TAG = "{{unsubscribe}}"
# Harry, 5 Oct 2026: plainer words in small grey type, so the line reads as part of a personal email
# rather than a bulk-mail footer; the link and the List-Unsubscribe header are unchanged.
UNSUBSCRIBE_ASK = "Not relevant?"
UNSUBSCRIBE_ANCHOR = "Unsubscribe here"
UNSUBSCRIBE_TEXT = f"{UNSUBSCRIBE_ASK} {UNSUBSCRIBE_ANCHOR}"
UNSUBSCRIBE_STYLE = "font-size:12px;color:#888888"
UNSUBSCRIBE_HTML = (f'<p style="{UNSUBSCRIBE_STYLE}">{UNSUBSCRIBE_ASK} '
                    f'<a href="{UNSUBSCRIBE_TAG}" style="color:#888888">{UNSUBSCRIBE_ANCHOR}</a>.</p>')
UNSUBSCRIBE_PLAIN = f"\n\n{UNSUBSCRIBE_TEXT}: {UNSUBSCRIBE_TAG}"
OLD_UNSUBSCRIBE_TEXT = "To stop hearing from us, unsubscribe here"  # emails sent before 5 Oct 2026


def unsubscribe_line(text_only: bool = False) -> str:
    """What the step template adds after the rendered body: Instantly's unsubscribe link."""
    return UNSUBSCRIBE_PLAIN if text_only else UNSUBSCRIBE_HTML


# text_only follows the General tab's email_format (Harry, 30 Sep 2026): html by default, so the
# copy's links are embedded; tracking stays off either way.
TEXT_ONLY = "text_only"


def campaign_settings(text_only: bool = False) -> dict[str, Any]:
    """The fixed SPEC 9 settings plus text_only for the sheet's email_format."""
    return {**CAMPAIGN_SETTINGS, TEXT_ONLY: bool(text_only)}

# Four steps, one variant each (SPEC 9/10), a week apart (Harry, 30 Sep 2026; SPEC 10 had days
# 0, 3, 8 and 15). Instantly counts delays in calendar days and moves a step due at the weekend
# to Monday; a week apart, every step falls on the same weekday as the first, so none does
# (docs/pipeline.md). The send forecast (enrol/capacity.py) reads the same days.
STEP_DAYS = (0, 7, 14, 21)
# A reply counts for a week after the last step: SPEC 12's 21 days of step 1 was a week after
# its day-15 step. The readout views use the same number (sql/views; tests/test_sql.py checks).
REPLY_WINDOW_DAYS = STEP_DAYS[-1] + 7

# Settings time zones -> Instantly's schedule enum, which has no America/New_York;
# America/Detroit is US Eastern with the same DST rules.
INSTANTLY_TIMEZONES = {"America/New_York": "America/Detroit"}
DEFAULT_WINDOW = SendWindow((0, 1, 2, 3, 4), time(9), time(16), "America/New_York")

# Lead fields this system sets; anything else in a lead dict is refused.
LEAD_FIELDS = frozenset(
    {"email", "first_name", "last_name", "company_name", "website", "job_title", "personalization", "custom_variables"}
)
# Import options on every POST /leads/add. skip_if_in_workspace keeps us from enrolling a
# person another (European) campaign already holds; verification would spend Instantly credits.
LEAD_IMPORT_OPTIONS = {"skip_if_in_workspace": True, "verify_leads_on_import": False}
# Counters in the /leads/add summary that add up across chunks.
ADD_COUNTS = frozenset(
    {"leads_uploaded", "in_blocklist", "duplicated_leads", "skipped_count", "invalid_email_count",
     "incomplete_count", "duplicate_email_count"}
)

# PHASE0-CONFIRM: the custom-variable length limit (SPEC 9) is measured by hand in phase 0.
CUSTOM_VARIABLE_LIMIT: int | None = None

ACCOUNT_STATUS = {1: "active", 2: "paused", 3: "maintenance", -1: "connection_error", -2: "soft_bounce_error", -3: "sending_error"}
WARMUP_STATUS = {0: "paused", 1: "active", -1: "banned", -2: "spam_folder_unknown", -3: "permanent_suspension"}
# GET /campaigns/{id}/sending-status: not_sending_status codes. 3 and 4 are documented;
# PHASE0-CONFIRM: the full list, which phase 0 reads from a paused campaign.
NOT_SENDING = {
    3: "the campaign reached its daily limit",
    4: "every sending account reached its daily limit",
}
AT_LIMIT_CODES = frozenset({3, 4})
CAMPAIGN_STATUS = {
    0: "draft", 1: "active", 2: "paused", 3: "completed", 4: "running_subsequences",
    -99: "account_suspended", -1: "accounts_unhealthy", -2: "bounce_protect",
}
# A lead's `status` (the v2 Lead schema: 1 active, 2 paused, 3 completed, -1 bounced, -2 unsubscribed,
# -3 skipped). sync_outcomes reads bounced and unsubscribed from it:
# a click on the {{unsubscribe}} link stops the lead and marks it unsubscribed (Harry, 1 Oct 2026:
# the opt-out is Instantly's own link). PHASE0-CONFIRM: the codes, read from a lead in a paused
# campaign, and that an unsubscribe click (and the List-Unsubscribe header) sets -2 on the lead.
LEAD_ACTIVE, LEAD_PAUSED, LEAD_BOUNCED, LEAD_UNSUBSCRIBED = 1, 2, -1, -2
# PHASE0-CONFIRM: that PATCH /leads/{id} takes status 2 (paused) and 1 (active), and that a lead set
# back to active goes on with its next step. Until phase 0 says so, nothing calls set_lead_paused:
# an out-of-office reply only records the return date (replies/poll.py).
LEAD_PAUSE_CONFIRMED = False

# POST /leads/update-interest-status values. PHASE0-CONFIRM: that 2 is "Meeting booked" and that a
# lead marked so gets no further steps (stop_lead).
INTEREST_MEETING_BOOKED = 2

_UNRESERVED = frozenset(b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~@")
_RE_PREFIX = ("re:", "re ")


def reply_subject(subject: Any) -> str:
    """The subject of a reply in the thread: the original's, with "Re: " unless it already has one."""
    s = str(subject or "").strip()
    return s if s.lower().startswith(_RE_PREFIX) else f"Re: {s}".strip()


def _segment(text: str) -> str:
    """Percent-encode one URL path segment (an email address keeps its @, loses a raw +)."""
    return "".join(chr(b) if b in _UNRESERVED else f"%{b:02X}" for b in str(text).encode())


def _lower_all(values: Iterable[str] | str) -> list[str]:
    """Lower-cased, stripped, de-duplicated, in order. A bare string counts as one value."""
    if isinstance(values, str):
        values = [values]
    return list(dict.fromkeys(s for s in (str(v).strip().lower() for v in values) if s))


def _chunks(items: Sequence[Any], size: int) -> Iterator[Sequence[Any]]:
    for i in range(0, len(items), size):
        yield items[i : i + size]


def _timestamp(value: datetime | date | str) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, datetime):
        return value.isoformat().replace("+00:00", "Z")
    return value.isoformat()


def text_to_html(text: str) -> str:
    """Plain text as Instantly body HTML: escaped, with <br/> line breaks (the API's documented form)."""
    return html.escape(text, quote=False).replace("\r\n", "\n").replace("\n", "<br/>")


# -- campaign shape helpers (pure; registry code uses them for drift checks) -----


def instantly_schedule(window: SendWindow | None = None, name: str = "US Outbound") -> dict:
    """campaign_schedule for a settings SendWindow (settings days: 0 = Monday).

    PHASE0-CONFIRM: Instantly's days keys run 0 = Sunday .. 6 = Saturday (JS getDay); the
    OpenAPI example is ambiguous. Phase 0 checks the created campaign shows Mon–Fri.
    """
    w = window or DEFAULT_WINDOW
    days = {str(d): False for d in range(7)}
    for d in w.days:
        days[str((d + 1) % 7)] = True
    return {
        "schedules": [
            {
                "name": name,
                "timing": {"from": w.start.strftime("%H:%M"), "to": w.end.strftime("%H:%M")},
                "days": days,
                "timezone": INSTANTLY_TIMEZONES.get(w.tz, w.tz),
            }
        ]
    }


def step_delays(step_days: Sequence[int] = STEP_DAYS) -> list[int]:
    """Instantly's delay is the wait before the NEXT email: day 0, 7, 14, 21 -> 7, 7, 7, 0.

    PHASE0-CONFIRM: the OpenAPI text says "the delay value before sending the NEXT email";
    phase 0 checks the created campaign shows steps on day 0, 7, 14 and 21.
    """
    days = list(step_days)
    return [days[i + 1] - days[i] for i in range(len(days) - 1)] + [0]


def sequences(steps: Sequence[Mapping[str, str]], step_days: Sequence[int] = STEP_DAYS) -> list[dict]:
    """The one-element sequences array: one email step per {subject, body}, one variant each."""
    if len(steps) != len(step_days):
        raise ValueError(f"expected {len(step_days)} steps (SPEC 9), got {len(steps)}")
    out = []
    for step, delay in zip(steps, step_delays(step_days)):
        if not step.get("subject") or not step.get("body"):
            raise ValueError("every step needs a subject and a body")
        out.append(
            {
                "type": "email",
                "delay": delay,
                "delay_unit": "days",
                "variants": [{"subject": step["subject"], "body": step["body"]}],
            }
        )
    return [{"steps": out}]


def settings_drift(
    campaign: Mapping[str, Any],
    *,
    accounts: Iterable[str] | None = None,
    daily_limit: int | None = None,
    window: SendWindow | None = None,
    step_days: Sequence[int] = STEP_DAYS,
    text_only: bool = False,
) -> dict[str, tuple[Any, Any]]:
    """{field: (expected, actual)} for every SPEC 9 setting the campaign no longer matches."""
    drift: dict[str, tuple[Any, Any]] = {}
    for key, want in campaign_settings(text_only).items():
        got = campaign.get(key)
        if got is None and (want is False or key in NOT_RETURNED):
            continue  # left out at its default (false), or never returned
        if got != want:
            drift[key] = (want, got)
    want_sched = instantly_schedule(window)["schedules"][0]
    got_scheds = (campaign.get("campaign_schedule") or {}).get("schedules") or [{}]
    got = got_scheds[0] or {}
    for key in ("timing", "timezone"):
        if got.get(key) != want_sched[key]:
            drift[f"schedule.{key}"] = (want_sched[key], got.get(key))
    got_days = {str(k): bool(v) for k, v in (got.get("days") or {}).items()}
    if {k for k, v in got_days.items() if v} != {k for k, v in want_sched["days"].items() if v}:
        drift["schedule.days"] = (want_sched["days"], got.get("days"))
    if len(got_scheds) != 1:
        drift["schedule.count"] = (1, len(got_scheds))
    seqs = campaign.get("sequences") or [{}]
    steps = (seqs[0] or {}).get("steps") or []
    got_delays = [s.get("delay") for s in steps]
    if got_delays != step_delays(step_days):
        drift["steps.delays"] = (step_delays(step_days), got_delays)
    if any(len(s.get("variants") or []) != 1 for s in steps):
        drift["steps.variants"] = (1, [len(s.get("variants") or []) for s in steps])
    if accounts is not None:
        want_acc, got_acc = sorted(_lower_all(accounts)), sorted(_lower_all(campaign.get("email_list") or []))
        if want_acc != got_acc:
            drift["email_list"] = (want_acc, got_acc)
    if daily_limit is not None and campaign.get("daily_limit") != daily_limit:
        drift["daily_limit"] = (daily_limit, campaign.get("daily_limit"))
    return drift


def custom_variable_limit_probe() -> int | None:
    """PHASE0-CONFIRM stub: the longest custom-variable value Instantly keeps intact.

    Phase 0 measures this by hand (SPEC 9, 14) and records it in CUSTOM_VARIABLE_LIMIT;
    add_leads then refuses longer values. None means not measured yet (no check).
    """
    return CUSTOM_VARIABLE_LIMIT


def _check_custom_variables(variables: Mapping[str, Any], limit: int | None) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in variables.items():
        if v is not None and not isinstance(v, (str, int, float, bool)):
            raise ValueError(f"custom variable {k!r} must be a string, number, boolean or null (Instantly)")
        if limit is not None and isinstance(v, str) and len(v) > limit:
            raise ValueError(f"custom variable {k!r} is {len(v)} characters; the Instantly limit is {limit}")
        out[str(k)] = v
    return out


def _check_campaign_fields(fields: Mapping[str, Any]) -> None:
    """Campaign fields may never turn tracking on (SPEC 1.8) or move off the SPEC 9 values."""
    for key in TRACKING_FIELDS & set(fields):
        if fields[key] is not False:
            raise GuardViolation(f"Instantly {key} stays off (SPEC 1.8)")
    for key in (set(CAMPAIGN_SETTINGS) - TRACKING_FIELDS) & set(fields):
        if fields[key] != CAMPAIGN_SETTINGS[key]:
            raise ValueError(f"{key} is fixed at {CAMPAIGN_SETTINGS[key]!r} by SPEC 9")
    if TEXT_ONLY in fields and not isinstance(fields[TEXT_ONLY], bool):
        raise ValueError("text_only is true or false (the General tab's email_format)")
    if "name" in fields:
        raise ValueError("campaigns are addressed by name; they are never renamed from here")
    if "email_list" in fields:
        raise ValueError("pass the sending list as accounts=, so the guard checks it")


class Instantly(HttpClient):
    system = "instantly"
    base_url = "https://api.instantly.ai/api/v2"

    def __init__(self, guard, transport, token: str = ""):
        super().__init__(guard, transport, token)
        self._ids: dict[str, str] = {}  # campaign name -> id, US Outbound campaigns only
        self._dupes: set[str] = set()

    # -- guard helpers ---------------------------------------------------------

    def _registry(self, action: str, accounts: Iterable[str], *, write: bool = False, target: str = "") -> list[str]:
        """Lower-cased accounts, all in the registry; otherwise the guard refuses before any request."""
        accs = _lower_all(accounts)
        allowed = {x.lower() for x in self.guard.bounds.registry_accounts}
        if not accs or any(a not in allowed for a in accs):
            self.guard.authorize(self.system, Op(action, target=target, write=write, detail={"accounts": accs}))
            raise GuardViolation(f"Instantly {action} must stay within the registry accounts (SPEC 1.2)")
        return accs

    def _us_name(self, name: str, action: str, write: bool) -> None:
        """Refuse a campaign outside "US Outbound – " before any request (even the id lookup)."""
        if not name.startswith(US_CAMPAIGN_PREFIX):
            self.guard.authorize(self.system, Op(action, target=name, write=write))
            raise GuardViolation(f"Instantly campaign {name!r} is not a US Outbound campaign")

    def _campaign_id(self, name: str, action: str, write: bool, *, required: bool = True) -> str | None:
        self._us_name(name, action, write)
        if name not in self._ids and name not in self._dupes:
            self.list_campaigns()
        if name in self._dupes:
            raise LookupError(f"more than one Instantly campaign is named {name!r}; fix it by hand")
        if name not in self._ids:
            if required:
                raise LookupError(f"no Instantly campaign named {name!r}")
            return None
        return self._ids[name]

    # -- accounts and warmup ---------------------------------------------------

    def _get_account(self, email: str) -> dict | None:
        try:
            body = self.request(
                "GET", f"/accounts/{_segment(email)}", Op("account.get", target=email, detail={"accounts": [email]})
            )
        except ApiError as exc:
            if exc.status == 404:
                return None
            raise
        if not isinstance(body, dict) or str(body.get("email", "")).strip().lower() != email:
            log("instantly_dropped", action="account.get", reason="response for another account")
            return None
        return body

    def list_accounts(self, emails: Iterable[str]) -> list[dict]:
        """The registry accounts that exist in Instantly, read one by one (never an unfiltered list)."""
        accs = self._registry("account.get", emails)
        return [a for a in (self._get_account(e) for e in accs) if a is not None]

    def warmup_status(self, emails: Iterable[str]) -> dict[str, dict]:
        """Per email: warmup_enabled, warmup_status, warmup_score, health_score(_label), status."""
        accs = self._registry("account.get", emails)
        found = {e: self._get_account(e) for e in accs}
        # Analytics only for accounts Instantly has: an unknown email could fail the whole call.
        present = [e for e, a in found.items() if a is not None]
        health: dict[str, dict] = {}
        for chunk in _chunks(present, ACCOUNTS_PER_CALL):
            chunk = list(chunk)
            body = self.request(
                "POST",
                "/accounts/warmup-analytics",
                Op("warmup.analytics", target="accounts", detail={"accounts": chunk}),
                json={"emails": chunk},
            ) or {}
            for email, agg in (body.get("aggregate_data") or {}).items():
                if str(email).lower() in chunk:
                    health[str(email).lower()] = agg or {}
        out: dict[str, dict] = {}
        for email in accs:
            acct = found[email]
            agg = health.get(email, {})
            if acct is None:
                out[email] = {"email": email, "found": False, "status": "not_found", "warmup_enabled": False,
                              "warmup_status": "not_found", "warmup_score": None, "health_score": None,
                              "health_score_label": None, "warmup_started_at": None, "daily_limit": None}
                continue
            ws = acct.get("warmup_status")
            score = acct.get("stat_warmup_score")
            out[email] = {
                "email": email,
                "found": True,
                "status": ACCOUNT_STATUS.get(acct.get("status"), "unknown"),
                "warmup_enabled": ws == 1,
                "warmup_status": WARMUP_STATUS.get(ws, "unknown"),
                "warmup_score": score if score is not None else agg.get("health_score"),
                "health_score": agg.get("health_score"),
                "health_score_label": agg.get("health_score_label"),
                "warmup_started_at": acct.get("timestamp_warmup_start"),
                "daily_limit": acct.get("daily_limit"),
            }
        return out

    def enable_warmup(self, emails: Iterable[str]) -> None:
        """Turn warmup on for these registry accounts (never include_all_emails)."""
        accs = self._registry("account.warmup_enable", emails, write=True)
        for chunk in _chunks(accs, ACCOUNTS_PER_CALL):
            chunk = list(chunk)
            self.request(
                "POST",
                "/accounts/warmup/enable",
                Op("account.warmup_enable", target="accounts", write=True, detail={"accounts": chunk}),
                json={"emails": chunk},
            )

    def daily_sends(
        self, emails: Iterable[str], *, start_date: date | str | None = None, end_date: date | str | None = None
    ) -> dict[str, dict[str, dict]]:
        """{email: {YYYY-MM-DD: {sent, bounced, new_leads_contacted, ...}}} for registry accounts only.

        GET /accounts/analytics/daily, filtered to these emails (without the filter it covers the
        whole workspace, so the filter is always sent). Rows for any other account are dropped.
        PHASE0-CONFIRM: that `emails` is sent as a repeated query parameter, and the row fields
        (date, email_account, sent).
        """
        accs = self._registry("account.analytics_daily", emails)
        out: dict[str, dict[str, dict]] = {e: {} for e in accs}
        for chunk in _chunks(accs, ACCOUNTS_PER_CALL):
            chunk = list(chunk)
            params: dict[str, Any] = {"emails": chunk}
            if start_date:
                params["start_date"] = str(start_date)[:10]
            if end_date:
                params["end_date"] = str(end_date)[:10]
            body = self.request(
                "GET", "/accounts/analytics/daily",
                Op("account.analytics_daily", target="accounts", detail={"accounts": chunk}), params=params,
            )
            rows = body if isinstance(body, list) else list((body or {}).get("items") or [])
            for r in rows:
                email = str(r.get("email_account") or r.get("email") or "").strip().lower()
                if email not in out:
                    log("instantly_dropped", action="account.analytics_daily", reason="row for another account")
                    continue
                out[email][str(r.get("date") or "")[:10]] = r
        return out

    def set_daily_limit(self, email: str, daily_limit: int) -> None:
        """Set a registry account's own daily sending limit (the Mailboxes tab's daily_cap)."""
        [acc] = self._registry("account.update_limit", [email], write=True)
        limit = int(daily_limit)
        self.request(
            "PATCH", f"/accounts/{_segment(acc)}",
            Op("account.update_limit", target=acc, write=True, detail={"accounts": [acc], "daily_limit": limit}),
            json={"daily_limit": limit},
        )

    # -- campaigns -------------------------------------------------------------

    def list_campaigns(self) -> list[dict]:
        """Campaigns named "US Outbound – …" (search, then exact prefix); refreshes the name→id cache."""
        found: list[dict] = []
        cursor = ""
        dropped = 0
        while True:
            params: dict[str, Any] = {"search": CAMPAIGN_SEARCH, "limit": PAGE}
            if cursor:
                params["starting_after"] = cursor
            body = self.request(
                "GET", "/campaigns", Op("campaign.list", target="campaigns", detail={"search": CAMPAIGN_SEARCH}), params=params
            ) or {}
            items = body.get("items") or []
            for c in items:
                if str(c.get("name", "")).startswith(US_CAMPAIGN_PREFIX) and c.get("id"):
                    found.append(c)
                else:
                    dropped += 1
            cursor = body.get("next_starting_after") or ""
            if not cursor or not items:
                break
        if dropped:
            log("instantly_dropped", action="campaign.list", dropped=dropped, reason="not a US Outbound campaign")
        ids: dict[str, str] = {}
        dupes: set[str] = set()
        for c in found:
            if c["name"] in ids and ids[c["name"]] != c["id"]:
                dupes.add(c["name"])
            ids[c["name"]] = str(c["id"])
        self._ids = {k: v for k, v in ids.items() if k not in dupes}
        self._dupes = dupes
        return found

    def get_campaign(self, name: str) -> dict | None:
        """The full campaign, or None if no US Outbound campaign has this name yet."""
        cid = self._campaign_id(name, "campaign.get", False, required=False)
        if cid is None:
            return None
        body = self.request("GET", f"/campaigns/{_segment(cid)}", Op("campaign.get", target=name, detail={"id": cid}))
        if not isinstance(body, dict) or str(body.get("id")) != cid or body.get("name") != name:
            raise GuardViolation(f"Instantly returned a different campaign for {name!r}")
        return body

    def create_campaign(
        self,
        name: str,
        *,
        accounts: Iterable[str],
        daily_limit: int,
        schedule: SendWindow | Mapping[str, Any] | None = None,
        steps: Sequence[Mapping[str, str]],
        options: Mapping[str, Any] | None = None,
        text_only: bool = False,
    ) -> dict | None:
        """Create the owner's campaign with the SPEC 9 settings. It stays in Draft: never activated here."""
        self._us_name(name, "campaign.create", True)
        options = dict(options or {})
        _check_campaign_fields(options)
        accs = self._registry("campaign.create", accounts, write=True, target=name)
        op = Op("campaign.create", target=name, write=True, detail={"accounts": accs, "daily_limit": int(daily_limit)})
        if name[len(US_CAMPAIGN_PREFIX):] not in self.guard.bounds.registry_owners:
            self.guard.authorize(self.system, op)  # refuses: no such owner in the registry
            raise GuardViolation(f"no registry owner for campaign {name!r}")
        if isinstance(schedule, SendWindow) or schedule is None:
            campaign_schedule = instantly_schedule(schedule)
        else:
            campaign_schedule = dict(schedule)
        payload: dict[str, Any] = {
            **options,
            **campaign_settings(text_only),
            "name": name,
            "campaign_schedule": campaign_schedule,
            "sequences": sequences(steps),
            "email_list": accs,
            "daily_limit": int(daily_limit),
        }
        if self._campaign_id(name, "campaign.create", True, required=False) is not None:
            raise ValueError(f"Instantly campaign {name!r} already exists")
        body = self.request(
            "POST", "/campaigns", op, json=payload, dry_result={"id": None, "name": name, "status": 0, "dry_run": True}
        )
        if isinstance(body, dict) and body.get("id") and not body.get("dry_run"):
            self._ids[name] = str(body["id"])
        return body

    def update_campaign(self, name: str, fields: Mapping[str, Any], *, accounts: Iterable[str] | None = None) -> None:
        """PATCH campaign fields; accounts replaces the sending list (registry accounts only)."""
        fields = dict(fields)
        _check_campaign_fields(fields)
        detail: dict[str, Any] = {"fields": sorted(fields)}
        if accounts is not None:
            accs = _lower_all(accounts)
            if not accs:
                raise ValueError("an empty sending list: pause the campaign instead")
            detail["accounts"] = self._registry("campaign.update", accs, write=True, target=name)
            fields["email_list"] = detail["accounts"]
        cid = self._campaign_id(name, "campaign.update", True)
        self.request("PATCH", f"/campaigns/{_segment(cid)}", Op("campaign.update", target=name, write=True, detail=detail), json=fields)

    def pause_campaign(self, name: str) -> None:
        cid = self._campaign_id(name, "campaign.pause", True)
        self.request("POST", f"/campaigns/{_segment(cid)}/pause", Op("campaign.pause", target=name, write=True))

    def activate_campaign(self, name: str) -> None:
        """Start or resume. Only the `start` command calls this, never create_campaign."""
        cid = self._campaign_id(name, "campaign.activate", True)
        self.request("POST", f"/campaigns/{_segment(cid)}/activate", Op("campaign.activate", target=name, write=True))

    def sending_status(self, name: str) -> dict:
        """Why the campaign is not sending, if it is not: {code, meaning, at_limit} (GET /campaigns/{id}/sending-status).

        code None means Instantly reports nothing holding it back.
        """
        cid = self._campaign_id(name, "campaign.sending_status", False)
        body = self.request(
            "GET", f"/campaigns/{_segment(cid)}/sending-status", Op("campaign.sending_status", target=name, detail={"id": cid})
        ) or {}
        code = body.get("not_sending_status") if isinstance(body, dict) else None
        try:
            code = int(code) if code is not None else None
        except (TypeError, ValueError):
            code = None
        meaning = NOT_SENDING.get(code, f"code {code} (see Instantly)") if code is not None else ""
        return {"code": code, "meaning": meaning, "at_limit": code in AT_LIMIT_CODES}

    def step_analytics(self, name: str, *, start_date: date | str | None = None, end_date: date | str | None = None) -> list[dict]:
        """Per step and variant counts for this campaign only (campaign_id is always set)."""
        cid = self._campaign_id(name, "campaign.steps_analytics", False)
        params: dict[str, Any] = {"campaign_id": cid}
        if start_date:
            params["start_date"] = _timestamp(start_date)
        if end_date:
            params["end_date"] = _timestamp(end_date)
        body = self.request(
            "GET", "/campaigns/analytics/steps", Op("campaign.steps_analytics", target=name, detail={"id": cid}), params=params
        )
        if isinstance(body, list):
            return body
        return list((body or {}).get("items") or [])

    # -- leads -----------------------------------------------------------------

    def add_leads(self, name: str, leads: Sequence[Mapping[str, Any]]) -> dict | None:
        """Bulk-add leads to the campaign (1000 per call). Returns the merged import summary."""
        if not leads:
            return {"total_sent": 0, "leads_uploaded": 0, "created_leads": []}
        limit = custom_variable_limit_probe()
        clean: list[dict] = []
        for lead in leads:
            extra = set(lead) - LEAD_FIELDS
            if extra:
                raise ValueError(f"lead fields {sorted(extra)} are not set by this system")
            if not str(lead.get("email") or "").strip():
                raise ValueError("every lead needs an email")
            row = {k: v for k, v in lead.items() if k != "custom_variables" and v is not None}
            row["email"] = str(lead["email"]).strip().lower()
            if lead.get("custom_variables"):
                row["custom_variables"] = _check_custom_variables(lead["custom_variables"], limit)
            clean.append(row)
        cid = self._campaign_id(name, "lead.add", True)
        merged: dict[str, Any] = {"total_sent": 0, "leads_uploaded": 0, "created_leads": []}
        sent_any = False
        for start in range(0, len(clean), LEADS_PER_ADD):
            chunk = clean[start : start + LEADS_PER_ADD]
            body = self.request(
                "POST",
                "/leads/add",
                Op("lead.add", target=name, write=True, detail={"count": len(chunk)}),
                json={"campaign_id": cid, "leads": chunk, **LEAD_IMPORT_OPTIONS},
                dry_result=None,
            )
            merged["total_sent"] += len(chunk)
            if body is None:
                continue
            sent_any = True
            for key, value in body.items():
                if key == "total_sent":
                    continue  # counted above
                if key == "created_leads":
                    # index is the lead's position in its chunk; make it the position in `leads`.
                    merged["created_leads"].extend({**c, "index": int(c.get("index", 0)) + start} for c in value or [])
                elif key in ADD_COUNTS:
                    merged[key] = merged.get(key, 0) + int(value or 0)
                else:
                    merged[key] = value  # status, blocklist_used, remaining_in_plan: the last chunk's
        if not sent_any:
            merged["dry_run"] = True
        return merged

    def list_leads(self, name: str) -> list[dict]:
        """Every lead in this campaign (POST /leads/list filtered by campaign id)."""
        cid = self._campaign_id(name, "lead.list", False)
        out: list[dict] = []
        cursor = ""
        dropped = 0
        while True:
            payload: dict[str, Any] = {"campaign": cid, "limit": PAGE}
            if cursor:
                payload["starting_after"] = cursor
            body = self.request("POST", "/leads/list", Op("lead.list", target=name, detail={"id": cid}), json=payload) or {}
            items = body.get("items") or []
            for lead in items:
                if str(lead.get("campaign") or "") == cid:
                    out.append(lead)
                else:
                    dropped += 1
            cursor = body.get("next_starting_after") or ""
            if not cursor or not items:
                break
        if dropped:
            log("instantly_dropped", action="lead.list", dropped=dropped, reason="lead of another campaign")
        return out

    def delete_lead(self, name: str, lead_id: str) -> None:
        """Delete one lead, after checking it belongs to this campaign. A lead already gone is fine."""
        cid = self._campaign_id(name, "lead.delete", True)
        try:
            lead = self.request("GET", f"/leads/{_segment(lead_id)}", Op("lead.get", target=name, detail={"lead_id": lead_id}))
        except ApiError as exc:
            if exc.status == 404:
                return
            raise
        if str((lead or {}).get("campaign") or "") != cid:
            raise GuardViolation(f"lead {lead_id} is not in campaign {name!r}")
        self.request(
            "DELETE", f"/leads/{_segment(lead_id)}", Op("lead.delete", target=name, write=True, detail={"lead_id": lead_id})
        )

    def set_lead_paused(self, name: str, lead_id: str, paused: bool) -> dict | None:
        """Pause one lead of this campaign, or set it going again (re-timing after an out-of-office reply).

        The lead is checked to belong to the campaign first, as delete_lead does. Only the
        status changes. PHASE0-CONFIRM: see LEAD_PAUSE_CONFIRMED.
        """
        cid = self._campaign_id(name, "lead.update", True)
        lead = self.request("GET", f"/leads/{_segment(lead_id)}", Op("lead.get", target=name, detail={"lead_id": lead_id}))
        if str((lead or {}).get("campaign") or "") != cid:
            raise GuardViolation(f"lead {lead_id} is not in campaign {name!r}")
        status = LEAD_PAUSED if paused else LEAD_ACTIVE
        return self.request(
            "PATCH", f"/leads/{_segment(lead_id)}",
            Op("lead.update", target=name, write=True, detail={"lead_id": lead_id, "status": status}),
            json={"status": status}, dry_result={"id": lead_id, "status": status, "dry_run": True},
        )

    def stop_lead(self, name: str, email: str) -> dict | None:
        """Stop a lead's remaining steps in this campaign once a meeting is booked (SPEC 9 hubspot_readback).

        It marks the lead "Meeting booked" (POST /leads/update-interest-status, scoped to the campaign)
        rather than deleting it, since SPEC 13 keeps leads 31 days after their last step.
        PHASE0-CONFIRM: the endpoint and its fields, INTEREST_MEETING_BOOKED, and that Instantly then
        sends the lead no further step; if it does not, delete_lead is the stop that is certain.
        """
        cid = self._campaign_id(name, "lead.stop", True)
        lead = str(email or "").strip().lower()
        if "@" not in lead:
            raise ValueError("stop_lead needs the lead's email address")
        op = Op("lead.stop", target=name, write=True, detail={"id": cid, "interest_value": INTEREST_MEETING_BOOKED})
        payload = {"lead_email": lead, "campaign_id": cid, "interest_value": INTEREST_MEETING_BOOKED}
        return self.request("POST", "/leads/update-interest-status", op, json=payload, dry_result={"dry_run": True})

    # -- emails ----------------------------------------------------------------

    def list_emails(
        self,
        accounts: Iterable[str],
        since: datetime | str | None = None,
        *,
        email_type: str | None = None,
        until: datetime | str | None = None,
    ) -> list[dict]:
        """Emails of the registry accounts, one filtered request series per account (eaccount=).

        email_type: "received", "sent" or "manual" (Instantly's filter); None for all.
        until: created at or before this time (max_timestamp_created), for a run that catches up a
        long gap a week at a time. PHASE0-CONFIRM: that Instantly applies it; if it does not, the
        run reads up to now, as before, and the events stay idempotent.
        Results whose eaccount is not the account asked for are dropped.
        """
        accs = self._registry("email.list", accounts)
        out: list[dict] = []
        seen: set[str] = set()
        dropped = 0
        for acct in accs:
            cursor = ""
            while True:
                params: dict[str, Any] = {"eaccount": acct, "limit": PAGE}
                if since:
                    params["min_timestamp_created"] = _timestamp(since)
                if until:
                    params["max_timestamp_created"] = _timestamp(until)
                if email_type:
                    params["email_type"] = email_type
                if cursor:
                    params["starting_after"] = cursor
                body = self.request(
                    "GET", "/emails", Op("email.list", target=acct, detail={"accounts": [acct]}), params=params
                ) or {}
                items = body.get("items") or []
                for e in items:
                    if str(e.get("eaccount", "")).strip().lower() != acct:
                        dropped += 1
                    elif str(e.get("id")) not in seen:
                        seen.add(str(e.get("id")))
                        out.append(e)
                cursor = body.get("next_starting_after") or ""
                if not cursor or not items:
                    break
        if dropped:
            log("instantly_dropped", action="email.list", dropped=dropped, reason="email of a non-registry account")
        return out

    def _owned_email(self, eaccount: str, email_id: str) -> dict:
        """The email, checked to belong to eaccount (replies go from the mailbox the prospect wrote to)."""
        body = self.request(
            "GET", f"/emails/{_segment(email_id)}", Op("email.get", target=eaccount, detail={"accounts": [eaccount], "id": email_id})
        )
        if not isinstance(body, dict) or str(body.get("eaccount", "")).strip().lower() != eaccount:
            raise GuardViolation(f"email {email_id} does not belong to {eaccount}")
        return body

    def get_email(self, eaccount: str, email_id: str) -> dict:
        """One email of a registry mailbox (GET /emails/{id}), with its body, checked to belong to that mailbox."""
        [acct] = self._registry("email.get", [eaccount], target=eaccount)
        return self._owned_email(acct, email_id)

    def _campaign_named_by_id(self, cid: str) -> str | None:
        """The US Outbound campaign with this id, or None; the name→id cache is refreshed once if needed."""
        for refresh in (False, True):
            if refresh:
                self.list_campaigns()
            name = next((n for n, known in self._ids.items() if known == cid), None)
            if name is not None:
                return name
        return None

    def _need_approval(self, acct: str, approved_by: str) -> None:
        """Refuse before any request unless an approver approved this reply (SPEC 1.3, D11; the guard checks too)."""
        b = self.guard.bounds
        owners = {sid for address, sid in b.owner_slack_ids if address.lower() == acct}
        by = str(approved_by or "").strip()
        if (by == CLI_APPROVER and self.guard.job == REPLIES_CLI_JOB) or (
            by not in ("", CLI_APPROVER) and by in b.approver_slack_ids | owners
        ):
            return
        detail = {"accounts": [acct], "campaign": US_CAMPAIGN_PREFIX, "approved_by": by}
        self.guard.authorize(self.system, Op("email.reply", target=acct, write=True, detail=detail))  # refuses
        raise GuardViolation("nothing goes to a prospect after their reply unless an approver approved it (SPEC 1.3)")

    def reply(
        self, eaccount: str, reply_to_uuid: str, subject: str | None, body: str, *, approved_by: str
    ) -> dict | None:
        """Reply in the thread from the registry mailbox that received the email (SPEC 11 Approval).

        Only in a thread of a US Outbound campaign: the email replied to must carry the id of a
        "US Outbound – {owner}" campaign. PHASE0-CONFIRM: that Instantly sets campaign_id on a
        prospect's reply, and that reply_to_uuid is the id of the email being answered.
        approved_by is the approver's Slack id, or "cli" from `us-outbound replies approve`; the
        guard refuses anyone else (SPEC 1.3, decision D11). subject None answers "Re: " the original's.
        """
        [acct] = self._registry("email.reply", [eaccount], write=True, target=eaccount)
        self._need_approval(acct, approved_by)
        original = self._owned_email(acct, reply_to_uuid)
        cid = str(original.get("campaign_id") or "").strip()
        campaign = self._campaign_named_by_id(cid) if cid else None
        op = Op(
            "email.reply",
            target=acct,
            write=True,
            detail={"accounts": [acct], "reply_to_uuid": reply_to_uuid, "campaign": campaign or "",
                    "approved_by": str(approved_by).strip()},
        )
        if campaign is None:
            self.guard.authorize(self.system, op)  # refuses: not a thread of a US Outbound campaign
            raise GuardViolation(f"email {reply_to_uuid} is not in a US Outbound campaign")
        payload = {
            "eaccount": acct,
            "reply_to_uuid": reply_to_uuid,
            "subject": reply_subject(original.get("subject")) if subject is None else subject,
            "body": {"text": body, "html": text_to_html(body)},
        }
        return self.request("POST", "/emails/reply", op, json=payload, dry_result={"id": None, "dry_run": True})

    def forward(
        self, eaccount: str, email_id: str, to: str | Sequence[str], note: str, *, subject: str | None = None
    ) -> dict | None:
        """Forward the email (with its original thread) from the mailbox that received it (SPEC 11).

        PHASE0-CONFIRM: POST /emails/forward is in the v2 OpenAPI; confirm it works on our plan.
        """
        [acct] = self._registry("email.forward", [eaccount], write=True, target=eaccount)
        recipients = _lower_all([to] if isinstance(to, str) else to)
        if not recipients:
            raise ValueError("forward needs a recipient")
        op = Op("email.forward", target=acct, write=True, detail={"accounts": [acct], "to": recipients, "id": email_id})
        escalation = self.guard.bounds.escalation_email.strip().lower()
        if not escalation or set(recipients) != {escalation}:
            self.guard.authorize(self.system, op)  # refuses before any request (even reading the email)
            raise GuardViolation("Instantly forwards go only to escalation_email (SPEC 11)")
        original = self._owned_email(acct, email_id)
        payload = {
            "eaccount": acct,
            "reply_to_uuid": email_id,
            "to_address_email_list": ",".join(recipients),
            "subject": subject or f"Fwd: {original.get('subject') or ''}".strip(),
            "body": {"text": note, "html": text_to_html(note)},
            "include_original_body": True,
        }
        return self.request("POST", "/emails/forward", op, json=payload, dry_result={"id": None, "dry_run": True})

    # -- blocklist -------------------------------------------------------------

    def blocklist_add(self, entries: Iterable[str]) -> None:
        """Add email addresses to the workspace blocklist (unsubscribes, SPEC 11).

        Domains are refused: the blocklist is shared with the European campaigns, and a
        domain entry would block the whole company for them too.
        """
        values = _lower_all(entries)
        bad = [v for v in values if "@" not in v]
        if bad:
            raise ValueError(f"only email addresses go on the shared Instantly blocklist, not {len(bad)} domain(s)")
        for chunk in _chunks(values, BLOCKLIST_PER_CALL):
            chunk = list(chunk)
            self.request(
                "POST",
                "/block-lists-entries/bulk-create",
                Op("blocklist.add", target="blocklist", write=True, detail={"entries": chunk}),
                json={"bl_values": chunk},
            )
