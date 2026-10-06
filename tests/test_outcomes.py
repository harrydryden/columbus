"""sync_outcomes: Instantly's sends, replies, bounces and unsubscribes into events, idempotently (SPEC 6, 11, 13)."""

from __future__ import annotations

from datetime import timedelta

import pytest

from tests.fakes_replies import BOB, HANNAH, HARRY, JANE, NOW, make_world
from us_outbound.clients.instantly import LEAD_BOUNCED, LEAD_UNSUBSCRIBED
from us_outbound.enrol.capacity import STOP_EVENTS, stopped_contacts
from us_outbound.replies import outcomes


@pytest.fixture
def world(default_settings):
    w = make_world(default_settings)
    w.ctx.job = "sync_outcomes"
    return w


def run(world):
    return outcomes.run(world.ctx)


def test_the_event_names_are_the_ones_capacity_reads():
    from us_outbound.replies import account_stop

    # lead_stopped: the account-level stop of a colleague's lead (Harry, 6 Oct 2026).
    assert set(STOP_EVENTS) == {outcomes.REPLIED, outcomes.BOUNCED, outcomes.UNSUBSCRIBED, account_stop.STOPPED}
    assert outcomes.SENT == "sent"


# -- sent ----------------------------------------------------------------------------------------------


def test_sends_become_events_numbered_by_step_with_their_mailbox(world):
    world.sent("S2", at=NOW - timedelta(days=3))
    world.sent("S1", at=NOW - timedelta(days=10))
    world.sent("S9", BOB, frm="harry@tryspill.org", campaign="cmp-harry", at=NOW - timedelta(days=8))
    out = run(world)
    sent = {e["event_id"]: e for e in world.events("sent")}
    assert (sent["S1"]["step"], sent["S2"]["step"], sent["S9"]["step"]) == (1, 2, 1)
    assert sent["S1"]["mailbox"] == HANNAH and sent["S9"]["mailbox"] == "harry@tryspill.org"
    assert sent["S1"]["contact_id"] == "k-jane" and sent["S1"]["account_id"] == "acc-acme"
    jane = world.ctx.store.get("contacts", contact_id="k-jane")
    bob = world.ctx.store.get("contacts", contact_id="k-bob")
    assert jane["last_step_at"] == NOW - timedelta(days=3)
    assert bob["mailbox"] == "harry@tryspill.org"  # step 1's address, which enrol could not know for Harry
    assert jane["mailbox"] == HANNAH
    assert out["sent"] == 3


def test_a_late_listed_send_renumbers_the_steps(world):
    world.sent("S2", at=NOW - timedelta(days=3))
    run(world)
    assert world.ctx.store.get("events", event_id="S2")["step"] == 1
    world.sent("S1", at=NOW - timedelta(days=10))
    run(world)
    assert world.ctx.store.get("events", event_id="S1")["step"] == 1
    assert world.ctx.store.get("events", event_id="S2")["step"] == 2


def test_a_reply_sent_from_the_desk_is_never_a_step(world):
    """The desk records its replies as reply_sent; even listed among the sent emails, they are no campaign step."""
    world.ctx.store.insert("events", [
        {"event_id": "R1", "type": "reply_sent", "contact_id": "k-jane", "account_id": "acc-acme", "mailbox": HANNAH,
         "step": None, "approval": "approved", "approved_by": "U_HARRY", "occurred_at": NOW - timedelta(days=2)},
        # One written before desk replies had their own type: a sent row with an approval.
        {"event_id": "R0", "type": "sent", "contact_id": "k-jane", "account_id": "acc-acme", "mailbox": HANNAH,
         "step": None, "approval": "edited", "approved_by": "U_HARRY", "occurred_at": NOW - timedelta(days=4)},
    ])
    world.sent("S1", at=NOW - timedelta(days=10))
    world.sent("R1", at=NOW - timedelta(days=2))  # Instantly lists the desk's reply as a sent email
    world.sent("S2", at=NOW - timedelta(days=1))
    out = run(world)
    events = {e["event_id"]: e for e in world.events()}
    assert (events["S1"]["step"], events["S2"]["step"]) == (1, 2)
    assert (events["R1"]["type"], events["R1"]["step"]) == ("reply_sent", None)
    assert (events["R0"]["type"], events["R0"]["step"]) == ("sent", None)  # left as it was, not renumbered
    assert out["sent"] == 2 and out["dropped"] == {"sent: a reply sent from the reply desk": 1}
    world.reply("E1", at=NOW - timedelta(hours=1))
    run(world)
    assert world.ctx.store.get("events", event_id="E1")["step"] == 2  # the reply answers step 2, not the desk's reply


def test_sends_outside_our_campaigns_or_contacts_are_dropped(world):
    world.sent("S1", campaign="cmp-eu")
    world.sent("S2", "someone@else.com")
    world.sent("S3", ue_type=3)  # a reply sent by hand is not a step
    out = run(world)
    assert world.events("sent") == []
    assert out["dropped"] == {"sent: another campaign": 1, "sent: no contact of ours": 1, "sent: not a campaign step": 1}


# -- replied -------------------------------------------------------------------------------------------


def test_a_reply_is_recorded_unclassified_and_engages_the_account(world):
    world.sent("S1", at=NOW - timedelta(days=10))
    world.sent("S2", at=NOW - timedelta(days=3))
    world.reply("E1")
    out = run(world)
    [ev] = world.events("replied")
    assert ev["event_id"] == "E1" and ev.get("reply_class") is None and ev["step"] == 2 and ev["mailbox"] == HANNAH
    assert world.ctx.store.get("accounts", account_id="acc-acme")["status"] == "engaged"
    assert out["replied"] == 1
    assert "k-jane" in stopped_contacts(world.ctx.store)


def test_an_auto_reply_is_left_to_poll_replies(world):
    world.reply("E1", text="I am out of the office.", is_auto_reply=1)
    out = run(world)
    assert world.events("replied") == [] and out["auto_replies_left_to_poll_replies"] == 1
    assert world.ctx.store.get("accounts", account_id="acc-acme")["status"] == "enrolled"


def test_engaged_never_moves_a_later_status_back(world):
    world.ctx.store.update("accounts", {"account_id": "acc-acme"}, {"status": "demo_booked"})
    world.reply("E1")
    run(world)
    assert world.ctx.store.get("accounts", account_id="acc-acme")["status"] == "demo_booked"


# -- bounced and unsubscribed leads ---------------------------------------------------------------------


def test_a_bounced_lead_is_recorded_and_suppressed(world):
    world.sent("S1", at=NOW - timedelta(days=1))
    world.lead("L-jane", JANE, LEAD_BOUNCED)
    out = run(world)
    [ev] = world.events("bounced")
    assert ev["event_id"] == "bounced:L-jane" and ev["step"] == 1 and ev["mailbox"] == HANNAH
    assert ev["occurred_at"] == NOW - timedelta(days=1)  # at its send, so v_mailbox_health counts it
    assert world.suppressed(JANE)[0]["reason"] == "bounce"
    assert world.ctx.store.get("contacts", contact_id="k-jane")["suppressed_reason"] == "bounce"
    assert out["bounced"] == 1
    run(world)
    assert len(world.events("bounced")) == 1


def test_an_instantly_unsubscribe_is_recorded_everywhere_when_live(world):
    world.hubspot_contacts[JANE] = {"hs_email_optout": "false"}
    world.lead("L-jane", JANE, LEAD_UNSUBSCRIBED)
    out = run(world)
    assert out["unsubscribed"] == {"done": 1}
    assert world.suppressed(JANE)[0]["reason"] == "unsubscribe"
    assert world.blocked == [[JANE]] and world.hubspot_unsubscribed == ["jane.doe%40acmecreative.com"]
    [ev] = world.events("unsubscribed")
    assert ev["event_id"] == "unsubscribed:lead:L-jane" and ev["contact_id"] == "k-jane"
    assert "k-jane" in stopped_contacts(world.ctx.store)
    writes = len(world.instantly_writes()) + len(world.hubspot_writes())
    world.at(NOW + timedelta(minutes=15))
    assert run(world)["unsubscribed"] == {"done": 1}
    assert len(world.instantly_writes()) + len(world.hubspot_writes()) == writes  # nothing again


def test_an_unsubscribe_already_opted_out_in_hubspot_is_left_alone(world):
    world.hubspot_contacts[JANE] = {"hs_email_optout": "true"}
    world.lead("L-jane", JANE, LEAD_UNSUBSCRIBED)
    run(world)
    assert world.hubspot_unsubscribed == [] and len(world.events("unsubscribed")) == 1


def test_an_unsubscribe_in_dry_run_suppresses_now_and_syncs_on_the_first_live_run(default_settings):
    world = make_world(default_settings, live=False)
    world.lead("L-jane", JANE, LEAD_UNSUBSCRIBED)
    out = outcomes.run(world.ctx)
    assert out["unsubscribed"] == {"dry_run": 1}
    assert world.suppressed(JANE)  # this system will never email them again, from now
    assert world.instantly_writes() == [] and world.hubspot_writes() == [] and world.events("unsubscribed") == []
    world.at(NOW + timedelta(minutes=15), live=True)
    assert outcomes.run(world.ctx)["unsubscribed"] == {"done": 1}
    assert world.blocked == [[JANE]] and len(world.events("unsubscribed")) == 1


def test_an_unsubscribe_whose_blocklist_call_fails_stays_pending(world):
    world.fail_blocklist(503)
    world.lead("L-jane", JANE, LEAD_UNSUBSCRIBED)
    assert run(world)["unsubscribed"] == {"pending": 1}
    assert world.events("unsubscribed") == [] and world.suppressed(JANE)
    world.transport.routes.pop()
    assert run(world)["unsubscribed"] == {"done": 1}


def test_leads_of_other_people_are_dropped(world):
    world.lead("L-x", "nobody@else.com", LEAD_UNSUBSCRIBED)
    out = run(world)
    assert world.blocked == [] and out["dropped"] == {"lead: no contact of ours": 1}


# -- idempotency, the read window, the guard ------------------------------------------------------------------


def test_a_second_run_adds_nothing(world):
    world.sent("S1", at=NOW - timedelta(days=10))
    world.reply("E1")
    world.lead("L-bob", BOB, LEAD_BOUNCED, campaign="cmp-harry")
    run(world)
    before = [dict(e) for e in world.events()]
    out = run(world)
    assert world.events() == before
    assert (out["sent"], out["replied"], out["bounced"]) == (0, 0, 0)


def test_it_reads_from_the_last_good_run_and_at_least_two_days_back(world):
    d = outcomes.Directory(world.ctx)
    assert outcomes.since(world.ctx, "sync_outcomes", earliest=d.earliest_enrolled()) == NOW - timedelta(days=10, hours=1)
    world.ctx.store.insert("heartbeats", [
        {"run_id": "r1", "job": "sync_outcomes", "status": "ok", "started_at": NOW - timedelta(minutes=15), "dry_run": True},
        {"run_id": "r2", "job": "poll_replies", "status": "ok", "started_at": NOW - timedelta(minutes=15), "dry_run": True},
    ])
    assert outcomes.since(world.ctx, "sync_outcomes") == NOW - outcomes.RECHECK
    # poll_replies counts only live runs: a dry run classified nothing.
    assert outcomes.since(world.ctx, "poll_replies", live_only=True, earliest=d.earliest_enrolled()) == \
        NOW - timedelta(days=10, hours=1)
    world.ctx.store.insert("heartbeats", [
        {"run_id": "r3", "job": "sync_outcomes", "status": "ok", "started_at": NOW - timedelta(days=90), "dry_run": False}])
    world.ctx.store.delete("heartbeats", {"run_id": "r1"})
    assert outcomes.since(world.ctx, "sync_outcomes") == NOW - outcomes.MAX_LOOKBACK


def test_the_last_good_run_is_read_as_one_row(world, monkeypatch):
    """since() asks the store for the latest ok heartbeat, never for every run the job ever had."""
    asked = []
    original = world.ctx.store.latest
    monkeypatch.setattr(world.ctx.store, "latest", lambda *a, **k: asked.append((a, k)) or original(*a, **k))
    world.ctx.store.insert("heartbeats", [
        {"run_id": f"r{i}", "job": "sync_outcomes", "status": "ok", "started_at": NOW - timedelta(hours=i),
         "dry_run": False, "detail": {}} for i in range(1, 30)])
    assert outcomes.since(world.ctx, "sync_outcomes") == NOW - outcomes.RECHECK
    assert outcomes.since(world.ctx, "sync_outcomes", recheck=timedelta(0)) == NOW - timedelta(hours=2)
    assert asked == [(("heartbeats", "started_at", {"job": "sync_outcomes", "status": "ok"}), {})] * 2
    assert outcomes.last_run(world.ctx, "poll_replies", live_only=True) is None


def _beat(world, ago: timedelta, *, dry_run: bool = False, **detail) -> None:
    world.ctx.store.insert("heartbeats", [{"run_id": f"r-{ago}", "job": "sync_outcomes", "status": "ok",
                                           "started_at": NOW - ago, "dry_run": dry_run, "detail": detail}])


def _email_reads(world) -> list:
    return [r for r in world.transport.requests if r.url.endswith("/api/v2/emails")]


def _lead_reads(world) -> list:
    return [r for r in world.transport.requests if r.url.endswith("/leads/list")]


def test_lead_statuses_are_read_at_most_hourly(world):
    world.lead("L-jane", JANE, LEAD_UNSUBSCRIBED)
    _beat(world, timedelta(minutes=15), leads_read_at=(NOW - timedelta(minutes=20)).isoformat(),
          recheck_at=(NOW - timedelta(hours=3)).isoformat())
    out = run(world)
    assert _lead_reads(world) == [] and out["leads_read"] is False and out["unsubscribed"] == {}
    assert out["leads_read_at"] == (NOW - timedelta(minutes=20)).isoformat()  # carried to the next run
    _beat(world, timedelta(minutes=5), leads_read_at=(NOW - timedelta(minutes=61)).isoformat())
    out = run(world)
    assert len(_lead_reads(world)) == 3 and out["leads_read"] is True and out["unsubscribed"] == {"done": 1}
    assert out["leads_read_at"] == NOW.isoformat()  # an hour on: the opt-out is honored the same day


def test_the_first_live_run_after_dry_runs_reads_lead_statuses(world):
    _beat(world, timedelta(minutes=15), dry_run=True, leads_read_at=(NOW - timedelta(minutes=10)).isoformat())
    assert run(world)["leads_read"] is True and len(_lead_reads(world)) == 3


def test_the_last_two_days_are_re_read_once_a_day_and_six_hours_otherwise(world):
    _beat(world, timedelta(minutes=15), recheck_at=(NOW - timedelta(hours=3)).isoformat(),
          leads_read_at=NOW.isoformat())
    out = run(world)
    start = NOW - timedelta(minutes=15) - outcomes.SYNC_OVERLAP
    assert out["since"] == start.isoformat() and out["until"] is None
    assert {r.params["min_timestamp_created"] for r in _email_reads(world)} == {start.isoformat().replace("+00:00", "Z")}
    assert out["recheck_at"] == (NOW - timedelta(hours=3)).isoformat()  # carried
    _beat(world, timedelta(minutes=5), recheck_at=(NOW - timedelta(hours=25)).isoformat())
    out = run(world)
    assert out["since"] == (NOW - outcomes.RECHECK).isoformat() and out["recheck_at"] == NOW.isoformat()


def test_a_long_outage_is_caught_up_a_week_a_run(world):
    from us_outbound.ops.heartbeat import run_job

    world.honor_until = True
    _beat(world, timedelta(days=20))  # the last good run, before an outage
    world.sent("S1", at=NOW - timedelta(days=18))
    world.sent("S2", at=NOW - timedelta(days=3))
    first = run_job(world.ctx, outcomes.run)
    start = NOW - timedelta(days=20) - outcomes.SYNC_OVERLAP
    assert (first["since"], first["until"]) == (start.isoformat(), (start + outcomes.CATCH_UP).isoformat())
    assert first["resume_from"] == first["until"] and first["recheck_at"] is None
    assert {r.params["max_timestamp_created"] for r in _email_reads(world)} == {
        (start + outcomes.CATCH_UP).isoformat().replace("+00:00", "Z")}
    assert [e["event_id"] for e in world.events("sent")] == ["S1"]
    world.at(NOW + timedelta(minutes=15))
    second = run_job(world.ctx, outcomes.run)
    assert second["since"] == (start + outcomes.CATCH_UP - outcomes.SYNC_OVERLAP).isoformat() and second["until"]
    world.at(NOW + timedelta(minutes=30))
    third = run_job(world.ctx, outcomes.run)
    assert third["until"] is None and "resume_from" not in third and third["recheck_at"] == world.ctx.now.isoformat()
    sent = {e["event_id"]: e["step"] for e in world.events("sent")}
    assert sent == {"S1": 1, "S2": 2}
    assert [b["status"] for b in world.ctx.store.select("heartbeats", {"job": "sync_outcomes"})].count("ok") == 4


def test_every_instantly_read_is_filtered_by_registry_mailbox_or_us_campaign(world):
    world.sent("S1")
    world.reply("E1")
    world.lead("L-jane", JANE, LEAD_BOUNCED)
    run(world)
    reads = [c for c in world.ctx.guard.calls if c.system == "instantly" and not c.write]
    assert {c.action for c in reads} == {"campaign.list", "email.list", "lead.list"}
    assert all(c.target.startswith("US Outbound – ") for c in reads if c.action == "lead.list")
    email_reads = [r for r in world.transport.requests if r.url.endswith("/api/v2/emails")]
    assert {r.params["eaccount"] for r in email_reads} == {HANNAH, HARRY, "sam@meetspill.org", "harry@tryspill.org"}
    assert {r.params["email_type"] for r in email_reads} == {"sent", "received"}
    lead_lists = [r for r in world.transport.requests if r.url.endswith("/leads/list")]
    assert {r.json["campaign"] for r in lead_lists} == {"cmp-hannah", "cmp-harry", "cmp-sam"}  # never the EU one


def test_dry_run_writes_the_database_and_nothing_else(default_settings):
    world = make_world(default_settings, live=False)
    world.sent("S1")
    world.reply("E1")
    world.lead("L-bob", BOB, LEAD_BOUNCED, campaign="cmp-harry")
    outcomes.run(world.ctx)
    assert {e["type"] for e in world.events()} == {"sent", "replied", "bounced"}
    assert world.instantly_writes() == [] and world.hubspot_writes() == []
