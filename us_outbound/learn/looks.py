"""Tests read only at pre-registered looks, so nobody peeks (SPEC 12 "Copy tests"; Harry, 6 Oct 2026).

SPEC 12: "Each test is read once, on its pre-registered date, on reply rate: human replies within 21 days of step
1 ÷ accounts with step 1 delivered" (28 days now: instantly.REPLY_WINDOW_DAYS). The Tests tab says, before a test
starts, what it compares and when it may be read:
  * kind: ab (the default) is a copy A/B: version_a and version_b are Copy-tab copy_versions, enrol gives each
    account one by hash (SPEC 9) and records it (contacts.test_id, copy_version). holdout reads a split enrol
    records on every contact anyway: the opener holdout (contacts.opener_arm, opener or holdout) or email 1's
    subject (contacts.subject_arm, personal or copy), for the accounts enrolled from start_date on. A holdout
    assigns nothing, so it may run beside the copy test (settings/validate.py). variant (Harry, 7 Oct 2026;
    enrol/variants.py) changes one part of one email for every account: enrol records each account's arm
    (contacts.test_id and test_arm, a for version_a and b for version_b), read for the accounts enrolled from
    start_date on. ab and variant are the copy tests: one runs at a time.
  * looks: interim looks, each a whole number N (both arms have N accounts with step 1 delivered whose 28-day
    reply window has closed) or a date. read_date is always the last look.
A count look is reached when the slower arm's Nth account's window closes; a date look on its date (UK). A test whose
arms are uneven (share_a; Harry, 8 Oct 2026) counts N in its smaller arm and N scaled in the larger (Test.scaled: at
70/30, a look at 200 is 200 accounts in the smaller arm and 467 in the larger), so each arm is read in its share.

`us-outbound test read ID` reads the test at its latest look reached, and only over what that look covers: for
a count look, each arm's first N accounts (by step 1); for a date look, the accounts whose window had closed by
that date. Replies count within each account's window and meetings until the look, so a read made a week late
says what it would have said on time. Before the first look it refuses (exit 2) and says only how far each arm
has got (accounts emailed, windows closed): never a reply, so nobody peeks. Reply rate decides (SPEC 12: the test
detects a 2x difference); positive and meeting rates are for information. Harry writes the result on the Tests tab.

The test (W2-3, 20 Oct 2026; redteam-stats §3 L10): Fisher's exact two-sided p (learn/stats.fisher_exact), against
this look's threshold, the O'Brien-Fleming nominal p for the test's looks at a total two-sided alpha of ALPHA
(stats.obrien_fleming: with one look, ALPHA itself; at the half-way look of two, 0.011, and 0.096 at the end), and
P(version_b > version_a) from Jeffreys Beta posteriors (stats.beta_posterior_gt) beside it. The pooled z-test it used
before (signal_review's, gone) was anti-conservative at these rates: 3 of 30 against 0 of 30 read p = 0.076
where Fisher's is 0.237. W3-4 wires the Tests tab's own alpha and test fields; until then every test is read this way.

Each outcome is learn/outcomes.py's (the one definition, W2-3): an account's first step 1, delivered, a human reply
within 28 days (the latest correction of a reply's class counting), meetings until the look.

Every email counts in the arm it was given, as assigned (Harry, 7 Oct 2026). (An email an approver edited on its send
card was counted that way too, and each read said how many; the send cards went with W2-1, 20 Oct 2026, after none was
edited.)

The Monday readout (learn/readout.py) lists each running test: a look reached in the last week, with its read,
or how far it has got and its next look. The outcomes are counted as v_account_outcomes counts them, here in
Python from the events table (as `test read` always did), so a read works on any store.

Starting a test (start, from learn/copy_tests.py until W2-3; 9 Oct 2026, the refactoring scan's phase 6): a refusal is
a ValueError, which `us-outbound test` reports in its own words (exit 2); reading before the first look is NotYet.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

from us_outbound import fmt
from us_outbound.base import say
from us_outbound.clients.instantly import REPLY_WINDOW_DAYS
from us_outbound.context import UK, Context
from us_outbound.learn import outcomes, stats
from us_outbound.settings.model import AB_TEST, HOLDOUT_ARMS, HOLDOUT_TEST, VARIANT_ARMS, VARIANT_TEST, Test
from us_outbound.timeparse import uk_midnight, utc

WINDOW = timedelta(days=REPLY_WINDOW_DAYS)
ALPHA = 0.10  # two-sided (one-sided 0.05): the plan's L10 default until W3-4 reads the Tests tab's alpha


class NotYet(Exception):
    """No pre-registered look has been reached: the test may not be read yet."""


@dataclass(frozen=True)
class Outcome:
    """One account in an arm: its first step 1 (delivered) and what followed (learn/outcomes.Outcome's)."""

    account_id: str
    step1_at: datetime
    did_reply: bool = False  # a human reply within the 28 days
    did_positive: bool = False  # a positive or referral one
    first_meeting_at: datetime | None = None

    @property
    def closes_at(self) -> datetime:
        return self.step1_at + WINDOW

    def replied(self) -> bool:
        return self.did_reply

    def positive(self) -> bool:
        return self.did_positive

    def met(self, by: datetime) -> bool:
        return self.first_meeting_at is not None and self.first_meeting_at < by


@dataclass
class Arm:
    name: str
    accounts: int = 0  # accounts in the arm (enrolled)
    emailed: list[Outcome] = field(default_factory=list)  # step 1 delivered, oldest first

    def closed(self, now: datetime) -> list[Outcome]:
        return [o for o in self.emailed if o.closes_at <= now]


@dataclass(frozen=True)
class Look:
    number: int  # 1-based, in the sheet's order, read_date last
    count: int | None = None
    day: date | None = None
    final: bool = False
    reached_at: datetime | None = None
    per_arm: tuple[tuple[str, int], ...] = ()  # (arm, its count) for a count look: count, scaled to the arm's share

    def needs(self, arm: str) -> int:
        """How many accounts a count look reads in this arm (its count unless the arms are uneven)."""
        return dict(self.per_arm).get(arm, self.count or 0)

    @property
    def label(self) -> str:
        counts = dict(self.per_arm)
        if self.count and len(set(counts.values())) > 1:
            what = " and ".join(f"{arm} {n}" for arm, n in counts.items()) + " accounts with closed reply windows"
        elif self.count:
            what = f"{self.count} accounts per arm with closed reply windows"
        else:
            what = f"{self.day:%a %d %b %Y}"
        return f"look {self.number}{' (the read date)' if self.final else ''}: {what}"


def arm_names(test: Test) -> tuple[str, str]:
    return test.version_a, test.version_b


def _since_start(test: Test, contacts: list[dict]) -> list[dict]:
    start = uk_midnight(test.start_date) if test.start_date else None
    return [c for c in contacts if (t := utc(c.get("enrolled_at"))) is not None and (start is None or t >= start)]


def _members(ctx: Context, test: Test) -> dict[str, str]:
    """account_id -> arm, from the account's first contact in the test."""
    a, b = arm_names(test)
    if test.kind == AB_TEST:
        contacts = ctx.store.select("contacts", {"test_id": test.test_id})

        def arm_of(c: dict) -> str:
            return str(c.get("copy_version") or "")
    elif test.kind == VARIANT_TEST:  # the arm enrol gave it: a is version_a, b version_b (enrol/variants.py)
        contacts = _since_start(test, ctx.store.select("contacts", {"test_id": test.test_id}))
        names = dict(zip(VARIANT_ARMS, (a, b)))

        def arm_of(c: dict) -> str:
            return names.get(str(c.get("test_arm") or ""), "")
    else:
        column = HOLDOUT_ARMS.get(a.casefold(), "")
        contacts = _since_start(test, ctx.store.select("contacts", {column: [a, b]}) if column else [])

        def arm_of(c: dict) -> str:
            return str(c.get(column) or "")
    never = datetime.max.replace(tzinfo=UTC)
    contacts.sort(key=lambda c: (utc(c.get("enrolled_at")) or never, str(c.get("contact_id"))))
    out: dict[str, str] = {}
    for c in contacts:
        arm = arm_of(c)
        if c.get("account_id") and arm in (a, b):
            out.setdefault(str(c["account_id"]), arm)
    return out


def arms(ctx: Context, test: Test) -> dict[str, Arm]:
    """Each arm's accounts and, for those whose step 1 was delivered, what followed (v_account_outcomes' rules)."""
    members = _members(ctx, test)
    out = {name: Arm(name) for name in arm_names(test)}
    for arm in members.values():
        out[arm].accounts += 1
    if not members:
        return out
    for o in outcomes.per_company(ctx):
        arm = members.get(o.account_id)
        if arm is None or not o.delivered:
            continue
        out[arm].emailed.append(Outcome(o.account_id, o.step1_at, o.replied_in_window, o.positive_in_window,
                                        o.first_meeting_at))
    for a in out.values():
        a.emailed.sort(key=lambda o: (o.step1_at, o.account_id))
    return out


def looks(test: Test, by_arm: Mapping[str, Arm]) -> list[Look]:
    """The test's looks in the sheet's order, read_date last, each with when it was reached (or None)."""
    points: list[int | date] = list(test.looks) + ([test.read_date] if test.read_date else [])
    out = []
    for i, p in enumerate(points, start=1):
        final = i == len(points) and test.read_date is not None
        if isinstance(p, date):
            out.append(Look(i, day=p, final=final, reached_at=uk_midnight(p)))
            continue
        per_arm = tuple((name, test.scaled(p, arm)) for name, arm in zip(arm_names(test), VARIANT_ARMS))
        need = dict(per_arm)
        lists = [(a.emailed, need.get(name, p)) for name, a in by_arm.items()]
        reached = (max(lst[n - 1].closes_at for lst, n in lists)
                   if lists and all(len(lst) >= n for lst, n in lists) else None)
        out.append(Look(i, count=p, final=final, reached_at=reached, per_arm=per_arm))
    return out


def reached(all_looks: Iterable[Look], now: datetime) -> list[Look]:
    return [lk for lk in all_looks if lk.reached_at is not None and lk.reached_at <= now]


def latest(all_looks: Iterable[Look], now: datetime) -> Look | None:
    done = reached(all_looks, now)
    return max(done, key=lambda lk: (lk.reached_at, lk.number)) if done else None


def upcoming(all_looks: Iterable[Look], now: datetime) -> Look | None:
    return next((lk for lk in all_looks if lk.reached_at is None or lk.reached_at > now), None)


def covered(arm: Arm, look: Look) -> list[Outcome]:
    """What a look reads in one arm: the first N accounts for a count look (N scaled to the arm's share when the arms
    are uneven), those closed by the date for a date look."""
    if look.count:
        return arm.emailed[: look.needs(arm.name)]
    return [o for o in arm.emailed if o.closes_at <= look.reached_at]


def _rate(n: int, d: int) -> float | None:
    return round(n / d, 4) if d else None


def progress(test: Test, by_arm: Mapping[str, Arm], now: datetime) -> str:
    """How far each arm has got, with no reply in it: "eap-v1 40 emailed (12 windows closed), ..."."""
    def closed(n: int) -> str:
        return f"{n} window{'' if n == 1 else 's'} closed"

    parts = [f"{a.name} {len(a.emailed)} emailed ({closed(len(a.closed(now)))})" for a in by_arm.values()]
    nxt = upcoming(looks(test, by_arm), now)
    when = f"; next {nxt.label}" if nxt else ""
    return ", ".join(parts) + when


def read(ctx: Context, test_id: str) -> dict:
    """The test at its latest look reached (see the module docstring); NotYet before the first look."""
    test = next((t for t in ctx.settings.tests if t.test_id == test_id), None)
    if test is None:
        raise LookupError(f"no test {test_id!r} on the Tests tab in the synced settings")
    by_arm = arms(ctx, test)
    all_looks = looks(test, by_arm)
    look = latest(all_looks, ctx.now)
    if look is None:
        if not all_looks:
            why = "it has no read_date and no looks on the Tests tab"
        else:
            nxt = upcoming(all_looks, ctx.now)
            why = f"the first is {nxt.label}" if nxt else "none is reached"
        raise NotYet(f"test {test_id} has reached no pre-registered look yet ({why}). So far: "
                     f"{progress(test, by_arm, ctx.now)}. Replies are not shown before a look, so nobody peeks.")
    versions: dict[str, dict] = {}
    for name, arm in by_arm.items():
        group = covered(arm, look)
        s = {"accounts": arm.accounts, "delivered": len(group), "replied": sum(o.replied() for o in group),
             "positive": sum(o.positive() for o in group), "meetings": sum(o.met(look.reached_at) for o in group)}
        versions[name] = {**s, "reply_rate": _rate(s["replied"], s["delivered"]),
                          "positive_rate": _rate(s["positive"], s["delivered"]),
                          "meeting_rate": _rate(s["meetings"], s["delivered"])}
    a, b = (versions[n] for n in arm_names(test))
    p = stats.fisher_exact(a["replied"], a["delivered"], b["replied"], b["delivered"]) if a["delivered"] and \
        b["delivered"] else None
    threshold = stats.obrien_fleming(len(all_looks), ALPHA)[look.number - 1]
    b_better = stats.beta_posterior_gt(b["replied"], b["delivered"], a["replied"], a["delivered"])
    nxt = upcoming(all_looks, ctx.now)
    return {
        "test_id": test.test_id, "kind": test.kind, "version_a": test.version_a, "version_b": test.version_b,
        "look": {"number": look.number, "label": look.label, "final": look.final,
                 "reached_at": look.reached_at.isoformat()},
        "next_look": nxt.label if nxt else None,
        "looks": [{"label": lk.label, "reached": lk in reached(all_looks, ctx.now)} for lk in all_looks],
        "versions": versions, "reply_p": round(p, 4) if p is not None else None,
        "reply_test": "fisher_exact", "look_threshold": round(threshold, 4), "alpha": ALPHA,
        "below_threshold": p is not None and p < threshold,
        "p_b_better": round(b_better, 3),
        "decision_rule": test.decision_rule, "status": test.status,
    }


def summary_line(result: Mapping[str, Any]) -> str:
    """One line of a read: each arm's reply rate, Fisher's p against this look's threshold, and P(b > a)."""
    arms_text = "; ".join(f"{name} {v['replied']} of {v['delivered']} replied ({fmt.pct(v['replied'], v['delivered'])})"
                          for name, v in result["versions"].items())
    p = result.get("reply_p")
    out = arms_text
    if p is not None:
        out += f" · Fisher p = {p:.3f}"
        if result.get("look_threshold") is not None:
            out += f" (this look's threshold {result['look_threshold']:.3f})"
    if result.get("p_b_better") is not None:
        out += f" · P({result.get('version_b') or 'b'} better) = {result['p_b_better']:.2f}"
    return out


KIND_WORDS = {AB_TEST: "A/B copy test", VARIANT_TEST: "one-change variant test", HOLDOUT_TEST: "holdout test"}


def readout_lines(ctx: Context, since: datetime) -> tuple[list[str], list[str]]:
    """The Monday readout's Tests section, and the ids of the tests at a look: each running test, at a look reached
    since `since` (its read, and what to do: apply its decision rule and write the result on the Tests tab), or how
    far it has got and its next look."""
    out: list[str] = []
    at_look: list[str] = []
    tab = ""
    for test in ctx.settings.tests:
        if test.status != "running":
            continue
        by_arm = arms(ctx, test)
        all_looks = looks(test, by_arm)
        new = [lk for lk in reached(all_looks, ctx.now) if lk.reached_at >= since]
        kind = KIND_WORDS.get(test.kind, test.kind)
        if not new:
            out.append(f"  {test.test_id} ({kind}): no look reached this week. {progress(test, by_arm, ctx.now)}.")
            continue
        at_look.append(test.test_id)
        result = read(ctx, test.test_id)
        look = next(lk for lk in all_looks if lk.number == result["look"]["number"])
        on = "" if look.day else f" on {look.reached_at.astimezone(UK):%a %d %b}"  # a date look's label has its date
        tab = tab or say.sheet_link(ctx, "Tests")
        rule = f' ("{test.decision_rule}")' if test.decision_rule else ""
        then = f"; if it does not decide yet, the test runs on to {result['next_look']}" if result["next_look"] else ""
        read_cmd = say.command(f"test read {test.test_id}")
        out.append(f"  {test.test_id} ({kind}) reached {look.label}{on}: {summary_line(result)}")
        out.append(f"    *What to do:* apply its decision rule{rule} and write the outcome in the `result` column on "
                   f"the {tab}{then}. {read_cmd} has the full read.")
    return (out or ["  No test is running."]), at_look


# -- starting a test (learn/copy_tests.py until W2-3) ------------------------------------------------------------------


def start(ctx: Context, test_id: str) -> dict:
    """Pre-registration first (SPEC 12; Harry, 6 Oct 2026: kind and looks, learn/looks.py). One copy test (kind ab,
    or variant: Harry, 7 Oct 2026) runs at a time, since it decides each account's copy (SPEC 9); a holdout assigns
    nothing, so it may run beside it, and its versions are the arms enrol records, not Copy rows. A variant's row is
    checked as settings_sync checks it (its texts against the copy rules; enrol/variants.py), and refused when it
    breaks one; the result says which sendable Copy rows the change can be made in (variants.coverage), and a
    change that fits none of them is refused."""
    from us_outbound.settings.model import AB_TEST, COPY_TEST_KINDS, TEST_KINDS, VARIANT_TEST
    from us_outbound.settings.validate import parse_looks, parse_share, validate_tab

    sheet_id = ctx.guard.bounds.settings_sheet_id
    if not sheet_id:
        raise ValueError("no settings sheet id: set US_OUTBOUND_SETTINGS_SHEET_ID")
    rows = ctx.clients.sheets.read_tabs(sheet_id, ["Tests"])["Tests"]
    row = next((r for r in rows if r.get("test_id", "").strip() == test_id), None)
    if row is None:
        raise ValueError(f"no test {test_id!r} on the Tests tab")

    def kind(r: dict) -> str:
        return (r.get("kind") or "").strip().lower() or AB_TEST

    if kind(row) not in TEST_KINDS:
        raise ValueError(f"kind {row.get('kind')!r} on the Tests tab is not one of {', '.join(TEST_KINDS)}")
    others = [r["test_id"] for r in rows if r.get("status", "").strip().lower() == "running"
              and r.get("test_id", "").strip() != test_id and kind(r) in COPY_TEST_KINDS]
    if others and kind(row) in COPY_TEST_KINDS:
        raise ValueError(f"only one copy test (ab or variant) runs at a time (SPEC 9); {', '.join(others)} is running")
    copy_rows: dict[str, Any] = {}
    if kind(row) == VARIANT_TEST:
        from us_outbound.enrol import enrol, variants

        checked, errors = validate_tab("Tests", [row])
        if errors:
            raise ValueError("the Tests tab's row does not pass the checks settings_sync makes: "
                          + "; ".join(f"{e.column}: {e.message}" for e in errors[:6]))
        fits, not_ = variants.coverage(checked[0], enrol.sendable_copy(ctx.settings).values(), ctx.settings)
        if not_ and not fits:
            raise ValueError("the change cannot be made in any sendable Copy row, so the test would have no accounts: "
                          + "; ".join(f"{v}: {why}" for v, why in list(not_.items())[:4]))
        copy_rows = {"change_fits": len(fits), "sendable": len(fits) + len(not_),
                     "not_in_the_test": dict(list(not_.items())[:10])}
    if row.get("status", "").strip().lower() == "running":
        return {"test_id": test_id, "status": "running", "changed": False}
    read_date = row.get("read_date", "").strip()
    if not read_date or not row.get("decision_rule", "").strip():
        raise ValueError("pre-register the read_date and decision_rule on the Tests tab first (SPEC 12)")
    try:
        parse_looks(row.get("looks", "") or "")
    except ValueError as exc:
        raise ValueError(f"looks on the Tests tab: {exc}") from exc
    start = row.get("start_date", "").strip() or ctx.today_uk().isoformat()
    # The row as it will be written, checked as settings_sync will read it, whatever its kind (9 Oct 2026: a read_date
    # of 2026-9-30 passed a comparison of text, the sheet said running, and the next sync refused the tab).
    _, errors = validate_tab("Tests", [{**row, "status": "running", "start_date": start}])
    if errors:
        raise ValueError("the Tests tab's row would not pass the checks settings_sync makes once it is running: "
                      + "; ".join(f"{e.column}: {e.message}" for e in errors[:6]))
    split: dict[str, str] = {}
    if (row.get("share_a") or "").strip():  # version_a's share of the accounts (Harry, 8 Oct 2026)
        try:
            share = parse_share(row["share_a"])
        except ValueError as exc:
            raise ValueError(f"share_a on the Tests tab: {exc}") from exc
        split = {"split": f"{row.get('version_a', '').strip()} {share:.0%}, {row.get('version_b', '').strip()} "
                          f"{1 - share:.0%}"}
    if kind(row) == AB_TEST:
        missing = [
            v for v in (row.get("version_a", "").strip(), row.get("version_b", "").strip())
            if (c := ctx.settings.copy_row(v)) is None or c.status != "approved" or not c.qa_current
        ]
        if missing:
            raise ValueError("copy is not approved, with a current QA pass, in the synced settings for: "
                          + ", ".join(missing))
    sheets = ctx.clients.sheets
    writes = [("start_date", start)] if not row.get("start_date", "").strip() else []
    for column, value in [*writes, ("status", "running")]:
        if not sheets.update_cell(sheet_id, "Tests", {"test_id": test_id}, column, value):
            raise ValueError(f"no row for test {test_id!r} on the Tests tab to update")
    return {"dry_run": ctx.dry_run, "test_id": test_id, "kind": kind(row), "status": "running", "start_date": start,
            "looks": row.get("looks", "").strip(), "read_date": read_date, "changed": ctx.live, **split,
            **({"copy_rows": copy_rows} if copy_rows else {})}
