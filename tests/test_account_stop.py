"""The account-level stop (replies/account_stop.sweep): a stop the guard refuses is recorded once, never raised.

code-replies-crm L2 (W1-2, 19 Oct 2026): Instantly.delete_lead refuses a lead that is not in the campaign stored for
the contact. sweep caught ApiError, LookupError and ConfigError only, so the GuardViolation failed sync_outcomes, a
safety job, after its writes, and again every 15 minutes: the marker was never written.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from tests.fakes_replies import CAMPAIGNS, HANNAH as HANNAH_ADDR, JANE, NOW as REPLY_NOW, make_world
from tests.test_suppression import deleted, hubspot_opts_out, in_flight_world
from us_outbound.clients import instantly as instantly_client
from us_outbound.clients.instantly import LEAD_BOUNCED, LEAD_UNSUBSCRIBED
from us_outbound.enrol import capacity
from us_outbound.logs import hash_email
from us_outbound.replies import account_stop, outcomes


def test_a_lead_no_longer_in_its_stored_campaign_is_marked_once_and_the_run_goes_on(default_settings):
    w = in_flight_world(default_settings)
    w.lead("L-jane", JANE, 1, campaign="cmp-harry")  # Jane's lead is in Harry's campaign now, not Hannah's
    assert hubspot_opts_out(w, JANE)["added"] == 1  # suppressed in flight: Jane's lead and Omar's are to stop
    out = outcomes.run(w.ctx)  # no exception: sync_outcomes finishes
    stops = out["account_stops"]
    # Jane's delete is refused by the client's own check (her lead is in another campaign); Omar's goes through.
    assert stops["to_stop"] == 2 and stops["stopped"] == {"deleted": 1}
    assert stops["errors"] == [] and stops["refused_alert"] == {"told": ["k-jane"], "unsent": []}  # the tests below
    assert [r["contact_id"] for r in stops["refused"]] == ["k-jane"]
    assert "not in campaign" in stops["refused"][0]["why"]
    assert deleted(w) == ["L-omar"]
    marker = w.ctx.store.get("events", event_id="lead_stopped:k-jane")
    assert (marker["type"], marker["source"]) == ("lead_stopped", "guard_refused") == ("lead_stopped",
                                                                                        account_stop.REFUSED_SOURCE)
    # The next run makes no call for her, and reports nothing again.
    before = len(w.transport.requests)
    again = account_stop.sweep(w.ctx)
    assert again == {"to_stop": 0, "stopped": {}, "errors": [], "refused": []}
    assert [r for r in w.transport.requests[before:] if "/leads/" in r.url] == []


def refused_posts(w) -> list[str]:
    return [p["text"] for p in w.slack_posts if "contact k-jane" in p.get("text", "")]


def test_a_refused_stop_is_told_even_when_a_later_clean_run_is_the_latest(default_settings):
    """The whole-wave review's probe: Jane opts out in HubSpot while her lead sits in a campaign other than the stored
    one, so account_stop is the only thing that could stop her sequence, and the guard refuses it. The :22 run refuses
    and writes the marker; the :37 run is clean; at the next :05, heartbeat_check reads only the :37 run. Before, the
    refusing run's one errors line was the only signal, so health.check found nothing and nobody was told. Now the
    sweep posts it to the alert channel itself, once: not again from the clean run, nor from heartbeat_check."""
    from datetime import timedelta

    from us_outbound.ops import health
    from us_outbound.ops.health import run_job

    w = in_flight_world(default_settings)
    w.lead("L-jane", JANE, 1, campaign="cmp-harry")
    hubspot_opts_out(w, JANE)
    t0 = w.ctx.now
    first = run_job(w.ctx, outcomes.run)["account_stops"]
    assert [r["contact_id"] for r in first["refused"]] == ["k-jane"] and first["errors"] == []
    [text] = refused_posts(w)
    assert "Find the lead in Instantly and delete it" in text and "cmp-harry" not in text  # the stored campaign
    assert account_stop.REFUSED_HEAD in text
    w.at(t0 + timedelta(minutes=15))
    second = run_job(w.ctx, outcomes.run)["account_stops"]
    assert (second["to_stop"], second["errors"], second["refused"]) == (0, [], [])
    assert "refused_alert" not in second
    w.at(t0 + timedelta(minutes=58))
    checked = health.report_errors(w.ctx, ["sync_outcomes"])
    assert checked["found"] == [] and len(refused_posts(w)) == 1  # told once, by the sweep


def test_a_refused_stop_whose_alert_failed_is_tried_again_and_seen_by_heartbeat_check(default_settings):
    """Slack down at the refusing run: nothing is recorded as told, the run puts it under errors (so heartbeat_check
    has it if that run is the latest), and the next run posts it and is clean; the run after that says nothing."""
    from datetime import timedelta

    from us_outbound.ops import health
    from us_outbound.ops.health import run_job

    w = in_flight_world(default_settings)
    w.lead("L-jane", JANE, 1, campaign="cmp-harry")
    hubspot_opts_out(w, JANE)
    w.transport.route("POST", "chat.postMessage", {"ok": False, "error": "service_unavailable"})
    t0 = w.ctx.now
    first = run_job(w.ctx, outcomes.run)["account_stops"]
    assert first["refused_alert"] == {"told": [], "unsent": ["k-jane"]} and refused_posts(w) == []
    [line] = first["errors"]
    assert line.startswith("k-jane: not stopped, the guard refused it") and "stop it by hand in Instantly" in line
    [hb] = [r for r in w.ctx.store.tables["heartbeats"] if r["job"] == "sync_outcomes"]
    [finding] = health.findings("sync_outcomes", hb)
    assert (finding.kind, finding.texts) == ("errors", [line])
    w.transport.route("POST", "chat.postMessage", fn=w._post)  # Slack is back
    w.at(t0 + timedelta(minutes=15))
    second = run_job(w.ctx, outcomes.run)["account_stops"]
    assert second["refused_alert"] == {"told": ["k-jane"], "unsent": []} and second["errors"] == []
    assert len(refused_posts(w)) == 1
    w.at(t0 + timedelta(minutes=30))
    third = run_job(w.ctx, outcomes.run)["account_stops"]
    assert (third["errors"], "refused_alert" in third) == ([], False) and len(refused_posts(w)) == 1


# -- two leads at one account: the sweep stops the colleague's (Harry, 6 Oct 2026) --------------------------------
# Kept from tests/test_second_contact.py when second contacts went (W2-1, 20 Oct 2026): the sweep stays, for any
# account with two leads in flight, and the suppressed-in-flight stop above runs through it.

OMAR = "omar@acmecreative.com"


def stop_world(default_settings, *, live=True, colleague=True):
    """Acme's Jane in flight (fakes_replies), and, with colleague, Omar in Hannah's campaign at the same account."""
    w = make_world(default_settings, live=live)
    w.ctx.job = "sync_outcomes"
    if colleague:
        w.ctx.store.insert("contacts", [{
            "contact_id": "k-omar", "account_id": "acc-acme", "first_name": "Omar", "last_name": "Reyes",
            "title": "CEO", "role": "Founder or executive", "email": OMAR, "email_sha256": hash_email(OMAR),
            "enrolment_month": "2026-10", "enrolled_at": REPLY_NOW - timedelta(days=6),
            "instantly_campaign": CAMPAIGNS["cmp-hannah"], "instantly_lead_id": "L-omar", "mailbox": HANNAH_ADDR,
            "contact_slot": 2}])
    return w


def deletes(w) -> list[str]:
    return [r.url.rsplit("/", 1)[1] for r in w.transport.requests if r.method == "DELETE"]


@pytest.mark.parametrize("who, status, stopped", [("L-jane", LEAD_BOUNCED, "L-omar"),
                                                  ("L-jane", LEAD_UNSUBSCRIBED, "L-omar"),
                                                  ("L-omar", LEAD_BOUNCED, "L-jane"),
                                                  ("L-omar", LEAD_UNSUBSCRIBED, "L-jane")])
def test_a_bounce_or_unsubscribe_stops_the_colleague_s_lead_and_suppresses_only_the_address(
        default_settings, who, status, stopped):
    w = stop_world(default_settings)
    address = JANE if who == "L-jane" else OMAR
    other = OMAR if who == "L-jane" else JANE
    w.lead(who, address, status)
    out = outcomes.run(w.ctx)
    assert out["account_stops"]["stopped"] == {"deleted": 1} and deletes(w) == [stopped]
    other_id = "k-omar" if who == "L-jane" else "k-jane"
    ev = w.ctx.store.get("events", event_id=f"lead_stopped:{other_id}")
    assert (ev["type"], ev["account_id"], ev["occurred_at"]) == ("lead_stopped", "acc-acme", w.ctx.now)
    # Per address: the one who bounced or unsubscribed is suppressed (and blocklisted for an unsubscribe); the
    # colleague's emails stop, but they are not suppressed, nor blocklisted.
    assert w.suppressed(address) and not w.suppressed(other)
    assert not w.ctx.store.get("contacts", contact_id=other_id).get("suppressed")
    assert all(other not in b for b in w.blocked)
    assert other_id in capacity.stopped_contacts(w.ctx.store)  # its later steps hold no slot
    w.at(w.ctx.now + timedelta(minutes=15))
    assert outcomes.run(w.ctx)["account_stops"]["to_stop"] == 0 and deletes(w) == [stopped]  # once


@pytest.mark.parametrize("cls, stops", [("positive", True), ("negative", True), ("referral", True),
                                        ("unsubscribe", True), ("other", True), ("out_of_office", False),
                                        (None, False)])
def test_a_reply_stops_the_colleague_s_lead_once_it_is_classified(default_settings, cls, stops):
    w = stop_world(default_settings)
    w.ctx.store.insert("events", [{"event_id": "em-1", "contact_id": "k-jane", "account_id": "acc-acme",
                                   "type": "replied", "reply_class": cls, "occurred_at": REPLY_NOW - timedelta(hours=1)}])
    out = account_stop.sweep(w.ctx)
    assert deletes(w) == (["L-omar"] if stops else []), out
    assert out["stopped"] == ({"deleted": 1} if stops else {})


def test_a_complaint_stops_the_colleague_and_the_lead_is_paused_once_instantly_s_pause_is_confirmed(
        default_settings, monkeypatch):
    monkeypatch.setattr(instantly_client, "LEAD_PAUSE_CONFIRMED", True)
    w = stop_world(default_settings)
    w.ctx.store.insert("events", [{"event_id": "c-1", "contact_id": "k-omar", "account_id": "acc-acme",
                                   "type": "complained", "occurred_at": REPLY_NOW - timedelta(hours=1)}])
    assert account_stop.sweep(w.ctx)["stopped"] == {"paused": 1}
    assert w.lead_patches == [("L-jane", {"status": instantly_client.LEAD_PAUSED})] and deletes(w) == []


def test_the_sweep_in_a_dry_run_calls_nothing(default_settings):
    w = stop_world(default_settings, live=False)
    w.lead("L-jane", JANE, LEAD_BOUNCED)
    out = outcomes.run(w.ctx)["account_stops"]
    assert out["to_stop"] == 1 and out["would_stop"] == [{"account_id": "acc-acme", "contact_id": "k-omar",
                                                          "because": "bounced"}]
    assert deletes(w) == [] and w.lead_patches == [] and not w.events("lead_stopped")


def test_with_one_contact_per_account_the_sweep_calls_nothing(default_settings):
    w = stop_world(default_settings, colleague=False)
    w.lead("L-jane", JANE, LEAD_BOUNCED)
    out = outcomes.run(w.ctx)
    assert out["bounced"] == 1 and out["account_stops"] == {"to_stop": 0, "stopped": {}, "errors": [], "refused": []}
    assert deletes(w) == [] and w.lead_patches == []


def test_is_stop_reads_every_stop_type_and_waits_for_a_reply_s_class():
    """account_stop.is_stop (moved from enrol/second.py with W2-1): a bounce, an unsubscribe or a complaint stops at
    once; a reply stops once classified, unless out of office; with classified_only an unclassified reply waits."""
    assert all(account_stop.is_stop({"type": t}) for t in ("bounced", "unsubscribed", "complained"))
    assert account_stop.is_stop({"type": "replied", "reply_class": "positive"})
    assert not account_stop.is_stop({"type": "replied", "reply_class": "out_of_office"})
    assert account_stop.is_stop({"type": "replied"}) and not account_stop.is_stop({"type": "replied"},
                                                                                     classified_only=True)
    assert not account_stop.is_stop({"type": "sent"}) and not account_stop.is_stop({"type": "lead_stopped"})
