"""monday_readout (learn/readout.py; Harry, 6 Oct 2026): last week in plain words, the targets and exit criteria, the
cuts and the signal table from the readout views, and the tests at a look. Small numbers read as small."""

from __future__ import annotations

import dataclasses
from datetime import UTC, date, datetime, timedelta

from tests.fakes import FakeTransport, make_context
from tests.test_db import db, dsn, learning_world, store  # noqa: F401  (Postgres fixtures, for the end-to-end read)
from tests.test_registry import SETTINGS, slack_routes
from us_outbound.base import say
from us_outbound.clients.guard import Guard
from us_outbound.learn import readout, signal_value
from us_outbound.ops import cli, heartbeat, schedule
from us_outbound.clients.slack import dry_run_note

DRY = dry_run_note("#us-outbound") + "\n"

MON = datetime(2026, 10, 12, 7, 30, tzinfo=UTC)  # Monday 08:30 UK (BST)
LAST_WEEK = datetime(2026, 10, 7, 15, tzinfo=UTC)  # Wednesday of the week read
LIVE = dataclasses.replace(SETTINGS, general=dataclasses.replace(SETTINGS.general, live_sending=True,
                                                                  optout_tested=True))


def week_row(cut: str, value: str, start: date = date(2026, 10, 5), **numbers) -> dict:
    """A v_readout_weekly row: zeros unless given."""
    cols = (*readout.COHORT, *readout.ACTIVITY)
    return {"week_start": start, "cut": cut, "cut_value": value, **{c: 0 for c in cols}, **numbers}


def world(settings=SETTINGS, *, live=False, now=MON, weekly=None, signals=None, outcomes=None):
    t = slack_routes(FakeTransport())
    ctx = make_context(settings, live=live, transport=t, now=now, job=readout.JOB)
    if weekly is not None:
        ctx.store.query_handlers["v_readout_weekly"] = lambda s, p: [dict(r) for r in weekly]
    if signals is not None:
        ctx.store.query_handlers["v_signal_value"] = lambda s, p: [dict(r) for r in signals]
    if outcomes is not None:
        ctx.store.query_handlers["v_account_outcomes"] = lambda s, p: [dict(r) for r in outcomes]
    return ctx, t


def outcome_rows(n: int, *, days_ago: float, replied: int = 0, positive: int = 0, bounced: int = 0,
                 meeting_day: dict[int, float] | None = None, now: datetime = MON) -> list[dict]:
    """n v_account_outcomes rows, step 1 sent `days_ago` before now: the first `bounced` bounced; of the rest, the
    first `replied` replied in the window, the first `positive` of those positively; meeting_day: row -> the day
    after step 1 a meeting was booked. window_closed as the view sets it (28 days after step 1)."""
    step1 = now - timedelta(days=days_ago)
    rows = []
    for i in range(n):
        delivered = i >= bounced
        j = i - bounced  # the index among the delivered
        meeting = (meeting_day or {}).get(i)
        rows.append({"account_id": f"o{days_ago:g}-{i}", "delivered": delivered, "step1_at": step1,
                     "replied_in_window": delivered and j < replied, "positive_in_window": delivered and j < positive,
                     "window_closed": days_ago >= 28,
                     "first_meeting_at": step1 + timedelta(days=meeting) if meeting is not None else None})
    return rows


def ev(i, type_, at=LAST_WEEK, **kw):
    return {"event_id": kw.pop("event_id", f"e{i}"), "type": type_, "occurred_at": at + timedelta(minutes=i), **kw}


def text(ctx) -> str:
    return "\n".join(readout.build(ctx)[0])


# -- small numbers ------------------------------------------------------------------------------------------------


def test_small_numbers_read_too_few_to_read_never_a_rate():
    weekly = [week_row("all", "all", accounts_enrolled=12, accounts_delivered=12, accounts_replied=2,
                       accounts_positive=1, sends=12, replies=2, positive_replies=1),
              week_row("arm", "random", accounts_enrolled=3, accounts_delivered=3, accounts_replied=1, sends=3,
                       replies=1)]
    ctx, _ = world(weekly=weekly, outcomes=outcome_rows(12, days_ago=10, replied=2, positive=1))
    out = text(ctx)
    assert ("*So far:* 0 of 12 companies emailed have had the full 28 days after email 1 to reply, too few to give a "
            "rate (it takes 30). Counts so far, windows still open: 2 replied · 1 positive · 0 meetings.") in out
    assert "  Companies emailed so far: 12 (12 delivered) · 2 replied · 1 positive · 0 meetings" in out
    # The explore arm cut in place of the tiers (W2-2): 1 replied of 3 is too few to give a rate.
    [arm] = [line for line in out.splitlines() if line.startswith("  random:")]
    assert arm == "  random: 3 sent, 1 reply · 3 emailed so far, 1 replied (too few to read)"
    assert "_By explore arm_ (random: picked at random from the queue; ranked: in score order)" in out
    assert "16.7%" not in out and "33" not in out  # 2 of 12 is never given as a rate


def test_with_no_reply_window_closed_there_are_counts_and_no_verdict():
    """metrics-audit H2: on 19 Oct every rate read "below" with 0 of 47 windows closed. 48 delivered, none matured:
    counts only, no rate and no verdict."""
    ctx, _ = world(weekly=[], outcomes=outcome_rows(48, days_ago=12, replied=1))
    lines, nums = readout.build(ctx)
    assert nums["targets"] is None and nums["matured"] == {"emailed": 48, "delivered": 48, "at_28": 0, "at_45": 0}
    assert lines[2] == ("*So far:* 0 of 48 companies emailed have had the full 28 days after email 1 to reply, too few "
                        "to give a rate (it takes 30). Counts so far, windows still open: 1 replied · 0 positive · "
                        "0 meetings.")
    out = "\n".join(lines)
    assert ": below" not in out and ": on track" not in out and "2.1%" not in out  # 1 of 48 is no rate


def test_from_30_matured_companies_each_rate_has_its_interval_and_no_verdict():
    """35 delivered 30 days ago (window closed): 3 replied, 2 of them positively; 13 delivered 10 days ago, 5 of them
    positive, which must not count (their windows are open).
    Wilson 95% (z = 1.96, z² = 3.8416), n = 35, so z²/n = 0.10976 and the denominator is 1.10976:
      2 of 35: p = 0.05714; centre (0.05714 + 0.05488) / 1.10976 = 0.10094;
               half-width 1.96 × √(0.05714 × 0.94286 / 35 + 3.8416 / 4900) / 1.10976 = 1.96 × 0.04820 / 1.10976
               = 0.08513: 1.6–18.6%.
      3 of 35: p = 0.08571; centre (0.08571 + 0.05488) / 1.10976 = 0.12669;
               half-width 1.96 × √(0.08571 × 0.91429 / 35 + 0.000784) / 1.10976 = 1.96 × 0.05498 / 1.10976
               = 0.09711: 3.0–22.4%.
    Meetings are judged at 45 days: none of them has had 45 days, so no meeting rate."""
    rows = outcome_rows(35, days_ago=30, replied=3, positive=2) + outcome_rows(13, days_ago=10, replied=5, positive=5)
    ctx, _ = world(weekly=[], outcomes=rows)
    lines, nums = readout.build(ctx)
    assert lines[2] == ("*So far* (35 of 48 companies emailed have had the full 28 days after email 1 to reply): "
                        "replies 8.6% (3 of 35; 95% 3.0–22.4) · positive 5.7% (2 of 35; 95% 1.6–18.6) · meetings: 0 "
                        "have had 45 days, too few to give a rate. SPEC 12 assumes replies 3–5%, positive about 1%, "
                        "meetings 0.5–1%.")
    assert nums["targets"] == {"matured": 35, "replies": {"k": 3, "n": 35, "rate": 0.0857, "low": 0.0296, "high": 0.2238},
                               "positive": {"k": 2, "n": 35, "rate": 0.0571, "low": 0.0158, "high": 0.1861},
                               "meetings": None}
    out = "\n".join(lines)
    assert ": on track" not in out and ": below" not in out and "14.6%" not in out  # 7 of 48 (open windows) is no rate
    assert readout.rate(5, 29) == "too few to read" and readout.rate(5, 30) == "16.7%"


def test_meetings_are_judged_on_companies_that_have_had_45_days():
    """30 delivered 50 days ago: one booked a meeting on day 10, one on day 47 (after the 45 days, so not counted);
    10 delivered 40 days ago are past 28 days but not 45, so they are in the reply rates only.
    Wilson 1 of 30: z²/n = 0.12805, denominator 1.12805; centre (0.03333 + 0.06403) / 1.12805 = 0.08631;
    half-width 1.96 × √(0.03333 × 0.96667 / 30 + 3.8416 / 3600) / 1.12805 = 1.96 × 0.04627 / 1.12805 = 0.08040:
    0.6–16.7%."""
    rows = outcome_rows(30, days_ago=50, meeting_day={0: 10, 1: 47}) + outcome_rows(10, days_ago=40)
    ctx, _ = world(weekly=[], outcomes=rows)
    lines, nums = readout.build(ctx)
    assert "meetings within 45 days 3.3% (1 of 30; 95% 0.6–16.7)" in lines[2]
    assert nums["targets"]["meetings"] == {"k": 1, "n": 30, "rate": 0.0333, "low": 0.0059, "high": 0.1667}
    assert nums["targets"]["replies"]["n"] == 40  # every company past 28 days


def test_the_stop_rule_line_counts_meetings_so_far():
    weekly = [week_row("all", "all", accounts_enrolled=120, accounts_delivered=120, accounts_replied=5,
                       accounts_positive=1, accounts_meeting=1, accounts_window_closed=40)]
    out = text(world(weekly=weekly)[0])
    assert "  Stop rule: 1 meeting from the first 120 companies; enrolment pauses by itself if the first 1,500 bring " \
           "fewer than 5" in out


def test_the_summary_opens_with_what_needs_action_or_says_nothing_does():
    ctx, _ = world(weekly=[])  # optout_tested is no: one thing to act on
    lines, nums = readout.build(ctx)
    assert lines[0] == ":warning: *Monday readout, 5–11 Oct: 1 thing to act on*"
    assert lines[3] == "*Needs action:* test the unsubscribe link (details below)"
    assert nums["to_act_on"] == ["test the unsubscribe link"]
    assert ("    *What to do:* `railway ssh -- us-outbound seed send ADDRESS --owner NAME --live` to an inbox of ours, "
            "click the email's unsubscribe link, then `railway ssh -- us-outbound seed check`; at PASS, set "
            f"`optout_tested` to yes on the {say.sheet_link(ctx, 'General')}") in lines

    # A bounce rate over 2% is not met, but has nothing to do: information only.
    ctx, _ = world(LIVE, weekly=[])
    ctx.store.insert("events", [*(ev(i, "sent", step=1, account_id=f"a{i}", contact_id=f"k{i}") for i in range(10)),
                                ev(20, "bounced", contact_id="k1", step=1)])
    lines, nums = readout.build(ctx)
    assert lines[0] == ":bar_chart: *Monday readout, 5–11 Oct: nothing to act on*"
    assert not lines[3] and nums["to_act_on"] == [] and "*What to do:*" not in "\n".join(lines)
    assert ("  *Not met:* bounces under 2% of the contacts sent email 1: too early: 10 of 100 contacts sent email 1 so "
            "far, 1 bounce") in lines

    # Too few contacts: no criterion reads as met on them, so nothing says to grow (12 Oct 2026: 13 sends did).
    ctx, _ = world(LIVE, weekly=[])
    ctx.store.insert("events", [ev(i, "sent", step=1, account_id=f"a{i}", contact_id=f"k{i}") for i in range(10)])
    lines, nums = readout.build(ctx)
    assert nums["to_act_on"] == [] and "weekly_enrol_cap` (now" not in "\n".join(lines)

    # All four measured met on 100 contacts, but the weekly limit was far off: the mailboxes hold sending back.
    ctx, _ = world(LIVE, weekly=[])
    ctx.store.insert("events", [ev(i, "sent", step=1, account_id=f"a{i}", contact_id=f"k{i}") for i in range(100)])
    lines, nums = readout.build(ctx)
    assert nums["to_act_on"] == []
    assert ("  All 4 measured are met. The weekly limit is not what holds sending back (0 of 150 last week): the "
            "mailboxes' warm-up is, and it rises by itself.") in lines


# -- last week and the cuts -----------------------------------------------------------------------------------------


def test_last_week_counts_only_last_week_in_uk_time():
    ctx, _ = world(weekly=[])
    ctx.store.insert("events", [
        ev(1, "sent", step=1, account_id="a1"), ev(2, "sent", step=2, account_id="a2"),
        ev(3, "replied", account_id="a1", reply_class="positive"),
        ev(4, "replied", account_id="a2", reply_class="out_of_office"),  # not a human reply
        ev(5, "replied", account_id="a3", reply_class=None),  # not yet classified: counts as human
        ev(6, "meeting_booked", account_id="a1", source="hubspot_meeting"),
        ev(7, "meeting_booked", account_id="a1", source="hubspot_deal"),  # the same company: one meeting
        ev(8, "bounced", contact_id="k2", step=1), ev(9, "unsubscribed", account_id="a2"),
        # Sunday 23:30 UK is last week; Monday 00:30 UK is this week; the Sunday before is the week before.
        ev(10, "sent", at=datetime(2026, 10, 11, 22, 30, tzinfo=UTC), step=1, account_id="a4"),
        ev(11, "sent", at=datetime(2026, 10, 11, 23, 30, tzinfo=UTC), step=1, account_id="a5"),
        ev(12, "sent", at=datetime(2026, 10, 4, 22, 0, tzinfo=UTC), step=1, account_id="a6"),
    ])
    lines, nums = readout.build(ctx)
    # a1 (positive) and a3 (not classified) replied; a2's away message is no reply: 2 companies, 2 emails.
    assert nums["last_week"] == {"sent": 3, "replies": 2, "positive": 1, "reply_emails": 2, "meetings": 1,
                                 "bounces": 1, "unsubscribes": 1}
    assert lines[1] == ("*Last week:* 3 emails sent · 2 companies replied (1 positive) · 1 meeting booked · 1 bounce · "
                        "1 unsubscribe")
    assert lines[0].startswith(":warning: *Monday readout, 5–11 Oct: ")  # the dry-run note is the Slack client's


def test_the_cuts_show_last_week_and_so_far_busiest_first():
    weekly = [
        week_row("all", "all", sends=40),
        week_row("angle", "General", sends=10, replies=1, accounts_enrolled=40, accounts_delivered=40,
                 accounts_replied=2),
        week_row("angle", "Upgrade the EAP", sends=30, replies=2, positive_replies=1, meetings_booked=1, bounces=1),
        week_row("angle", "Upgrade the EAP", start=date(2026, 9, 28), accounts_enrolled=20, accounts_delivered=20,
                 accounts_replied=3),
        week_row("sender", "Hannah Spalding", sends=40),
        week_row("step", "step 2", sends=15, replies=1), week_row("step", "step 1", sends=25, replies=2, bounces=1),
        week_row("arm", "random", start=date(2026, 9, 28)),  # nothing in it: not listed
    ]
    out = text(world(weekly=weekly)[0]).splitlines()
    assert "*Cuts* (the numbers split several ways: last week · companies emailed so far)" in out
    angle = out.index("_By angle_ (the pitch of the copy)")
    assert out[angle + 1] == ("  Upgrade the EAP: 30 sent, 2 replies (1 positive), 1 meeting, 1 bounce · 20 emailed so "
                              "far, 3 replied (too few to read)")
    assert out[angle + 2] == "  General: 10 sent, 1 reply · 40 emailed so far, 2 replied (5.0%)"  # no zeros
    step = out.index("_By step_ (which email of the sequence)")
    assert out[step + 1:step + 3] == ["  step 1: 25 sent, 2 replies, 1 bounce", "  step 2: 15 sent, 1 reply"]
    assert not any(x.startswith(("_By tier_", "_By explore arm_")) for x in out)
    assert "_By sender_ (whose mailbox sent it)" in out


def test_without_the_views_the_rest_still_posts():
    ctx, _ = world()  # a MemoryStore with no view handlers
    lines, nums = readout.build(ctx)
    out = "\n".join(lines)
    assert nums["views"] is False and "need the database's views" in out
    assert "*Exit criteria to scale*" in out and "*Tests*" in out and "*Cuts*" not in out


# -- the exit criteria to scale ---------------------------------------------------------------------------------------


def test_exit_criteria_met():
    """1 of 150 contacts bounced: 0.7%, under 2%, so met; but its Wilson interval still reaches 2%:
    z²/n = 0.025611, denominator 1.025611; centre (0.006667 + 0.012805) / 1.025611 = 0.018986; half-width
    1.96 × √(0.006667 × 0.993333 / 150 + 3.8416 / 90000) / 1.025611 = 1.96 × 0.009318 / 1.025611 = 0.017808:
    0.1–3.7%."""
    ctx, _ = world(LIVE, weekly=[])
    sends = [ev(i, "sent", step=1, account_id=f"a{i}", contact_id=f"k{i}", at=MON - timedelta(days=20))
             for i in range(150)]
    ctx.store.insert("events", [*sends, ev(200, "bounced", contact_id="k1", step=1),
                                ev(201, "replied", event_id="em-1", account_id="a1", reply_class="positive"),
                                ev(202, "replied", event_id="em-2", account_id="a2", reply_class="out_of_office"),
                                ev(203, "replied", event_id="em-3", account_id="a3", reply_class="unsubscribe"),
                                ev(204, "unsubscribed", event_id="unsubscribed:em-3", contact_id="k3")])
    ctx.store.insert("contacts", [{"contact_id": "k3", "account_id": "a3", "suppressed": True,
                                   "suppressed_reason": "unsubscribe"}])
    ctx.store.insert("hitl_items", [{"item_id": "reply:em-1", "kind": "reply", "status": "handled"},
                                    {"item_id": "out_of_office:em-2", "kind": "out_of_office", "status": "handled"}])
    ctx.store.insert("heartbeats", [{"run_id": "e1", "job": "enrol", "status": "ok", "started_at": LAST_WEEK,
                                     "detail": {"skipped": {"copy blocked": 2}}}])
    lines, nums = readout.build(ctx)
    out = "\n".join(lines)
    assert nums["exit_criteria"] == {"bounces": True, "unsubscribes": True, "replies": True, "refusals": True}
    assert nums["bounces"] == {"contacts": 150, "bounced": 1, "interval": [0.0012, 0.0368]}
    assert "*Exit criteria to scale* (all 4 measured must be met before sending grows): 4 of 4 met" in out
    assert ("  Met: bounces under 2% of the contacts sent email 1 (1 of 150 contacts, 0.7% (95% 0.1–3.7%); the "
            "interval still reaches 2%: 150 contacts cannot show the rate is under it) · unsubscribes work end to end "
            "(link tested; 1 opt-out so far, each in suppression, Instantly's blocklist and HubSpot) · every reply "
            "sorted and routed (3 so far) · no unexplained safety-check refusal (2 emails held back by the copy rules, "
            "each with its reason)") in lines
    assert "  Not measured: spam complaints (nothing reports them to us yet), so they are not counted as met" in lines
    assert "*Not met:*" not in out


def _contacts_sent(ctx, n: int, bounced: int, *, bounce_step: dict[int, int] | None = None, later_steps: int = 0):
    """n contacts sent email 1 last week; the first `bounced` bounce (at step 1 unless bounce_step says which);
    every contact not bounced at step 1 gets up to `later_steps` more emails, stopping at its bounce."""
    rows = []
    for i in range(n):
        stop = (bounce_step or {}).get(i, 1) if i < bounced else None
        last = 1 + later_steps if stop is None else stop
        rows += [ev(1000 * i + s, "sent", step=s, account_id=f"a{i}", contact_id=f"k{i}") for s in range(1, last + 1)]
        if stop is not None:
            rows.append(ev(1000 * i + 900, "bounced", step=stop, account_id=f"a{i}", contact_id=f"k{i}"))
    ctx.store.insert("events", rows)


def test_the_bounce_criterion_counts_contacts_not_sends():
    """metrics-audit H4: 2 of 48 contacts bounced (4.2%), which per send read about 1.1% once the sequences finished.
    Here k0 bounced at step 1 (1 send), k1 at step 2 (2 sends), and the other 46 got all 4 emails (184): 187 sends,
    2 of them bounced (1.07%, under 2% on 100+ sends, which the per-send criterion called met). Per contact it is
    48 of the 100 needed: too early."""
    ctx, _ = world(LIVE, weekly=[])
    _contacts_sent(ctx, 48, 2, bounce_step={1: 2}, later_steps=3)
    assert len(ctx.store.select("events", {"type": "sent"})) == 187
    lines, nums = readout.build(ctx)
    assert nums["exit_criteria"]["bounces"] is False and nums["bounces"]["contacts"] == 48
    assert ("  *Not met:* bounces under 2% of the contacts sent email 1: too early: 48 of 100 contacts sent email 1 so "
            "far, 2 bounces") in lines


def test_the_bounce_criterion_from_100_contacts_with_its_interval():
    """2 of 120: 1.7%, met. Wilson: z²/n = 0.032013, denominator 1.032013; centre (0.016667 + 0.016007) / 1.032013
    = 0.031661; half-width 1.96 × √(0.016667 × 0.983333 / 120 + 3.8416 / 57600) / 1.032013 = 1.96 × 0.014257 /
    1.032013 = 0.027077: 0.46–5.9%. 3 of 120: 2.5%, not met (2% is the limit, and an exit_bounce_limit_50 mutant,
    0.5, would call it met)."""
    ctx, _ = world(LIVE, weekly=[])
    _contacts_sent(ctx, 120, 2)
    lines, nums = readout.build(ctx)
    assert nums["exit_criteria"]["bounces"] is True
    assert nums["bounces"] == {"contacts": 120, "bounced": 2, "interval": [0.0046, 0.0587]}
    assert any(x.startswith("  Met: bounces under 2% of the contacts sent email 1 (2 of 120 contacts, 1.7% (95% "
                            "0.5–5.9%); the interval still reaches 2%") for x in lines)

    ctx, _ = world(LIVE, weekly=[])
    _contacts_sent(ctx, 120, 3)
    lines, nums = readout.build(ctx)
    assert nums["exit_criteria"]["bounces"] is False
    assert ("  *Not met:* bounces under 2% of the contacts sent email 1: 3 of 120 contacts, 2.5% (95% 0.9–7.1%)"
            in lines)


def test_R1_no_bounce_on_13_sends_is_not_yet_met():
    """tests-mutation-safety-measure R1 (the 12 Oct regression): 0 bounces in 13 sends read as met."""
    ctx, _ = world(LIVE, weekly=[])
    ctx.store.insert("events", [ev(i, "sent", step=1, account_id=f"a{i}") for i in range(13)])
    assert readout.build(ctx)[1]["exit_criteria"]["bounces"] is False


def test_R2_a_pending_opt_out_is_not_met_once_the_link_is_tested():
    """tests-mutation-safety-measure R2: with the link tested, an opt-out not yet everywhere is still not met."""
    ctx, _ = world(LIVE, weekly=[])  # optout_tested = yes
    ctx.store.insert("contacts", [{"contact_id": "k7", "suppressed": True, "suppressed_reason": "unsubscribe"}])
    assert readout.build(ctx)[1]["exit_criteria"]["unsubscribes"] is False


def test_complaints_are_not_measured_and_the_raise_line_needs_the_cap_to_bind():
    """No code writes a complained event (metrics-audit H1), so complaints are out of the met count; with every
    measured criterion met, the "raise weekly_enrol_cap" line comes only when last week reached 90% of the cap:
    35 of 150 does not (the mailboxes hold sending back), 140 of 150 does (140 ≥ 135)."""
    ctx, _ = world(LIVE, weekly=[])
    _contacts_sent(ctx, 100, 0)
    start, end = readout.last_week(ctx)
    out, met, todo = readout.exit_criteria(ctx, readout._Events(ctx), start, end, 35)
    assert met == {"bounces": True, "unsubscribes": True, "replies": True, "refusals": True}
    assert "complaints" not in met and todo == []
    assert not any("raise `weekly_enrol_cap`" in x for x in out)
    assert ("  All 4 measured are met. The weekly limit is not what holds sending back (35 of 150 last week): the "
            "mailboxes' warm-up is, and it rises by itself.") in out
    out, met, todo = readout.exit_criteria(ctx, readout._Events(ctx), start, end, 140)
    assert todo == ["decide whether to raise the weekly limit"]
    assert ("  *What to do:* all 4 measured are met and last week reached the weekly limit, so sending can grow: raise "
            f"`weekly_enrol_cap` (now 150) on the {say.sheet_link(ctx, 'General')} in step with the mailbox ramp") in out


def test_each_source_s_bounces_per_contact_are_shown_beside_the_per_send_rule():
    """D19: source_bounce_rate stays per send (a SPEC 12 change is Harry's); the readout prints the per-contact figure
    beside it. 48 apollo contacts emailed in the last 7 days, 2 bounced at step 1, 46 with a second email: 94 sends,
    2 bounced (2.1%); per contact 2 of 48 (4.2%; Wilson 1.2–14.0%: z²/n = 0.080033, denominator 1.080033, centre
    (0.041667 + 0.040017) / 1.080033 = 0.075631, half-width 1.96 × √(0.041667 × 0.958333 / 48 + 3.8416 / 9216) /
    1.080033 = 1.96 × 0.035337 / 1.080033 = 0.064129)."""
    ctx, _ = world(LIVE, weekly=[])
    rows, contacts = [], []
    for i in range(48):
        at = MON - timedelta(days=3)
        rows.append(ev(10 * i, "sent", step=1, at=at, account_id=f"a{i}", contact_id=f"k{i}", mailbox="h@x.org"))
        if i < 2:
            rows.append(ev(10 * i + 1, "bounced", step=1, at=at, account_id=f"a{i}", contact_id=f"k{i}"))
        else:
            rows.append(ev(10 * i + 2, "sent", step=2, at=at + timedelta(days=1), account_id=f"a{i}",
                           contact_id=f"k{i}", mailbox="h@x.org"))
        contacts.append({"contact_id": f"k{i}", "account_id": f"a{i}", "email_source": "apollo"})
    ctx.store.insert("events", rows)
    ctx.store.insert("contacts", contacts)
    lines, nums = readout.build(ctx)
    assert nums["source_bounces"] == {"apollo": {"contacts": 48, "bounced_contacts": 2, "sends": 94,
                                                 "bounced_sends": 2}}
    assert ("  Emails found by apollo, last 7 days: 2 of 48 contacts sent email 1 bounced (4.2%; 95% 1.2–14.0%) · the "
            "safety pause counts sends: 2 of 94 sends (2.1%), and pauses the source over 3% from 100 sends") in lines


def test_exit_criteria_not_met_say_why():
    ctx, _ = world(weekly=[])  # optout_tested is no
    ctx.store.insert("events", [
        *[ev(i, "sent", step=1, account_id=f"a{i}", contact_id=f"k{i}") for i in range(40)],
        ev(50, "bounced", contact_id="k1", step=1),
        ev(52, "replied", event_id="em-9", account_id="a9", reply_class=None, at=MON - timedelta(hours=3)),
        ev(53, "replied", event_id="em-8", account_id="a8", reply_class="objection"),  # no card
    ])
    ctx.store.insert("contacts", [{"contact_id": "k7", "suppressed": True, "suppressed_reason": "unsubscribe"}])
    ctx.store.insert("heartbeats", [{"run_id": "x", "job": "enrol", "status": "error", "started_at": LAST_WEEK,
                                     "detail": {"violation": True}, "error": "GuardViolation: ..."}])
    lines, nums = readout.build(ctx)
    out = "\n".join(lines)
    assert not any(nums["exit_criteria"].values()) and "complaints" not in nums["exit_criteria"]
    assert "): 0 of 4 met" in out and "  Met:" not in out
    assert ("  *Not met:* bounces under 2% of the contacts sent email 1: too early: 40 of 100 contacts sent email 1 so "
            "far, 1 bounce") in lines
    assert ("  *Not met:* unsubscribes work end to end: the unsubscribe-link test has not passed (`optout_tested` is "
            "no); 1 opt-out not yet in Instantly's blocklist and HubSpot (retried by the next live run of recording "
            "email results or checking for replies)") in lines
    replies = lines.index("  *Not met:* every reply sorted and routed: 1 not sorted (positive, objection, …) after an "
                          "hour; 1 with no reply card in Slack, out-of-office record or opt-out")
    assert lines[replies + 1] == "    *What to do:* look at them with `railway ssh -- us-outbound replies list`"
    refusal = lines.index("  *Not met:* no unexplained safety-check refusal: 1 refusal last week, in adding people to "
                          "the email campaign")
    assert lines[refusal + 1].startswith("    *What to do:* read each error with "
                                         "`railway ssh -- us-outbound status` or in ")
    assert nums["to_act_on"] == ["test the unsubscribe link", "check 2 replies", "explain 1 refusal"]
    assert lines[0] == ":warning: *Monday readout, 5–11 Oct: 3 things to act on*"


# -- the signal table and the tests ---------------------------------------------------------------------------------


def signal_row(name: str, **numbers) -> dict:
    base = {"signal": name, "weight": 10, "below_control": False, "accounts_enrolled": 0, "accounts_sent": 0,
            "accounts_delivered": 0, "accounts_replied": 0, "accounts_positive": 0, "accounts_meeting": 0,
            "without_delivered": 0, "without_replied": 0}
    return {**base, **numbers}


def test_the_signal_headline_says_too_few_and_reads_the_rest():
    signals = [
        signal_row("EAP named", accounts_enrolled=60, accounts_sent=60, accounts_delivered=60, accounts_replied=12,
                   accounts_positive=3, accounts_meeting=2, without_delivered=100, without_replied=4),
        signal_row("Hiring and growth", accounts_enrolled=40, accounts_sent=40, accounts_delivered=40,
                   accounts_replied=2, without_delivered=120, without_replied=6),
        signal_row("New People leader", accounts_enrolled=4, accounts_sent=3, accounts_delivered=3,
                   accounts_replied=1, without_delivered=157, without_replied=17),
        signal_row("Layoffs"),
    ]
    out = text(world(weekly=[], signals=signals)[0])
    assert ("  EAP named: 20.0% of 60 replied, against 4.0% of 100 without · 3 positive, 2 meetings: replies more "
            "(p = 0.00)") in out
    assert ("  Hiring and growth: 5.0% of 40 replied, against 5.0% of 120 without · 0 positive, 0 meetings: no clear "
            "difference yet (p = 1.00)") in out
    assert "  Too few to read (under 30 companies emailed with or without it): New People leader (3)." in out
    assert "33.3%" not in out and "Layoffs" not in out and "*What to do:* decide whether to lower" not in out
    assert signal_value.verdict(signals[2]) == "too few to read"


def test_a_signal_below_the_control_tier_is_monitoring_not_an_action():
    """metrics-audit H5: below_control flags about 46% of signals by chance at these numbers, so it is listed but is
    not a "Needs action" item, and no "lower its weight" advice is given."""
    signals = [signal_row("Layoffs", weight=5, below_control=True, accounts_enrolled=220, accounts_sent=210,
                          accounts_delivered=205, accounts_replied=2, without_delivered=300, without_replied=15)]
    ctx, _ = world(LIVE, weekly=[], signals=signals)
    lines, nums = readout.build(ctx)
    at = lines.index("  Layoffs replies less than the comparison group (Control) after 200 companies")
    assert lines[at + 1] == ("  Monitoring only: at these numbers a gap like this is usually chance, so no weight "
                             "changes on it.")
    assert nums["to_act_on"] == [] and lines[0].endswith("nothing to act on*")
    assert not any("lower its weight" in x for x in lines)


def test_tests_at_a_look_are_listed_with_their_read(monkeypatch):
    from us_outbound.settings.model import Test

    test = Test("t1", "h", "eap-v1", "general-v1", 400, "running", date(2026, 8, 1), date(2026, 10, 8), "reply rate")
    ctx, _ = world(dataclasses.replace(SETTINGS, tests=(test,)), weekly=[])
    ctx.store.insert("contacts", [{"contact_id": f"k{i}", "account_id": f"a{i}", "test_id": "t1",
                                   "copy_version": "eap-v1" if i % 2 else "general-v1"} for i in range(4)])
    ctx.store.insert("events", [ev(i, "sent", step=1, account_id=f"a{i}", contact_id=f"k{i}",
                                   at=datetime(2026, 9, 1, tzinfo=UTC)) for i in range(4)])
    lines, nums = readout.build(ctx)
    out = "\n".join(lines)
    assert nums["tests_at_a_look"] == ["t1"] and "decide on test t1" in nums["to_act_on"]
    at = lines.index("  t1 (A/B copy test) reached look 1 (the read date): Thu 08 Oct 2026: eap-v1 0 of 2 replied "
                     "(0.0%); general-v1 0 of 2 replied (0.0%) · p = 1.00")
    assert lines[at + 1] == ('    *What to do:* apply its decision rule ("reply rate") and write the outcome in the '
                             f"`result` column on the {say.sheet_link(ctx, 'Tests')}. `railway ssh -- us-outbound test "
                             "read t1` has the full read.")
    assert "*Tests* (copy tests, each read only at points fixed in advance on the Tests tab, called looks)" in out


# -- the job: schedule, heartbeat, cli, the post -------------------------------------------------------------------------


def test_registered_on_the_schedule_heartbeat_and_cli():
    job = schedule.by_name()["monday_readout"]
    assert (job.cron, job.live, job.enabled) == ("30 8 * * 1", True, True)
    assert cli.JOBS["monday_readout"] == "us_outbound.learn.readout:run" and "monday_readout" in heartbeat.scheduled_jobs()
    assert heartbeat.EXPECTED["monday_readout"] == 8 * 24 * 60


def test_dry_run_posts_to_the_dev_channel_and_writes_only_its_heartbeat():
    ctx, t = world(weekly=[])
    before = {k: list(v) for k, v in ctx.store.tables.items()}
    summary = heartbeat.run_job(ctx, readout.run)
    [post] = [r for r in t.requests if r.url.endswith("chat.postMessage")]
    assert post.json["channel"] == "C_DEV" and post.json["text"].startswith(DRY + ":warning: *Monday readout")
    assert summary["dry_run"] and summary["alert"]["posted"]
    after = {k: v for k, v in ctx.store.tables.items() if v != before[k]}
    assert set(after) == {"heartbeats"}


def test_live_posts_to_the_alert_channel():
    ctx, t = world(LIVE, live=True, weekly=[])
    readout.run(ctx)
    [post] = [r for r in t.requests if r.url.endswith("chat.postMessage")]
    assert post.json["channel"] == "C_ALERT" and "*Exit criteria to scale*" in post.json["text"]


def test_the_cli_prints_it_and_posts_nothing(capsys):
    from tests.test_cli import Harness

    h = Harness()
    assert h.run("readout") == 0
    out = capsys.readouterr().out
    assert "*Monday readout" in out and "*Exit criteria to scale*" in out
    assert not [r for r in h.transport.requests if r.url.endswith("chat.postMessage")]


def test_signals_value_prints_the_table(capsys):
    from tests.test_cli import Harness

    h = Harness()
    assert h.run("signals", "value") == 0
    assert "reads the database's views" in capsys.readouterr().out  # a MemoryStore has none
    h.store.query_handlers["v_signal_value"] = lambda s, p: [
        signal_row("EAP named", accounts_enrolled=4, accounts_sent=3, accounts_delivered=3, accounts_replied=1,
                   without_delivered=40, without_replied=2),
        signal_row("Layoffs")]
    assert h.run("signals", "value") == 0
    out = capsys.readouterr().out
    assert ("  EAP named (weight 10): 4 enrolled with it, 3 emailed, 1 replied, 0 positive, 0 meetings: too few to "
            "read (under 30 emailed with it)") in out
    assert "  No company enrolled with: Layoffs." in out and "%" not in out


# -- end to end on Postgres: the views behind the readout --------------------------------------------------------------


def test_the_readout_reads_the_views_on_postgres(store):  # noqa: F811
    now = learning_world(store)
    store.guard = Guard()
    ctx = make_context(SETTINGS, store=store, now=now)
    lines, nums = readout.build(ctx)
    out = "\n".join(lines)
    assert nums["views"] is True and nums["so_far"]["accounts_enrolled"] == 70
    # learning_world: 70 emailed 48 or more days ago, 69 delivered and all matured; 12 replied (3 positive); a00
    # booked on day 1 and a02 on day 35, both within 45 days (a01 booked before email 1).
    # Wilson 12 of 69: z²/n = 0.055675, denominator 1.055675; centre (0.173913 + 0.027838) / 1.055675 = 0.191113;
    # half-width 1.96 × √(0.173913 × 0.826087 / 69 + 3.8416 / 19044) / 1.055675 = 1.96 × 0.047790 / 1.055675
    # = 0.088729: 10.2–28.0%.
    assert nums["matured"] == {"emailed": 70, "delivered": 69, "at_28": 69, "at_45": 69}
    assert "replies 17.4% (12 of 69; 95% 10.2–28.0)" in lines[2] and "on track" not in out
    assert (nums["targets"]["positive"]["k"], nums["targets"]["meetings"]["k"]) == (3, 2)
    assert "  Top fit (Priority): 0 sent · 35 emailed so far, 10 replied (28.6%)" in out
    assert "  eap_named: 28.6% of 35 replied, against 5.9% of 34 without · 3 positive, 2 meetings: replies more" in out
    assert "hiring" not in out.split("*Signal value*")[1].split("*Tests*")[0]  # nobody emailed with it
