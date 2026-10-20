"""hubspot_readback (crm/readback.py): meetings booked, demos held and deals, read back from HubSpot (SPEC 9, 11)."""

from __future__ import annotations

from datetime import timedelta

from tests.test_hubspot_writes import ACCOUNT, HANNAH, NOW, world
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


# -- step 5: records HubSpot made at companies we emailed (Harry, 20 Oct 2026) ----------------------------------------
# NOW is Mon 5 Oct 15:00 UTC; LOOKBACK_DAYS is 3, so the createdate searches reach back to Fri 2 Oct 15:00. acc-1
# (acmecreative.com) is emailed once it has a sent event at step 1; its first one is its "emailed at".

FORM = "#wf-form-Demo-Form .form-3"
WORKFLOW = "[Sales] Demo Requested via Website"


def emailed(ctx, account_id="acc-1", at=NOW - timedelta(days=5), step=1, contact_id="con-1"):
    ctx.store.upsert("events", [{"event_id": f"sent:{account_id}:{contact_id}:{step}:{at.isoformat()}", "type": "sent",
                                 "step": step, "account_id": account_id, "contact_id": contact_id, "mailbox": HANNAH,
                                 "occurred_at": at}])


def made(label, detail=None, created=None, **props):
    """A record's properties as HubSpot's createdate search returns them: createdate ISO text, how it was made."""
    created = created or NOW - timedelta(hours=2)
    return {"createdate": created.isoformat(), "hs_lastmodifieddate": ms(created), "hs_object_source_label": label,
            **({"hs_object_source_detail_1": detail} if detail else {}), **props}


def test_a_form_contact_after_step_1_is_recorded_for_the_person_we_emailed(default_settings):
    """Jane (con-1, first emailed 30 Sep) fills the website's demo form 2 hours ago: her email hashes to con-1, so one
    hubspot_contact_created for acc-1 with contact_id con-1, source "form" (FORM lower-cased), source_detail the form's
    name, occurred_at HubSpot's createdate. A second run records nothing more."""
    ctx, t, crm, stops = setup(default_settings, status="enrolled")
    emailed(ctx)
    kid = crm.add("contacts", {"email": JANE, **made("FORM", FORM)})
    out = hubspot_readback(ctx)
    [e] = events(ctx, "hubspot_contact_created")
    assert (e["event_id"], e["account_id"], e["contact_id"], e["source"], e["source_detail"], e["occurred_at"]) == (
        f"hs-contact:{kid}", "acc-1", "con-1", "form", FORM, NOW - timedelta(hours=2))
    assert (out["counts"]["created_contacts_seen"], out["counts"]["hubspot_contact_created"]) == (1, 1)
    again = hubspot_readback(ctx)
    assert len(events(ctx, "hubspot_contact_created")) == 1 and "hubspot_contact_created" not in again["counts"]
    assert crm.writes(t) == []  # HubSpot is only read


def test_a_colleague_at_the_emailed_company_counts_with_no_contact(default_settings):
    """sam.lee@acmecreative.com is nobody we hold; the email's domain is acc-1's: account matched, contact_id None
    (a colleague), source "extension" (logged from an email through the HubSpot sales extension, no detail)."""
    ctx, t, crm, stops = setup(default_settings, status="enrolled")
    emailed(ctx)
    kid = crm.add("contacts", {"email": "Sam.Lee@acmecreative.com", **made("EXTENSION")})
    hubspot_readback(ctx)
    [e] = events(ctx, "hubspot_contact_created")
    assert (e["event_id"], e["account_id"], e["contact_id"], e["source"], e["source_detail"]) == (
        f"hs-contact:{kid}", "acc-1", None, "extension", None)


def test_personal_mail_a_company_never_emailed_and_a_stranger_count_for_nothing(default_settings):
    """Three new contacts, none ours: someone@gmail.com (an emailed account row on gmail.com is here only so that the
    personal-mail rule is what refuses it; admit never makes one), pat@brightfin.com (acc-2 is enrolled but has no
    step-1 send) and pat@unrelated.org (no account): 3 seen, 3 not ours, 0 events."""
    ctx, t, crm, stops = setup(default_settings, status="enrolled")
    ctx.store.upsert("accounts", [
        {"account_id": "acc-g", "domain": "gmail.com", "clean_name": "Gmail", "status": "enrolled"},
        {"account_id": "acc-2", "domain": "brightfin.com", "clean_name": "Brightfin", "status": "enrolled"}])
    emailed(ctx)
    emailed(ctx, "acc-g")
    for email in ("someone@gmail.com", "pat@brightfin.com", "pat@unrelated.org"):
        crm.add("contacts", {"email": email, **made("FORM", FORM)})
    out = hubspot_readback(ctx)
    assert events(ctx, "hubspot_contact_created") == []
    assert (out["counts"]["created_contacts_seen"], out["counts"]["created_not_ours"]) == (3, 3)


def test_a_record_made_before_the_first_step_1_is_not_counted(default_settings):
    """acc-1's first step 1 went 1 day ago (a later step 2 changes nothing); Jane's contact and the company were made 2
    days ago, inside the 3-day window but before we emailed: 0 events, created_before_emailed 2."""
    ctx, t, crm, stops = setup(default_settings, status="enrolled")
    emailed(ctx, at=NOW - timedelta(days=1))
    emailed(ctx, at=NOW - timedelta(hours=12), step=2)
    crm.add("contacts", {"email": JANE, **made("FORM", FORM, created=NOW - timedelta(days=2))})
    crm.add("companies", {"name": "Acme Creative", "domain": "acmecreative.com",
                          **made("CRM_SETTING", created=NOW - timedelta(days=2))})
    out = hubspot_readback(ctx)
    assert events(ctx, "hubspot_contact_created") == [] and events(ctx, "hubspot_company_created") == []
    assert out["counts"]["created_before_emailed"] == 2


def test_a_company_made_from_the_new_contact_matches_by_domain(default_settings):
    """HubSpot's "Create and associate companies with contacts" setting made the company (www.acmecreative.com, root
    acmecreative.com = acc-1's domain): one hubspot_company_created, source crm_setting, contact_id None."""
    ctx, t, crm, stops = setup(default_settings, status="enrolled")
    emailed(ctx)
    setting = '"Create and associate companies with contacts" setting'
    co = crm.add("companies", {"name": "Acme Creative", "domain": "www.acmecreative.com",
                               **made("CRM_SETTING", setting)})
    out = hubspot_readback(ctx)
    [e] = events(ctx, "hubspot_company_created")
    assert (e["event_id"], e["account_id"], e["contact_id"], e["source"], e["source_detail"]) == (
        f"hs-company:{co}", "acc-1", None, "crm_setting", setting)
    assert out["counts"]["created_companies_seen"] == 1


def test_a_workflow_deal_matched_through_its_company_is_recorded_once_and_read_once(default_settings):
    """A HubSpot workflow made a deal in the sales pipeline (not Spill 3.0, so steps 2 and 4 never see it) on a
    company with acc-1's domain: one get_record (associations), then _match reads the company's domain: deal_created
    hs-deal:{id}, source automation_platform, detail the workflow's name. Run 2: the event exists, so 0 more reads of
    the deal and still 1 event. A deal at a UK company is not ours: one read, a readback_seen row on its
    hs_lastmodifieddate, and run 2 reads it 0 more times (created_deals_not_ours_before 1)."""
    ctx, t, crm, stops = setup(default_settings, status="enrolled")
    emailed(ctx)
    co = crm.add("companies", {"name": "Acme Creative", "domain": "acmecreative.com"})
    did = crm.add("deals", {"dealname": "Acme Creative", "pipeline": "pipe-sales", "dealstage": "s1",
                            **made("AUTOMATION_PLATFORM", WORKFLOW)}, companies=[co])
    uk = crm.add("companies", {"name": "A UK customer", "domain": "ukcustomer.co.uk"})
    other = crm.add("deals", {"dealname": "A UK customer", "pipeline": "pipe-sales", "dealstage": "s1",
                              **made("AUTOMATION_PLATFORM", WORKFLOW)}, companies=[uk])
    first = hubspot_readback(ctx)
    [e] = events(ctx, "deal_created")
    assert (e["event_id"], e["account_id"], e["contact_id"], e["source"], e["source_detail"]) == (
        f"hs-deal:{did}", "acc-1", None, "automation_platform", WORKFLOW)
    counts = first["counts"]
    assert (counts["created_deals_seen"], counts["deal_created"], counts["created_not_ours"]) == (2, 1, 1)
    [seen] = events(ctx, "readback_seen")
    assert seen["event_id"] == f"seen:deal:{other}:{crm.objects['deals'][other]['hs_lastmodifieddate']}"
    assert (len(lookups(t, "deals", did)), len(lookups(t, "deals", other))) == (1, 1)
    second = hubspot_readback(ctx)
    assert len(events(ctx, "deal_created")) == 1 and "deal_created" not in second["counts"]
    assert (len(lookups(t, "deals", did)), len(lookups(t, "deals", other))) == (1, 1)
    assert second["counts"]["created_deals_not_ours_before"] == 1
    assert ctx.store.get("accounts", account_id="acc-1")["status"] == "enrolled"  # a signal only: no status moves


def test_columbus_s_own_deal_and_company_are_labelled_not_counted_again(default_settings):
    """ensure_deal made "US Outbound – Acme Creative" and recorded it (deal_created, no source); record_reply made the
    company through our private app (INTEGRATION) and we hold its id. The deal is not recorded twice: its row gets
    source "columbus" (created_source_filled 1, deal_created not counted, the deal never read); the company is
    hubspot_company_created with source "columbus". A Spill 3.0 website deal step 4 records as a booking (its
    deal_created with no source) is labelled the same way on the run that records it."""
    from us_outbound.crm.hubspot_writes import record_deal

    ctx, t, crm, stops = setup(default_settings, status="demo_requested", hubspot_company_id="co-x")
    emailed(ctx)
    crm.objects["companies"]["co-x"] = {"name": "Acme Creative", "domain": "acmecreative.com", **made("INTEGRATION")}
    did = crm.add("deals", {"dealname": "US Outbound – Acme Creative", "pipeline": "pipe-spill3",
                            "dealstage": "stage-first", **made("INTEGRATION")}, companies=["co-x"])
    record_deal(ctx, "acc-1", None, did, NOW - timedelta(hours=2))
    out = hubspot_readback(ctx)
    [deal] = events(ctx, "deal_created")
    assert (deal["event_id"], deal["source"], deal["source_detail"]) == (f"hs-deal:{did}", "columbus", None)
    assert out["counts"]["created_source_filled"] == 1 and "deal_created" not in out["counts"]
    [company] = events(ctx, "hubspot_company_created")
    assert (company["event_id"], company["source"]) == ("hs-company:co-x", "columbus")
    assert len(crm.objects["deals"]) == 1 and lookups(t, "deals", did) == []
    web = crm.add("deals", {"dealname": "Acme Creative - Demo request", "pipeline": "pipe-spill3",
                            "dealstage": "stage-first", **made("AUTOMATION_PLATFORM", WORKFLOW)}, companies=["co-x"])
    hubspot_readback(ctx)
    row = ctx.store.get("events", event_id=f"hs-deal:{web}")
    assert (row["type"], row["source"], row["source_detail"]) == ("deal_created", "automation_platform", WORKFLOW)
    assert len(events(ctx, "deal_created")) == 2


def test_step_5_in_dry_run_reports_and_writes_nothing(default_settings):
    """A form contact, a company made from it, a workflow deal on that company and a deal on no company, in dry-run:
    counted 1, 1, 1 and 1 not ours, and listed in the summary's events; no event row, no readback_seen row, no
    HubSpot write. Our own "US Outbound – Acme Creative" deal, recorded by ensure_deal with no source before the run:
    step 5 would label it (created_source_filled 1) but dry-run leaves its row as it was, source and source_detail
    None (steps 2 to 4 pass it by: no company id held, status enrolled, and ours at Demo requested is no booking)."""
    from us_outbound.crm.hubspot_writes import record_deal

    ctx, t, crm, stops = setup(default_settings, live=False, status="enrolled")
    emailed(ctx)
    crm.add("contacts", {"email": JANE, **made("FORM", FORM)})
    co = crm.add("companies", {"name": "Acme Creative", "domain": "acmecreative.com", **made("CRM_SETTING")})
    crm.add("deals", {"dealname": "Acme Creative", "pipeline": "pipe-sales", **made("AUTOMATION_PLATFORM", WORKFLOW)},
            companies=[co])
    crm.add("deals", {"dealname": "Someone else", "pipeline": "pipe-sales", **made("AUTOMATION_PLATFORM", WORKFLOW)})
    ours = crm.add("deals", {"dealname": "US Outbound – Acme Creative", "pipeline": "pipe-spill3",
                             "dealstage": "stage-first", **made("INTEGRATION")}, companies=[co])
    record_deal(ctx, "acc-1", None, ours, NOW - timedelta(hours=2))
    out = hubspot_readback(ctx)
    c = out["counts"]
    assert (c["hubspot_contact_created"], c["hubspot_company_created"], c["deal_created"], c["created_not_ours"]) == (
        1, 1, 1, 1)
    assert c["created_source_filled"] == 1
    assert {e["event"] for e in out["events"]} == {"hubspot_contact_created", "hubspot_company_created", "deal_created"}
    for type_ in ("hubspot_contact_created", "hubspot_company_created", "readback_seen"):
        assert events(ctx, type_) == []
    [row] = events(ctx, "deal_created")
    assert (row["event_id"], row.get("source"), row.get("source_detail")) == (f"hs-deal:{ours}", None, None)
    assert crm.writes(t) == []


def test_a_failing_search_in_step_5_stops_nothing_else_and_the_reverse(default_settings):
    """HubSpot answers 400 to the contacts search: steps 1 to 4 still record the booking, and the run says which step
    failed. The other way round: the meetings search fails, and step 5 still records the form contact."""
    from us_outbound.clients.http import Response

    ctx, t, crm, stops = setup(default_settings, status="enrolled")
    emailed(ctx)
    kid = crm.add("contacts", {"email": JANE, **made("FORM", FORM)})
    meeting(crm, contacts=[kid])
    t.route("POST", "/crm/v3/objects/contacts/search", fn=lambda r: Response(400, {"message": "bad"}))
    out = hubspot_readback(ctx)
    assert len(events(ctx, "meeting_booked")) == 1 and events(ctx, "hubspot_contact_created") == []
    assert [e.split(":")[0] for e in out["errors"]] == ["created contacts"]
    ctx2, t2, crm2, _ = setup(default_settings, status="enrolled")
    emailed(ctx2)
    kid2 = crm2.add("contacts", {"email": JANE, **made("FORM", FORM)})
    meeting(crm2, contacts=[kid2])
    t2.route("POST", "/crm/v3/objects/meetings/search", fn=lambda r: Response(400, {"message": "bad"}))
    out2 = hubspot_readback(ctx2)
    assert events(ctx2, "meeting_booked") == [] and len(events(ctx2, "hubspot_contact_created")) == 1
    assert [e.split(":")[0] for e in out2["errors"]] == ["meetings"]


# -- step 5, review notes (20 Oct 2026): the first step 1, our own ids, one dry-run report, the read volume, isolation --


def test_the_account_s_first_step_1_counts_and_a_record_made_at_that_moment_counts(default_settings):
    """acc-1 (acmecreative.com): con-1's step 1 went 5 days ago, con-2's 1 day ago, so acc-1 was first emailed 5 days
    ago; a colleague's contact made 2 days ago is after that: 1 hubspot_contact_created (with the latest step 1, 1
    day ago, it would be before). acc-2 (brightfin.com) has only a step 2, 6 days ago (con-3): never sent step 1, so
    its new contact is not ours (created_not_ours 1; counting any step would make it ours). acc-3 (northwind.com)
    was first sent step 1 exactly 2 days ago and HubSpot made its company at that same instant: it counts
    (createdate >= the first step 1), 1 hubspot_company_created. created_before_emailed 0."""
    ctx, t, crm, stops = setup(default_settings, status="enrolled")
    ctx.store.upsert("accounts", [
        {"account_id": "acc-2", "domain": "brightfin.com", "clean_name": "Brightfin", "status": "enrolled"},
        {"account_id": "acc-3", "domain": "northwind.com", "clean_name": "Northwind", "status": "enrolled"}])
    emailed(ctx, at=NOW - timedelta(days=5))
    emailed(ctx, at=NOW - timedelta(days=1), contact_id="con-2")
    emailed(ctx, "acc-2", at=NOW - timedelta(days=6), step=2, contact_id="con-3")
    emailed(ctx, "acc-3", at=NOW - timedelta(days=2), contact_id="con-4")
    sam = crm.add("contacts", {"email": "sam.lee@acmecreative.com", **made("EXTENSION", created=NOW - timedelta(days=2))})
    crm.add("contacts", {"email": "pat@brightfin.com", **made("FORM", FORM, created=NOW - timedelta(days=2))})
    north = crm.add("companies", {"name": "Northwind", "domain": "northwind.com",
                                  **made("CRM_SETTING", created=NOW - timedelta(days=2))})
    out = hubspot_readback(ctx)
    assert [(e["event_id"], e["account_id"], e["contact_id"]) for e in events(ctx, "hubspot_contact_created")] == [
        (f"hs-contact:{sam}", "acc-1", None)]
    assert [(e["event_id"], e["account_id"]) for e in events(ctx, "hubspot_company_created")] == [
        (f"hs-company:{north}", "acc-3")]
    assert out["counts"]["created_not_ours"] == 1 and "created_before_emailed" not in out["counts"]


def test_a_contact_our_private_app_made_whose_id_we_hold_is_columbus_s(default_settings):
    """record_reply made Jane's HubSpot contact (INTEGRATION) and stored its id on con-1. HubSpot holds her under
    another address (jane@acme-creative.co: no hash, no domain of ours), so only the stored id matches: one
    hubspot_contact_created for acc-1, contact_id con-1, source "columbus" (not "integration")."""
    ctx, t, crm, stops = setup(default_settings, status="enrolled")
    emailed(ctx)
    kid = crm.add("contacts", {"email": "jane@acme-creative.co", **made("INTEGRATION")})
    ctx.store.update("contacts", {"contact_id": "con-1"}, {"hubspot_contact_id": kid})
    hubspot_readback(ctx)
    [e] = events(ctx, "hubspot_contact_created")
    assert (e["event_id"], e["account_id"], e["contact_id"], e["source"]) == (
        f"hs-contact:{kid}", "acc-1", "con-1", "columbus")


def test_a_company_whose_id_we_hold_counts_on_another_domain(default_settings):
    """acc-1 holds HubSpot company co-x, whose domain is acme-creative.co (not acc-1's acmecreative.com): matched by
    the stored id alone, one hubspot_company_created for acc-1, source "crm_ui" (made by hand)."""
    ctx, t, crm, stops = setup(default_settings, status="enrolled", hubspot_company_id="co-x")
    emailed(ctx)
    crm.objects["companies"]["co-x"] = {"name": "Acme Creative", "domain": "acme-creative.co", **made("CRM_UI")}
    hubspot_readback(ctx)
    [e] = events(ctx, "hubspot_company_created")
    assert (e["event_id"], e["account_id"], e["contact_id"], e["source"]) == ("hs-company:co-x", "acc-1", None, "crm_ui")


def test_a_contact_columbus_made_for_a_colleague_who_replied_is_columbus_s(default_settings):
    """Sam (acmecreative.com) replied for Jane; record_reply made Sam's HubSpot contact (INTEGRATION) and kept its id
    on the reply item only (payload.hubspot.contact_id, contact_created; a colleague's id is never on our contacts
    row). One hubspot_contact_created for acc-1, contact_id None (a colleague), source "columbus", which the daily
    post shows as "by Columbus after their reply"; emailed 30 Sep (NOW - 5 days, 16:00 UK)."""
    from us_outbound.learn.daily_post import hubspot_created
    from us_outbound.replies.desk import save_payload

    ctx, t, crm, stops = setup(default_settings, status="engaged")
    emailed(ctx)
    sam = crm.add("contacts", {"email": "sam.lee@acmecreative.com", **made("INTEGRATION")})
    save_payload(ctx.store, "item-0001-aaaa", hubspot={"status": "written", "contact_id": sam, "contact_created": True,
                                                       "replier": "sam.lee@acmecreative.com"})
    hubspot_readback(ctx)
    [e] = events(ctx, "hubspot_contact_created")
    assert (e["event_id"], e["account_id"], e["contact_id"], e["source"]) == (
        f"hs-contact:{sam}", "acc-1", None, "columbus")
    lines, _ = hubspot_created(ctx, [e], {"acc-1": "Acme Creative"})
    assert lines == ["Acme Creative: contact (a colleague) (by Columbus after their reply) · emailed 30 Sep"]


def test_a_deal_step_4_reported_in_a_dry_run_is_reported_once(default_settings):
    """Dry-run: a Spill 3.0 website demo request at acc-1's company. Step 4 reports its deal_created (and the
    booking) and writes nothing; step 5 meets the same deal in its createdate search and, as step 4 noted it this
    run, neither reports it again nor looks it up: deal_created 1, one summary entry for it, one get_record of the
    deal (step 4's)."""
    ctx, t, crm, stops = setup(default_settings, live=False, status="enrolled")
    emailed(ctx)
    co = crm.add("companies", {"name": "Acme Creative", "domain": "acmecreative.com"})
    did = demo_deal(crm, companies=[co])
    crm.objects["deals"][did].update(hs_object_source_label="AUTOMATION_PLATFORM", hs_object_source_detail_1=WORKFLOW)
    out = hubspot_readback(ctx)
    assert out["counts"]["deal_created"] == 1 and out["counts"]["meeting_booked"] == 1
    assert [e["event"] for e in out["events"] if e["id"] == f"hs-deal:{did}"] == ["deal_created"]
    assert len(lookups(t, "deals", did)) == 1 and events(ctx, "deal_created") == []


def counted_reads(ctx) -> list[str]:
    """The tables of every store read (select, and get and latest through it) from here on."""
    calls: list[str] = []
    select = ctx.store.select

    def counting(table, where=None):
        calls.append(table)
        return select(table, where)

    ctx.store.select = counting
    return calls


def test_a_bulk_import_costs_no_more_reads_than_a_few_records(default_settings):
    """A HubSpot import of 2,000 contacts in the window, none ours (40 domains no account has): step 5 reads what it
    matches against once a run and the contacts' events rows in one select, so the run makes as many store reads as
    with 20 such contacts: 6. Step 2's enrolled accounts (1); step 5's step-1 sends, their accounts, their contacts
    and their reply items (4), and the events rows of the 2,000 contacts' ids (1); no meeting, deal or company is
    found, so nothing more. All 2,000 are seen and not ours, 0 events. (A select per record, as before, would be
    2,000 or more.)"""
    def run(n):
        ctx, t, crm, stops = setup(default_settings, status="enrolled")
        emailed(ctx)
        for i in range(n):
            crm.add("contacts", {"email": f"person{i}@import{i % 40}.example.com", **made("IMPORT")})
        reads = counted_reads(ctx)
        out = hubspot_readback(ctx)
        made_reads = len(reads)
        assert (out["counts"]["created_contacts_seen"], out["counts"]["created_not_ours"]) == (n, n)
        assert events(ctx, "hubspot_contact_created") == []
        return made_reads

    assert run(20) == run(2000) == 6


def test_a_failing_contacts_search_leaves_the_companies_and_deals_to_run(default_settings):
    """HubSpot answers 400 to step 5's contacts search: the company made from the form contact and the workflow deal
    on it are still recorded (1 hubspot_company_created, 1 deal_created), the contact is not, and the run says which
    part failed ("created contacts")."""
    from us_outbound.clients.http import Response

    ctx, t, crm, stops = setup(default_settings, status="enrolled")
    emailed(ctx)
    crm.add("contacts", {"email": JANE, **made("FORM", FORM)})
    co = crm.add("companies", {"name": "Acme Creative", "domain": "acmecreative.com", **made("CRM_SETTING")})
    did = crm.add("deals", {"dealname": "Acme Creative", "pipeline": "pipe-sales", "dealstage": "s1",
                            **made("AUTOMATION_PLATFORM", WORKFLOW)}, companies=[co])
    t.route("POST", "/crm/v3/objects/contacts/search", fn=lambda r: Response(400, {"message": "bad"}))
    out = hubspot_readback(ctx)
    assert events(ctx, "hubspot_contact_created") == []
    assert [e["event_id"] for e in events(ctx, "hubspot_company_created")] == [f"hs-company:{co}"]
    assert [e["event_id"] for e in events(ctx, "deal_created")] == [f"hs-deal:{did}"]
    assert [e.split(":")[0] for e in out["errors"]] == ["created contacts"]
