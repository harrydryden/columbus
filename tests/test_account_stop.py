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
    assert stops["to_stop"] == 2 and stops["stopped"] == {"deleted": 1} and stops["errors"] == []
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
