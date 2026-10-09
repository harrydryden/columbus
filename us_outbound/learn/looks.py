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
detects a 2x difference); positive and meeting rates are for information, with the same two-proportion test as
`signals review` (signal_review.p_value). Harry writes the result on the Tests tab.

An email an approver edited on its send card (approved_edited; enrol/approvals.py) counts in the arm it was given,
as assigned (Harry, 7 Oct 2026): leaving edits out would bias the comparison whenever approvers edit one arm's emails
more than the other's (a warm intro they dislike, say), while counting them only dilutes it. Each arm's read says how
many of its emails were edited (edited), so a large or lopsided number is seen.

The Monday readout (learn/readout.py) lists each running test: a look reached in the last week, with its read,
or how far it has got and its next look. The outcomes are counted as v_account_outcomes counts them, here in
Python from the events table (as `test read` always did), so a read works on any store.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

from us_outbound.clients.instantly import REPLY_WINDOW_DAYS
from us_outbound.context import UK, Context
from us_outbound.learn import signal_review
from us_outbound.settings.model import AB_TEST, HOLDOUT_ARMS, VARIANT_ARMS, VARIANT_TEST, Test
from us_outbound.timeparse import utc

WINDOW = timedelta(days=REPLY_WINDOW_DAYS)
POSITIVE = frozenset({"positive", "referral"})
NOT_HUMAN = "out_of_office"
EDITED = "approved_edited"  # a send approval's outcome when the approver edited the card (enrol/approvals.py)


class NotYet(Exception):
    """No pre-registered look has been reached: the test may not be read yet."""


@dataclass(frozen=True)
class Outcome:
    """One account in an arm: its first step 1 (delivered) and what followed."""

    account_id: str
    step1_at: datetime
    replies: tuple[tuple[datetime, str], ...] = ()  # (when, class) of each reply at the account
    meetings: tuple[datetime, ...] = ()
    edited: bool = False  # its step-1 contact's emails were edited on the send card before they went

    @property
    def closes_at(self) -> datetime:
        return self.step1_at + WINDOW

    def replied(self) -> bool:
        return any(self.step1_at <= t < self.closes_at and c != NOT_HUMAN for t, c in self.replies)

    def positive(self) -> bool:
        return any(self.step1_at <= t < self.closes_at and c in POSITIVE for t, c in self.replies)

    def met(self, by: datetime) -> bool:
        return any(self.step1_at <= t < by for t in self.meetings)


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


def _midnight(d: date) -> datetime:
    return datetime(d.year, d.month, d.day, tzinfo=UK)


def arm_names(test: Test) -> tuple[str, str]:
    return test.version_a, test.version_b


def _since_start(test: Test, contacts: list[dict]) -> list[dict]:
    start = _midnight(test.start_date) if test.start_date else None
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
    events = ctx.store.select("events", {"account_id": sorted(members),
                                         "type": ["sent", "bounced", "replied", "meeting_booked", "send_approval"]})
    by_account: dict[str, list[dict]] = {}
    for e in events:
        by_account.setdefault(str(e["account_id"]), []).append(e)
    never = datetime.max.replace(tzinfo=UTC)
    for account_id, arm in members.items():
        evs = sorted(by_account.get(account_id, []),
                     key=lambda e: (utc(e.get("occurred_at")) or never, str(e.get("event_id"))))
        step1 = next((e for e in evs if e.get("type") == "sent" and e.get("step") == 1 and utc(e.get("occurred_at"))),
                     None)
        if step1 is None:
            continue
        bounced = any(e.get("type") == "bounced" and e.get("contact_id") == step1.get("contact_id")
                      and e.get("step") in (1, None) for e in evs)
        if bounced:
            continue
        replies = tuple((t, str(e.get("reply_class") or "").strip().lower()) for e in evs
                        if e.get("type") == "replied" and (t := utc(e.get("occurred_at"))))
        meetings = tuple(t for e in evs if e.get("type") == "meeting_booked" and (t := utc(e.get("occurred_at"))))
        edited = any(e.get("type") == "send_approval" and e.get("approval") == EDITED
                     and e.get("contact_id") == step1.get("contact_id") for e in evs)
        out[arm].emailed.append(Outcome(account_id, utc(step1["occurred_at"]), replies, meetings, edited))
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
            out.append(Look(i, day=p, final=final, reached_at=_midnight(p)))
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
             "positive": sum(o.positive() for o in group), "meetings": sum(o.met(look.reached_at) for o in group),
             "edited": sum(o.edited for o in group)}  # counted in their arm, as assigned (module docstring)
        versions[name] = {**s, "reply_rate": _rate(s["replied"], s["delivered"]),
                          "positive_rate": _rate(s["positive"], s["delivered"]),
                          "meeting_rate": _rate(s["meetings"], s["delivered"])}
    a, b = (versions[n] for n in arm_names(test))
    p = signal_review.p_value(a["delivered"], a["replied"], b["delivered"], b["replied"])
    nxt = upcoming(all_looks, ctx.now)
    return {
        "test_id": test.test_id, "kind": test.kind, "version_a": test.version_a, "version_b": test.version_b,
        "look": {"number": look.number, "label": look.label, "final": look.final,
                 "reached_at": look.reached_at.isoformat()},
        "next_look": nxt.label if nxt else None,
        "looks": [{"label": lk.label, "reached": lk in reached(all_looks, ctx.now)} for lk in all_looks],
        "versions": versions, "reply_p_value": round(p, 4) if p is not None else None,
        "decision_rule": test.decision_rule, "status": test.status,
    }


def _pct(k: int, n: int) -> str:
    return f"{k / n:.1%}" if n else "-"


def summary_line(result: Mapping[str, Any]) -> str:
    """One line of a read: each arm's reply rate, and the test's p."""
    arms_text = "; ".join(f"{name} {v['replied']} of {v['delivered']} replied ({_pct(v['replied'], v['delivered'])})"
                          for name, v in result["versions"].items())
    p = result.get("reply_p_value")
    return arms_text + (f" · p = {p:.2f}" if p is not None else "")


def edited_line(result: Mapping[str, Any]) -> str:
    """How many emails each arm had edited on the send card, or "" when none was: they count in their arm."""
    versions = result["versions"].items()
    if not any(v.get("edited") for _, v in versions):
        return ""
    each = ", ".join(f"{name} {v.get('edited', 0)} of {v['delivered']}" for name, v in versions)
    return f"Edited by an approver before sending, and counted in the arm they were given: {each}."


def readout_lines(ctx: Context, since: datetime) -> tuple[list[str], list[str]]:
    """The Monday readout's Tests section, and the ids of the tests at a look: each running test, at a look reached
    since `since` (with its read), or how far it has got and its next look."""
    out: list[str] = []
    at_look: list[str] = []
    for test in ctx.settings.tests:
        if test.status != "running":
            continue
        by_arm = arms(ctx, test)
        all_looks = looks(test, by_arm)
        new = [lk for lk in reached(all_looks, ctx.now) if lk.reached_at >= since]
        if new:
            at_look.append(test.test_id)
            result = read(ctx, test.test_id)
            at = datetime.fromisoformat(result["look"]["reached_at"]).astimezone(UK)
            out.append(f"  {test.test_id} ({test.kind}) reached {result['look']['label']} on {at:%a %d %b}: "
                       f"{summary_line(result)}. `us-outbound test read {test.test_id}` has the full read; the "
                       "decision rule is on the Tests tab.")
        else:
            out.append(f"  {test.test_id} ({test.kind}): no look reached this week. {progress(test, by_arm, ctx.now)}.")
    return (out or ["  No test is running."]), at_look
