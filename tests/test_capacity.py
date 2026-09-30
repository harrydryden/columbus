"""Sending capacity (enrol/capacity.py): the send days of each step, follow-ups already due, and the free slots."""

from __future__ import annotations

import dataclasses
from collections import Counter
from datetime import UTC, date, datetime, timedelta

from tests.fakes import make_context
from tests.test_render import HANNAH, HARRY_M, HARRY_T, SAM, make_settings
from us_outbound.context import ET
from us_outbound.enrol import capacity

S = make_settings()
TUE = date(2026, 10, 27)


def at_noon_uk(d: date) -> datetime:
    return datetime(d.year, d.month, d.day, 11, 0, tzinfo=UTC)  # 07:00 ET, when enrol runs


def test_steps_fall_on_days_0_3_8_and_15():
    assert capacity.step_days(TUE, S) == [date(2026, 10, 27), date(2026, 10, 30), date(2026, 11, 4), date(2026, 11, 11)]


def test_a_step_due_at_the_weekend_goes_on_monday_and_the_next_waits_from_there():
    assert capacity.step_days(date(2026, 10, 29), S) == [
        date(2026, 10, 29), date(2026, 11, 2), date(2026, 11, 9), date(2026, 11, 16)]


def test_a_step_due_in_a_blackout_waits_for_the_first_send_day_after_it():
    # Thu 19 Nov: step 2 falls on Sun 22, and 23–27 Nov is the Thanksgiving blackout.
    assert capacity.step_days(date(2026, 11, 19), S) == [
        date(2026, 11, 19), date(2026, 11, 30), date(2026, 12, 7), date(2026, 12, 14)]


def lead(i: int, owner: str, enrolled: date) -> dict:
    return {"contact_id": f"c{i}", "account_id": f"a{i}", "instantly_campaign": f"US Outbound – {owner}",
            "enrolled_at": at_noon_uk(enrolled)}


def test_follow_ups_already_due_take_slots_on_the_days_a_new_lead_would_use():
    # Leads enrolled Tue 20 Oct send step 3 on Wed 4 Nov, the day a lead enrolled today sends step 3.
    leads = [lead(i, "Hannah Spalding", date(2026, 10, 20)) for i in range(25)]
    committed = capacity.committed_steps(leads, set(), S, TUE)
    assert committed[("Hannah Spalding", date(2026, 11, 4))] == 25
    free = capacity.free_slots(S, committed, capacity.mailbox_caps(S, {}), TUE)
    hannah = free["Hannah Spalding"]
    assert (hannah.cap, hannah.free, hannah.tightest_day, hannah.held_that_day) == (30, 5, date(2026, 11, 4), 25)
    assert hannah.describe() == "Hannah Spalding: 5 new leads today, 30 sends a day (25 follow-ups already due on Wed 04 Nov)"
    # With nothing due, the steady pace binds: cap ÷ 4, rounded up; Harry has two mailboxes.
    assert (free["Harry Dryden"].free, free["Sam Jackson"].free) == (15, 8)
    assert "8 new a day keeps 30 sends a day steady" in free["Sam Jackson"].describe()


def test_stopped_leads_hold_nothing():
    leads = [lead(i, "Hannah Spalding", date(2026, 10, 20)) for i in range(25)]
    committed = capacity.committed_steps(leads, {"c0", "c1"}, S, TUE)
    assert committed[("Hannah Spalding", date(2026, 11, 4))] == 23


def test_stopped_contacts_come_from_stop_events_and_accounts_no_longer_enrolled():
    ctx = make_context(S)
    ctx.store.insert("accounts", [{"account_id": "a1", "status": "enrolled"}, {"account_id": "a2", "status": "engaged"}])
    ctx.store.insert("contacts", [
        {"contact_id": "c1", "account_id": "a1", "enrolled_at": at_noon_uk(TUE)},
        {"contact_id": "c2", "account_id": "a2", "enrolled_at": at_noon_uk(TUE)},  # someone at the account replied
        {"contact_id": "c3", "account_id": "a1", "enrolled_at": at_noon_uk(TUE)},
    ])
    ctx.store.insert("events", [{"event_id": "e1", "contact_id": "c3", "type": "bounced"}])
    assert capacity.stopped_contacts(ctx.store) == {"c2", "c3"}


def test_instantly_s_own_lower_limit_caps_a_mailbox():
    caps = capacity.mailbox_caps(S, {"hannah@meetspill.org": 20, "sam@meetspill.org": 50})
    by = {m.address: m for m in caps}
    assert by["hannah@meetspill.org"].cap == 20  # Instantly's 20 is lower than the sheet's 30
    assert by["sam@meetspill.org"].cap == 30  # the sheet's cap wins when it is lower
    free = capacity.free_slots(S, Counter(), caps, TUE)
    assert free["Hannah Spalding"].cap == 20 and free["Hannah Spalding"].free == 5
    assert "Instantly limits hannah@meetspill.org to 20 a day (sheet: 30)" in free["Hannah Spalding"].describe()


def test_instantly_limits_come_from_the_latest_mailbox_health_run():
    ctx = make_context(S)
    ctx.store.insert("heartbeats", [
        {"run_id": "h1", "job": "mailbox_health", "status": "ok", "started_at": datetime(2026, 10, 26, 7, tzinfo=UTC),
         "detail": {"instantly_daily_limits": {"hannah@meetspill.org": 10}}},
        {"run_id": "h2", "job": "mailbox_health", "status": "ok", "started_at": datetime(2026, 10, 27, 7, tzinfo=UTC),
         "detail": {"instantly_daily_limits": {"Hannah@Meetspill.org": 25}}},
        {"run_id": "h3", "job": "mailbox_health", "status": "error", "started_at": datetime(2026, 10, 27, 8, tzinfo=UTC),
         "detail": {"instantly_daily_limits": {"hannah@meetspill.org": 1}}},
    ])
    assert capacity.instantly_limits(ctx.store) == {"hannah@meetspill.org": 25}


def test_paused_and_warming_mailboxes_give_no_capacity():
    s = make_settings(mailboxes=(HANNAH, dataclasses.replace(SAM, status="Warming"), HARRY_M,
                                 dataclasses.replace(HARRY_T, status="Paused")))
    free = capacity.free_slots(s, Counter(), capacity.mailbox_caps(s, {}), TUE)
    assert {o: c.cap for o, c in free.items()} == {"Hannah Spalding": 30, "Harry Dryden": 30}


def simulate(weeks: int = 12) -> tuple[list[int], Counter]:
    """Enrol as many as capacity allows each send day for one 30-a-day inbox; (new per send day, sends per day)."""
    s = make_settings(mailboxes=(HANNAH,), blackout_dates=())
    caps = capacity.mailbox_caps(s, {})
    leads: list[dict] = []
    per_day: list[int] = []
    day = date(2027, 1, 4)
    for _ in range(weeks * 7):
        if capacity.next_send_day(day, s) == day:
            committed = capacity.committed_steps(leads, set(), s, day)
            n = capacity.free_slots(s, committed, caps, day)["Hannah Spalding"].free
            leads += [lead(len(leads) + i, "Hannah Spalding", day) for i in range(n)]
            per_day.append(n)
        day += timedelta(days=1)
    return per_day, Counter(d for c in leads for d in capacity.step_days(c["enrolled_at"].astimezone(ET).date(), s))


def test_the_campaign_steps_and_the_forecast_share_one_cadence():
    from us_outbound.clients.instantly import STEP_DAYS

    assert STEP_DAYS == (0, 3, 8, 15) and capacity.STEP_DELAYS == (0, 3, 5, 7)


def test_no_day_is_ever_overfilled():
    per_day, sends = simulate()
    assert max(sends.values()) <= 30
    assert per_day[0] == 8  # the steady pace, even into an empty campaign


def test_with_steps_on_days_0_3_8_15_weekend_steps_pile_onto_mondays():
    """Instantly counts delays in calendar days: most later steps of leads enrolled Wednesday to
    Friday fall at the weekend and go on Monday, so Mondays fill first and hold the week back."""
    per_day, sends = simulate()
    by_weekday = Counter()
    for d, n in sends.items():
        by_weekday[d.weekday()] += n
    assert by_weekday[0] == max(by_weekday.values())  # Monday carries the most sends
    assert sum(per_day[-20:]) / 4 < 20  # well under the 37.5 a week one inbox could take


def test_a_weekly_cadence_fills_the_inbox(monkeypatch):
    monkeypatch.setattr(capacity, "STEP_DELAYS", (0, 7, 7, 7))  # steps on days 0, 7, 14, 21: always the same weekday
    per_day, sends = simulate()
    assert max(sends.values()) <= 30
    assert sum(per_day[-20:]) / 4 == 37.5  # 30 sends a day, 5 days, 4 steps each
