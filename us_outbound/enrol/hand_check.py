"""The weekly hand-check (SPEC 11 "Weekly hand-check"; SPEC 14 phase 1 acceptance; Harry's D5, 19 Oct 2026): Harry
judges a random sample of the companies about to be emailed, and the judgements measure how often they are the right
ones. It is the only working measure of ICP precision the system has (code-approvals-handcheck-copy §1).

hand_check_draw, Mondays 05:45 UK (after pick_contacts at 05:30, before enrol at 12:00), and on demand
(`us-outbound run hand_check_draw`):
  1. the pool: the accounts enrol could take that already have a sendable contact (contacts/pick.ready_accounts), in
     queue order (queue.order_key), from position today's number + 1 to today's number + weekly_enrol_cap. Those are
     the accounts the coming send days would take, not today's, so holding them never touches Monday's own enrolment
     (RS §2 W1-5). Before, the sample was dealt across every active industry group, so 18 of W43's 48 rows came from
     groups rarely emailed (code-approvals-handcheck-copy B5). While the hand-check holds the whole day (gates()) and
     fewer than hand_check_sample accounts are ready beyond today's (supply binds: 30 to 60 ready against a number of
     about 30), the pool is every ready account from position 1: the day waits for the check anyway, so a sample from
     today's accounts holds nothing the gate does not, and the week is checked as it was before W1-5;
  2. the sample: General hand_check_sample of them (10 by default, Harry's 19 Oct number; his D5 asks for 20, set on
     the sheet), drawn with random.Random(week).sample, so a rerun the same week draws the same;
  3. records the week's item in hitl_items (kind hand_check, item_id "hand_check-{iso_week}", status open): when it was
     drawn, today's number and the positions drawn from, the gate version (labels_hash and the ICP rules' hash), each
     sampled company's facts (facts(): clean name, HQ state, size band, the contact enrol would choose with role and
     title, the opener as enrol would send it, with its evidence), and the companies verify_accounts holds for doubtful
     Apollo facts (verify.open_doubts), which wait for a decision as before;
  4. writes one row per company to the Hand-check tab (hand_check_tab.write: appended under every earlier week) and
     posts the Monday notice to the alert channel, linking there. A dry run records the item and posts to the dev
     channel; a live run posts an item a dry run recorded unposted. A week with nothing to check (no company in the
     pool and none held for doubtful facts) is recorded live as handled, with payload nothing_to_check, so the gate
     has nothing to wait for and the readout says 0 picked: with no item at all enrol waited the whole week for a
     check that could never be drawn, and `handcheck approve` refused it (the W1-5 review, 19 Oct 2026). A dry run
     records no such item, as it must never open the gate. A run later in the week (`us-outbound run
     hand_check_draw`) draws again over a week recorded with nothing to check, as the queue may have grown since.
  5. catches up (catch_up, from poll_approvals every 5 minutes, live): a week with no item DRAW_GRACE after its Monday
     draw (the job failed, or never started) is drawn then, and, while the hand-check holds the whole day (gates()), a
     week recorded with nothing to check is drawn again once an account is ready or a company waits for doubtful
     facts. Before W1-5 the tab's poll posted a missing week itself, so a failed Monday recovered within minutes; the
     draw alone left the gated week waiting ("has not been posted") until someone ran it by hand, and a Monday whose
     05:45 draw ran before pick_contacts had made anyone ready opened the whole gated week with no check (the W1-5
     review, round 2). With the gate off, a week with nothing beyond today's number is not drawn again by itself: it
     holds nobody either way, and that week goes unmeasured unless someone runs the draw (a known gap).

Harry decides each row on the tab; poll_approvals applies each decision within 5 minutes (hand_check_tab.apply) and
writes one hand_check judgement fact per row (judged, tally). What waits for him is General hand_check_gate:
  * no (D5: a measure, not a gate): enrol leaves out only the sampled companies with no applied decision, for
    HOLD_DAYS (7) from the draw (held); then they are released and the Monday readout lists them as unjudged, sent.
    Everyone else is emailed as usual. On 15 Oct the whole-day gate cost about 21 leads behind an error that fired
    208 times (code-approvals-handcheck-copy B6);
  * yes (the default until the sheet says no: SPEC 11 as built): enrol waits until every hand_check item of the
    ISO week is handled (enrol.hand_check), as before. (Until W2-1 that was with General auto_send = yes, as the live
    sheet had it; with auto_send = no each email waited for its own send approval instead.)
Either way a week closes once every sampled row has an applied decision (or its company is no longer on file), or 7
days after the draw (close); a week still open at Wednesday 09:00 UK posts one reminder (remind).

Without Slack or the tab (operator commands, live with --live alone):
  us-outbound handcheck show                 prints this week's companies; it never writes
  us-outbound handcheck approve [--pull ID]  marks the week's item handled, with the pulled accounts (ids or domains)
                                             in payload.pulled_account_ids, which enrol leaves out (and in
                                             operator_pulled, which no later decision on the tab takes off). With
                                             no item yet it records the week's draw first, or the week as nothing to
                                             check.
"""

from __future__ import annotations

import math
import random
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from datetime import date, datetime, time, timedelta
from typing import Any

from us_outbound import labels, verify
from us_outbound.base import notify, say
from us_outbound.clients.guard import GuardViolation
from us_outbound.context import UK, Context
from us_outbound.enrol import enrol, openers, render
from us_outbound.logs import log
from us_outbound.scoring.score import score_account
from us_outbound.timeparse import utc

JOB = "hand_check_draw"
KIND = "hand_check"
HOLD_DAYS = 7  # D5: a sampled company waits for its judgement this long from the draw, then it is released
REMIND_WEEKDAY, REMIND_AT = 2, time(9)  # Wednesday 09:00 UK: a week still open then posts one reminder
DRAW_AT = time(5, 45)  # Monday 05:45 UK, the hand_check_draw job's time (ops/schedule.py; a test keeps them in step)
# How long after DRAW_AT poll_approvals waits before drawing a week with no item itself (catch_up): past the job's own
# 10-minute timeout, so the two never draw side by side (two notices, the rows written twice).
DRAW_GRACE = timedelta(minutes=30)
# The ICP rules' part of the gate version: "legacy" until W2-2's one ICP gate has a hash of its own (the plan, §8).
ICP_RULES_HASH = "legacy"
EVIDENCE_LIMIT = 3
QUOTE_LIMIT = 160

# The judgement facts (signal_events: source hand_check, fact judgement), one per decided row and week: W0's export
# brought them forward on 19 Oct 2026; hand_check_tab.apply writes them as each row is applied.
SOURCE, FACT = "hand_check", "judgement"
FINE, LEAVE_OUT, WRONG_INDUSTRY = DECISIONS = ("fine", "leave out", "wrong industry")
SAMPLE, DOUBT = "random sample", "doubtful facts"


def item_id(week: str) -> str:
    return f"{KIND}-{week}"


def event_id(week: str, account_id: str) -> str:
    """The judgement fact's event id: one per account and week, so a row applied again replaces its fact."""
    return f"{SOURCE}:{week}:{account_id}"


def this_week(ctx: Context) -> str:
    """The ISO week enrol checks: of today's US Eastern date."""
    return enrol.iso_week(ctx.now_et().date())


def week_monday(week: str) -> date | None:
    """"2026-W44" -> Monday 26 Oct 2026; None for anything else."""
    year, _, number = str(week or "").partition("-W")
    try:
        return date.fromisocalendar(int(year), int(number), 1)
    except ValueError:
        return None


def gate_version(ctx: Context) -> str:
    """Which gate let the sampled companies through: the labels' hash and the ICP rules' (so precision can be read
    per version of the gate, T1)."""
    return f"{labels.labels_hash(ctx.settings)}:{ICP_RULES_HASH}"


def _clip(text: Any, n: int = QUOTE_LIMIT) -> str:
    t = " ".join(str(text or "").split())
    return t if len(t) <= n else t[: n - 1] + "…"


# -- the sample ---------------------------------------------------------------------------------


def today_number(ctx: Context, errors: list[str] | None = None) -> int:
    """Today's number as enrol works it out (enrol/today.py, limits.today), the owners' campaigns not read from
    Instantly: a paused campaign still counts its owner's slots here, so the number is never below enrol's and the
    sample never reaches into the accounts enrol takes today. When it cannot be worked out (a read failed), the most
    the weekly target gives a send day at its pace (weekly_enrol_cap over the send days, 30 at 150 a week), so the
    draw still runs and still starts after any account Monday could take. errors: the failure is added to it, so the
    draw's summary carries it (number_error) and heartbeat_check tells someone; the log line alone reached nobody."""
    from us_outbound.enrol import today

    try:
        return max(0, int(today.read(ctx, campaigns=False).limits.number))
    except GuardViolation:
        raise
    except Exception as exc:
        g = ctx.settings.general
        pace = math.ceil(g.weekly_enrol_cap / max(1, len(g.send_window.days)))
        error = f"{type(exc).__name__}: {str(exc)[:160]}"
        log("hand_check_number_failed", error=error, used=pace)
        if errors is not None:
            errors.append(f"today's number could not be worked out ({error}); the draw used {pace}, the weekly pace")
        return pace


def window(ctx: Context, number: int) -> tuple[int, list[dict]]:
    """(the first queue position drawn from, the accounts there): the ready accounts in queue order
    (pick.ready_accounts) from position number + 1 to number + weekly_enrol_cap, the ones the coming send days would
    take. While the hand-check holds the whole day (gates()) and fewer than hand_check_sample are there, every ready
    account from position 1 to number + weekly_enrol_cap instead: today's accounts wait for the check anyway, and a
    pool of none would leave the week with no check for the gate to wait on (the W1-5 review, 19 Oct 2026)."""
    from us_outbound.contacts import pick

    g = ctx.settings.general
    n = max(0, number)
    ready = pick.ready_accounts(ctx)
    ahead = ready[n : n + g.weekly_enrol_cap]
    if gates(ctx) and len(ahead) < g.hand_check_sample:
        return 1, ready[: n + g.weekly_enrol_cap]
    return n + 1, ahead


def pool(ctx: Context, number: int | None = None) -> list[dict]:
    """The accounts the sample is drawn from (window). number: today's number (today_number when None)."""
    return window(ctx, today_number(ctx) if number is None else number)[1]


def sample_of(candidates: Sequence[dict], week: str, size: int) -> list[dict]:
    """size of the candidates at random (all of them when fewer), seeded with the week: the same draw all week."""
    return random.Random(week).sample(list(candidates), min(max(0, size), len(candidates)))


def draw(ctx: Context, week: str, number: int | None = None) -> list[dict]:
    """The week's random sample, hand_check_sample accounts from the pool."""
    return sample_of(pool(ctx, number), week, ctx.settings.general.hand_check_sample)


def _person(ctx: Context, account_id: str, domains: set[str], hashes: set[str]) -> tuple[dict, str]:
    """(the contact enrol would choose, or the first on file, or {}; why it cannot be emailed yet, or "")."""
    contacts = ctx.store.select("contacts", {"account_id": account_id})
    chosen, why = enrol.pick_contact(contacts, domains, hashes)
    if chosen is not None:
        return chosen, ""
    if contacts:
        first = sorted(contacts, key=lambda c: (str(c.get("created_at") or ""), str(c.get("contact_id"))))[0]
        return dict(first), f"not sendable yet: {why}"
    return {}, "no contact yet (one is chosen before the first email)"


def opener_for(ctx: Context, account: Mapping[str, Any], contact: Mapping[str, Any],
               events: Sequence[Mapping[str, Any]]) -> openers.Opener:
    """The opener enrol would give this contact (enrol/openers.py), with no model call: a "what they do"
    phrase only if one is stored already."""
    exempt = (str(account.get("clean_name") or ""), str(contact.get("first_name") or ""))

    def check(text: str) -> str:
        return render.pick_opener(text, sender_is_harry=False, demo_host=ctx.settings.general.demo_host,
                                  exempt=exempt)[1]

    return openers.for_account(ctx, account, contact, events, check=check)


def facts(ctx: Context, account: Mapping[str, Any], domains: set[str], hashes: set[str]) -> dict[str, Any]:
    """What Harry checks for one account (SPEC 14 phase 1 acceptance), the opener as enrol would send it."""
    events = ctx.store.select("signal_events", {"account_id": account["account_id"]})
    r = score_account(account, events, ctx.settings, ctx.today_uk())
    person, note = _person(ctx, str(account["account_id"]), domains, hashes)
    op = opener_for(ctx, account, person, events)
    matches = sorted((m for m in r.matches if m.signal.action == "Score"), key=lambda m: -m.weight_applied)
    evidence = []
    for m in matches[:EVIDENCE_LIMIT]:
        ev = m.evidence[0] if m.evidence else None
        evidence.append({"signal": m.signal.signal, "weight": m.weight_applied, "text": _clip(ev.text if ev else ""),
                         "quote": _clip(ev.quote if ev else ""), "url": (ev.url if ev else "") or ""})
    return {
        "account_id": account["account_id"], "domain": account.get("domain") or "", "clean_name": account.get("clean_name") or "",
        "hq_city": account.get("hq_city") or "", "hq_state": account.get("hq_state") or "",
        "size_band": account.get("size_band") or "", "employees": account.get("employees"),
        "industry": account.get("industry") or "", "industry_group": ctx.settings.industry_group_of(account),
        "tier": account.get("tier") or "", "score": account.get("score"), "status": account.get("status") or "",
        "angle": account.get("angle") or "",
        "contact": {"role": person.get("role") or "", "title": person.get("title") or "", "note": note},
        "opener": _clip(op.text or op.would_be, 300), "opener_arm": op.arm, "opener_source": op.source,
        "evidence": evidence,
        # The label check (labels.py; Harry, 7 Oct 2026): where the label came from and what the model read.
        "label_source": account.get("label_source") or "", "label_confidence": account.get("label_confidence") or "",
        "what_they_do": str((labels.latest_verdict(events) or {}).get("what_they_do") or ""),
    }


def build(ctx: Context, week: str, number: int | None = None) -> dict[str, Any]:
    """The week's payload: the draw with each account's facts, where it was drawn from, and the accounts held for
    doubtful facts."""
    domains, hashes = enrol.suppressed(ctx)
    errors: list[str] = []
    n = today_number(ctx, errors=errors) if number is None else max(0, number)
    start, candidates = window(ctx, n)
    cap = ctx.settings.general.weekly_enrol_cap
    sample = sample_of(candidates, week, ctx.settings.general.hand_check_sample)
    payload = {
        "iso_week": week, "drawn_at": ctx.now.isoformat(), "sample_size": ctx.settings.general.hand_check_sample,
        "number_today": n, "positions": [start, n + cap], "pool": len(candidates), "gate_version": gate_version(ctx),
        "accounts": [facts(ctx, a, domains, hashes) for a in sample],
        "doubtful": verify.open_doubts(ctx),
        "pulled_account_ids": [], "dry_run": ctx.dry_run, "posted": False,
    }
    if errors:
        payload["number_error"] = errors[0]
    return payload


def has_work(payload: Mapping[str, Any]) -> bool:
    """Whether the payload has anything for Harry to check."""
    return bool(payload.get("accounts") or payload.get("doubtful"))


# -- what the tab and Slack show -------------------------------------------------------------------


def _who(a: Mapping[str, Any]) -> str:
    c = a.get("contact") or {}
    return c.get("role") or c.get("note") or "no contact"


def _opener(a: Mapping[str, Any]) -> str:
    """The opener as enrol would send it; a held-out account sends none, and shows the line it would have had.

    The generic line (General opener_generic_*) says nothing about the account, so it is marked: there is
    no evidence for Harry to check behind it."""
    line = a.get("opener") or ""
    if line and str(a.get("opener_source") or "").startswith(openers.GENERIC_OPENER_KEY):
        line += " (the generic line)"
    if a.get("opener_arm") == openers.HOLDOUT:
        return f"none (held out; would be: {line})" if line else "none (held out)"
    return line or "none"


def week_label(week: Any) -> str:
    """"2026-W41" -> "Week 41"."""
    number = str(week or "").rpartition("W")[2].lstrip("0")
    return f"Week {number}" if number.isdigit() else f"Week {week}"


def _accounts(n: int) -> str:
    return f"{n} compan{'y' if n == 1 else 'ies'}"


def gates(ctx: Context) -> bool:
    """Whether the hand-check holds the whole day's enrolment: hand_check_gate = yes."""
    return ctx.settings.general.hand_check_gate


WHOLE_DAY, SAMPLE_ONLY = "whole day", "sample only"


def holds(ctx: Context) -> str:
    """What waits for the check, as enrol.hand_check has it, for the notice and the reminder to say: WHOLE_DAY while it
    gates() the day; SAMPLE_ONLY with hand_check_gate = no (held: the sampled companies, for HOLD_DAYS at most). (A
    third mode, nothing waits, was for auto_send = no, and went with it: W2-1.)"""
    return WHOLE_DAY if gates(ctx) else SAMPLE_ONLY


def slack_text(ctx: Context, payload: Mapping[str, Any], tab_rows: Sequence[Mapping[str, Any]]) -> str:
    """The Monday notice (Harry, 12 Oct 2026): what the check is, how many and why, and a link to the Hand-check tab where
    each company gets a decision from a dropdown. No list of companies and no command: the tab has both."""
    from us_outbound.enrol import hand_check_tab as tab

    week = week_label(payload.get("iso_week"))
    doubts = [r for r in tab_rows if r.get("check") == DOUBT]
    drawn = len(tab_rows) - len(doubts)
    kinds = {"an uncertain industry": 0, "a missing HQ state, size or industry": 0, "other doubtful facts": 0}
    for r in doubts:
        why = str(r.get("why") or "")
        if why.startswith(labels.UNCERTAIN):
            kinds["an uncertain industry"] += 1
        elif any(m in why for m in verify.MISSING):
            kinds["a missing HQ state, size or industry"] += 1
        else:
            kinds["other doubtful facts"] += 1
    parts = ([f"{drawn} picked at random from those about to be emailed"] if drawn else []) + \
        [f"{n} with {k}" for k, n in kinds.items() if n]
    n = len(tab_rows)
    mode = holds(ctx)
    head = (f"Weekly hand-check: {_accounts(n)} to look at before this week's new emails go out" if mode == WHOLE_DAY
            else f"Weekly hand-check: {_accounts(n)} to look at")
    return say.compose(
        say.ACTION, head, [f"{week}: {', '.join(parts)}."],
        do=[f"Open the {say.sheet_link(ctx, tab.TAB)}: one row per company, with the reason it is there.",
            "Pick a `decision` on each row: *fine*, *leave out* (never email it; say why in `reason`), or *wrong "
            "industry* (then pick the right one in `right_label`). If the person or the opening line is wrong, pick "
            "*no* in `contact_ok` or `opener_ok`.",
            "That's all: each choice is applied within 5 minutes, and a change later is applied too."],
        why={WHOLE_DAY: f"until every {'company picked at random' if drawn else 'row'} has a decision, no new leads go "
                        "to Instantly this week.",
             SAMPLE_ONLY: f"only these companies wait for you, for {HOLD_DAYS} days at most; everyone else is emailed "
                          "as usual."}[mode])


def lines(payload: Mapping[str, Any]) -> list[str]:
    """`handcheck show`: one line per company, as the tab shows it."""
    out = [f"{week_label(payload.get('iso_week'))}: {_accounts(len(payload.get('accounts') or ()))} picked at random "
           f"from those about to be emailed (positions {'-'.join(str(p) for p in payload.get('positions') or ())} of the "
           "queue)" + (f", {_accounts(len(payload.get('doubtful') or ()))} held for doubtful facts"
                       if payload.get("doubtful") else "") + "."]
    for i, a in enumerate(payload.get("accounts") or (), 1):
        out.append(f"  {i}. {a.get('clean_name') or '?'} ({a.get('domain')}) · {a.get('industry_group') or '?'} · "
                   f"{a.get('hq_state') or '?'} · {a.get('size_band') or '?'} · {_who(a)} · {_opener(a)} · "
                   f"id {a.get('account_id')}")
    for d in payload.get("doubtful") or ():
        out.append(f"  - {d.get('clean_name') or '?'} ({d.get('domain')}) · {'; '.join(d.get('reasons') or ())} · "
                   f"id {d.get('account_id')}")
    return out


# -- the item, the holds and the close ---------------------------------------------------------------------


def items(ctx: Context) -> list[dict]:
    return ctx.store.select("hitl_items", {"kind": KIND})


def current(ctx: Context, week: str | None = None) -> dict | None:
    """This week's hand_check item, if one is recorded."""
    week = week or this_week(ctx)
    rows = [r for r in items(ctx) if enrol._item_week(r) == week]
    if not rows:
        return None
    return next((r for r in rows if r.get("item_id") == item_id(week)), rows[0])


def _payload(item: Mapping[str, Any] | None) -> dict[str, Any]:
    p = (item or {}).get("payload")
    return dict(p) if isinstance(p, Mapping) else {}


def _fresh(ctx: Context, item: Mapping[str, Any]) -> Mapping[str, Any]:
    """The item as stored now. pull, close and remind each write the whole payload back, so each starts from the
    stored row, not from a copy read at the start of a pass: two leave outs in one pass both stay pulled, and a pull a
    later decision took off is not written back when the week closes (the W1-5 review, 19 Oct 2026). An item that
    is not stored (a dry run's approve) is itself."""
    return ctx.store.get("hitl_items", item_id=item["item_id"]) or item


def drawn_at(item: Mapping[str, Any]) -> datetime | None:
    """When the item's sample was drawn (an item from before 19 Oct 2026: when it was recorded)."""
    return utc(_payload(item).get("drawn_at")) or utc(item.get("created_at"))


def sample_ids(item: Mapping[str, Any]) -> list[str]:
    return [str(a["account_id"]) for a in _payload(item).get("accounts") or () if a.get("account_id")]


def judged(ctx: Context, week: str, account_ids: Iterable[str]) -> dict[str, dict]:
    """account id -> the value of its judgement fact for the week, for those of these accounts that have one."""
    ids = list(dict.fromkeys(str(i) for i in account_ids))
    if not ids:
        return {}
    rows = ctx.store.select("signal_events", {"event_id": [event_id(week, i) for i in ids]})
    return {str(e["account_id"]): dict(e["value"]) for e in rows if isinstance(e.get("value"), Mapping)}


def held(ctx: Context) -> frozenset[str]:
    """The sampled companies enrol leaves out while hand_check_gate is no (D5): those of a week still open, drawn
    under HOLD_DAYS ago, with no judgement yet. A week closed (every row applied, or `handcheck approve`) holds none."""
    out: set[str] = set()
    for item in items(ctx):
        at = drawn_at(item)
        if item.get("status") == "handled" or at is None or ctx.now - at >= timedelta(days=HOLD_DAYS):
            continue
        ids = sample_ids(item)
        out |= set(ids) - set(judged(ctx, enrol._item_week(item), ids))
    return frozenset(out)


def expired(ctx: Context, item: Mapping[str, Any]) -> bool:
    """HOLD_DAYS have passed since the draw: the week closes, its unjudged companies released."""
    at = drawn_at(item)
    return at is not None and ctx.now - at >= timedelta(days=HOLD_DAYS)


def close(ctx: Context, item: Mapping[str, Any], by: str, *, announce: bool = True) -> dict[str, Any]:
    """Mark a week's item handled. Live only (hand_check_tab.apply runs it only live). Its pulls are the companies its
    judgements left out, with any pulled before (`handcheck approve --pull`); the sampled companies with no judgement
    are listed as released. announce: post the confirmation to the alert channel when the item was posted (not when
    the week closes on its own after HOLD_DAYS: the Monday readout says so)."""
    item = _fresh(ctx, item)
    payload = _payload(item)
    week = enrol._item_week(item)
    ids = sample_ids(item)
    doubt_ids = [str(d.get("account_id")) for d in payload.get("doubtful") or () if d.get("account_id")]
    found = judged(ctx, week, [*ids, *doubt_ids])
    left_out = [aid for aid, v in found.items() if v.get("decision") == LEAVE_OUT]
    pulled = list(dict.fromkeys([*(str(p) for p in payload.get("pulled_account_ids") or ()), *left_out]))
    released = [i for i in ids if i not in found]
    payload.update(pulled_account_ids=pulled, released=released, closed_at=ctx.now.isoformat())
    ctx.store.upsert("hitl_items", [{"item_id": item["item_id"], "status": "handled", "handled_at": ctx.now,
                                     "handled_by": by, "payload": payload}])
    if announce and item.get("slack_ts"):
        notify.alert(ctx, confirmation(ctx, week, by, pulled, 0))
    return {"item_id": item["item_id"], "iso_week": week, "pulled_account_ids": pulled, "released": released}


def pull(ctx: Context, item: Mapping[str, Any] | None, account_id: str, *, out: bool) -> None:
    """Keep the item's pulls in step with a row's latest decision: a leave out adds the company, any other decision
    takes it off again (a leave out changed to fine is emailed after all). A company pulled with `handcheck approve
    --pull` (operator_pulled) stays pulled whatever the tab says: that pull only ever narrows."""
    if item is None:
        return
    item = _fresh(ctx, item)
    payload = _payload(item)
    before = [str(p) for p in payload.get("pulled_account_ids") or ()]
    kept = {str(p) for p in payload.get("operator_pulled") or ()}
    after = list(dict.fromkeys([*before, account_id])) if out else [p for p in before if p != account_id or p in kept]
    if after != before:
        payload["pulled_account_ids"] = after
        ctx.store.upsert("hitl_items", [{"item_id": item["item_id"], "payload": payload}])


def remind_at(week: str) -> datetime | None:
    """Wednesday 09:00 UK of the week."""
    monday = week_monday(week)
    if monday is None:
        return None
    return datetime.combine(monday + timedelta(days=REMIND_WEEKDAY), REMIND_AT, tzinfo=UK)


def remind(ctx: Context, item: Mapping[str, Any], waiting: int) -> dict | None:
    """Once a week, from Wednesday 09:00 UK, while rows of the week's open item still wait: one post saying how many
    and what waits on them. Live only (hand_check_tab.apply runs it only live). None when nothing was posted."""
    from us_outbound.enrol import hand_check_tab as tab

    item = _fresh(ctx, item)
    payload = _payload(item)
    week = enrol._item_week(item)
    when = remind_at(week)
    if waiting <= 0 or item.get("status") == "handled" or payload.get("reminded_at") or when is None or ctx.now < when:
        return None
    text = say.compose(
        say.ACTION, f"Weekly hand-check: {_accounts(waiting)} of {week_label(week)} still "
                    f"{'needs' if waiting == 1 else 'need'} a decision",
        do=f"Pick a `decision` on {'its row' if waiting == 1 else 'each of their rows'} on the "
           f"{say.sheet_link(ctx, tab.TAB)}.",
        why={WHOLE_DAY: "no new leads go to Instantly this week until every row has one.",
             SAMPLE_ONLY: f"those companies wait for it, for {HOLD_DAYS} days from Monday at most; sending does not."}[
            holds(ctx)])
    sent = notify.alert(ctx, notify.mention(ctx) + text)
    payload["reminded_at"] = ctx.now.isoformat()
    ctx.store.upsert("hitl_items", [{"item_id": item["item_id"], "payload": payload}])
    log("hand_check_reminded", run_id=ctx.run_id, week=week, waiting=waiting, posted=bool(sent.get("posted")))
    return sent


# -- the job and the commands ------------------------------------------------------------------------


def _record(ctx: Context, payload: Mapping[str, Any]) -> dict:
    """The week's item, open (a rebuilt one replaces the one before it, approved or not)."""
    row = {"item_id": item_id(payload["iso_week"]), "kind": KIND, "status": "open", "created_at": ctx.now,
           "handled_at": None, "handled_by": None, "payload": dict(payload)}
    ctx.store.upsert("hitl_items", [row])
    return row


def _record_nothing(ctx: Context, payload: Mapping[str, Any]) -> dict:
    """The week's item for a week with nothing to check: handled as it is recorded, by the draw, payload
    nothing_to_check. Live only: it lets a gated day go ahead (enrol.hand_check), which a dry run never may."""
    row = {"item_id": item_id(payload["iso_week"]), "kind": KIND, "status": "handled", "created_at": ctx.now,
           "handled_at": ctx.now, "handled_by": JOB, "payload": {**payload, "nothing_to_check": True}}
    ctx.store.upsert("hitl_items", [row])
    return row


NOTHING = ("nothing to check: no company is ready to email beyond today's, and none is held for doubtful facts")


def _post(ctx: Context, item: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """(the Monday notice's result, the tab write's): a tab that could not be written is in the run's summary (tab);
    the next poll_approvals writes the rows (hand_check_tab.apply)."""
    from us_outbound.enrol import hand_check_tab as tab

    payload = _payload(item)
    wrote = tab.write(ctx, payload)
    sent = notify.alert(ctx, notify.mention(ctx) + slack_text(ctx, payload, tab.rows(ctx, payload)))
    payload["posted"] = bool(sent.get("posted")) and ctx.live
    update: dict[str, Any] = {"item_id": item["item_id"], "payload": payload}
    if sent.get("ts"):
        update.update(slack_channel=sent.get("channel"), slack_ts=sent["ts"])
    ctx.store.upsert("hitl_items", [update])
    return sent, wrote


def run(ctx: Context) -> dict:
    """The hand_check_draw job (JOB CONTRACT: run(ctx) -> summary): this week's draw, recorded, written to the tab and
    posted, once a week. A week recorded with nothing to check is drawn again (the queue may have grown since)."""
    week = this_week(ctx)
    summary: dict[str, Any] = {"job": JOB, "dry_run": ctx.dry_run, "iso_week": week}
    item = current(ctx, week)
    nothing_before = item is not None and bool(_payload(item).get("nothing_to_check"))
    if item is not None and item.get("status") == "handled" and not nothing_before:
        summary.update(item_id=item["item_id"], status="done already", posted=False)
        return summary
    if item is not None and not nothing_before:
        if ctx.live and not _payload(item).get("posted"):
            sent, wrote = _post(ctx, item)
            summary.update(item_id=item["item_id"], status="posted", alert=sent, tab=wrote)
        else:
            summary.update(item_id=item["item_id"], status="posted already" if _payload(item).get("posted")
                           else "recorded; a live run posts it")
        return summary
    payload = build(ctx, week)
    numbers = {k: payload[k] for k in ("number_today", "positions", "pool", "sample_size", "number_error")
               if k in payload}
    if not has_work(payload):
        summary.update(status=NOTHING, **numbers)
        if item is not None:
            summary["item_id"] = item["item_id"]
        elif ctx.live:
            summary["item_id"] = _record_nothing(ctx, payload)["item_id"]
        log("hand_check_draw", **summary)
        return summary
    if item is not None:  # a week recorded with nothing to check: this draw replaces it, open, keeping its pulls
        before = _payload(item)
        payload.update({k: list(before.get(k) or ()) for k in ("pulled_account_ids", "operator_pulled")})
    item = _record(ctx, payload)
    sent, wrote = _post(ctx, item)
    summary.update(item_id=item["item_id"], status="recorded", accounts=len(payload["accounts"]),
                   doubtful=len(payload["doubtful"]), **numbers, alert=sent, tab=wrote)
    log("hand_check_draw", **{k: v for k, v in summary.items() if k != "alert"})
    return summary


def draw_due(week: str) -> datetime | None:
    """When poll_approvals draws a week that has no item (catch_up): DRAW_GRACE after its Monday's DRAW_AT, UK."""
    monday = week_monday(week)
    return None if monday is None else datetime.combine(monday, DRAW_AT, tzinfo=UK) + DRAW_GRACE


def catch_up(ctx: Context) -> dict | None:
    """The draw poll_approvals runs when the week's own did not happen (hand_check_tab.apply, live only): run()'s
    summary, or None when nothing is due. Due when, DRAW_GRACE past the Monday draw, the week has no item at all, or,
    while the check gates() the day, its item says nothing to check and an account is now ready or a company waits
    for doubtful facts (with the gate the pool reaches back to position 1, so either one gives run() a draw: the check
    is cheap to rule out every 5 minutes, and run() is never called just to find nothing again). run() itself leaves a
    recorded week alone, so a second call the same week does nothing."""
    from us_outbound.contacts import pick

    week = this_week(ctx)
    due = draw_due(week)
    if due is None or ctx.now < due:
        return None
    item = current(ctx, week)
    if item is not None and not (_payload(item).get("nothing_to_check") and gates(ctx)
                                 and (pick.ready_accounts(ctx) or verify.open_doubts(ctx))):
        return None
    out = run(ctx)
    log("hand_check_caught_up", run_id=ctx.run_id, iso_week=week, status=out.get("status"),
        was="nothing to check" if item is not None else "no item")
    return out


def show(ctx: Context) -> tuple[dict | None, dict]:
    """(this week's item, or None when it is not recorded yet; the payload to print). It never writes: with no item
    it prints the draw `handcheck approve` would record (the same all week while the queue stands, being seeded with
    the week)."""
    week = this_week(ctx)
    item = current(ctx, week)
    if item is not None:
        return item, _payload(item)
    return None, build(ctx, week)


def _resolve(payload: Mapping[str, Any], wanted: Iterable[str], known: set[str],
             on_file: Mapping[str, str] | None = None) -> tuple[list[str], list[str]]:
    """(account ids for these ids or domains, the values matching no account). on_file: domain -> account id for
    accounts outside the payload."""
    by_domain = {**(on_file or {}), **{str(a.get("domain") or "").lower(): a["account_id"]
                                       for a in [*(payload.get("accounts") or ()), *(payload.get("doubtful") or ())]}}
    ids, unknown = [], []
    for w in wanted:
        v = str(w).strip()
        if not v:
            continue
        aid = by_domain.get(v.lower()) or (v if v in known else None)
        (ids if aid else unknown).append(aid or v)
    return list(dict.fromkeys(ids)), unknown


def approve(ctx: Context, pulled: Sequence[str], by: str) -> dict:
    """`handcheck approve`: mark this week's hand-check handled, with the pulled accounts. Live only; dry-run says what
    it would do.

    Every hand_check item of the week is marked (enrol waits for all of them while hand_check_gate is yes); the pulled
    ids go on each, merged with any pulled before, and on operator_pulled, so no later decision on the tab takes them
    off (pull). A pull is an account id or a domain, in the sample or not, and one matching no account is refused
    (ValueError) before anything is written. With no item yet it records the week's draw itself, the one `handcheck
    show` printed; with nothing to draw it records the week as nothing to check, handled, so a gated day can go ahead
    (refusing it left enrol waiting all week with no way out: the W1-5 review, 19 Oct 2026). Approving clears the
    doubts of each company held for doubtful facts that was not pulled (verify_accounts then decides on its facts as
    usual); a missing fact stays until an Overrides row fills it in.
    """
    week = this_week(ctx)
    item = current(ctx, week)
    recorded = False
    if item is None:
        payload = build(ctx, week)
        if not has_work(payload):
            payload["nothing_to_check"] = True
        item = _record(ctx, payload) if ctx.live else {"item_id": item_id(week), "kind": KIND, "status": "open",
                                                        "payload": payload}
        recorded = True
    payload = _payload(item)
    sample = {a["account_id"] for a in [*(payload.get("accounts") or ()), *(payload.get("doubtful") or ())]}
    wanted = [str(p).strip() for p in pulled if str(p).strip()]
    on_file = {str(a["account_id"]) for a in ctx.store.select("accounts", {"account_id": wanted})} if wanted else set()
    domains = [w.lower() for w in wanted if "." in w]
    by_domain = {str(a.get("domain") or "").lower(): str(a["account_id"])
                 for a in (ctx.store.select("accounts", {"domain": domains}) if domains else ())}
    ids, unknown = _resolve(payload, wanted, sample | on_file, by_domain)
    if unknown:
        raise ValueError(f"no account with id or domain {', '.join(unknown)}")
    rows = [r for r in items(ctx) if enrol._item_week(r) == week and r.get("item_id") != item["item_id"]] + [item]
    before = [x for r in rows for x in (_payload(r).get("pulled_account_ids") or ())]
    all_pulled = list(dict.fromkeys([*before, *ids]))
    by_hand = list(dict.fromkeys([*(x for r in rows for x in (_payload(r).get("operator_pulled") or ())), *ids]))
    cleared = [d for r in rows for d in (_payload(r).get("doubtful") or ()) if d.get("account_id") not in all_pulled]
    missing = verify.missing_facts(ctx, [d["account_id"] for d in cleared])  # these stay on the check
    message = confirmation(ctx, week, by, all_pulled, len(cleared) - len(missing), len(missing))
    if ctx.live:
        ctx.store.upsert("hitl_items", [
            {"item_id": r["item_id"], "status": "handled", "handled_at": ctx.now, "handled_by": by,
             "payload": {**_payload(r), "pulled_account_ids": all_pulled, "operator_pulled": by_hand}}
            for r in rows
        ])
        verify.clear_doubts(ctx, cleared, by, week)  # the next verify_accounts run decides on their facts
        if item.get("slack_ts"):
            notify.alert(ctx, message)
    return {"dry_run": ctx.dry_run, "iso_week": week, "item_id": item["item_id"], "approved": ctx.live,
            "was": "not recorded" if recorded else item.get("status"), "recorded": recorded and ctx.live,
            "pulled_account_ids": all_pulled,
            "doubts_cleared": [d["account_id"] for d in cleared if d["account_id"] not in missing] if ctx.live else [],
            "still_missing_a_fact": sorted(missing),
            "outside_sample": [i for i in ids if i not in sample], "checked": len(sample), "message": message}


def confirmation(ctx: Context, week: str, by: str, pulled: Sequence[str], cleared: int, missing: int = 0) -> str:
    """What closing the week says in the channel, in plain words: what happens next. cleared: the accounts
    verify_accounts checks again; missing: those still missing a fact, which stay."""
    lines_ = [f"{say.DONE} *Weekly hand-check done by {by}* ({week_label(week)})"]
    if pulled:
        lines_.append(f"Left out: {', '.join(pulled)}")
    if gates(ctx):
        lines_.append("New leads can go to Instantly this week.")
    tomorrow = ctx.today_uk() + timedelta(days=1)
    when = "tomorrow" if tomorrow.weekday() < 5 else "on Monday"  # verify_accounts runs weekdays at 04:30 UK
    if cleared:
        lines_.append(("The company with doubtful facts is" if cleared == 1 else f"The {cleared} companies with doubtful "
                       "facts are") + f" checked again at 04:30 {when}, then cleared to email if "
                      f"{'its' if cleared == 1 else 'their'} facts hold.")
    if missing:
        lines_.append(("The company" if missing == 1 else f"The {missing} companies") + " missing a fact (HQ state, size "
                      f"or industry) {'stays' if missing == 1 else 'stay'} on the check until a row on the Overrides tab "
                      "fills it in.")
    if len(lines_) == 1:
        lines_.append("Nothing else waits on it.")
    return "\n".join(lines_)


# -- the measure ---------------------------------------------------------------------------------------


def week_judgements(ctx: Context, week: str) -> list[dict]:
    """The values of the week's judgement facts."""
    return [dict(e["value"]) for e in ctx.store.select("signal_events", {"source": SOURCE, "fact": FACT})
            if isinstance(e.get("value"), Mapping) and str(e["value"].get("week")) == week]


def tally(ctx: Context, week: str) -> tuple[int, int]:
    """(sampled companies judged fine, sampled companies judged) for the week, from the judgement facts: the hand-check's
    precision, T1. The doubtful-facts rows are not a random sample, so they are left out."""
    sample = [v for v in week_judgements(ctx, week) if v.get("check") == SAMPLE]
    return sum(1 for v in sample if v.get("decision") == FINE), len(sample)


def readout_lines(ctx: Context, start: datetime) -> tuple[list[str], dict[str, Any]]:
    """The Monday readout's hand-check section for the week starting at start: how many were drawn and judged, each
    decision counted, and the companies sent unjudged (released after HOLD_DAYS, D5) or still held. Counts only: at
    10 to 20 rows a week a rate would say little (its interval is ±25 points at 10)."""
    week = enrol.iso_week(start.astimezone(UK).date())
    item = current(ctx, week)
    head = ["", "*Hand-check* (companies picked at random from those about to be emailed, judged by hand)"]
    if item is None:
        return [*head, f"  {week_label(week)}: no hand-check was drawn."], {"iso_week": week, "drawn": 0}
    if _payload(item).get("nothing_to_check"):
        return ([*head, f"  {week_label(week)}: 0 picked: no company was ready to email beyond that day's number."],
                {"iso_week": week, "drawn": 0, "nothing_to_check": True})
    ids = sample_ids(item)
    found = judged(ctx, week, ids)
    counts = Counter(str(v.get("decision")) for v in found.values())
    unjudged = len(ids) - len(found)
    line = (f"  {week_label(week)}: {len(ids)} picked; {len(found)} judged: {counts[FINE]} fine, "
            f"{counts[LEAVE_OUT]} left out, {counts[WRONG_INDUSTRY]} wrong industry")
    gone = item.get("status") == "handled" or expired(ctx, item)
    if unjudged:
        line += f"; {unjudged} unjudged, {'sent' if gone else 'still held'}"
    reasons = Counter(str(v.get("reason") or "no reason given") for v in found.values()
                      if v.get("decision") == LEAVE_OUT)
    out = [*head, line + "."]
    if reasons:
        out.append("  Left out because: " + ", ".join(f"{r} {n}" for r, n in reasons.most_common()) + ".")
    return out, {"iso_week": week, "drawn": len(ids), "judged": len(found), "decisions": dict(counts),
                 "unjudged": unjudged, "released": bool(unjudged and gone)}
