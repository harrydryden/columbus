"""The account-level stop (replies/account_stop.sweep): a stop the guard refuses is recorded once, never raised.

code-replies-crm L2 (W1-2, 19 Oct 2026): Instantly.delete_lead refuses a lead that is not in the campaign stored for
the contact. sweep caught ApiError, LookupError and ConfigError only, so the GuardViolation failed sync_outcomes, a
safety job, after its writes, and again every 15 minutes: the marker was never written.
"""

from __future__ import annotations

from tests.fakes_replies import JANE
from tests.test_suppression import deleted, hubspot_opts_out, in_flight_world
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
    refusing run's one errors line was the only signal, so job_errors.check found nothing and nobody was told. Now the
    sweep posts it to the alert channel itself, once: not again from the clean run, nor from heartbeat_check."""
    from datetime import timedelta

    from us_outbound.ops import job_errors
    from us_outbound.ops.heartbeat import run_job

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
    checked = job_errors.check(w.ctx, ["sync_outcomes"])
    assert checked["found"] == [] and len(refused_posts(w)) == 1  # told once, by the sweep


def test_a_refused_stop_whose_alert_failed_is_tried_again_and_seen_by_heartbeat_check(default_settings):
    """Slack down at the refusing run: nothing is recorded as told, the run puts it under errors (so heartbeat_check
    has it if that run is the latest), and the next run posts it and is clean; the run after that says nothing."""
    from datetime import timedelta

    from us_outbound.ops import job_errors
    from us_outbound.ops.heartbeat import run_job

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
    [finding] = job_errors.findings("sync_outcomes", hb)
    assert (finding.kind, finding.texts) == ("errors", [line])
    w.transport.route("POST", "chat.postMessage", fn=w._post)  # Slack is back
    w.at(t0 + timedelta(minutes=15))
    second = run_job(w.ctx, outcomes.run)["account_stops"]
    assert second["refused_alert"] == {"told": ["k-jane"], "unsent": []} and second["errors"] == []
    assert len(refused_posts(w)) == 1
    w.at(t0 + timedelta(minutes=30))
    third = run_job(w.ctx, outcomes.run)["account_stops"]
    assert (third["errors"], "refused_alert" in third) == ([], False) and len(refused_posts(w)) == 1
