"""Suppression: hashes only, domain entries, expiry, idempotent loads from HubSpot (SPEC 6, 9, 11, 14), and the stop of
a lead in flight whose address or domain is suppressed after enrolment (Harry, 7 Oct 2026)."""

from datetime import UTC, datetime, timedelta

from tests.fakes import FakeTransport, make_context
from tests.fakes_replies import BOB, CAMPAIGNS, HANNAH, JANE, NOW as REPLY_NOW, make_world
from us_outbound import suppression
from us_outbound.clients.db import MemoryStore
from us_outbound.clients.guard import Guard
from us_outbound.enrol import capacity
from us_outbound.logs import hash_email
from us_outbound.replies import account_stop, optout, outcomes
from us_outbound.settings.model import General, Settings

NOW = datetime(2026, 10, 27, 12, 0, tzinfo=UTC)


def store():
    return MemoryStore(Guard())


def test_email_entry_stores_only_the_hash():
    s = store()
    assert suppression.add(s, email=" Jane.Doe@Acme.com ", reason="unsubscribe", source="reply", now=NOW)
    [row] = s.tables["suppression"]
    assert row["email_sha256"] == hash_email("jane.doe@acme.com")
    assert row["domain"] is None and row["expires_at"] is None
    assert "jane" not in repr(row).lower()


def test_domain_entry_is_cleaned_to_the_domain():
    s = store()
    suppression.add(s, domain="https://www.Acme.com/careers", reason="kill_rule", source="kill_rules", now=NOW)
    [row] = s.tables["suppression"]
    assert row == {"email_sha256": None, "domain": "acme.com", "reason": "kill_rule", "source": "kill_rules",
                   "added_at": NOW, "expires_at": None}


def test_is_suppressed_by_email_domain_and_subdomain():
    s = store()
    suppression.add(s, email="jane@acme.com", reason="unsubscribe", source="reply", now=NOW)
    suppression.add(s, domain="layoffco.com", reason="signal:Layoffs", source="scoring", now=NOW)
    assert suppression.is_suppressed(s, email="JANE@acme.com", now=NOW)
    assert not suppression.is_suppressed(s, email="john@acme.com", now=NOW)  # an email entry is not the domain
    assert not suppression.is_suppressed(s, domain="acme.com", now=NOW)
    assert suppression.is_suppressed(s, domain="layoffco.com", now=NOW)
    assert suppression.is_suppressed(s, email="ceo@layoffco.com", now=NOW)
    assert suppression.is_suppressed(s, email="ceo@mail.layoffco.com", now=NOW)
    assert not suppression.is_suppressed(s, email="ceo@notlayoffco.com", now=NOW)


def test_expiry_is_honored():
    s = store()
    until = NOW + timedelta(days=90)
    suppression.add(s, domain="layoffco.com", reason="signal:Layoffs", source="scoring", now=NOW, expires_at=until)
    assert suppression.is_suppressed(s, domain="layoffco.com", now=NOW + timedelta(days=89))
    assert not suppression.is_suppressed(s, domain="layoffco.com", now=until)


def test_add_is_idempotent_and_never_shortens():
    s = store()
    later = NOW + timedelta(days=1)
    assert suppression.add(s, domain="x.com", reason="signal:A", source="scoring", now=NOW, expires_at=NOW + timedelta(days=30))
    assert not suppression.add(s, domain="x.com", reason="signal:A", source="scoring", now=later, expires_at=NOW + timedelta(days=10))
    assert suppression.add(s, domain="x.com", reason="kill_rule", source="kill_rules", now=later)  # indefinite wins
    [row] = s.tables["suppression"]
    assert row["expires_at"] is None and row["reason"] == "kill_rule" and row["added_at"] == NOW
    assert not suppression.add(s, domain="x.com", reason="signal:A", source="scoring", now=later, expires_at=later)


def test_add_needs_an_email_or_domain():
    import pytest

    with pytest.raises(ValueError):
        suppression.add(store(), reason="x", source="y", now=NOW)


def test_load_from_hubspot_hashes_and_is_idempotent():
    t = FakeTransport()
    emails = [{"id": str(i), "properties": {"email": f"person{i}@example.com"}} for i in (1, 2, 3)]
    t.route("POST", "/crm/v3/objects/contacts/search",
            fn=lambda req: {"results": emails if req.json["filterGroups"][0]["filters"][1]["value"] == "0" else []})
    ctx = make_context(Settings(general=General()), transport=t)
    first = suppression.load_from_hubspot(ctx)
    assert {k: first[k] for k in ("seen", "added", "already_suppressed")} == {"seen": 3, "added": 3,
                                                                              "already_suppressed": 0}
    rows = ctx.store.tables["suppression"]
    assert {r["email_sha256"] for r in rows} == {hash_email(f"person{i}@example.com") for i in (1, 2, 3)}
    assert all(r["reason"] == "hubspot_opt_out_or_bounce" and r["source"] == "hubspot" for r in rows)
    assert "person1" not in repr(rows)
    second = suppression.load_from_hubspot(ctx)
    assert {k: second[k] for k in ("seen", "added", "already_suppressed")} == {"seen": 3, "added": 0,
                                                                               "already_suppressed": 3}
    assert len(ctx.store.tables["suppression"]) == 3
    assert not ctx.guard.writes("hubspot")  # reads only
    # This HubSpot has no customer companies: surely a fault, so it is said, and nothing else changes.
    assert second["customers_error"] == "HubSpot returned no Spill customer companies; yesterday's entries stand"


# -- suppressed while in flight (Harry, 7 Oct 2026) --------------------------------------------------------------------
# Only enrol checked suppression, so a HubSpot opt-out loaded after enrolment did not stop the lead already in
# Instantly. sync_outcomes' account-level stop now stops it, and everyone else's at the account (replies/account_stop.py).

OMAR = "omar@acmecreative.com"


def in_flight_world(settings, *, live=True):
    """Jane (and her colleague Omar) at Acme in Hannah's campaign, Bob at Beta in Harry's: every lead in flight."""
    w = make_world(settings, live=live)
    w.ctx.job = "sync_outcomes"
    w.ctx.store.insert("contacts", [{
        "contact_id": "k-omar", "account_id": "acc-acme", "first_name": "Omar", "email": OMAR,
        "email_sha256": hash_email(OMAR), "enrolment_month": "2026-10", "enrolled_at": REPLY_NOW - timedelta(days=6),
        "instantly_campaign": CAMPAIGNS["cmp-hannah"], "instantly_lead_id": "L-omar", "mailbox": HANNAH,
        "contact_slot": 2, "config_version": "abc123abc123"}])  # a cohort or version changes nothing here
    w.lead("L-bob", BOB, 1, campaign="cmp-harry")  # still sending: Instantly knows nothing of the opt-out
    return w


def hubspot_opts_out(w, email: str) -> dict:
    """suppression_load, with HubSpot answering that this person opted out of email."""
    def search(req):
        f = req.json["filterGroups"][0]["filters"]
        found = f[0].get("value") == "true" and f[1]["value"] == "0"
        return {"results": [{"id": "9", "properties": {"email": email, "hs_object_id": "9"}}] if found else []}

    w.transport.route("POST", "/crm/v3/objects/contacts/search", fn=search)
    return suppression.load_from_hubspot(w.ctx)


def deleted(w) -> list[str]:
    return [r.url.rsplit("/", 1)[1] for r in w.transport.requests if r.method == "DELETE"]


def test_a_hubspot_opt_out_stops_the_lead_in_flight_and_the_rest_of_its_account(default_settings):
    w = in_flight_world(default_settings)
    assert hubspot_opts_out(w, JANE)["added"] == 1
    out = outcomes.run(w.ctx)["account_stops"]
    assert out == {"to_stop": 2, "stopped": {"deleted": 2}, "errors": []}
    assert deleted(w) == ["L-jane", "L-omar"]  # Bob, at another company, goes on
    calls = [c for c in w.ctx.guard.writes("instantly") if c.action == "lead.delete"]
    assert [(c.target, c.sent) for c in calls] == [(CAMPAIGNS["cmp-hannah"], True)] * 2  # through the guard, live
    for cid in ("k-jane", "k-omar"):
        ev = w.ctx.store.get("events", event_id=f"lead_stopped:{cid}")
        assert (ev["type"], ev["account_id"], ev["source"], ev["occurred_at"]) == (
            "lead_stopped", "acc-acme", "suppression", w.ctx.now)
    # The send forecast frees their later steps, and they are no longer in flight.
    assert {"k-jane", "k-omar"} <= capacity.stopped_contacts(w.ctx.store)
    assert [c["contact_id"] for c in capacity.in_flight(w.ctx.store, w.ctx.settings, w.ctx.now_et().date())] == ["k-bob"]
    w.at(w.ctx.now + timedelta(minutes=15))
    assert outcomes.run(w.ctx)["account_stops"]["to_stop"] == 0 and len(deleted(w)) == 2  # once


def test_in_a_dry_run_it_says_what_it_would_stop_and_writes_nothing(default_settings):
    w = in_flight_world(default_settings, live=False)
    hubspot_opts_out(w, JANE)
    out = account_stop.sweep(w.ctx)
    assert out["to_stop"] == 2 and out["would_stop"] == [
        {"account_id": "acc-acme", "contact_id": "k-jane", "because": "suppressed"},
        {"account_id": "acc-acme", "contact_id": "k-omar", "because": "suppressed"}]
    assert deleted(w) == [] and w.lead_patches == [] and not w.events("lead_stopped")
    assert not w.ctx.guard.writes("instantly")


def test_live_with_instantly_s_pause_confirmed_the_lead_is_paused_through_the_guard(default_settings, monkeypatch):
    from us_outbound.clients import instantly as instantly_client

    monkeypatch.setattr(instantly_client, "LEAD_PAUSE_CONFIRMED", True)
    w = in_flight_world(default_settings)
    hubspot_opts_out(w, BOB)
    assert account_stop.sweep(w.ctx)["stopped"] == {"paused": 1}
    assert w.lead_patches == [("L-bob", {"status": instantly_client.LEAD_PAUSED})] and deleted(w) == []
    [call] = [c for c in w.ctx.guard.writes("instantly") if c.action == "lead.update"]
    assert (call.target, call.sent) == (CAMPAIGNS["cmp-harry"], True)


def test_a_suppressed_domain_stops_every_lead_at_the_company(default_settings):
    w = in_flight_world(default_settings)
    suppression.add(w.ctx.store, domain="betalegal.com", reason="hubspot_customer", source="lookalikes",
                    now=w.ctx.now, expires_at=w.ctx.now + timedelta(days=30))
    assert account_stop.sweep(w.ctx)["stopped"] == {"deleted": 1} and deleted(w) == ["L-bob"]


def test_an_expired_entry_and_a_finished_lead_stop_nothing(default_settings):
    w = in_flight_world(default_settings)
    suppression.add(w.ctx.store, domain="betalegal.com", reason="signal:Layoffs", source="scoring",
                    now=w.ctx.now - timedelta(days=40), expires_at=w.ctx.now - timedelta(days=1))
    w.ctx.store.update("contacts", {"account_id": "acc-acme"}, {"enrolled_at": REPLY_NOW - timedelta(days=40)})
    suppression.add(w.ctx.store, email=JANE, reason="hubspot_opt_out_or_bounce", source="hubspot", now=w.ctx.now)
    assert account_stop.sweep(w.ctx) == {"to_stop": 0, "stopped": {}, "errors": []} and deleted(w) == []


def test_an_instantly_unsubscribe_still_pending_is_left_to_its_own_path(default_settings):
    """Instantly stopped that lead itself; deleting it would end replies/optout.py's retries, which read its status."""
    w = in_flight_world(default_settings)
    optout.suppress_now(w.ctx, [JANE], contact=w.ctx.store.get("contacts", contact_id="k-jane"), source="instantly")
    assert account_stop.sweep(w.ctx)["to_stop"] == 0 and deleted(w) == []


# -- customers daily (Harry, 7 Oct 2026) ---------------------------------------------------------------------------------
# Customer domains were suppressed only by the monthly lookalikes job, so a company that became a Spill customer
# mid-month could still be enrolled, and its leads in flight ran on. suppression_load now reads the customers nightly.


def customer_hub(w, *domains: str):
    """HubSpot with these companies as Spill customers (made-up domains), on the world's transport."""
    from tests.test_lookalikes import FakeHubSpot, company

    hub = FakeHubSpot({str(100 + i): company(d, "Law", covered=20, country="united states")
                       for i, d in enumerate(domains)})
    hub.install(w.transport)
    w.transport.route("POST", "/crm/v3/objects/contacts/search", body={"results": []})  # no opt-outs tonight
    return hub


def test_a_new_customer_is_suppressed_by_the_nightly_load_and_its_lead_in_flight_is_stopped(default_settings):
    w = in_flight_world(default_settings)
    customer_hub(w, "otherfirm.com")
    first = suppression.load_from_hubspot(w.ctx)
    assert first["customers"] == {"domains": 1, "suppressed": 1, "new": 1, "accounts_marked": 0}
    assert account_stop.sweep(w.ctx)["to_stop"] == 0  # nobody in flight there
    # Beta Legal signs up mid-month: the next night's load keeps it out, and the sweep stops Bob's lead.
    w.at(w.ctx.now + timedelta(days=1))
    customer_hub(w, "otherfirm.com", "betalegal.com")
    out = suppression.load_from_hubspot(w.ctx)
    assert out["customers"] == {"domains": 2, "suppressed": 2, "new": 1, "accounts_marked": 1}
    entry = w.ctx.store.get("suppression", email_sha256=None, domain="betalegal.com")
    assert (entry["reason"], entry["source"]) == ("hubspot_customer", "lookalikes")
    assert entry["expires_at"] == w.ctx.now + timedelta(days=70)  # renewed nightly, as the monthly job renews it
    fact = [e for e in w.ctx.store.select("signal_events", {"account_id": "acc-beta"}) if e["fact"] == "hubspot_customer"]
    assert fact and fact[0]["value"] is True  # the 02:00 rescore tiers it Excluded
    assert not w.ctx.store.select("lookalike_cells")  # the cells are the monthly job's
    assert suppression.is_suppressed(w.ctx.store, domain="betalegal.com", now=w.ctx.now)
    stops = account_stop.sweep(w.ctx)
    assert stops["stopped"] == {"deleted": 1} and deleted(w) == ["L-bob"]
    assert w.ctx.store.get("events", event_id="lead_stopped:k-bob")["source"] == "suppression"
    assert not w.ctx.guard.writes("hubspot")  # HubSpot is only read


def test_a_failed_customer_read_keeps_the_opt_outs_and_says_so(default_settings):
    from us_outbound.ops import job_errors

    w = in_flight_world(default_settings)
    w.transport.route("POST", "/crm/v3/objects/companies/search", status=403, body={"message": "missing scope"})
    out = hubspot_opts_out(w, JANE)
    assert out["added"] == 1 and suppression.is_suppressed(w.ctx.store, email=JANE, now=w.ctx.now)
    assert out["customers_error"].startswith("Spill's customers could not be read from HubSpot (AuthError: ")  # a 403
    assert out["customers_error"].endswith("yesterday's entries stand") and "customers" not in out
    [finding] = job_errors.findings("suppression_load", {"status": "ok", "detail": out, "started_at": w.ctx.now})
    assert finding.kind == "customers_error"  # heartbeat_check tells Harry, once a day


def test_the_monthly_job_still_rebuilds_the_cells_and_suppresses(default_settings):
    """lookalikes.run is unchanged: the cells, the exclusions and the facts, monthly."""
    from tests.test_lookalikes import FakeHubSpot, company
    from us_outbound.sources import lookalikes

    t = FakeHubSpot({"100": company("betalegal.com", "Law", covered=20, country="united states")}).install(FakeTransport())
    ctx = make_context(default_settings, transport=t, now=NOW, job="lookalikes")
    out = lookalikes.run(ctx)
    assert out["excluded"] == {"domains": 1, "suppressed": 1, "new": 1, "accounts_marked": 0}
    assert ctx.store.select("lookalike_cells")
