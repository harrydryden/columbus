"""erase --email (SPEC 6): database rows go, the suppression hash stays; HubSpot and Instantly only when live."""

from tests.fakes import FakeTransport, make_context
from tests.test_registry import C_HANNAH, SETTINGS, FakeInstantly
from us_outbound.logs import hash_email
from us_outbound.ops.erase import erase
from us_outbound.suppression import is_suppressed

JANE = "Jane.Doe@Acme.com"
SHA = hash_email(JANE)


def setup(live: bool):
    t = FakeTransport()
    inst = FakeInstantly(t)
    t.route("POST", "/crm/v3/objects/contacts/search",
            body={"results": [{"id": "901", "properties": {"email": "jane.doe@acme.com"}}]})
    ctx = make_context(SETTINGS, live=live, transport=t, job="erase")
    ours = inst.add_campaign(C_HANNAH)
    eu = inst.add_campaign("EU Outbound – Anna", status=1)
    lead = inst.add_lead(ours["id"], "jane.doe@acme.com")
    inst.add_lead(ours["id"], "john@acme.com")
    inst.add_lead(eu["id"], "jane.doe@acme.com")  # another team's lead: never listed, never touched
    s = ctx.store
    s.insert("contacts", [
        {"contact_id": "k1", "account_id": "a1", "email": "jane.doe@acme.com", "email_sha256": SHA,
         "first_name": "Jane", "instantly_campaign": C_HANNAH, "instantly_lead_id": lead["id"]},
        {"contact_id": "k2", "account_id": "a1", "email": "Jane.Doe@acme.com", "email_sha256": None},
        {"contact_id": "k3", "account_id": "a1", "email": "john@acme.com", "email_sha256": hash_email("john@acme.com")},
    ])
    s.insert("events", [
        {"event_id": "e1", "contact_id": "k1", "account_id": "a1", "type": "replied", "reply_text": "Call me on 555"},
        {"event_id": "e2", "contact_id": "k3", "account_id": "a1", "type": "replied", "reply_text": "Not now"},
    ])
    s.insert("hitl_items", [{"item_id": "h1", "contact_id": "k1", "payload": {"draft": "Hi Jane"}, "status": "open"}])
    s.insert("raw_clay_contacts", [
        {"key": "acme.com|Jane Doe", "payload": {"email": "jane.doe@acme.com"}},
        {"key": "acme.com|John Roe", "payload": {"email": "john@acme.com"}},
    ])
    return ctx, t, inst, lead


def test_erase_in_dry_run_clears_the_database_but_sends_nothing():
    ctx, t, inst, lead = setup(live=False)
    report = erase(ctx, JANE)
    s = ctx.store
    assert [c["contact_id"] for c in s.tables["contacts"]] == ["k3"]
    events = {e["event_id"]: e for e in s.tables["events"]}
    assert events["e1"]["reply_text"] is None and events["e1"]["type"] == "replied"
    assert events["e2"]["reply_text"] == "Not now"
    assert s.tables["hitl_items"][0]["payload"] is None
    assert [r["key"] for r in s.tables["raw_clay_contacts"]] == ["acme.com|John Roe"]
    [supp] = s.tables["suppression"]
    assert supp["email_sha256"] == SHA and supp["reason"] == "erasure" and supp["domain"] is None
    assert is_suppressed(s, email=JANE, now=ctx.now)
    assert report["database"]["contacts_deleted"] == 2
    assert ctx.guard.writes("hubspot", sent=True) == [] and ctx.guard.writes("instantly", sent=True) == []
    assert [r for r in t.requests if r.method == "DELETE" or "gdpr" in r.url] == []
    assert lead["id"] in inst.leads
    assert report["hubspot"] == {"contacts_found": 1, "deleted": False}
    assert report["instantly"]["leads_found"] == 1 and report["instantly"]["deleted"] == 0
    assert report["clay"]["manual"] is True and any("Clay" in step for step in report["manual_steps"])
    assert "jane" not in repr(report).lower()


def test_erase_live_deletes_in_hubspot_and_instantly():
    ctx, t, inst, lead = setup(live=True)
    report = erase(ctx, JANE)
    [gdpr] = [r for r in t.requests if r.url.endswith("/crm/v3/objects/contacts/gdpr-delete")]
    assert gdpr.json == {"objectId": "jane.doe@acme.com", "idProperty": "email"}
    deletes = [r.url for r in t.requests if r.method == "DELETE"]
    assert deletes == [f"https://api.instantly.ai/api/v2/leads/{lead['id']}"]
    assert sorted(x["email"] for x in inst.leads.values()) == ["jane.doe@acme.com", "john@acme.com"]  # EU lead stays
    assert report["hubspot"]["deleted"] is True and report["instantly"]["deleted"] == 1
    assert report["instantly"]["campaigns"] == [C_HANNAH]


def test_erase_twice_is_harmless():
    ctx, t, inst, lead = setup(live=False)
    erase(ctx, JANE)
    again = erase(ctx, JANE)
    assert again["database"]["contacts_deleted"] == 0 and again["database"]["suppression"] == "already suppressed"
    assert len(ctx.store.tables["suppression"]) == 1


def test_erase_reaches_where_else_the_address_is_written_and_names_what_it_cannot():
    """A colleague's reply card that names them, a reply quoting their address, and the manual steps for Slack, the
    escalation inbox and HubSpot's note and task (7 Oct 2026)."""
    from datetime import UTC, datetime

    ctx, t, inst, lead = setup(live=False)
    s = ctx.store
    s.update("hitl_items", {"item_id": "h1"}, {"slack_ts": "1.1", "created_at": datetime(2026, 10, 5, 11, tzinfo=UTC),
                                               "escalated_at": datetime(2026, 10, 6, 11, tzinfo=UTC),
                                               "payload": {"draft": "Hi Jane", "hubspot": {"note_id": "n9",
                                                                                           "task_id": "t9"}}})
    s.insert("hitl_items", [
        {"item_id": "h-referral", "contact_id": "k3", "kind": "reply", "status": "handled",
         "payload": {"reply_class": "referral", "reply_excerpt": "Ask Jane: jane.doe@acme.com",
                     "referral": {"name": "Jane Doe", "email": "Jane.Doe@acme.com"}, "mailbox": "hannah@meetspill.org"}},
        {"item_id": "h-elsewhere", "contact_id": "k3", "kind": "reply", "status": "handled",
         "payload": {"reply_class": "other", "note": "cc jane.doe@acme.com"}},
        {"item_id": "h-unrelated", "contact_id": "k3", "kind": "reply", "payload": {"reply_excerpt": "Not now"}},
    ])
    s.insert("events", [{"event_id": "e3", "contact_id": "k3", "account_id": "a1", "type": "replied",
                         "reply_text": "Write to jane.doe@acme.com instead", "reply_class": "referral"}])
    report = erase(ctx, JANE)
    items = {i["item_id"]: i for i in s.tables["hitl_items"]}
    referral = items["h-referral"]["payload"]
    assert referral["reply_excerpt"] is None and referral["referral"] is None and referral["reply_class"] == "referral"
    assert items["h-elsewhere"]["payload"] is None  # the address was outside the reply text: the payload goes
    assert items["h-unrelated"]["payload"] == {"reply_excerpt": "Not now"}
    events = {e["event_id"]: e for e in s.tables["events"]}
    assert events["e3"]["reply_text"] is None and events["e2"]["reply_text"] == "Not now"
    assert report["database"]["mentions_cleared"] == {"hitl_items": 2, "events_reply_text": 1}
    steps = report["manual_steps"]
    assert any(x.startswith("Slack: 1 card in #us-outbound showed them (posted Mon 05 Oct 2026 12:00 UK)") for x in steps)
    assert any(x.startswith("Inbox: their reply was escalated to harry@spill.chat (Tue 06 Oct 2026 12:00 UK)")
               for x in steps)
    assert any("delete note n9, task t9 by hand" in x for x in steps)
    assert "jane" not in repr(report).lower()


def test_erase_without_traces_lists_only_clay_and_instantly():
    ctx, t, inst, lead = setup(live=True)
    report = erase(ctx, JANE)
    assert [x.split(":")[0] for x in report["manual_steps"]] == ["Clay", "Instantly"]
