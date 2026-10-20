"""monday_readout (learn/readout.py; Harry, 6 Oct 2026; rebuilt in W3-1, 20 Oct 2026): last week in a line, the
operating metric, the north star as counts until 30 companies have had 45 days, splits only when every cell has 15,
the A/A sham arm, delivery and capture, the forecast scorecard, the multiplicity line and the decisions waiting. No
verdict words. The rates come from learn/outcomes.per_company (W2-3), so these worlds are events, not view rows; every
expected number is worked out in the test's docstring or comment (Jeffreys intervals from stats.jeffreys, whose values
are pinned in tests/test_stats.py)."""

from __future__ import annotations

import dataclasses
import re
from datetime import UTC, date, datetime, timedelta

from tests.fakes import FakeTransport, make_context
from tests.test_db import db, dsn, learning_world, store  # noqa: F401  (Postgres fixtures, for the end-to-end read)
from tests.test_precision import judgement
from tests.test_registry import SETTINGS, slack_routes
from us_outbound.base import say
from us_outbound.clients.guard import Guard
from us_outbound.clients.slack import dry_run_note
from us_outbound.enrol import queue
from us_outbound.learn import metrics, readout
from us_outbound.ops import cli, health, schedule
from us_outbound.settings.model import Signal

DRY = dry_run_note("#us-outbound") + "\n"

MON = datetime(2026, 10, 12, 7, 30, tzinfo=UTC)  # Monday 08:30 UK (BST)
LAST_WEEK = datetime(2026, 10, 7, 15, tzinfo=UTC)  # Wednesday of the week read
LIVE = dataclasses.replace(SETTINGS, general=dataclasses.replace(SETTINGS.general, live_sending=True,
                                                                  optout_tested=True))
VERDICTS = re.compile(r"\bbelow\b|on track|\braise|needs action", re.IGNORECASE)


def week_row(cut: str, value: str, start: date = date(2026, 10, 5), **numbers) -> dict:
    """A v_readout_weekly row: zeros unless given."""
    cols = (*readout.COHORT, *readout.ACTIVITY)
    return {"week_start": start, "cut": cut, "cut_value": value, **{c: 0 for c in cols}, **numbers}


def world(settings=SETTINGS, *, live=False, now=MON, weekly=None, outcomes=None):
    t = slack_routes(FakeTransport())
    ctx = make_context(settings, live=live, transport=t, now=now, job=readout.JOB)
    if weekly is not None:
        ctx.store.query_handlers["v_readout_weekly"] = lambda s, p: [dict(r) for r in weekly]
    if outcomes is not None:
        ctx.store.insert("events", outcomes)
    return ctx, t


def outcome_rows(n: int, *, days_ago: float, replied: int = 0, positive: int = 0, bounced: int = 0,
                 meeting_day: dict[int, float] | None = None, now: datetime = MON, prefix: str = "o") -> list[dict]:
    """The events of n companies, step 1 sent `days_ago` before now: the first `bounced` bounced; of the rest, the
    first `replied` replied a day after step 1 (inside the 28 days), the first `positive` of those positively (the
    others with an objection); meeting_day: company -> the day after step 1 a meeting was booked."""
    step1 = now - timedelta(days=days_ago)
    rows = []
    for i in range(n):
        aid, cid = f"{prefix}{days_ago:g}-{i}", f"k{prefix}{days_ago:g}-{i}"
        rows.append({"event_id": f"s-{aid}", "type": "sent", "step": 1, "account_id": aid, "contact_id": cid,
                     "occurred_at": step1})
        j = i - bounced  # the index among the delivered
        if i < bounced:
            rows.append({"event_id": f"b-{aid}", "type": "bounced", "step": 1, "account_id": aid, "contact_id": cid,
                         "occurred_at": step1 + timedelta(minutes=5)})
        elif j < replied:
            rows.append({"event_id": f"r-{aid}", "type": "replied", "account_id": aid, "contact_id": cid,
                         "reply_class": "positive" if j < positive else "objection",
                         "occurred_at": step1 + timedelta(days=1)})
        meeting = (meeting_day or {}).get(i)
        if meeting is not None:
            rows.append({"event_id": f"m-{aid}", "type": "meeting_booked", "account_id": aid,
                         "occurred_at": step1 + timedelta(days=meeting)})
    return rows


def ev(i, type_, at=LAST_WEEK, **kw):
    return {"event_id": kw.pop("event_id", f"e{i}"), "type": type_, "occurred_at": at + timedelta(minutes=i), **kw}


def text(ctx) -> str:
    return "\n".join(readout.build(ctx)[0])


def block(lines: list[str], head: str) -> list[str]:
    """The lines of the section whose title starts with head, up to the next blank line."""
    at = next(i for i, x in enumerate(lines) if x.startswith(head))
    end = next((i for i in range(at + 1, len(lines)) if not lines[i]), len(lines))
    return lines[at + 1:end]


# -- the operating metric and the north star --------------------------------------------------------------------------


def test_the_operating_metric_with_its_caveat():
    """Last week (5-11 Oct) 35 companies were sent email 1; 13 more in the three weeks before (8 on 30 Sep, 5 on 18
    Sep, inside the 4 weeks from Mon 14 Sep but not 3 weeks from 21 Sep), 2 of the 18 Sep ones bounced: 48 contacted
    in 4 weeks, 46 delivered. The hand-check judged 38 of 48 rows fine over W38-W41 (the four weeks
    ending with last week). 35 × 38/48 × 46/48 = 35 × 0.79167 × 0.95833 = 26.55 ICP companies reached."""
    rows = (outcome_rows(35, days_ago=5, prefix="w") + outcome_rows(8, days_ago=12, prefix="v")
            + outcome_rows(5, days_ago=24, bounced=2, prefix="u"))
    ctx, _ = world(weekly=[], outcomes=rows)
    weeks = ("2026-W38", "2026-W39", "2026-W40", "2026-W41")
    ctx.store.insert("signal_events", [judgement(i, weeks[i // 12], "fine" if i < 38 else "leave out",
                                                 reason="" if i < 38 else "other") for i in range(48)])
    lines, nums = readout.build(ctx)
    assert lines[2] == ("*Operating metric* (until replies mature): 35 companies contacted last week (of the 150 a "
                        "week allowed) × 79% judged in the ICP (38 of 48 hand-checked, 4 weeks) × 96% delivered (46 of "
                        "48 contacted in 4 weeks) ≈ 26.6 ICP companies reached. The hand-check holds its sampled "
                        "companies until judged, so this overstates by at most about 2 points.")
    assert nums["operating"] == {"contacted": 35, "cap": 150, "fine": 38, "judged": 48, "delivered": 46, "pool": 48,
                                 "value": 26.6}
    # With no row judged there is no product.
    ctx, _ = world(weekly=[], outcomes=rows)
    assert "ICP precision not measured yet" in readout.build(ctx)[0][2]


def test_47_delivered_and_none_matured_prints_counts_and_no_percentage():
    """metrics-audit H2: on 19 Oct every rate read "below" with 0 of 47 windows closed. 47 companies sent email 1
    twelve days ago, none bounced, one replied: counts only, no rate, no verdict."""
    ctx, _ = world(weekly=[], outcomes=outcome_rows(47, days_ago=12, replied=1))
    lines, nums = readout.build(ctx)
    star = block(lines, "*North star*")
    assert star[0] == "  0 of 47 companies have had 45 days, so no rate until 30 have. So far, 0 engaged."
    assert "  0 of 47 have had 28 days (a rate from 30); so far 1 replied, 0 positively." in star
    assert not any("%" in x for x in star)
    assert (nums["north_star"]["matured_45"], nums["north_star"]["contacted"]) == (0, 47)
    # The reply curve (O5): all 47 have reached day 7, none day 14; the reply came on day 1.
    assert "day 7: 1 / 0 of 47." in next(x for x in star if x.startswith("  Reply curve"))


def test_35_matured_at_28_days_with_2_positive_prints_the_rate_with_its_interval():
    """35 companies sent 30 days ago (their 28 days are over): 3 replied, 2 positively; 13 sent 10 days ago, 5
    positive, which do not count (their windows are open). Jeffreys 95%: 2 of 35 → 1.2-17.1% (the plan's 1.6-18.6%
    is the Wilson interval, which outcome rates no longer use); 3 of 35 → 2.5-21.1%. None has had 45 days, so the
    north star is still counts."""
    rows = outcome_rows(35, days_ago=30, replied=3, positive=2) + outcome_rows(13, days_ago=10, replied=5, positive=5)
    ctx, _ = world(weekly=[], outcomes=rows)
    lines, nums = readout.build(ctx)
    star = block(lines, "*North star*")
    assert ("  Within 28 days (the leading indicator): positive 2 of 35 (5.7%, 95% 1.2–17.1%) · any human reply 3 of "
            "35 (8.6%, 95% 2.5–21.1%).") in star
    assert star[0].startswith("  0 of 48 companies have had 45 days, so no rate")
    assert (nums["north_star"]["positive_28"], nums["north_star"]["matured_28"]) == (2, 35)
    assert "14.6%" not in "\n".join(lines)  # 7 of 48 (open windows) is no rate


def test_the_north_star_from_30_companies_that_have_had_45_days_and_bookings_two_ways():
    """30 companies sent 50 days ago: one booked a meeting on day 10, one on day 47 (after the 45 days); 10 sent 40
    days ago are past 28 days but not 45. Engaged within 45 days: 1 of 30 (Jeffreys 0.4-14.5%). Bookings: 2 at any
    time, both with no recorded source, so both "not from an ad"."""
    rows = outcome_rows(30, days_ago=50, meeting_day={0: 10, 1: 47}) + outcome_rows(10, days_ago=40)
    ctx, _ = world(weekly=[], outcomes=rows)
    star = block(readout.build(ctx)[0], "*North star*")
    assert star[0] == "  Engaged within 45 days: 1 of 30 (3.3%, 95% 0.4–14.5%)."
    assert star[1] == ("  Meetings booked: 2 at any time · 2 not from an ad (the booker's HubSpot source is not paid "
                       "search or paid social).")
    # A paid-search booker is no outbound-consistent booking (O1b).
    ctx.store.update("events", {"event_id": "m-o50-0"}, {"analytics_source": "PAID_SEARCH"})
    star = block(readout.build(ctx)[0], "*North star*")
    assert star[1].startswith("  Meetings booked: 2 at any time · 1 not from an ad")
    # One fewer at 45 days (29 of 39): still counts, no rate. Engaged so far: the day-10 booking.
    rows = outcome_rows(29, days_ago=50, meeting_day={0: 10, 1: 47}) + outcome_rows(10, days_ago=40)
    star = block(readout.build(world(weekly=[], outcomes=rows)[0])[0], "*North star*")
    assert star[0] == "  29 of 39 companies have had 45 days, so no rate until 30 have. So far, 1 engaged."


def test_the_stop_rule_line_is_a_count_not_advice():
    rows = outcome_rows(12, days_ago=10, meeting_day={3: 2})
    out = text(world(weekly=[], outcomes=rows)[0])
    assert ("  Stop rule (a brake): 1 meeting from the first 12 companies emailed; enrolment pauses by itself if the "
            "first 1,500 bring fewer than 5.") in out


# -- the headline, last week and the decisions waiting -------------------------------------------------------------


def test_the_headline_counts_the_decisions_waiting_and_the_last_line_lists_them():
    ctx, _ = world(weekly=[])  # optout_tested is no: one decision
    lines, nums = readout.build(ctx)
    assert lines[0] == ":warning: *Monday readout, 5–11 Oct: 1 decision waiting*"
    assert lines[-1] == "*Decisions waiting:* test the unsubscribe link"
    assert nums["decisions_waiting"] == ["test the unsubscribe link"]
    assert ("    *What to do:* `railway ssh -- us-outbound seed send ADDRESS --owner NAME --live` to an inbox of ours, "
            "click the email's unsubscribe link, then `railway ssh -- us-outbound seed check`; at PASS, set "
            f"`optout_tested` to yes on the {say.sheet_link(ctx, 'General')}") in lines
    ctx, _ = world(LIVE, weekly=[])
    lines, nums = readout.build(ctx)
    assert lines[0] == ":bar_chart: *Monday readout, 5–11 Oct: no decision waiting*"
    assert lines[-1] == "*Decisions waiting:* none"


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


def test_no_verdict_word_anywhere_in_a_full_readout():
    """Acceptance (W3-1): the readout has no "below", "on track", "raise" or "Needs action" text, on a world with
    something in every section (the metrics world of tests/test_metrics.py, read the Monday after)."""
    from tests.test_metrics import _populated

    ctx = _populated()
    health.run_job(ctx, metrics.run)
    ctx.job = readout.JOB
    out = "\n".join(readout.build(ctx)[0])
    assert not VERDICTS.search(out), VERDICTS.findall(out)
    for title in ("*Operating metric*", "*North star*", "*Throughput and supply*", "*ICP precision*",
                  "*Industry labels*", "*Delivery and capture*", "*Splits*", "*Tests*", "*Forecasts*"):
        assert title in out, title


# -- splits, the sham arm and the multiplicity line --------------------------------------------------------------------


def _contacts(ctx, ids, **stamp):
    ctx.store.insert("contacts", [{"contact_id": f"k{a}", "account_id": a, **stamp} for a in ids])


def test_a_split_prints_only_when_every_cell_has_15():
    """35 companies matured at 28 days: 20 Legal Teams, 12 Technology & Startups, 3 Marketing & Creative Agencies:
    two cells are under 15, so the group split is "pooled only". The explore arm's table (W3-4: learn/experiments.py)
    has random 20 (2 replied) and ranked 15 (1): both have 15, so O4 prints with Jeffreys intervals: 2 of 20 →
    2.1-28.4%, 1 of 15 → 0.7-27.2% (stats.jeffreys: the Beta(k + ½, n − k + ½) 2.5% and 97.5% quantiles)."""
    rows = outcome_rows(35, days_ago=30, replied=3)
    ctx, _ = world(weekly=[], outcomes=rows)
    ids = [f"o30-{i}" for i in range(35)]
    ctx.store.insert("contacts", [{"contact_id": f"ko30-{i}", "account_id": a,
                                   "industry_group_at_enrol": ("Legal Teams" if i < 20 else "Technology & Startups"
                                                               if i < 32 else "Marketing & Creative Agencies"),
                                   "arm": "random" if i in (0, 1) or 3 <= i < 21 else "ranked"}
                                  for i, a in enumerate(ids)])
    splits = block(readout.build(ctx)[0], "*Splits*")
    assert ("  By industry group: pooled only (matured companies: Legal Teams 20, Marketing & Creative Agencies 3, "
            "Technology & Startups 12; a split prints once every cell has 15).") in splits
    assert ("    O4 any human reply within 28 days: random 10.0% (2 of 20; 2.1–28.4%), ranked 6.7% (1 of 15; "
            "0.7–27.2%).") in splits
    # The size band has one cell (no stamp: unknown): no split to print.
    assert "  By size band: pooled only (matured companies: unknown 35; a split prints once every cell has 15)." in splits
    # The edge: a cell of 14 is under 15 (pooled only), one of 15 is enough.
    assert readout.split_line("arm", {"random": (1, 21), "ranked": (0, 14)}) == (
        "  By arm: pooled only (matured companies: random 21, ranked 14; a split prints once every cell has 15).")
    assert readout.split_line("arm", {"random": (1, 20), "ranked": (0, 15)}).startswith("  By arm: random 1 of 20")


def _sham_world():
    """200 companies s000-s199 sent email 1 30 days ago; queue.sham_arm (sha256 of "sham:" + id + the salt) puts 99
    in a and 101 in b. Six reply positively, four messages each (production's shape): the first three of a (s007,
    s009, s011) and the first three of b (s000, s001, s002)."""
    step1 = MON - timedelta(days=30)
    ids = [f"s{i:03d}" for i in range(200)]
    a = [x for x in ids if queue.sham_arm(x) == "a"]
    b = [x for x in ids if queue.sham_arm(x) == "b"]
    assert (len(a), len(b), a[:3], b[:3]) == (99, 101, ["s007", "s009", "s011"], ["s000", "s001", "s002"])
    rows = [{"event_id": f"s-{x}", "type": "sent", "step": 1, "account_id": x, "contact_id": f"k{x}",
             "occurred_at": step1} for x in ids]
    rows += [{"event_id": f"r-{x}-{m}", "type": "replied", "account_id": x, "contact_id": f"k{x}",
              "reply_class": "positive", "occurred_at": step1 + timedelta(days=1 + m)}
             for x in a[:3] + b[:3] for m in range(4)]
    ctx, _ = world(weekly=[], outcomes=rows)
    ctx.store.insert("contacts", [{"contact_id": f"k{x}", "account_id": x, "sham_arm": queue.sham_arm(x)} for x in ids])
    return ctx


def test_the_sham_arm_differs_by_chance_only_and_a_double_count_is_caught(monkeypatch):
    """Each company once: a 3 of 99, b 3 of 101, difference 3/99 − 3/101 = 0.03030 − 0.02970 = +0.1 points, inside
    its Newcombe interval (it straddles 0). The mutation the plan names, one arm's replies counted per message instead
    of per company: a 12 of 99 against b 3 of 101, a difference of +9.2 points whose interval (Wilson 12/99: 0.0707-
    0.2000; 3/101: 0.0102-0.0837; lower end 0.0915 − √(0.0505² + 0.0540²) = +0.018) excludes 0, so the line says
    "outside chance" and the first assertion would fail."""
    ctx = _sham_world()
    line = next(x for x in readout.build(ctx)[0] if x.startswith("  A/A check"))
    assert "a 3 of 99, b 3 of 101; difference +0.1 points" in line and line.endswith(": within chance.")

    real = readout.sham_counts

    def per_message(m28):
        (ka, na), b = real(m28)
        messages = {str(e["account_id"]) for e in ctx.store.select("events", {"type": "replied"})}
        a_ids = {o.account_id for o in m28 if o.stamp("sham_arm") == "a"}
        return (sum(4 for x in a_ids if x in messages), na), b  # four messages each, counted

    monkeypatch.setattr(readout, "sham_counts", per_message)
    line = next(x for x in readout.build(ctx)[0] if x.startswith("  A/A check"))
    assert "a 12 of 99, b 3 of 101" in line and "outside chance" in line


def test_the_multiplicity_line():
    """RT §3: 40 intervals at 95% → 40 × 0.05 = 2 expected to miss by chance; 7 → 0.35, fewer than one."""
    assert "expect about 2 a week to exclude the pooled rate by chance alone" in readout.multiplicity_line(40)
    assert "expect fewer than one" in readout.multiplicity_line(7)
    ctx = _sham_world()
    lines, nums = readout.build(ctx)
    assert nums["intervals"] == sum(x.count("95% ") for x in lines[:-2])
    assert lines[-2].startswith(f"_{nums['intervals']} intervals at 95% in this readout")


# -- delivery and capture ---------------------------------------------------------------------------------------------


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


def test_bounces_are_counted_per_contact_not_per_send():
    """metrics-audit H4: 2 of 48 contacts bounced at step 1 (4.2%), which per send read about 1.6% once the sequences
    finished. k0 and k1 bounced at step 1 (1 send each), k2 at step 2 (2 sends: not a step-1 bounce, as metrics D1
    counts it), the other 45 got all 4 emails (180): 184 sends, 3 bounced. Wilson 2 of 48: z²/n = 0.080033, centre
    (0.041667 + 0.040017) / 1.080033 = 0.075631, half-width 1.96 × 0.035337 / 1.080033 = 0.064129: 1.2-14.0%."""
    ctx, _ = world(LIVE, weekly=[])
    _contacts_sent(ctx, 48, 3, bounce_step={2: 2}, later_steps=3)
    assert len(ctx.store.select("events", {"type": "sent"})) == 184
    lines, nums = readout.build(ctx)
    assert nums["bounces"] == {"contacts": 48, "bounced": 2, "interval": [0.0115, 0.1398]}
    assert "  Step-1 bounces per contact: 2 of 48 contacts sent email 1 (4.2%; 95% 1.2–14.0%)." in lines


def test_each_source_s_bounces_per_contact_beside_the_per_send_rule():
    """D19: source_bounce_rate stays per send; the readout prints the per-contact figure beside it. 48 apollo contacts
    emailed in the last 7 days, 2 bounced at step 1, 46 with a second email, k2's of which bounced: 94 sends, 3
    bounced (3.2%); per contact at step 1 2 of 48 (4.2%; Wilson 1.2-14.0%, as above): k2's bounce is at step 2."""
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
        if i == 2:
            rows.append(ev(10 * i + 3, "bounced", step=2, at=at + timedelta(days=1, minutes=5), account_id=f"a{i}",
                           contact_id=f"k{i}"))
        contacts.append({"contact_id": f"k{i}", "account_id": f"a{i}", "email_source": "apollo"})
    ctx.store.insert("events", rows)
    ctx.store.insert("contacts", contacts)
    lines, nums = readout.build(ctx)
    assert nums["source_bounces"] == {"apollo": {"contacts": 48, "bounced_contacts": 2, "sends": 94,
                                                 "bounced_sends": 3}}
    assert ("  Emails found by apollo, last 7 days: 2 of 48 contacts sent email 1 bounced (4.2%; 95% 1.2–14.0%) · the "
            "safety pause counts sends: 3 of 94 sends (3.2%), and pauses the source over 3% from 100 sends") in lines


def test_the_checks_say_what_is_not_dealt_with_and_what_to_do():
    ctx, _ = world(weekly=[])  # optout_tested is no
    ctx.store.insert("events", [
        *[ev(i, "sent", step=1, account_id=f"a{i}", contact_id=f"k{i}") for i in range(40)],
        ev(52, "replied", event_id="em-9", account_id="a9", reply_class=None, at=MON - timedelta(hours=3)),
        ev(53, "replied", event_id="em-8", account_id="a8", reply_class="objection"),  # no card
    ])
    ctx.store.insert("contacts", [{"contact_id": "k7", "suppressed": True, "suppressed_reason": "unsubscribe"}])
    ctx.store.insert("heartbeats", [{"run_id": "x", "job": "enrol", "status": "error", "started_at": LAST_WEEK,
                                     "detail": {"violation": True}, "error": "GuardViolation: ..."}])
    lines, nums = readout.build(ctx)
    d = block(lines, "*Delivery and capture*")
    assert "  Opt-outs: the unsubscribe-link test has not passed (`optout_tested` is no); no opt-out so far." in d
    assert ("  1 opt-out not yet in Instantly's blocklist and HubSpot (retried by the next live run of recording email "
            "results or checking for replies).") in d
    at = d.index("  Replies: 1 not sorted (positive, objection, …) after an hour; 1 with no reply card in Slack, "
                 "out-of-office record or opt-out.")
    assert d[at + 1] == "    *What to do:* look at them with `railway ssh -- us-outbound replies list`"
    assert "  Safety-check refusals: 1 last week, in adding people to the email campaign." in d
    assert "  Complaints and inbox placement: not measured (nothing reports them to us yet: D11)." in d
    assert "  Reply capture not checked this week: the reply counts are unverified." in d
    assert nums["decisions_waiting"] == ["test the unsubscribe link", "check 2 replies", "explain 1 refusal"]
    assert lines[0] == ":warning: *Monday readout, 5–11 Oct: 3 decisions waiting*"


def test_a_verified_capture_and_everything_dealt_with():
    ctx, _ = world(LIVE, weekly=[])
    ctx.store.insert("signal_events", [{"event_id": "capture:2026-W42", "account_id": None, "source": "capture",
                                        "fact": "capture_verified", "observed_at": MON - timedelta(hours=1),
                                        "value": {"reasons": []}}])
    d = block(readout.build(ctx)[0], "*Delivery and capture*")
    assert "  Reply capture: this week's check found Instantly's counts and ours the same, and the canary back." in d
    assert "  Opt-outs: the unsubscribe link is tested; no opt-out so far." in d
    assert "  Replies: every one of the 0 so far sorted and routed." in d


# -- last week by cut (the views) -------------------------------------------------------------------------------------


def test_the_cuts_show_last_week_and_so_far_as_counts():
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
    assert "*Last week by cut* (activity · companies emailed so far)" in out
    angle = out.index("_By angle_ (the pitch of the copy)")
    assert out[angle + 1] == "  Upgrade the EAP: 30 sent, 2 replies (1 positive), 1 meeting, 1 bounce · 20 emailed so " \
                             "far, 3 replied"
    assert out[angle + 2] == "  General: 10 sent, 1 reply · 40 emailed so far, 2 replied"  # no rate on open windows
    step = out.index("_By step_ (which email of the sequence)")
    assert out[step + 1:step + 3] == ["  step 1: 25 sent, 2 replies, 1 bounce", "  step 2: 15 sent, 1 reply"]


def test_without_the_views_the_rest_still_posts():
    """A MemoryStore with no view handlers: the cuts need v_readout_weekly, and say nothing; the north star comes from
    the events (learn/outcomes.py, W2-3), so it reads on any store."""
    ctx, _ = world(outcomes=outcome_rows(3, days_ago=10, replied=1))
    lines, nums = readout.build(ctx)
    out = "\n".join(lines)
    assert nums["views"] is False and "*Last week by cut*" not in out
    assert block(lines, "*North star*")[0].startswith("  0 of 3 companies have had 45 days")
    assert "*Delivery and capture*" in out and "*Tests*" in out


# -- the forecasts ---------------------------------------------------------------------------------------------------


def test_the_forecast_scorecard_and_its_flag():
    """tests/test_metrics.py's forecast world: forecast:contacted for W46 was 85.75 (69-103) and W46 contacted 50, the
    third week running outside its interval: the scorecard line and the flag."""
    from tests.test_metrics import _monday, _week

    ctx, _ = world(weekly=[], now=_monday(43))
    ctx.store.insert("events", _week(39, 120) + _week(40, 130) + _week(41, 125) + _week(42, 125))
    for n, count in ((43, 118), (44, 50), (45, 50), (46, 50)):
        health.run_job(make_context(SETTINGS, now=_monday(n), job=metrics.JOB, store=ctx.store), metrics.run)
        ctx.store.insert("events", _week(n, count))
    rctx, _ = world(weekly=[], now=_monday(47))
    rctx.store = ctx.store
    rctx.store.guard = rctx.guard
    f = block(readout.build(rctx)[0], "*Forecasts*")
    assert "  companies contacted: forecast 85.8 (69–103), came to 50: outside its interval." in f
    assert ("  Outside its interval 3 weeks running: companies contacted. Check its definition or the forecast "
            "(loop L13).") in f
    ctx, _ = world(weekly=[])
    assert block(readout.build(ctx)[0], "*Forecasts*") == [
        "  No forecast was made for last week (metrics_daily makes them each Monday)."]


# -- the signal table and the tests ---------------------------------------------------------------------------------


def signals_world(settings=SETTINGS):
    """40 companies emailed 30 days ago, all delivered: g0–g19 showed "EAP named" at enrolment and g0–g3 replied;
    g20–g39 did not, and g20 replied; g0–g2 also showed "New People leader". The odd ones were drawn at random."""
    s = dataclasses.replace(settings, signals=(
        Signal("EAP named", ("careers_pages",), "eap", 10, "Score", 90, True),
        Signal("New People leader", ("apollo_people",), "people", 30, "Score", 90, True),
        Signal("Layoffs", ("layoffs",), "layoffs", -20, "Suppress", 90, True)))
    ctx, t = world(s, weekly=[])
    events, contacts = [], []
    step1 = MON - timedelta(days=30)
    for i in range(40):
        aid, cid = f"g{i}", f"kg{i}"
        eap = i < 20
        events.append({"event_id": f"s-{aid}", "type": "sent", "step": 1, "account_id": aid, "contact_id": cid,
                       "occurred_at": step1})
        if (eap and i < 4) or i == 20:
            events.append({"event_id": f"r-{aid}", "type": "replied", "account_id": aid, "reply_class": "objection",
                           "occurred_at": step1 + timedelta(days=2)})
        snap = [{"signal": "EAP named"}] if eap else []
        if i < 3:
            snap.append({"signal": "New People leader"})
        contacts.append({"contact_id": cid, "account_id": aid, "signals_at_enrol": snap,
                         "arm": "random" if i % 2 else "ranked"})
    ctx.store.insert("events", events)
    ctx.store.insert("contacts", contacts)
    return ctx, t


SIGNALS_HEAD = "*Signals* (monitoring: replies with each buying signal at enrolment against without, with 95% intervals)"


def test_the_signals_are_counts_with_intervals_and_too_few_is_said():
    """EAP named: with it 4 of 20, without 1 of 20; Jeffreys 95%: 4 of 20 → 7.2–40.8%, 1 of 20 → 0.5–21.1%. New
    People leader: 3 with it, under 15 a side: too few. Layoffs is a Suppress row, not a score: not shown. No p, no
    verdict; the only decision waiting is the five replies with no reply card (this world has none)."""
    ctx, _ = signals_world(LIVE)
    lines, nums = readout.build(ctx)
    body = block(lines, SIGNALS_HEAD)
    assert "    EAP named (weight 10): 4 of 20 (20.0%; 95% 7.2–40.8%) vs 1 of 20 (5.0%; 95% 0.5–21.1%)" in body
    assert "  Too few to show (under 15 emailed companies with or without it): New People leader (3)." in body
    assert not any("Layoffs" in x or " p = " in x for x in body)
    assert nums["decisions_waiting"] == ["check 5 replies"]
    assert "signals_readable" not in nums and "tests_at_a_look" not in nums  # removed in W3-1


def test_with_nothing_emailed_the_signals_section_says_so():
    ctx, _ = world(SETTINGS, weekly=[])
    assert block(readout.build(ctx)[0], SIGNALS_HEAD) == ["  No company has been emailed yet, so there is nothing to "
                                                          "review."]


def test_a_section_that_fails_never_stops_the_readout(monkeypatch):
    """The signal review and the forecasts are monitoring: if one raises, its section says it could not be built and
    the rest of the readout is built as usual."""
    from us_outbound.learn import signal_review

    def broken(*_a, **_k):
        raise KeyError("signals_at_enrol")

    monkeypatch.setattr(signal_review, "review", broken)
    monkeypatch.setattr(metrics, "scorecard", broken)
    ctx, _ = signals_world(LIVE)
    lines, nums = readout.build(ctx)
    assert block(lines, SIGNALS_HEAD) == ["  could not be built (KeyError); the rest of the readout is complete."]
    assert block(lines, "*Forecasts*") == ["  could not be built (KeyError); the rest of the readout is complete."]
    assert any(x.startswith("*Tests*") for x in lines) and lines[-1].startswith("*Decisions waiting:*")


def test_a_test_at_a_look_is_a_decision_waiting():
    from us_outbound.settings.model import Test

    test = Test("t1", "h", "eap-v1", "general-v1", 400, "running", date(2026, 8, 1), date(2026, 10, 8), "reply rate")
    ctx, _ = world(dataclasses.replace(SETTINGS, tests=(test,)), weekly=[])
    ctx.store.insert("contacts", [{"contact_id": f"k{i}", "account_id": f"a{i}", "test_id": "t1",
                                   "copy_version": "eap-v1" if i % 2 else "general-v1"} for i in range(4)])
    ctx.store.insert("events", [ev(i, "sent", step=1, account_id=f"a{i}", contact_id=f"k{i}",
                                   at=datetime(2026, 9, 1, tzinfo=UTC)) for i in range(4)])
    lines, nums = readout.build(ctx)
    assert "decide on test t1" in nums["decisions_waiting"]
    # One look (the read date): its threshold is the whole alpha, 0.10; Fisher on 0 of 2 against 0 of 2 is 1; with
    # no reply either side the Beta posteriors are the same, so P(general-v1 better) = 0.5.
    at = lines.index("  t1 (A/B copy test) reached look 1 (the read date): Thu 08 Oct 2026: eap-v1 0 of 2 replied "
                     "(0.0%); general-v1 0 of 2 replied (0.0%) · Fisher p = 1.000 (this look's threshold 0.100) · "
                     "P(general-v1 better) = 0.50")
    assert lines[at + 1].startswith('    *What to do:* apply its decision rule ("reply rate")')


# -- the job: schedule, heartbeat, cli, the post -------------------------------------------------------------------------


def test_registered_on_the_schedule_heartbeat_and_cli():
    job = schedule.by_name()["monday_readout"]
    assert (job.cron, job.live, job.enabled) == ("30 8 * * 1", True, True)
    assert cli.JOBS["monday_readout"] == "us_outbound.learn.readout:run" and "monday_readout" in health.scheduled_jobs()
    assert health.EXPECTED["monday_readout"] == 8 * 24 * 60


def test_dry_run_posts_to_the_dev_channel_and_writes_only_its_heartbeat():
    ctx, t = world(weekly=[])
    before = {k: list(v) for k, v in ctx.store.tables.items()}
    summary = health.run_job(ctx, readout.run)
    [post] = [r for r in t.requests if r.url.endswith("chat.postMessage")]
    assert post.json["channel"] == "C_DEV" and post.json["text"].startswith(DRY + ":warning: *Monday readout")
    assert summary["dry_run"] and summary["alert"]["posted"]
    after = {k: v for k, v in ctx.store.tables.items() if v != before[k]}
    assert set(after) == {"heartbeats"}


def test_live_posts_to_the_alert_channel():
    ctx, t = world(LIVE, live=True, weekly=[])
    readout.run(ctx)
    [post] = [r for r in t.requests if r.url.endswith("chat.postMessage")]
    assert post.json["channel"] == "C_ALERT" and "*Delivery and capture*" in post.json["text"]


def test_the_cli_prints_it_and_posts_nothing(capsys):
    from tests.test_cli import Harness

    h = Harness()
    assert h.run("report", "readout") == 0
    out = capsys.readouterr().out
    assert "*Monday readout" in out and "*North star*" in out
    assert not [r for r in h.transport.requests if r.url.endswith("chat.postMessage")]


# -- end to end on Postgres: the views behind the readout --------------------------------------------------------------


def test_the_readout_reads_the_views_on_postgres(store):  # noqa: F811
    now = learning_world(store)
    store.guard = Guard()
    signals = (Signal("eap_named", ("careers_pages",), "eap", 10, "Score", 90, True),
               Signal("hiring", ("apollo_org",), "hiring", 15, "Score", 90, True))
    ctx = make_context(dataclasses.replace(SETTINGS, signals=signals), store=store, now=now)
    lines, nums = readout.build(ctx)
    out = "\n".join(lines)
    assert nums["views"] is True and nums["so_far"]["accounts_enrolled"] == 70
    # learning_world: 70 emailed 48 or more days ago (a69 bounced, kept in), all past 45 days. Engaged: a00-a02
    # replied positively, a00 and a02 also booked within 45 days (a01 booked before email 1): 3 of 70, Jeffreys
    # 1.2-11.0%. Within 28 days 12 replied, 3 positively, of 70: Jeffreys 9.7-27.2% and 1.2-11.0%.
    star = block(lines, "*North star*")
    assert star[0] == "  Engaged within 45 days: 3 of 70 (4.3%, 95% 1.2–11.0%)."
    assert ("  Within 28 days (the leading indicator): positive 3 of 70 (4.3%, 95% 1.2–11.0%) · any human reply 12 of "
            "70 (17.1%, 95% 9.7–27.2%).") in star
    assert nums["north_star"]["meeting_any"] == 2
    # The explore arm cut of last week's activity (W2-2): random is a00 to a34, ranked (NULL arm) a35 to a69.
    assert "  random: 0 sent · 35 emailed so far, 10 replied" in out
    assert "  ranked: 0 sent · 35 emailed so far, 2 replied" in out
    signals_block = out.split("*Signals*")[1].split("*Forecasts*")[0]
    assert "    eap_named (weight 10): 10 of 35 (28.6%; 95% 15.7–44.8%) vs 2 of 34 (5.9%; 95% 1.2–17.6%)" in signals_block
    assert not VERDICTS.search(out)
