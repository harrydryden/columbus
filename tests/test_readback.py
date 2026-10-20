"""hubspot_readback (crm/readback.py): meetings booked, demos held and deals, read back from HubSpot (SPEC 9, 11)."""

from __future__ import annotations

from datetime import timedelta

from tests.test_hubspot_writes import ACCOUNT, NOW, world
from us_outbound.crm.readback import hubspot_readback

JANE = "jane.doe@acmecreative.com"


def ms(dt) -> str:
    return str(int(dt.timestamp() * 1000))


def setup(settings, *, live=True, **account):
    ctx, t, crm = world(settings, live=live)
    ctx.store.upsert("accounts", [{**ACCOUNT, **account}])
    stops: list[dict] = []
    t.route("GET", "api.instantly.ai/api/v2/campaigns", {"items": [{"id": "c-hannah", "name": "US Outbound – Hannah Spalding"}]})
    t.route("POST", "/leads/update-interest-status", fn=lambda r: stops.append(r.json) or {"status": "ok"})
    return ctx, t, crm, stops


def meeting(crm, *, source="MEETINGS_PUBLIC", owner="owner-harry", outcome="SCHEDULED", created=None, **links):
    created = created or NOW - timedelta(hours=1)
    return crm.add("meetings", {"hs_meeting_source": source, "hubspot_owner_id": owner, "hs_meeting_outcome": outcome,
                                "hs_createdate": created.isoformat(), "hs_lastmodifieddate": ms(created),
                                "hs_meeting_start_time": (NOW + timedelta(days=2)).isoformat()}, **links)


def events(ctx, type_):
    return ctx.store.select("events", {"type": type_})


def test_a_booking_on_the_link_records_the_meeting_stops_the_lead_and_makes_the_deal(default_settings):
    """The prospect booked from the signature's link without replying: matched by email, the company by domain."""
    ctx, t, crm, stops = setup(default_settings, status="enrolled")
    co = crm.add("companies", {"name": "Acme Creative", "domain": "acmecreative.com"})
    kid = crm.add("contacts", {"email": JANE})  # made by HubSpot's meetings tool
    mid = meeting(crm, contacts=[kid])
    out = hubspot_readback(ctx)
    [booked] = events(ctx, "meeting_booked")
    assert (booked["event_id"], booked["account_id"], booked["contact_id"]) == (f"hs-meeting:{mid}", "acc-1", "con-1")
    account = ctx.store.get("accounts", account_id="acc-1")
    assert account["status"] == "demo_booked" and account["hubspot_company_id"] == co
    [(deal_id, deal)] = crm.objects["deals"].items()
    assert deal["dealname"] == "US Outbound – Acme Creative" and deal["dealstage"] == "stage-first"
    assert stops == [{"lead_email": JANE, "campaign_id": "c-hannah", "interest_value": 2}]
    assert out["counts"]["meeting_booked"] == 1 and out["counts"]["leads_stopped"] == 1
    # Again: nothing new.
    hubspot_readback(ctx)
    assert len(events(ctx, "meeting_booked")) == 1 and len(crm.objects["deals"]) == 1 and len(stops) == 1
    assert len(events(ctx, "deal_created")) == 1


def test_held_when_the_outcome_is_completed(default_settings):
    ctx, t, crm, stops = setup(default_settings, status="demo_booked", hubspot_company_id="co-x")
    crm.objects["companies"]["co-x"] = {"name": "Acme Creative", "domain": "acmecreative.com"}
    mid = meeting(crm, outcome="COMPLETED", companies=["co-x"])
    hubspot_readback(ctx)
    assert [e["event_id"] for e in events(ctx, "demo_held")] == [f"hs-meeting:{mid}:held"]


def test_other_meetings_are_not_ours(default_settings):
    ctx, t, crm, stops = setup(default_settings, status="enrolled")
    kid = crm.add("contacts", {"email": JANE})
    meeting(crm, source="CRM_UI", contacts=[kid])  # logged by hand, not booked on the link
    meeting(crm, owner="owner-sally", contacts=[kid])  # someone else's calendar
    other = crm.add("contacts", {"email": "pat@unrelated.org"})
    meeting(crm, contacts=[other])  # a UK customer, say
    meeting(crm, contacts=[kid], created=NOW - timedelta(days=20))  # before we wrote to them (and outside the window)
    out = hubspot_readback(ctx)
    assert events(ctx, "meeting_booked") == [] and crm.objects["deals"] == {} and stops == []
    assert out["counts"]["meetings_not_ours"] == 1


def test_a_colleague_booking_counts_for_the_account(default_settings):
    ctx, t, crm, stops = setup(default_settings, status="enrolled")
    kid = crm.add("contacts", {"email": "sam.lee@acmecreative.com"})  # not our contact, same company
    meeting(crm, contacts=[kid])
    hubspot_readback(ctx)
    [booked] = events(ctx, "meeting_booked")
    assert booked["account_id"] == "acc-1" and booked["contact_id"] is None
    assert ctx.store.get("accounts", account_id="acc-1")["status"] == "demo_booked"


def test_deal_stages_record_bookings_and_demos_held(default_settings):
    ctx, t, crm, stops = setup(default_settings, status="engaged", hubspot_company_id="co-x")
    crm.objects["companies"]["co-x"] = {"name": "Acme Creative", "domain": "acmecreative.com"}
    did = crm.add("deals", {"pipeline": "pipe-spill3", "dealstage": "stage-held", "createdate": NOW.isoformat()},
                  companies=["co-x"])
    crm.add("deals", {"pipeline": "pipe-uk", "dealstage": "uk-1"}, companies=["co-x"])  # another pipeline: not ours
    hubspot_readback(ctx)
    assert [e["event_id"] for e in events(ctx, "deal_created")] == [f"hs-deal:{did}"]
    assert [e["event_id"] for e in events(ctx, "meeting_booked")] == [f"hs-deal:{did}:booked"]
    assert [e["event_id"] for e in events(ctx, "demo_held")] == [f"hs-deal:{did}:held"]
    assert ctx.store.get("accounts", account_id="acc-1")["status"] == "demo_booked"
    hubspot_readback(ctx)
    assert len(events(ctx, "meeting_booked")) == len(events(ctx, "demo_held")) == 1


def test_a_lost_deal_counts_for_neither(default_settings):
    ctx, t, crm, stops = setup(default_settings, status="engaged", hubspot_company_id="co-x")
    crm.objects["companies"]["co-x"] = {"name": "Acme Creative", "domain": "acmecreative.com"}
    crm.add("deals", {"pipeline": "pipe-spill3", "dealstage": "stage-lost", "hs_is_closed": "true"}, companies=["co-x"])
    hubspot_readback(ctx)
    assert len(events(ctx, "deal_created")) == 1
    assert events(ctx, "meeting_booked") == [] and events(ctx, "demo_held") == []
    assert ctx.store.get("accounts", account_id="acc-1")["status"] == "demo_requested"


def test_a_demo_request_without_a_deal_gets_one(default_settings):
    ctx, t, crm, stops = setup(default_settings, status="demo_requested", hubspot_company_id="co-x")
    crm.objects["companies"]["co-x"] = {"name": "Acme Creative", "domain": "acmecreative.com"}
    hubspot_readback(ctx)
    assert len(crm.objects["deals"]) == 1 and len(events(ctx, "deal_created")) == 1
    hubspot_readback(ctx)
    assert len(crm.objects["deals"]) == 1


def test_dry_run_reads_and_writes_nothing(default_settings):
    ctx, t, crm, stops = setup(default_settings, live=False, status="enrolled")
    crm.add("companies", {"name": "Acme Creative", "domain": "acmecreative.com"})
    kid = crm.add("contacts", {"email": JANE})
    meeting(crm, contacts=[kid])
    out = hubspot_readback(ctx)
    assert out["dry_run"] and out["counts"]["meeting_booked"] == 1
    assert events(ctx, "meeting_booked") == [] and crm.objects["deals"] == {}
    assert stops == [] and ctx.store.get("accounts", account_id="acc-1")["status"] == "enrolled"
    assert [r for r in t.writes() if "api.hubapi.com" in r.url and not r.url.endswith("/search")] == []


# -- bookings: Spill 3.0 deals at Demo requested or later, read back (Harry, 6 Oct 2026) ------------------------------


def demo_deal(crm, *, stage="stage-first", name="Acme Creative - Demo request", created=None, **links):
    """A deal as Spill's website flow might make it: not ours, in Spill 3.0, changed within the lookback."""
    created = created or NOW - timedelta(hours=2)
    return crm.add("deals", {"pipeline": "pipe-spill3", "dealstage": stage, "dealname": name,
                             "createdate": created.isoformat(), "hs_lastmodifieddate": ms(created)}, **links)


def test_a_demo_request_on_the_website_is_a_booking_matched_by_the_company_domain(default_settings):
    """The prospect used the demo page without replying: the deal's company is new to us, found by its domain."""
    ctx, t, crm, stops = setup(default_settings, status="enrolled")
    co = crm.add("companies", {"name": "Acme Creative", "domain": "www.acmecreative.com"})
    did = demo_deal(crm, companies=[co])
    out = hubspot_readback(ctx)
    [booked] = events(ctx, "meeting_booked")
    assert (booked["event_id"], booked["account_id"], booked["source"]) == (f"hs-deal:{did}:booked", "acc-1",
                                                                            "hubspot_deal")
    assert [e["event_id"] for e in events(ctx, "deal_created")] == [f"hs-deal:{did}"]
    account = ctx.store.get("accounts", account_id="acc-1")
    assert account["status"] == "demo_requested" and account["hubspot_company_id"] == co
    assert stops == [{"lead_email": JANE, "campaign_id": "c-hannah", "interest_value": 2}]
    assert out["counts"]["demo_deals_seen"] == 1 and len(crm.objects["deals"]) == 1  # no deal of ours on top
    hubspot_readback(ctx)
    assert len(events(ctx, "meeting_booked")) == 1 and len(stops) == 1
    assert [w for w in crm.writes(t) if "/deals" in w.url] == []  # HubSpot is only read


def test_a_demo_request_matched_by_a_colleagues_email_and_the_meeting_counts_once(default_settings):
    ctx, t, crm, stops = setup(default_settings, status="enrolled")
    kid = crm.add("contacts", {"email": "sam.lee@acmecreative.com"})
    meeting(crm, contacts=[kid])  # the same booking, seen as a meeting first
    demo_deal(crm, stage="stage-created", contacts=[kid])
    hubspot_readback(ctx)
    [booked] = events(ctx, "meeting_booked")
    assert booked["source"] == "hubspot_meeting" and booked["account_id"] == "acc-1"
    assert ctx.store.get("accounts", account_id="acc-1")["status"] == "demo_booked"


def test_our_own_deal_is_a_booking_only_past_demo_requested(default_settings):
    ctx, t, crm, stops = setup(default_settings, status="demo_requested")
    co = crm.add("companies", {"name": "Acme Creative", "domain": "acmecreative.com"})
    did = demo_deal(crm, name="US Outbound – Acme Creative", companies=[co])  # made from a reply asking for a demo
    hubspot_readback(ctx)
    assert events(ctx, "meeting_booked") == []
    crm.objects["deals"][did].update(dealstage="stage-created", hs_lastmodifieddate=ms(NOW - timedelta(hours=1)))
    hubspot_readback(ctx)
    [booked] = events(ctx, "meeting_booked")
    assert booked["source"] == "hubspot_deal" and ctx.store.get("accounts", account_id="acc-1")["status"] == "demo_booked"


def test_deals_that_are_not_bookings_or_not_ours_are_left(default_settings):
    ctx, t, crm, stops = setup(default_settings, status="enrolled")
    co = crm.add("companies", {"name": "Acme Creative", "domain": "acmecreative.com"})
    demo_deal(crm, stage="stage-lost", companies=[co])  # Closed lost
    demo_deal(crm, created=NOW - timedelta(days=11), companies=[co])  # before our first enrolment (10 days ago)
    crm.add("deals", {"pipeline": "pipe-uk", "dealstage": "stage-first", "createdate": NOW.isoformat(),
                      "hs_lastmodifieddate": ms(NOW)}, companies=[co])  # another pipeline
    uk = crm.add("companies", {"name": "A UK customer", "domain": "ukcustomer.co.uk"})
    demo_deal(crm, name="A UK customer - Demo", companies=[uk])
    out = hubspot_readback(ctx)
    assert events(ctx, "meeting_booked") == [] and stops == []
    assert out["counts"]["demo_deals_not_ours"] == 1 and out["counts"]["demo_deals_seen"] == 1


def test_bookings_in_dry_run_are_reported_not_recorded(default_settings):
    ctx, t, crm, stops = setup(default_settings, live=False, status="enrolled")
    co = crm.add("companies", {"name": "Acme Creative", "domain": "acmecreative.com"})
    demo_deal(crm, companies=[co])
    out = hubspot_readback(ctx)
    assert out["counts"]["meeting_booked"] == 1 and any(e.get("source") == "hubspot_deal" for e in out["events"])
    assert events(ctx, "meeting_booked") == [] and stops == []
    assert ctx.store.get("accounts", account_id="acc-1")["status"] == "enrolled"


def test_a_demo_request_at_a_company_we_hold_is_counted_without_another_lookup(default_settings):
    """Step 2 meets the deal first (we hold the company's id) and records it; step 4 counts it from that record."""
    ctx, t, crm, stops = setup(default_settings, status="engaged", hubspot_company_id="co-x")
    crm.objects["companies"]["co-x"] = {"name": "Acme Creative", "domain": "acmecreative.com"}
    did = demo_deal(crm, companies=["co-x"])
    out = hubspot_readback(ctx)
    assert [(e["event_id"], e["source"]) for e in events(ctx, "meeting_booked")] == [(f"hs-deal:{did}:booked",
                                                                                     "hubspot_deal")]
    assert "demo_deals_looked_up" not in out["counts"] and len(stops) == 1
    assert ctx.store.get("accounts", account_id="acc-1")["status"] == "demo_requested"
    hubspot_readback(ctx)
    assert not [r for r in t.requests if r.method == "GET" and f"/deals/{did}" in r.url]  # never fetched


def test_a_meeting_records_its_source(default_settings):
    ctx, t, crm, stops = setup(default_settings, status="enrolled")
    kid = crm.add("contacts", {"email": JANE})
    meeting(crm, contacts=[kid])
    hubspot_readback(ctx)
    assert [e["source"] for e in events(ctx, "meeting_booked")] == ["hubspot_meeting"]


# -- W2-4 (20 Oct 2026): the negative cache, the booking read-back, the analytics source ---------------------------


def lookups(t, obj, rid):
    return [r for r in t.requests if r.method == "GET" and r.url.split("?")[0].endswith(f"/crm/v3/objects/{obj}/{rid}")]


def test_a_meeting_that_is_not_ours_is_looked_up_once_until_hubspot_changes_it(default_settings):
    """The negative cache (RS §2 W2-4): run 1 looks a "not ours" meeting up (get_record) and records it as
    readback_seen under its hs_lastmodifieddate; run 2 makes 0 lookups for it; once HubSpot changes the meeting (a
    later hs_lastmodifieddate) it is looked up again. Dry-run writes no cache row."""
    ctx, t, crm, stops = setup(default_settings, status="enrolled")
    other = crm.add("contacts", {"email": "pat@unrelated.org"})
    mid = meeting(crm, contacts=[other])
    first = hubspot_readback(ctx)
    assert len(lookups(t, "meetings", mid)) == 1 and first["counts"]["meetings_not_ours"] == 1
    stamp = crm.objects["meetings"][mid]["hs_lastmodifieddate"]
    [seen] = events(ctx, "readback_seen")
    assert seen["event_id"] == f"seen:meeting:{mid}:{stamp}"
    second = hubspot_readback(ctx)
    assert len(lookups(t, "meetings", mid)) == 1 and second["counts"]["meetings_not_ours_before"] == 1
    crm.objects["meetings"][mid]["hs_lastmodifieddate"] = ms(NOW - timedelta(minutes=5))  # HubSpot changed it
    hubspot_readback(ctx)
    assert len(lookups(t, "meetings", mid)) == 2 and len(events(ctx, "readback_seen")) == 2
    dry, t2, crm2, _ = setup(default_settings, live=False, status="enrolled")
    other2 = crm2.add("contacts", {"email": "pat@unrelated.org"})
    meeting(crm2, contacts=[other2])
    hubspot_readback(dry)
    assert events(dry, "readback_seen") == []


def test_a_booking_before_the_first_enrolment_is_not_ours(default_settings):
    """M6 alone (code-replies-crm): a booking inside the 3-day window, by our contact, but made before we enrolled
    anyone at the company (2 hours ago; the contact was enrolled 30 minutes ago): no event, no lead stopped, no deal."""
    ctx, t, crm, stops = setup(default_settings, status="enrolled")
    ctx.store.update("contacts", {"contact_id": "con-1"}, {"enrolled_at": NOW - timedelta(minutes=30)})
    kid = crm.add("contacts", {"email": JANE})
    meeting(crm, contacts=[kid], created=NOW - timedelta(hours=2))
    out = hubspot_readback(ctx)
    assert events(ctx, "meeting_booked") == [] and stops == [] and crm.objects["deals"] == {}
    assert "leads_stopped" not in out["counts"]


def instantly_lead(t, status):
    """Instantly's lead-1 as the read-back sees it; the requests to it are recorded."""
    t.route("GET", "api.instantly.ai/api/v2/leads/lead-1", {"id": "lead-1", "campaign": "c-hannah", "status": status})
    t.route("DELETE", "api.instantly.ai/api/v2/leads/lead-1", {"id": "lead-1"})


def test_a_booked_lead_still_active_after_stop_lead_is_deleted_and_recorded(default_settings):
    """M7 (code-replies-crm), until D10 d settles it: stop_lead marks the lead "Meeting booked"; the read-back shows
    it still active (status 1), so it is deleted, the stop that is certain: the contact gets lead_deleted_at and its
    lead id cleared, and a lead_stopped row with source meeting_booked; leads_stopped 1, leads_deleted 1."""
    ctx, t, crm, stops = setup(default_settings, status="enrolled")
    instantly_lead(t, 1)
    kid = crm.add("contacts", {"email": JANE})
    meeting(crm, contacts=[kid])
    out = hubspot_readback(ctx)
    assert stops == [{"lead_email": JANE, "campaign_id": "c-hannah", "interest_value": 2}]
    assert (out["counts"]["leads_stopped"], out["counts"]["leads_deleted"]) == (1, 1)
    assert [r.method for r in t.requests if r.url.endswith("/leads/lead-1")].count("DELETE") == 1
    c = ctx.store.get("contacts", contact_id="con-1")
    assert c["lead_deleted_at"] == NOW and c.get("instantly_lead_id") is None
    ev = ctx.store.get("events", event_id="lead_stopped:con-1")
    assert (ev["type"], ev["source"], ev["account_id"]) == ("lead_stopped", "meeting_booked", "acc-1")


def test_a_booked_lead_the_read_back_shows_stopped_is_not_deleted(default_settings):
    ctx, t, crm, stops = setup(default_settings, status="enrolled")
    instantly_lead(t, 3)  # completed: stop_lead did stop it
    kid = crm.add("contacts", {"email": JANE})
    meeting(crm, contacts=[kid])
    out = hubspot_readback(ctx)
    assert out["counts"]["leads_stopped"] == 1 and "leads_deleted" not in out["counts"]
    assert not [r for r in t.requests if r.method == "DELETE"]
    assert ctx.store.get("contacts", contact_id="con-1")["instantly_lead_id"] == "lead-1"
    assert ctx.store.get("events", event_id="lead_stopped:con-1") is None


def test_a_new_booking_records_where_the_booker_came_from(default_settings):
    """hs_analytics_source on meeting_booked (design §2.1 O1b): the booking's first contact's, else its first
    company's; read once, for the new event only."""
    ctx, t, crm, stops = setup(default_settings, status="enrolled")
    kid = crm.add("contacts", {"email": JANE, "hs_analytics_source": "PAID_SEARCH"})
    meeting(crm, contacts=[kid])
    hubspot_readback(ctx)
    [booked] = events(ctx, "meeting_booked")
    assert booked["analytics_source"] == "PAID_SEARCH"

    def source_reads():
        return [r for r in t.requests if "hs_analytics_source" in str(r.params or "") + r.url]

    assert len(source_reads()) == 1
    hubspot_readback(ctx)
    assert len(source_reads()) == 1  # not read again for a booking already recorded
    ctx2, t2, crm2, _ = setup(default_settings, status="enrolled")
    co = crm2.add("companies", {"name": "Acme Creative", "domain": "acmecreative.com", "hs_analytics_source": "OFFLINE"})
    kid2 = crm2.add("contacts", {"email": JANE})
    meeting(crm2, contacts=[kid2], companies=[co])
    hubspot_readback(ctx2)
    assert [e["analytics_source"] for e in events(ctx2, "meeting_booked")] == ["OFFLINE"]
