"""Instantly client: US-only campaigns by name, registry-filtered reads, dry-run, payload shapes."""

from datetime import UTC, datetime, time

import pytest

from tests.fakes import FakeTransport
from us_outbound.clients.guard import Guard, GuardViolation
from us_outbound.clients.instantly import (
    CAMPAIGN_SETTINGS,
    LEAD_IMPORT_OPTIONS,
    Instantly,
    instantly_schedule,
    settings_drift,
    step_delays,
)
from us_outbound.context import boundaries_for
from us_outbound.logs import redact
from us_outbound.settings.model import General, Mailbox, SendWindow, Settings

BASE = "https://api.instantly.ai/api/v2"
HANNAH, SAM, HARRY = "hannah@meetspill.org", "sam@meetspill.org", "harry@meetspill.org"
OUTSIDER = "anna@eu-outbound.example"
C_HANNAH = "US Outbound – Hannah Spalding"
C_HARRY = "US Outbound – Harry Dryden"
C_SAM = "US Outbound – Sam Jackson"
C_EU = "EU Outbound – Anna"

MAILBOXES = tuple(
    Mailbox(address, address.split("@")[1], owner, "Active", 30, instantly_account_id=address, slack_id=slack)
    for address, owner, slack in (
        (HANNAH, "Hannah Spalding", "U_HANNAH"),  # D11: Hannah approves replies to her own mailbox
        (SAM, "Sam Jackson", ""),
        (HARRY, "Harry Dryden", "U_HARRY"),
        ("harry@tryspill.org", "Harry Dryden", "U_HARRY"),
    )
)
SETTINGS = Settings(general=General(approver_slack_ids=("U_HARRY",)), mailboxes=MAILBOXES)

CAMPAIGNS = [
    {"id": "c-hannah", "name": C_HANNAH, "status": 2},
    {"id": "c-harry", "name": C_HARRY, "status": 2},
    {"id": "c-eu", "name": C_EU, "status": 1},  # shared workspace: must never be returned or touched
    {"id": "c-old", "name": "US Outbound old test", "status": 0},  # matches the search, not the prefix
]
STEPS = [{"subject": f"{{{{s{i}_subject}}}}", "body": f"{{{{s{i}_body}}}}"} for i in range(1, 5)]


def make(live=True):
    t = FakeTransport()
    pages = {
        None: {"items": CAMPAIGNS[:2], "next_starting_after": "c-harry"},
        "c-harry": {"items": CAMPAIGNS[2:], "next_starting_after": None},
    }
    t.route("GET", "/campaigns", fn=lambda req: pages[(req.params or {}).get("starting_after")])
    guard = Guard(live=live, bounds=boundaries_for(SETTINGS))
    return Instantly(guard, t, "inst-test"), t, guard


def urls(t, method=None):
    return [r.url for r in t.requests if method is None or r.method == method]


def test_list_campaigns_searches_us_outbound_and_keeps_only_the_prefix():
    inst, t, guard = make()
    got = inst.list_campaigns()
    assert [c["name"] for c in got] == [C_HANNAH, C_HARRY]
    first, second = t.requests
    assert first.url == f"{BASE}/campaigns"
    assert first.params == {"search": "US Outbound –", "limit": 100}
    assert second.params["starting_after"] == "c-harry"
    assert first.headers["Authorization"] == "Bearer inst-test"
    assert all(c.action == "campaign.list" and not c.write for c in guard.calls)


def test_name_resolution_is_cached():
    inst, t, guard = make()
    inst.pause_campaign(C_HANNAH)
    inst.activate_campaign(C_HANNAH)
    assert urls(t, "GET").count(f"{BASE}/campaigns") == 2  # one listing (two pages), then the cache
    assert urls(t, "POST") == [f"{BASE}/campaigns/c-hannah/pause", f"{BASE}/campaigns/c-hannah/activate"]
    assert [(c.action, c.target, c.sent) for c in guard.writes("instantly")] == [
        ("campaign.pause", C_HANNAH, True),
        ("campaign.activate", C_HANNAH, True),
    ]


@pytest.mark.parametrize("live", [True, False])
@pytest.mark.parametrize("name", [C_EU, "US Outbound old test", "us outbound – hannah spalding"])
def test_non_us_campaign_refused_before_any_request(live, name):
    inst, t, _ = make(live)
    calls = [
        lambda: inst.get_campaign(name),
        lambda: inst.pause_campaign(name),
        lambda: inst.activate_campaign(name),
        lambda: inst.update_campaign(name, {"daily_limit": 10}),
        lambda: inst.add_leads(name, [{"email": "jane@acme.example"}]),
        lambda: inst.list_leads(name),
        lambda: inst.delete_lead(name, "l1"),
        lambda: inst.step_analytics(name),
        lambda: inst.create_campaign(name, accounts=[HANNAH], daily_limit=30, steps=STEPS),
    ]
    for call in calls:
        with pytest.raises(GuardViolation):
            call()
    assert t.requests == []


def test_eu_campaign_in_the_list_is_never_touched():
    inst, t, guard = make()
    t.route("GET", "/campaigns/c-hannah", body={"id": "c-hannah", "name": C_HANNAH})
    t.route("POST", "/leads/list", body={"items": [], "next_starting_after": None})
    assert all(c["id"] != "c-eu" for c in inst.list_campaigns())
    inst.get_campaign(C_HANNAH)
    inst.pause_campaign(C_HARRY)
    inst.list_leads(C_HANNAH)
    for r in t.requests:
        assert "c-eu" not in r.url and "c-eu" not in str(r.params) and "c-eu" not in str(r.json)
    assert all(C_EU not in c.target for c in guard.calls)


def test_unknown_or_duplicate_us_campaign():
    inst, t, _ = make()
    assert inst.get_campaign("US Outbound – Nobody Here") is None
    with pytest.raises(LookupError):
        inst.pause_campaign("US Outbound – Nobody Here")
    assert all("/pause" not in u for u in urls(t))

    inst, t, _ = make()
    t.route("GET", "/campaigns", body={"items": [CAMPAIGNS[0], {"id": "c-other", "name": C_HANNAH}]})
    with pytest.raises(LookupError, match="more than one"):
        inst.pause_campaign(C_HANNAH)
    assert urls(t, "POST") == []


def test_get_campaign_checks_the_response():
    inst, t, _ = make()
    t.route("GET", "/campaigns/c-hannah", body={"id": "c-hannah", "name": C_HANNAH, "text_only": True})
    assert inst.get_campaign(C_HANNAH)["text_only"] is True
    t.route("GET", "/campaigns/c-hannah", body={"id": "c-eu", "name": C_EU})
    with pytest.raises(GuardViolation):
        inst.get_campaign(C_HANNAH)


def test_create_campaign_payload_and_left_paused():
    inst, t, guard = make(live=True)
    t.route("POST", "/campaigns", fn=lambda req: {"id": "c-sam", "name": req.json["name"], "status": 0})
    out = inst.create_campaign(C_SAM, accounts=["Sam@MeetSpill.org"], daily_limit=30, steps=STEPS)
    assert out["id"] == "c-sam"
    [post] = t.writes()
    assert post.url == f"{BASE}/campaigns"
    body = post.json
    assert body["name"] == C_SAM
    assert body["email_list"] == [SAM]
    assert body["daily_limit"] == 30
    for key, value in CAMPAIGN_SETTINGS.items():
        assert body[key] == value, key
    assert body["open_tracking"] is False and body["link_tracking"] is False
    assert body["stop_on_reply"] and body["stop_for_company"] and not body["stop_on_auto_reply"]
    assert body["text_only"] is False  # html by default: the copy's links are embedded (email_format)
    assert body["insert_unsubscribe_header"] and not body["allow_risky_contacts"]
    assert body["is_evergreen"] is True
    [sched] = body["campaign_schedule"]["schedules"]
    assert sched["timezone"] == "America/Detroit"
    assert sched["timing"] == {"from": "09:00", "to": "16:00"}
    assert sched["days"] == {"0": False, "1": True, "2": True, "3": True, "4": True, "5": True, "6": False}
    [seq] = body["sequences"]
    assert [s["delay"] for s in seq["steps"]] == [7, 7, 7, 0]
    assert all(s["type"] == "email" and s["delay_unit"] == "days" and len(s["variants"]) == 1 for s in seq["steps"])
    assert seq["steps"][0]["variants"][0] == {"subject": "{{s1_subject}}", "body": "{{s1_body}}"}
    assert not any("/activate" in u for u in urls(t))
    [rec] = guard.writes("instantly")
    assert (rec.action, rec.target, rec.sent) == ("campaign.create", C_SAM, True)
    t.route("POST", "/pause", body={})
    inst.pause_campaign(C_SAM)  # the new id is cached
    assert urls(t, "POST")[-1] == f"{BASE}/campaigns/c-sam/pause"


def test_create_campaign_refusals():
    inst, t, _ = make(live=True)
    with pytest.raises(GuardViolation):
        inst.create_campaign("US Outbound – Anna Berg", accounts=[HANNAH], daily_limit=30, steps=STEPS)
    with pytest.raises(GuardViolation):
        inst.create_campaign(C_SAM, accounts=[OUTSIDER], daily_limit=30, steps=STEPS)
    with pytest.raises(GuardViolation):
        inst.create_campaign(C_SAM, accounts=[SAM], daily_limit=30, steps=STEPS, options={"open_tracking": True})
    assert t.requests == []
    with pytest.raises(ValueError):
        inst.create_campaign(C_SAM, accounts=[SAM], daily_limit=30, steps=STEPS, options={"stop_on_auto_reply": True})
    with pytest.raises(ValueError):
        inst.create_campaign(C_SAM, accounts=[SAM], daily_limit=30, steps=STEPS[:3])
    with pytest.raises(ValueError, match="already exists"):
        inst.create_campaign(C_HANNAH, accounts=[HANNAH], daily_limit=30, steps=STEPS)
    assert t.writes() == []


def test_update_campaign():
    inst, t, _ = make(live=True)
    inst.update_campaign(C_HANNAH, {"daily_limit": 60}, accounts=[HANNAH])
    [patch] = t.writes()
    assert (patch.method, patch.url) == ("PATCH", f"{BASE}/campaigns/c-hannah")
    assert patch.json == {"daily_limit": 60, "email_list": [HANNAH]}
    with pytest.raises(GuardViolation):
        inst.update_campaign(C_HANNAH, {"link_tracking": True})
    with pytest.raises(GuardViolation):
        inst.update_campaign(C_HANNAH, {"daily_limit": 60}, accounts=[HANNAH, OUTSIDER])
    for bad in ({"name": C_HARRY}, {"email_list": [HANNAH]}, {"text_only": "no"}, {"stop_on_reply": False}):
        with pytest.raises(ValueError):
            inst.update_campaign(C_HANNAH, bad)
    with pytest.raises(ValueError):
        inst.update_campaign(C_HANNAH, {}, accounts=[])
    assert len(t.writes()) == 1


def test_dry_run_makes_no_instantly_writes():
    inst, t, guard = make(live=False)
    t.route("GET", "/leads/l1", body={"id": "l1", "campaign": "c-hannah"})
    t.route("GET", "/emails/e1", body={"id": "e1", "eaccount": HANNAH, "subject": "Re: hello", "campaign_id": "c-hannah"})
    assert inst.create_campaign(C_SAM, accounts=[SAM], daily_limit=30, steps=STEPS)["dry_run"] is True
    inst.update_campaign(C_HANNAH, {"daily_limit": 60}, accounts=[HANNAH])
    inst.pause_campaign(C_HANNAH)
    inst.activate_campaign(C_HANNAH)
    assert inst.add_leads(C_HANNAH, [{"email": "jane@acme.example"}])["dry_run"] is True
    inst.delete_lead(C_HANNAH, "l1")
    assert inst.reply(HANNAH, "e1", "Re: hello", "Thanks", approved_by="U_HARRY")["dry_run"] is True
    assert inst.stop_lead(C_HANNAH, "jane@acme.example")["dry_run"] is True
    assert inst.forward(HANNAH, "e1", "harry@spill.chat", "Waiting 24h")["dry_run"] is True
    inst.blocklist_add(["jane@acme.example"])
    inst.enable_warmup([HANNAH])
    assert t.writes() == []
    writes = guard.writes("instantly")
    assert len(writes) == 11 and not any(w.sent for w in writes)


def test_list_emails_outside_registry_refused_before_any_request():
    inst, t, guard = make()
    with pytest.raises(GuardViolation):
        inst.list_emails([HANNAH, OUTSIDER])
    with pytest.raises(GuardViolation):
        inst.list_emails([])
    assert t.requests == []
    assert guard.calls and not any(c.sent for c in guard.calls)


def test_list_emails_one_filtered_call_per_account_and_drops_others():
    inst, t, guard = make()
    pages = {
        (HANNAH, None): {"items": [{"id": "e1", "eaccount": HANNAH}, {"id": "x1", "eaccount": OUTSIDER}], "next_starting_after": "e1"},
        (HANNAH, "e1"): {"items": [{"id": "e2", "eaccount": "Hannah@MeetSpill.org"}], "next_starting_after": None},
        (SAM, None): {"items": [{"id": "e3", "eaccount": SAM}, {"id": "e1", "eaccount": SAM}]},
    }
    t.route("GET", "/emails", fn=lambda req: pages[(req.params["eaccount"], req.params.get("starting_after"))])
    since = datetime(2026, 10, 27, 12, 0, tzinfo=UTC)
    got = inst.list_emails([HANNAH, SAM], since=since, email_type="received")
    assert [e["id"] for e in got] == ["e1", "e2", "e3"]
    assert [r.params["eaccount"] for r in t.requests] == [HANNAH, HANNAH, SAM]
    assert all(r.url == f"{BASE}/emails" for r in t.requests)
    assert all(r.params["min_timestamp_created"] == "2026-10-27T12:00:00Z" for r in t.requests)
    assert all(r.params["email_type"] == "received" for r in t.requests)
    # The guard records (and logs) every account read; its records hash the addresses.
    assert [list(c.detail["accounts"]) for c in guard.calls] == [redact([a]) for a in (HANNAH, HANNAH, SAM)]


def test_list_accounts_and_warmup_status_filtered_to_registry():
    inst, t, _ = make()
    t.route("GET", f"/accounts/{HANNAH}", body={
        "email": HANNAH, "status": 1, "warmup_status": 1, "stat_warmup_score": 98,
        "timestamp_warmup_start": "2026-09-01T00:00:00Z", "daily_limit": 30,
    })
    t.route("GET", f"/accounts/{SAM}", status=404, body={"message": "not found"})
    t.route("POST", "/accounts/warmup-analytics", body={
        "aggregate_data": {HANNAH: {"health_score": 97, "health_score_label": "Excellent"}, OUTSIDER: {"health_score": 1}},
    })
    assert [a["email"] for a in inst.list_accounts([HANNAH, SAM])] == [HANNAH]
    status = inst.warmup_status([HANNAH, SAM])
    assert set(status) == {HANNAH, SAM}
    assert status[HANNAH]["warmup_enabled"] is True
    assert (status[HANNAH]["warmup_score"], status[HANNAH]["health_score_label"], status[HANNAH]["status"]) == (98, "Excellent", "active")
    assert status[SAM]["found"] is False and status[SAM]["warmup_enabled"] is False
    analytics = [r for r in t.requests if r.url.endswith("/accounts/warmup-analytics")]
    assert analytics[0].json == {"emails": [HANNAH]}  # Sam is not in Instantly: left out
    assert not any(r.url == f"{BASE}/accounts" for r in t.requests)  # never an unfiltered account list

    inst, t, _ = make()
    with pytest.raises(GuardViolation):
        inst.warmup_status([OUTSIDER])
    with pytest.raises(GuardViolation):
        inst.list_accounts([HANNAH, OUTSIDER])
    assert t.requests == []


def test_enable_warmup():
    inst, t, _ = make(live=True)
    inst.enable_warmup([HANNAH, "HANNAH@meetspill.org", SAM])
    [post] = t.writes()
    assert (post.url, post.json) == (f"{BASE}/accounts/warmup/enable", {"emails": [HANNAH, SAM]})
    with pytest.raises(GuardViolation):
        inst.enable_warmup([OUTSIDER])
    assert len(t.writes()) == 1


def test_add_leads_chunks_and_merges():
    inst, t, guard = make(live=True)

    def added(req):
        return {"status": "success", "total_sent": len(req.json["leads"]), "leads_uploaded": len(req.json["leads"]) - 1,
                "skipped_count": 1, "remaining_in_plan": 5000,
                "created_leads": [{"index": 0, "id": f"lead-{len(req.json['leads'])}", "email": req.json["leads"][0]["email"]}]}

    t.route("POST", "/leads/add", fn=added)
    leads = [
        {"email": f"Person{i}@Acme.example", "first_name": "Pat", "last_name": "Lee", "company_name": "Acme",
         "custom_variables": {"s1_subject": "Hello", "s1_body": "Hi Pat,\nline two", "step_count": 4}}
        for i in range(1001)
    ]
    out = inst.add_leads(C_HANNAH, leads)
    first, second = t.writes()
    assert first.url == f"{BASE}/leads/add"
    assert first.json["campaign_id"] == "c-hannah" and len(first.json["leads"]) == 1000 and len(second.json["leads"]) == 1
    for key, value in LEAD_IMPORT_OPTIONS.items():
        assert first.json[key] == value
    assert first.json["leads"][0]["email"] == "person0@acme.example"
    assert out["total_sent"] == 1001 and out["leads_uploaded"] == 999 and out["skipped_count"] == 2
    assert [c["index"] for c in out["created_leads"]] == [0, 1000]
    assert [c.detail["count"] for c in guard.writes("instantly")] == [1000, 1]

    for bad in (
        [{"email": "a@acme.example", "assigned_to": "u1"}],
        [{"first_name": "No email"}],
        [{"email": "a@acme.example", "custom_variables": {"nested": {"x": 1}}}],
    ):
        with pytest.raises(ValueError):
            inst.add_leads(C_HANNAH, bad)


def test_list_leads_filtered_to_the_campaign():
    inst, t, _ = make()
    t.route("POST", "/leads/list", body={"items": [
        {"id": "l1", "campaign": "c-hannah", "email": "a@acme.example"},
        {"id": "l2", "campaign": "c-eu", "email": "b@acme.example"},
    ], "next_starting_after": None})
    assert [x["id"] for x in inst.list_leads(C_HANNAH)] == ["l1"]
    [req] = [r for r in t.requests if r.url.endswith("/leads/list")]
    assert req.json == {"campaign": "c-hannah", "limit": 100}


def test_delete_lead_checks_its_campaign():
    inst, t, _ = make(live=True)
    t.route("GET", "/leads/l1", body={"id": "l1", "campaign": "c-hannah"})
    t.route("GET", "/leads/l2", body={"id": "l2", "campaign": "c-eu"})
    t.route("GET", "/leads/l3", status=404, body={})
    inst.delete_lead(C_HANNAH, "l1")
    with pytest.raises(GuardViolation):
        inst.delete_lead(C_HANNAH, "l2")
    inst.delete_lead(C_HANNAH, "l3")
    assert [(r.method, r.url) for r in t.writes()] == [("DELETE", f"{BASE}/leads/l1")]


def test_reply_from_the_mailbox_that_received_it():
    inst, t, guard = make(live=True)
    t.route("GET", "/emails/e1", body={"id": "e1", "eaccount": HANNAH, "campaign_id": "c-hannah"})
    t.route("GET", "/emails/e2", body={"id": "e2", "eaccount": SAM, "campaign_id": "c-hannah"})
    inst.reply("Hannah@meetspill.org", "e1", "Re: hello", "Hi Jane,\nGreat to hear <3", approved_by="U_HARRY")
    [post] = t.writes()
    assert post.url == f"{BASE}/emails/reply"
    assert post.json == {
        "eaccount": HANNAH,
        "reply_to_uuid": "e1",
        "subject": "Re: hello",
        "body": {"text": "Hi Jane,\nGreat to hear <3", "html": "Hi Jane,<br/>Great to hear &lt;3"},
    }
    [rec] = guard.writes("instantly")
    assert rec.detail["accounts"] == redact([HANNAH])
    assert (rec.detail["campaign"], rec.detail["approved_by"]) == (C_HANNAH, "U_HARRY")
    with pytest.raises(GuardViolation):
        inst.reply(HANNAH, "e2", "Re: hello", "Hi", approved_by="U_HARRY")  # e2 was received by Sam
    n = len(t.requests)
    with pytest.raises(GuardViolation):
        inst.reply(OUTSIDER, "e1", "Re: hello", "Hi", approved_by="U_HARRY")
    assert len(t.requests) == n and len(t.writes()) == 1


def test_reply_only_in_a_us_outbound_thread():
    inst, t, guard = make(live=True)
    t.route("GET", "/emails/e3", body={"id": "e3", "eaccount": HANNAH, "campaign_id": "c-eu"})
    t.route("GET", "/emails/e4", body={"id": "e4", "eaccount": HANNAH})  # no campaign at all
    for email_id in ("e3", "e4"):
        with pytest.raises(GuardViolation, match="US Outbound"):
            inst.reply(HANNAH, email_id, "Re: hello", "Hi", approved_by="U_HARRY")
    assert t.writes() == []
    refused = [c for c in guard.calls if c.action == "email.reply"]
    assert refused and not any(c.sent for c in refused)


def test_reply_needs_an_approver_d11():
    """SPEC 1.3 as changed by D11: approver_slack_ids, or the owner for their own mailbox, or the CLI command."""
    inst, t, guard = make(live=True)

    def email(req):
        email_id = req.url.rsplit("/", 1)[1]
        return {"id": email_id, "eaccount": email_id.split("-")[0], "subject": "Hello Jane", "campaign_id": "c-hannah"}

    t.route("GET", "/emails/", fn=email)
    hannah_email, sam_email = f"{HANNAH}-1", f"{SAM}-1"
    for mailbox, email_id, by in ((HANNAH, hannah_email, ""), (HANNAH, hannah_email, "U_SAM"),
                                  (SAM, sam_email, "U_HANNAH"), (HANNAH, hannah_email, "cli")):
        n = len(t.requests)
        with pytest.raises(GuardViolation, match="approver"):
            inst.reply(mailbox, email_id, None, "Hi", approved_by=by)
        assert len(t.requests) == n  # refused before reading the email
    inst.reply(HANNAH, hannah_email, None, "Hi Jane", approved_by="U_HANNAH")  # her own mailbox
    inst.reply(SAM, sam_email, None, "Hi Jane", approved_by="U_HARRY")
    guard.configure(job="replies_approve")
    inst.reply(SAM, sam_email, None, "Hi Jane", approved_by="cli")
    posts = [r for r in t.writes() if r.url.endswith("/emails/reply")]
    assert [p.json["eaccount"] for p in posts] == [HANNAH, SAM, SAM]
    assert {p.json["subject"] for p in posts} == {"Re: Hello Jane"}  # no subject given: "Re: " the original's


def test_reply_subject_keeps_one_re():
    from us_outbound.clients.instantly import reply_subject

    assert reply_subject("Re: hello") == "Re: hello" and reply_subject("RE hello") == "RE hello"
    assert reply_subject("hello") == "Re: hello" and reply_subject(None) == "Re:"


def test_stop_lead_marks_meeting_booked_in_the_campaign():
    inst, t, guard = make(live=True)
    inst.stop_lead(C_HANNAH, "Jane@Acme.example")
    [post] = t.writes()
    assert post.url == f"{BASE}/leads/update-interest-status"
    assert post.json == {"lead_email": "jane@acme.example", "campaign_id": "c-hannah", "interest_value": 2}
    assert guard.writes("instantly")[0].action == "lead.stop"
    n = len(t.requests)
    with pytest.raises(GuardViolation):
        inst.stop_lead(C_EU, "jane@acme.example")
    assert len(t.requests) == n


def test_forward_with_the_original_thread():
    inst, t, _ = make(live=True)
    t.route("GET", "/emails/e1", body={"id": "e1", "eaccount": HANNAH, "subject": "Re: hello"})
    inst.forward(HANNAH, "e1", "harry@spill.chat", "Waiting 24 hours.\nSlack: link")
    [post] = t.writes()
    assert post.url == f"{BASE}/emails/forward"
    assert post.json["eaccount"] == HANNAH and post.json["reply_to_uuid"] == "e1"
    assert post.json["to_address_email_list"] == "harry@spill.chat"
    assert post.json["subject"] == "Fwd: Re: hello"
    assert post.json["include_original_body"] is True
    assert post.json["body"]["text"] == "Waiting 24 hours.\nSlack: link"


def test_forward_goes_only_to_escalation_email():
    inst, t, _ = make(live=True)
    t.route("GET", "/emails/e1", body={"id": "e1", "eaccount": HANNAH, "subject": "Re: hello"})
    for to in ("someone@other.com", ["harry@spill.chat", "someone@other.com"]):
        with pytest.raises(GuardViolation, match="escalation_email"):
            inst.forward(HANNAH, "e1", to, "Waiting 24 hours.")
    assert t.requests == []  # refused before even reading the email
    inst.forward(HANNAH, "e1", "Harry@Spill.chat", "Waiting 24 hours.")
    assert len(t.writes()) == 1


def test_blocklist_add_emails_only():
    inst, t, _ = make(live=True)
    inst.blocklist_add(["Jane@Acme.example", "jane@acme.example", "bob@acme.example"])
    [post] = t.writes()
    assert (post.url, post.json) == (f"{BASE}/block-lists-entries/bulk-create", {"bl_values": ["jane@acme.example", "bob@acme.example"]})
    with pytest.raises(ValueError):
        inst.blocklist_add(["acme.example"])
    inst.blocklist_add([])
    assert len(t.writes()) == 1


def test_step_analytics_always_filtered_by_campaign():
    inst, t, _ = make()
    t.route("GET", "/campaigns/analytics/steps", body=[{"step": "0", "variant": "0", "sent": 10}])
    assert inst.step_analytics(C_HANNAH, start_date="2026-10-01")[0]["sent"] == 10
    [req] = [r for r in t.requests if "analytics" in r.url]
    assert req.params == {"campaign_id": "c-hannah", "start_date": "2026-10-01"}


def test_schedule_days_and_step_delays():
    assert step_delays() == [7, 7, 7, 0]
    weekend = instantly_schedule(SendWindow((5, 6), time(10), time(12), "America/Chicago"))["schedules"][0]
    assert {k for k, v in weekend["days"].items() if v} == {"6", "0"}  # Saturday, Sunday
    assert weekend["timezone"] == "America/Chicago"


def test_settings_drift():
    inst, t, _ = make(live=True)
    t.route("POST", "/campaigns", fn=lambda req: {"id": "c-sam", "name": req.json["name"]})
    inst.create_campaign(C_SAM, accounts=[SAM], daily_limit=30, steps=STEPS)
    campaign = dict(t.writes()[0].json)
    assert settings_drift(campaign, accounts=[SAM], daily_limit=30) == {}
    campaign["link_tracking"] = True
    campaign["email_list"] = [SAM, HANNAH]
    campaign["campaign_schedule"] = instantly_schedule(SendWindow((0, 1, 2, 3, 4, 5), time(9), time(16), "America/New_York"))
    drift = settings_drift(campaign, accounts=[SAM], daily_limit=60)
    assert set(drift) == {"link_tracking", "email_list", "schedule.days", "daily_limit"}
    assert drift["link_tracking"] == (False, True)
