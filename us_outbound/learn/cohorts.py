"""The cohort report: each enrolment week's companies at fixed ages, and what changed between them (Harry, 7 Oct 2026).

Harry: "it would also be helpful to have cohort based reports: performance and settings, to help the system learn
and generate insights."

A cohort is the ISO week (UK time) its company's first contact was enrolled in, "2026-W41": derived from
contacts.enrolled_at, never stored, so every contact is in one from the start. Within a week, contacts carry the
config version they were rendered under (contacts.config_version; config_version.py), the split when a week holds
two; a contact enrolled before 8 Oct 2026 is "unstamped". The company is the unit (SPEC 2): every contact of the
company counts to its first contact's cohort, a second contact's sends, bounces and unsubscribes too.

Performance (rows): each cohort at AGES, 7, 14, 21 and 28 days after the company's first step-1 send (as
v_account_outcomes finds step 1). A company counts at an age once it has reached it, so a 3-day-old cohort shows
nothing at 7 days and two cohorts are compared at the same age over the companies that have reached it:
  delivered companies (step 1 not bounced): replied (a human reply: every class but out-of-office, an unclassified
  one too), positive (positive or referral), meetings (meeting_booked), demos held (demo_held), each within the age;
  emailed companies: sends, sends by step, bounces, unsubscribes and complaints within the age.
Apollo credits are counted per cohort from credit_ledger rows that carry a company; Claude's ledger rows carry none,
so its spend is the month's, on one line, and not split by cohort.

Settings (changes): what differs between two config versions in plain words, from their config_versions rows (the
code, the campaign constants, the signature, the General content keys, the sendable Copy rows) and the settings
table's history (the content tabs' rows then in force), and the campaign changes made under leads in flight
between them (config_log).

Insights (insights): consecutive cohorts compared at the latest age both have reached, with signal_review's
two-proportion test, only from MIN_COMPANIES (30) companies a side: "W42 replies less than W41 at 14 days (2.9%
vs 6.2%, p = 0.07)" and then what changed between them, "a coincidence to test, not a cause"; two versions inside
one week, likewise; and a reply rate that has risen or fallen three cohorts running. Nothing re-weights or decides:
a line points at a change worth a pre-registered test on the Tests tab.

Shown in the Monday readout (readout_lines, after the cuts) and by `us-outbound cohorts` (lines, changes_lines,
in_flight_lines). Read-only: it writes nothing. Python over the tables, like learn/looks.py, so it runs on a
MemoryStore as on Postgres.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

from us_outbound import config_version
from us_outbound.context import ET, UK, Context
from us_outbound.enrol import capacity
from us_outbound.enrol.enrol import iso_week
from us_outbound.learn import signal_review

AGES = (7, 14, 21, 28)  # days after email 1; 28 is the reply window (instantly.REPLY_WINDOW_DAYS)
CUTS = ("all", "tier", "angle", "industry_group", "sender", "copy_version", "subject_arm", "opener_arm",
        "config_version")
MIN_COMPANIES = signal_review.MIN_COMPANIES  # 30: a rate on fewer companies is "too few to read"
MIN_SENDS = 100  # a bounce rate on fewer sends moves a lot with one bounce (as the readout)
P_VALUE = signal_review.P_VALUE  # 0.10
WEEKS = 8  # the CLI's default
READOUT_WEEKS = 4
READOUT_CHANGES = 8  # change lines in the readout before "and N more"
TAB_ROWS = 4  # rows named per tab in a change line before "and N more"
SHORT = 40  # a value this long or shorter is shown as "x → y"; a longer one as "changed"
UNSTAMPED = "unstamped"
NOT_RECORDED = "not recorded"
SENT, BOUNCED, REPLIED, UNSUBSCRIBED, COMPLAINED = "sent", "bounced", "replied", "unsubscribed", "complained"
MEETING, DEMO = "meeting_booked", "demo_held"
EVENT_TYPES = (SENT, BOUNCED, REPLIED, UNSUBSCRIBED, COMPLAINED, MEETING, DEMO)
NOT_HUMAN = "out_of_office"
POSITIVE = frozenset({"positive", "referral"})
ID_CHUNK = 1000


# -- small helpers ----------------------------------------------------------------------------------------------------


def _ts(v: Any) -> datetime | None:
    if isinstance(v, str) and v:
        try:
            v = datetime.fromisoformat(v.replace("Z", "+00:00"))
        except ValueError:
            return None
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=UTC)
    return None


def _n(n: int | float, one: str, many: str | None = None) -> str:
    return f"{n:,} {one if n == 1 else (many or one + 's')}"


def _pct(k: int, n: int) -> str:
    return f"{k / n:.1%}" if n else "-"


def rate(k: int, n: int, minimum: int = MIN_COMPANIES) -> str:
    return _pct(k, n) if n >= minimum else "too few to read"


def short_week(week: str) -> str:
    """"2026-W41" -> "W41"."""
    return week.split("-", 1)[-1]


def week_days(start: date) -> str:
    """The Monday of a week -> "5–11 Oct", or "28 Sep–4 Oct" across a month."""
    end = start + timedelta(days=6)
    return f"{start.day}–{end.day} {end:%b}" if start.month == end.month else f"{start.day} {start:%b}–{end.day} {end:%b}"


def _uk_day(t: datetime) -> date:
    return t.astimezone(UK).date()


def _chunks(items: Sequence[str]) -> Iterable[list[str]]:
    for i in range(0, len(items), ID_CHUNK):
        yield list(items[i : i + ID_CHUNK])


# -- the companies ----------------------------------------------------------------------------------------------------


@dataclass
class Company:
    """One company, in the cohort of its first enrolled contact."""

    account_id: str
    week: str
    week_start: date
    enrolled_at: datetime
    contacts: int
    values: dict[str, str]  # cut -> its value
    code_sha: str = ""
    step1_at: datetime | None = None
    delivered: bool = False
    events: list[dict] = field(default_factory=list)  # every event of its contacts or at the account

    def within(self, types: Iterable[str], days: int) -> list[dict]:
        """Its events of these types from email 1 to `days` after it."""
        if self.step1_at is None:
            return []
        end = self.step1_at + timedelta(days=days)
        return [e for e in self.events if e["type"] in types and self.step1_at <= e["at"] < end]

    def reached(self, days: int, now: datetime) -> bool:
        return self.step1_at is not None and now >= self.step1_at + timedelta(days=days)


def _values(ctx: Context, account: Mapping[str, Any], first: Mapping[str, Any]) -> dict[str, str]:
    def text(v: Any) -> str:
        return str(v or "").strip() or NOT_RECORDED

    return {
        "all": "all",
        "tier": text(first.get("tier_at_enrol") or account.get("tier")),
        "angle": text(first.get("angle") or account.get("angle")),
        "industry_group": text(ctx.settings.industry_group_of(account)),
        "sender": text(account.get("sender") or capacity.owner_of(str(first.get("instantly_campaign") or ""))),
        "copy_version": text(first.get("copy_version")),
        "subject_arm": text(first.get("subject_arm")),
        "opener_arm": text(first.get("opener_arm")),
        "config_version": str(first.get("config_version") or "") or UNSTAMPED,
    }


def companies(ctx: Context) -> list[Company]:
    """Every company with an enrolled contact, with its events, step 1 and whether step 1 was delivered."""
    store = ctx.store
    contacts = [c for c in store.select("contacts") if _ts(c.get("enrolled_at")) and c.get("account_id")]
    by_account: dict[str, list[dict]] = defaultdict(list)
    for c in contacts:
        by_account[str(c["account_id"])].append(c)
    accounts = {str(a["account_id"]): a for chunk in _chunks(sorted(by_account))
                for a in store.select("accounts", {"account_id": chunk})}
    out: dict[str, Company] = {}
    for aid, people in by_account.items():
        first = min(people, key=lambda c: (_ts(c["enrolled_at"]), str(c.get("contact_id"))))
        at = _ts(first["enrolled_at"])
        day = _uk_day(at)
        out[aid] = Company(aid, iso_week(day), day - timedelta(days=day.weekday()), at, len(people),
                           _values(ctx, accounts.get(aid, {}), first), str(first.get("code_sha") or ""))
    account_of = {str(c["contact_id"]): str(c["account_id"]) for c in contacts}
    for e in store.select("events", {"type": list(EVENT_TYPES)}):
        t = _ts(e.get("occurred_at"))
        co = out.get(str(e.get("account_id") or "") or account_of.get(str(e.get("contact_id") or ""), ""))
        if t is not None and co is not None:
            co.events.append({**e, "at": t})
    for co in out.values():
        co.events.sort(key=lambda e: (e["at"], str(e.get("event_id"))))
        step1 = next((e for e in co.events if e["type"] == SENT and e.get("step") == 1), None)
        if step1 is not None:
            co.step1_at = step1["at"]
            co.delivered = not any(e["type"] == BOUNCED and e.get("contact_id") == step1.get("contact_id")
                                   and e.get("step") in (1, None) for e in co.events)
        co.events = [e for e in co.events if e["type"] != REPLIED or str(e.get("reply_class") or "") != NOT_HUMAN]
    return sorted(out.values(), key=lambda c: (c.enrolled_at, c.account_id))


# -- performance ------------------------------------------------------------------------------------------------------


@dataclass
class Age:
    days: int
    matured: int = 0  # delivered companies that have reached it: the denominator of the reply rates
    replied: int = 0
    positive: int = 0
    meetings: int = 0
    demos: int = 0
    emailed: int = 0  # emailed companies that have reached it: the bounces, sends and opt-outs are theirs
    sends: int = 0
    sends_by_step: Counter = field(default_factory=Counter)
    bounced: int = 0
    unsubscribed: int = 0
    complained: int = 0

    def add(self, co: Company) -> None:
        self.emailed += 1
        sends = co.within((SENT,), self.days)
        self.sends += len(sends)
        self.sends_by_step.update(int(e["step"]) for e in sends if isinstance(e.get("step"), int))
        self.bounced += len(co.within((BOUNCED,), self.days))
        self.unsubscribed += bool(co.within((UNSUBSCRIBED,), self.days))
        self.complained += bool(co.within((COMPLAINED,), self.days))
        if not co.delivered:
            return
        self.matured += 1
        replies = co.within((REPLIED,), self.days)
        self.replied += bool(replies)
        self.positive += any(str(e.get("reply_class") or "") in POSITIVE for e in replies)
        self.meetings += bool(co.within((MEETING,), self.days))
        self.demos += bool(co.within((DEMO,), self.days))


@dataclass
class Cohort:
    week: str
    week_start: date
    cut: str
    value: str
    enrolled: int = 0  # companies
    contacts: int = 0
    emailed: int = 0
    delivered: int = 0
    ages: dict[int, Age] = field(default_factory=lambda: {a: Age(a) for a in AGES})
    versions: Counter = field(default_factory=Counter)  # config version -> companies
    code: Counter = field(default_factory=Counter)  # code sha -> companies
    apollo_credits: float = 0.0
    companies: list[Company] = field(default_factory=list)

    @property
    def label(self) -> str:
        return short_week(self.week)

    def latest_age(self) -> int | None:
        """The oldest age any of its companies has reached."""
        return max((a for a, n in self.ages.items() if n.emailed), default=None)

    def last_version(self) -> str:
        """The version of its last company enrolled (unstamped when that one has none)."""
        return self.companies[-1].values["config_version"] if self.companies else UNSTAMPED


def rows(ctx: Context, cut: str = "all", weeks: int | None = None, found: Sequence[Company] | None = None) -> list[Cohort]:
    """Each cohort (week, and the cut's value), oldest first; the last `weeks` weeks when given. found: the
    companies, when the caller has loaded them already."""
    if cut not in CUTS:
        raise ValueError(f"cut is one of {', '.join(CUTS)}")
    out: dict[tuple[str, str], Cohort] = {}
    for co in companies(ctx) if found is None else found:
        key = (co.week, co.values[cut])
        c = out.setdefault(key, Cohort(co.week, co.week_start, cut, co.values[cut]))
        c.companies.append(co)
        c.enrolled += 1
        c.contacts += co.contacts
        c.versions[co.values["config_version"]] += 1
        c.code[co.code_sha or UNSTAMPED] += 1
        if co.step1_at is None:
            continue
        c.emailed += 1
        c.delivered += co.delivered
        for a in AGES:
            if co.reached(a, ctx.now):
                c.ages[a].add(co)
    _apollo(ctx, out.values())
    cohorts = sorted(out.values(), key=lambda c: (c.week_start, c.value))
    if weeks:
        keep = sorted({c.week_start for c in cohorts})[-weeks:]
        cohorts = [c for c in cohorts if c.week_start in keep]
    return cohorts


def _apollo(ctx: Context, cohorts: Iterable[Cohort]) -> None:
    """Apollo credits spent on each cohort's companies (reveals, enrichment and lookups carry the company)."""
    of = {co.account_id: c for c in cohorts for co in c.companies}
    for r in ctx.store.select("credit_ledger", {"system": "apollo"}):
        c = of.get(str(r.get("account_id") or ""))
        if c is not None:
            c.apollo_credits += float(r.get("credits") or 0)


def spend_line(ctx: Context) -> str:
    """This month's Apollo credits spent on no company in particular (searches) and Claude's dollars: neither is
    split by cohort, Claude's ledger rows carrying no company."""
    now = ctx.now.astimezone(UTC)
    rows_ = ctx.store.select("credit_ledger", {"system": ["apollo", "claude"]})
    month = [r for r in rows_ if (t := _ts(r.get("occurred_at"))) is not None and (t.year, t.month) == (now.year, now.month)]
    searches = sum(float(r.get("credits") or 0) for r in month if r.get("system") == "apollo" and not r.get("account_id"))
    claude = sum(float(r.get("usd") or 0) for r in month if r.get("system") == "claude")
    return (f"Not split by cohort, this month (UTC): {searches:,.0f} Apollo credits on searches, and ${claude:,.2f} of "
            "Claude (its spend is recorded without a company).")


# -- what changed between two versions --------------------------------------------------------------------------------


def _short(v: Any) -> bool:
    text = str(v if v is not None else "")
    return len(text) <= SHORT and "\n" not in text


def _value_change(name: str, old: Any, new: Any) -> str:
    old, new = ("(blank)" if v in (None, "") else str(v) for v in (old, new))
    return f"{name} {old} → {new}" if _short(old) and _short(new) else f"{name} changed"


def _rows_at(ctx: Context, tab: str, at: datetime | None) -> dict[str, dict]:
    """The tab's rows in force at that moment, by key, from the settings table's history."""
    if at is None:
        return {}
    out: dict[str, dict] = {}
    for r in ctx.store.select("settings", {"tab": tab}):
        start, end = _ts(r.get("effective_from")), _ts(r.get("effective_to"))
        if start is not None and start <= at and (end is None or end > at):
            out[str(r.get("key"))] = dict(r.get("values") or {})
    return out


def _tab_lines(ctx: Context, tab: str, before: datetime | None, after: datetime | None,
               only: Iterable[str] | None = None) -> list[str]:
    """"Signals New People leader: weight 20 → 30, opener_people changed", one a row, from the settings history."""
    old, new = _rows_at(ctx, tab, before), _rows_at(ctx, tab, after)
    keys = sorted(set(only) if only is not None else set(old) | set(new))
    out: list[str] = []
    for k in keys:
        if k not in old and k not in new:  # not in the settings history (a store without it)
            continue
        if k not in new:
            out.append(f"{tab} {k}: removed")
        elif k not in old:
            out.append(f"{tab} {k}: added")
        elif old[k] != new[k]:
            cols = sorted(set(old[k]) | set(new[k]))
            out.append(f"{tab} {k}: " + ", ".join(_value_change(col, old[k].get(col), new[k].get(col))
                                                for col in cols if old[k].get(col) != new[k].get(col)))
    if len(out) > TAB_ROWS:
        out = out[:TAB_ROWS] + [f"{tab}: and {len(out) - TAB_ROWS} more rows"]
    return out or [f"{tab}: changed"]


def _copy_lines(ctx: Context, a: Mapping[str, Any], b: Mapping[str, Any]) -> list[str]:
    old, new = a.get("copy_hashes") or {}, b.get("copy_hashes") or {}
    out = [f"Copy {v}: now sendable" for v in sorted(set(new) - set(old))]
    out += [f"Copy {v}: no longer sent" for v in sorted(set(old) - set(new))]
    reworded = sorted(v for v in set(old) & set(new) if old[v] != new[v])
    if reworded:
        found = _tab_lines(ctx, "Copy", _ts(a.get("first_seen")), _ts(b.get("first_seen")), reworded)
        named = [line for line in found if line != "Copy: changed"]
        out += named or [f"Copy {v}: new wording ({old[v]} → {new[v]})" for v in reworded]
    return out


def diff(ctx: Context, a: Mapping[str, Any], b: Mapping[str, Any]) -> list[str]:
    """What differs between two config_versions rows, in plain words; [] when nothing does."""
    out: list[str] = []
    if a.get("code_sha") != b.get("code_sha"):
        out.append(f"code {str(a.get('code_sha') or '?')[:7]} → {str(b.get('code_sha') or '?')[:7]}")
    if a.get("step_days") != b.get("step_days"):
        days = (", ".join(str(d) for d in (r.get("step_days") or ())) for r in (a, b))
        out.append("step days {} → {}".format(*days))
    elif a.get("campaign_fingerprint") != b.get("campaign_fingerprint"):
        out.append("campaign template or settings changed (the unsubscribe line, text_only or the SPEC 9 settings)")
    if a.get("signature_hash") != b.get("signature_hash"):
        out.append("signature template changed")
    old_g, new_g = a.get("general") or {}, b.get("general") or {}
    for k in config_version.CONTENT_KEYS:
        if old_g.get(k) != new_g.get(k):
            out.append(_value_change(f"General {k}:", old_g.get(k), new_g.get(k)))
    out += _copy_lines(ctx, a, b)
    old_v, new_v = a.get("settings_versions") or {}, b.get("settings_versions") or {}
    for tab in config_version.CONTENT_TABS:
        if old_v.get(tab) != new_v.get(tab):
            out += _tab_lines(ctx, tab, _ts(old_v.get(tab)), _ts(new_v.get(tab)))
    return out


def log_lines(ctx: Context, start: datetime | None, end: datetime | None) -> list[str]:
    """The config_log rows in [start, end): campaign changes made under leads in flight, From names set, and the
    campaigns paused over a blackout and started again after it (registry/blackout.py)."""
    out = []
    found = [(t, r) for r in ctx.store.select(config_version.LOG_TABLE) if (t := _ts(r.get("changed_at"))) is not None
             and (start is None or t >= start) and (end is None or t < end)]
    for t, r in sorted(found, key=lambda x: (x[0], str(x[1].get("log_id")))):
        when = t.astimezone(UK).strftime("%a %d %b %H:%M")
        n = int(r.get("leads_in_flight") or 0)
        detail = r.get("detail") or {}
        if r.get("kind") == config_version.SENDER_NAME:
            name = " ".join(str(x) for x in (detail.get("to") or ()))
            out.append(f"{when}: From name of {', '.join(r.get('changed_keys') or ())} set to \"{name}\" "
                       f"({_n(n, 'lead')} in flight)")
        elif r.get("kind") == config_version.BLACKOUT_PAUSE:
            until = f" until {detail['resumes_on']}" if detail.get("resumes_on") else ""
            out.append(f"{when}: {r.get('campaign')} paused for the blackout{until} ({_n(n, 'lead')} in flight)")
        elif r.get("kind") == config_version.BLACKOUT_RESUME:
            done = ("started again after the blackout" if detail.get("outcome") == "resumed"
                    else f"not started again after the blackout: {detail.get('why') or detail.get('outcome')}")
            out.append(f"{when}: {r.get('campaign')} {done} ({_n(n, 'lead')} in flight)")
        else:
            out.append(f"{when}: {', '.join(r.get('changed_keys') or ())} applied to {r.get('campaign')} with "
                       f"{_n(n, 'lead')} in flight")
    return out


def _version(ctx: Context, vid: str) -> dict | None:
    return ctx.store.get(config_version.TABLE, config_version=vid) if vid and vid != UNSTAMPED else None


def changes(ctx: Context, a: str, b: str) -> list[str]:
    """What changed from version a to version b: the snapshot diff, then the campaign changes logged between when
    each was first seen. ["No change."] when nothing did."""
    if a == b:
        return ["No change."]
    old, new = _version(ctx, a), _version(ctx, b)
    if old is None or new is None:
        missing = ", ".join(v for v, row in ((a, old), (b, new)) if row is None)
        return [f"Not recorded for {missing}: contacts enrolled before 8 Oct 2026 are unstamped."]
    out = diff(ctx, old, new)
    seen = sorted(t for t in (_ts(old.get("first_seen")), _ts(new.get("first_seen"))) if t is not None)
    if len(seen) == 2:
        out += log_lines(ctx, seen[0], seen[1])
    return out or ["No change."]


def versions(ctx: Context) -> list[dict]:
    """Every recorded config version, first seen first."""
    rows_ = ctx.store.select(config_version.TABLE)
    return sorted(rows_, key=lambda r: (_ts(r.get("first_seen")) or datetime.min.replace(tzinfo=UTC),
                                        str(r.get("config_version"))))


def week_changes(ctx: Context, start: datetime, end: datetime) -> list[str]:
    """What changed in [start, end): each version first seen then against the one before it, and the config_log."""
    out: list[str] = []
    before: dict | None = None
    for v in versions(ctx):
        t = _ts(v.get("first_seen"))
        if t is None or t >= end:
            break
        if t >= start and before is not None:
            out += diff(ctx, before, v)
        before = v
    return out + log_lines(ctx, start, end)


# -- insights ---------------------------------------------------------------------------------------------------------


def common_age(a: Cohort, b: Cohort) -> int | None:
    """The latest age both cohorts have companies at."""
    return max((x for x in AGES if a.ages[x].matured and b.ages[x].matured), default=None)


def _compare(what: str, verb: str, label_a: str, label_b: str, age: int, a: tuple[int, int], b: tuple[int, int],
             always: bool) -> str | None:
    """One verdict line: b against a (k of n each). always: also say so when there is no clear difference."""
    (ka, na), (kb, nb) = a, b
    p = signal_review.p_value(na, ka, nb, kb)
    numbers = f"{_pct(kb, nb)} vs {_pct(ka, na)}" + (f", p = {p:.2f}" if p is not None else "")
    if p is not None and p < P_VALUE:
        more = "more" if kb / nb > ka / na else "less"
        return f"{label_b} {verb} {more} than {label_a} at {age} days ({numbers})."
    return f"No clear difference in {what} between {label_b} and {label_a} at {age} days ({numbers})." if always else None


def compare(ctx: Context, older: Cohort, newer: Cohort, label_a: str = "", label_b: str = "") -> list[str]:
    """newer against older at the latest age both have reached: replies, then positive replies and bounces when
    they differ; and what changed between them when something does. Nothing until both have 30 companies."""
    label_a, label_b = label_a or older.label, label_b or newer.label
    age = common_age(older, newer)
    if age is None:
        return []
    a, b = older.ages[age], newer.ages[age]
    if min(a.matured, b.matured) < MIN_COMPANIES:
        label, small = (label_b, b) if b.matured < MIN_COMPANIES else (label_a, a)
        return [f"Too few to read: {label} has {_n(small.matured, 'company', 'companies')} at {age} days "
                f"({MIN_COMPANIES} needed on each side to compare {label_b} with {label_a})."]
    out = [_compare("replies", "replies", label_a, label_b, age, (a.replied, a.matured), (b.replied, b.matured), True)]
    out.append(_compare("positive replies", "has positive replies", label_a, label_b, age,
                        (a.positive, a.matured), (b.positive, b.matured), False))
    if min(a.sends, b.sends) >= MIN_SENDS:
        out.append(_compare("bounces", "bounces", label_a, label_b, age, (a.bounced, a.sends), (b.bounced, b.sends),
                            False))
    out = [line for line in out if line]
    if any(not line.startswith("No clear") for line in out):
        a_v, b_v = older.last_version(), newer.last_version()
        found = changes(ctx, a_v, b_v)
        if UNSTAMPED in (a_v, b_v):
            out.append("What changed between them is not recorded: contacts enrolled before 8 Oct 2026 are unstamped.")
        else:
            out.append("Nothing changed between them." if found == ["No change."]
                       else f"Between them: {'; '.join(found)}. A coincidence to test, not a cause.")
    return out


def run_of_three(cohorts: Sequence[Cohort], age: int = 14) -> str | None:
    """The latest three cohorts readable at `age` whose reply rate rose, or fell, each time."""
    ok = [c for c in cohorts if c.ages[age].matured >= MIN_COMPANIES]
    if len(ok) < 3:
        return None
    last = ok[-3:]
    rates = [c.ages[age].replied / c.ages[age].matured for c in last]
    if not (rates[0] < rates[1] < rates[2] or rates[0] > rates[1] > rates[2]):
        return None
    way = "risen" if rates[2] > rates[0] else "fallen"
    shown = ", ".join(f"{c.label} {_pct(c.ages[age].replied, c.ages[age].matured)}" for c in last)
    return f"The {age}-day reply rate has {way} three cohorts running: {shown}."


def within_week(ctx: Context, weeks: Iterable[str], found: Sequence[Company] | None = None) -> list[str]:
    """Two config versions inside one week, each with 30 companies at a common age: compared, naming the change."""
    by_week: dict[str, list[Cohort]] = defaultdict(list)
    for c in rows(ctx, "config_version", found=found):
        if c.value != UNSTAMPED:
            by_week[c.week].append(c)
    first_seen = {str(v["config_version"]): i for i, v in enumerate(versions(ctx))}
    out: list[str] = []
    for week in weeks:
        parts = sorted(by_week.get(week, ()), key=lambda c: first_seen.get(c.value, len(first_seen)))
        for older, newer in zip(parts, parts[1:]):
            age = common_age(older, newer)
            if age is None or min(older.ages[age].matured, newer.ages[age].matured) < MIN_COMPANIES:
                continue
            out += compare(ctx, older, newer, f"{short_week(week)} version {older.value}",
                           f"{short_week(week)} version {newer.value}")
    return out


def insights(ctx: Context, cohorts: Sequence[Cohort], found: Sequence[Company] | None = None,
             latest_only: bool = False) -> list[str]:
    """Verdicts with their numbers over the `all` cut's cohorts, oldest first: each consecutive pair (latest_only: the
    latest pair that has a common age), the weeks with two versions, and a three-cohort run."""
    out: list[str] = []
    for older, newer in reversed(list(zip(cohorts, cohorts[1:]))):
        pair = compare(ctx, older, newer)
        out = pair + out
        if latest_only and pair:
            break
    weeks = [c.week for c in cohorts]
    out += within_week(ctx, weeks[-2:] if latest_only else weeks, found)
    three = run_of_three(cohorts)
    return out + ([three] if three else [])


# -- in flight --------------------------------------------------------------------------------------------------------


def in_flight_lines(ctx: Context) -> list[str]:
    """Each owner's campaign and its leads with a step still to send: what a change to its steps, delays or
    text_only would reach (registry/mailboxes.IN_FLIGHT_KEYS holds that drift while this is above 0)."""
    from us_outbound.registry.mailboxes import campaign_name

    today = ctx.now_et().date()
    flying = capacity.in_flight(ctx.store, ctx.settings, today)
    by_campaign: dict[str, list[dict]] = defaultdict(list)
    for c in flying:
        by_campaign[str(c.get("instantly_campaign"))].append(c)
    out = [f"Leads in flight on {today:%a %d %b} (US Eastern): a step still to send, from the send forecast. A change "
           "to a campaign's steps, delays or text_only reaches each of them."]
    names = [campaign_name(o) for o in ctx.settings.owners()]
    for name in names + sorted(set(by_campaign) - set(names)):
        leads = by_campaign.get(name, [])
        if not leads:
            out.append(f"  {name}: none: a change to its campaign reaches no lead")
            continue
        last = max(capacity.step_days(_ts(c["enrolled_at"]).astimezone(ET).date(), ctx.settings)[-1] for c in leads)
        weeks = Counter(iso_week(_uk_day(_ts(c["enrolled_at"]))) for c in leads)
        out.append(f"  {name}: {_n(len(leads), 'lead')}, the last step due {last:%a %d %b} ("
                   + ", ".join(f"{short_week(w)} {n}" for w, n in sorted(weeks.items())) + ")")
    return out


# -- the CLI's table and the readout's section -----------------------------------------------------------------------


def age_line(a: Age) -> str:
    """"14 days: 5 of 60 replied (too few to read), 2 positive, 1 meeting, 0 demos held · 3 bounces of 230 sends
    (1.3%), 0 unsubscribes, 0 complaints"."""
    if not a.emailed:
        return f"{a.days} days: not reached yet"
    bounce = f"{_n(a.bounced, 'bounce')} of {_n(a.sends, 'send')}" + (
        f" ({_pct(a.bounced, a.sends)})" if a.sends >= MIN_SENDS else "")
    steps = ", ".join(f"step {s} {n}" for s, n in sorted(a.sends_by_step.items()))
    return (f"{a.days} days: {a.replied} of {a.matured} replied ({rate(a.replied, a.matured)}), {a.positive} positive "
            f"({rate(a.positive, a.matured)}), {_n(a.meetings, 'meeting')}, {_n(a.demos, 'demo')} held · {bounce}"
            + (f" ({steps})" if steps else "") + f", {_n(a.unsubscribed, 'unsubscribe')}, "
            f"{_n(a.complained, 'complaint')}")


def _versions_text(c: Cohort) -> str:
    return ", ".join(f"{v} ({n})" for v, n in sorted(c.versions.items(), key=lambda x: (-x[1], x[0])))


def cohort_head(c: Cohort) -> str:
    code = ", ".join(sha[:7] for sha in sorted(c.code) if sha != UNSTAMPED) or UNSTAMPED
    return (f"{c.week} ({week_days(c.week_start)}): {_n(c.enrolled, 'company', 'companies')} enrolled "
            f"({_n(c.contacts, 'contact')}), {c.emailed:,} emailed, {c.delivered:,} delivered · versions "
            f"{_versions_text(c)} · code {code} · {c.apollo_credits:,.0f} Apollo credits")


def lines(ctx: Context, cut: str = "all", age: int | None = None, weeks: int = WEEKS) -> list[str]:
    """`us-outbound cohorts`: each week (the latest first) and cut value at each age, then the insights."""
    found = companies(ctx)
    cohorts = rows(ctx, cut, weeks, found)
    out = [f"Cohorts by enrolment week (UK), by {cut}: each company counts in the week its first contact was enrolled, "
           "at 7, 14, 21 and 28 days after its email 1, once it has reached that age. A rate needs "
           f"{MIN_COMPANIES} companies; a bounce rate {MIN_SENDS} sends."]
    if not cohorts:
        return out + ["No contact has been enrolled yet."]
    ages = [age] if age else list(AGES)
    for week in sorted({c.week for c in cohorts}, reverse=True):
        here = [c for c in cohorts if c.week == week]
        out.append("")
        for c in here:
            head = cohort_head(c)
            out.append(head if cut == "all" else f"{c.value}: {head}")
            out += [f"  {age_line(c.ages[a])}" for a in ages]
    said = insights(ctx, rows(ctx, "all", weeks, found), found)
    out += ["", "Insights (each a lead to test, not a cause):", *(f"  {x}" for x in said or ["Nothing to compare yet."])]
    return out + ["", spend_line(ctx)]


def changes_lines(ctx: Context, a: str | None = None, b: str | None = None) -> list[str]:
    """`us-outbound cohorts changes [A B]`: what changed between two config versions, by default the last two."""
    known = versions(ctx)
    if a is None or b is None:
        if len(known) < 2:
            return [f"{_n(len(known), 'config version')} recorded so far: nothing to compare yet."]
        a, b = str(known[-2]["config_version"]), str(known[-1]["config_version"])
    seen = {str(v["config_version"]): _ts(v.get("first_seen")) for v in known}

    def name(v: str) -> str:
        t = seen.get(v)
        return f"{v} (first seen {t.astimezone(UK):%a %d %b %H:%M} UK)" if t else v

    return [f"From {name(a)} to {name(b)}:", *(f"  {line}" for line in changes(ctx, a, b))]


def readout_lines(ctx: Context, start: datetime, end: datetime) -> tuple[list[str], dict[str, Any]]:
    """The readout's section: the last four cohorts in a line each, the insights, and last week's changes."""
    found = companies(ctx)
    cohorts = rows(ctx, "all", READOUT_WEEKS, found)
    out = ["", "*Cohorts* (companies by enrolment week, at the latest age reached after email 1; "
               "`us-outbound cohorts` has the table)"]
    if not cohorts:
        out.append("  No contact has been enrolled yet.")
    for c in reversed(cohorts):
        at = c.latest_age()
        a = c.ages[at] if at else None
        tail = (f"; at {at} days {a.replied} of {a.matured} replied ({rate(a.replied, a.matured)}), "
                f"{_n(a.meetings, 'meeting')}; {_n(a.bounced, 'bounce')} of {_n(a.sends, 'send')}") if a else \
            "; no company has reached 7 days yet"
        out.append(f"  {c.label} ({week_days(c.week_start)}): {c.enrolled:,} enrolled, {c.emailed:,} emailed{tail}")
    out += [f"  {x}" for x in insights(ctx, cohorts, found, latest_only=True)]  # the CLI has every pair
    changed = week_changes(ctx, start, end)
    out += ["", "*Settings changes* (last week)"]
    if not changed:
        out.append("  None recorded.")
    out += [f"  {x}" for x in changed[:READOUT_CHANGES]]
    if len(changed) > READOUT_CHANGES:
        out.append(f"  and {len(changed) - READOUT_CHANGES} more: `us-outbound cohorts changes`")
    nums = {c.week: {"enrolled": c.enrolled, "emailed": c.emailed, "delivered": c.delivered,
                     "replied": {a: [c.ages[a].replied, c.ages[a].matured] for a in AGES if c.ages[a].emailed}}
            for c in cohorts}
    return out, {"cohorts": nums, "settings_changes": len(changed)}
