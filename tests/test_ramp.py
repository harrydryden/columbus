"""The sending ramp (registry/ramp.py; Harry, 1 Oct 2026): 10 a day in a mailbox's first sending
week, 20 in its second, then its cap, in the forecast, the campaigns, Instantly's limits and status."""

from __future__ import annotations

import dataclasses
from datetime import UTC, date, datetime, timedelta

from tests.fakes import make_context
from tests.test_render import HANNAH, HARRY_M, HARRY_T, SAM, make_settings
from us_outbound import limits
from us_outbound.enrol import capacity
from us_outbound.registry import ramp

TUE = date(2026, 10, 27)
NOW = datetime(2026, 10, 27, 11, 0, tzinfo=UTC)  # 07:00 ET
S = make_settings()


def past_ramp(store, mailboxes, now: datetime = NOW, days: int = 45) -> None:
    """Give each mailbox a first send `days` ago, so the ramp is behind it (for tests about other things)."""
    store.insert("events", [
        {"event_id": f"ramp-{m.address}", "type": "sent", "mailbox": m.address.lower(), "step": 1,
         "occurred_at": now - timedelta(days=days)}
        for m in mailboxes
    ])


def sent(store, address: str, when: datetime, event_id: str | None = None) -> None:
    store.insert("events", [{"event_id": event_id or f"s-{address}-{when.isoformat()}", "type": "sent",
                             "mailbox": address, "step": 1, "occurred_at": when}])


def ramps_for(ctx, today: date = TUE) -> dict[str, ramp.Ramp]:
    return ramp.ramps(ctx.store, ctx.settings, today)


# -- where a mailbox is on the ramp ---------------------------------------------------------------------


def test_a_mailbox_that_has_not_sent_starts_at_ten():
    ctx = make_context(S)
    r = ramps_for(ctx)[HANNAH.address]
    assert (r.week, r.cap, r.ramping, r.start, r.basis) == (1, 10, True, None, ramp.NOT_STARTED)
    assert r.describe() == "ramp week 1 (no send yet): 10 a day of its 30; 20 from a week after its first send"


def test_the_weeks_count_from_the_first_send():
    ctx = make_context(S)
    sent(ctx.store, HANNAH.address, datetime(2026, 10, 26, 15, tzinfo=UTC))  # Mon 26 Oct
    sent(ctx.store, HANNAH.address, datetime(2026, 10, 27, 14, tzinfo=UTC))
    sent(ctx.store, SAM.address, datetime(2026, 10, 19, 15, tzinfo=UTC))  # Mon 19 Oct: second week
    sent(ctx.store, HARRY_M.address, datetime(2026, 10, 12, 15, tzinfo=UTC))  # Mon 12 Oct: done
    r = ramps_for(ctx)
    hannah, sam, harry = r[HANNAH.address], r[SAM.address], r[HARRY_M.address]
    assert (hannah.week, hannah.cap, hannah.start, hannah.basis) == (1, 10, date(2026, 10, 26), ramp.FIRST_SEND)
    assert hannah.next_step() == (date(2026, 11, 2), 20)
    assert hannah.describe() == "ramp week 1 (first send Mon 26 Oct): 10 a day of its 30; 20 from Mon 02 Nov"
    assert (sam.week, sam.cap) == (2, 20)
    assert sam.describe() == "ramp week 2 (first send Mon 19 Oct): 20 a day of its 30; 30 from Mon 02 Nov"
    assert (harry.week, harry.cap, harry.ramping, harry.describe()) == (3, 30, False, "")
    # Sends are on record, and none came from harry@tryspill.org: it has not started, whatever else is known.
    assert (r[HARRY_T.address].cap, r[HARRY_T.address].basis) == (10, ramp.NOT_STARTED)


def test_the_sheet_cap_wins_when_it_is_lower():
    s = make_settings(mailboxes=(dataclasses.replace(HANNAH, daily_cap=5),))
    r = ramp.ramps(make_context(s).store, s, TUE)[HANNAH.address]
    assert (r.cap, r.ramping, r.describe()) == (5, False, "")


def enrolled(store, owner: str, when: datetime, i: int = 1) -> None:
    store.insert("contacts", [{"contact_id": f"c-{owner}-{i}", "account_id": f"a-{owner}-{i}",
                               "instantly_campaign": f"US Outbound – {owner}", "enrolled_at": when}])


def promoted(store, address: str, when: datetime, *, job: str = "mailbox_health", dry_run: bool = False) -> None:
    detail = {"dry_run": dry_run, "promoted": [address]} if job == "mailbox_health" else \
        {"dry_run": dry_run, "address": address, "status": "Active"}
    store.insert("heartbeats", [{"run_id": f"{job}-{address}-{when.isoformat()}", "job": job, "status": "ok",
                                 "dry_run": dry_run, "started_at": when, "detail": detail}])


def test_with_no_send_recorded_the_promotion_counts_from_the_owner_s_first_enrolment():
    """sync_outcomes is not feeding the events table yet: the promotion, but never before a lead went in."""
    ctx = make_context(S)
    promoted(ctx.store, HANNAH.address, datetime(2026, 9, 30, 7, tzinfo=UTC))
    enrolled(ctx.store, "Hannah Spalding", datetime(2026, 10, 19, 11, tzinfo=UTC))
    promoted(ctx.store, HARRY_T.address, datetime(2026, 10, 22, 7, tzinfo=UTC), job="mailbox_add")
    enrolled(ctx.store, "Harry Dryden", datetime(2026, 10, 5, 11, tzinfo=UTC))
    promoted(ctx.store, SAM.address, datetime(2026, 10, 1, 7, tzinfo=UTC), dry_run=True)  # changed nothing
    enrolled(ctx.store, "Sam Jackson", datetime(2026, 10, 20, 11, tzinfo=UTC))
    r = ramps_for(ctx)
    # Promoted 30 Sep, but nothing went out before the first lead on 19 Oct.
    assert (r[HANNAH.address].start, r[HANNAH.address].basis, r[HANNAH.address].cap) == (
        date(2026, 10, 19), ramp.PROMOTION, 20)
    # A new mailbox joins a campaign that has run since 5 Oct: its own promotion counts.
    assert (r[HARRY_T.address].start, r[HARRY_T.address].cap) == (date(2026, 10, 22), 10)
    # No promotion on record (Active by hand on the sheet): the owner's first enrolment.
    assert (r[HARRY_M.address].start, r[HARRY_M.address].basis, r[HARRY_M.address].cap) == (
        date(2026, 10, 5), ramp.FIRST_ENROLMENT, 30)
    assert (r[SAM.address].start, r[SAM.address].basis) == (date(2026, 10, 20), ramp.FIRST_ENROLMENT)


def test_a_mailbox_promoted_long_ago_that_never_sent_still_starts_at_ten():
    ctx = make_context(S)
    promoted(ctx.store, HANNAH.address, datetime(2026, 9, 1, 7, tzinfo=UTC))
    r = ramps_for(ctx)[HANNAH.address]
    assert (r.cap, r.basis) == (10, ramp.NOT_STARTED)  # no lead was ever enrolled for Hannah


def test_retired_mailboxes_are_left_out():
    s = make_settings(mailboxes=(HANNAH, dataclasses.replace(SAM, status="Retired")))
    assert set(ramp.ramps(make_context(s).store, s, TUE)) == {HANNAH.address}


# -- the forecast and what limits today's number ----------------------------------------------------


def test_the_forecast_and_the_limiter_follow_the_ramp():
    ctx = make_context(S, now=NOW)
    senders = capacity.sending_capacity(ctx.store, S, TUE)
    hannah, harry = senders["Hannah Spalding"], senders["Harry Dryden"]
    assert (hannah.cap, hannah.pace, hannah.free) == (10, 3, 3)
    assert (harry.cap, harry.free) == (20, 5)  # two new mailboxes at 10 each
    assert "hannah@meetspill.org is on its ramp week 1 (no send yet): 10 a day of its 30" in hannah.describe()
    lim = limits.today(ctx, TUE, ready_accounts=100)
    assert lim.number == 11 and lim.terms["binding"] == "sending_capacity"
    assert any(line.startswith("Hannah Spalding is on the sending ramp (hannah@meetspill.org ramp week 1")
               for line in lim.detail), lim.detail
    assert not any("Add a mailbox" in line for line in lim.detail)


def test_past_the_ramp_the_forecast_uses_the_sheet_caps():
    ctx = make_context(S, now=NOW)
    past_ramp(ctx.store, S.mailboxes)
    senders = capacity.sending_capacity(ctx.store, S, TUE)
    assert {o: c.cap for o, c in senders.items()} == {"Hannah Spalding": 30, "Harry Dryden": 60, "Sam Jackson": 30}
    assert not any(c.ramping for c in senders.values())


def test_instantly_s_lower_limit_during_the_ramp_says_so():
    ctx = make_context(S, now=NOW)
    sent(ctx.store, HANNAH.address, datetime(2026, 10, 19, 15, tzinfo=UTC))  # week 2: 20 allowed
    ctx.store.insert("heartbeats", [{"run_id": "mh", "job": "mailbox_health", "status": "ok",
                                     "started_at": datetime(2026, 10, 27, 6, tzinfo=UTC),
                                     "detail": {"instantly_daily_limits": {HANNAH.address: 10}}}])
    hannah = capacity.sending_capacity(ctx.store, S, TUE)["Hannah Spalding"]
    assert hannah.cap == 10
    assert "Instantly limits hannah@meetspill.org to 10 a day (ramp: 20)" in hannah.describe()


# -- the campaigns and Instantly's own limits (registry/mailboxes.py) ----------------------------------------


def test_new_campaigns_take_the_ramp_s_daily_limit():
    from tests.test_registry import C_HANNAH, C_HARRY, setup
    from us_outbound.registry import mailboxes as reg

    ctx, t, inst, _ = setup(ramp_done=False)
    reg.ensure_campaigns(ctx)
    assert inst.by_name(C_HANNAH)["daily_limit"] == 10
    assert inst.by_name(C_HARRY)["daily_limit"] == 20  # two new mailboxes at 10 each


def test_the_drift_fix_moves_a_campaign_to_the_ramp_and_on_as_the_ramp_moves():
    from tests.test_registry import C_HANNAH, C_HARRY, C_SAM, HARRY, HARRY2, setup
    from tests.test_registry import HANNAH as HANNAH_ADDRESS
    from tests.test_registry import NOW as REG_NOW
    from tests.test_registry import SAM as SAM_ADDRESS
    from us_outbound.registry import mailboxes as reg

    ctx, t, inst, _ = setup(ramp_done=False)
    inst.standard(C_HANNAH, [HANNAH_ADDRESS], 30)
    inst.standard(C_SAM, [SAM_ADDRESS], 30)
    inst.standard(C_HARRY, [HARRY, HARRY2], 60)
    out = reg.ensure_campaigns(ctx)
    assert out["drift"][C_HANNAH] == {"daily_limit": [10, 30]} and out["fixed"] == []
    reg.ensure_campaigns(ctx, fix=True)
    assert [inst.by_name(c)["daily_limit"] for c in (C_HANNAH, C_SAM, C_HARRY)] == [10, 10, 20]
    # Eight days after Hannah's first send she is in her second week: the check reports 20, the fix sets it.
    sent(ctx.store, HANNAH_ADDRESS, REG_NOW - timedelta(days=8))
    out = reg.ensure_campaigns(ctx, fix=True)
    assert out["drift"] == {C_HANNAH: {"daily_limit": [20, 10]}} and inst.by_name(C_HANNAH)["daily_limit"] == 20


def test_mailbox_health_sets_each_instantly_account_to_its_ramp_cap():
    from tests.test_registry import HANNAH as HANNAH_ADDRESS
    from tests.test_registry import setup
    from us_outbound.registry import mailboxes as reg

    ctx, t, inst, _ = setup(ramp_done=False)
    out = reg.mailbox_health(ctx)
    assert out["limit_set"][HANNAH_ADDRESS] == {"from": 30, "to": 10}
    assert inst.accounts[HANNAH_ADDRESS]["daily_limit"] == 10
    assert out["ramp"][HANNAH_ADDRESS] == {"cap": 10, "sheet_cap": 30, "week": 1, "ramping": True, "start": None,
                                           "basis": ""}
    [post] = [r for r in t.requests if r.url.endswith("chat.postMessage")]
    assert ("hannah@meetspill.org (Hannah Spalding): Active, warmup on, score 98, ramp week 1 (no send yet)"
            in post.json["text"])
    assert "Set hannah@meetspill.org's Instantly daily limit from 30 to 10" in post.json["text"]


def test_status_shows_the_ramp(capsys):
    from tests.test_cli import Harness
    from tests.test_registry import HANNAH as HANNAH_ADDRESS

    h = Harness()
    sent(h.store, HANNAH_ADDRESS, datetime(2026, 10, 26, 15, tzinfo=UTC))
    assert h.run("status") == 0
    out = capsys.readouterr().out
    assert ("hannah@meetspill.org         Hannah Spalding    Active   cap 30; today 10: ramp week 1 "
            "(first send Mon 26 Oct): 10 a day of its 30; 20 from Mon 02 Nov") in out
