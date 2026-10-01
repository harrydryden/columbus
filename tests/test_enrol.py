"""The enrol job (SPEC 9, 10, 11): gates, the daily number, candidates, HubSpot re-check,
sender continuity, test versions, and dry-run versus live Instantly writes."""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta

import pytest

from tests.fakes import FakeTransport, make_context
from tests.test_render import (
    AGENCIES,
    BODIES,
    COPY,
    DEMO,
    EAP_OPENER,
    FIRST_TEST,
    HANNAH,
    HARRY_M,
    HARRY_T,
    SAM,
    SUBJECTS,
    account,
    contact,
    copy_row,
    make_settings,
)
from us_outbound import suppression
from us_outbound.enrol import enrol, queue
from us_outbound.logs import hash_email

NOW = datetime(2026, 10, 27, 11, 0, tzinfo=UTC)  # Tuesday 11:00 UK, 07:00 ET (ISO week 2026-W44)
INSTANTLY = "https://api.instantly.ai/api/v2"
CAMPAIGNS = {
    "items": [
        {"id": "c-hannah", "name": "US Outbound – Hannah Spalding"},
        {"id": "c-sam", "name": "US Outbound – Sam Jackson"},
        {"id": "c-harry", "name": "US Outbound – Harry Dryden"},
        {"id": "c-eu", "name": "EU Outbound – Anna"},
    ]
}
CAMPAIGN_OWNER = {"c-hannah": "Hannah Spalding", "c-sam": "Sam Jackson", "c-harry": "Harry Dryden"}


@pytest.fixture(autouse=True)
def default_openers(monkeypatch):
    """Openers come from scoring (another module); here each angle's default opener."""

    def opener(ctx, a):
        angle = ctx.settings.angle(str(a.get("angle") or ""))
        return (angle.default_opener if angle else ""), ""

    monkeypatch.setattr(enrol, "account_opener", opener)


def added(req):
    return {
        "status": "success",
        "total_sent": len(req.json["leads"]),
        "leads_uploaded": len(req.json["leads"]),
        "created_leads": [{"index": i, "id": f"lead-{lead['email']}", "email": lead["email"]}
                          for i, lead in enumerate(req.json["leads"])],
    }


def accounts3() -> list[dict]:
    return [
        account(),  # Priority, Upgrade the EAP
        account(account_id="acc-2", domain="brightfin.com", clean_name="Brightfin", industry="Fintech",
                industry_group="Technology & Startups", tier="Standard", score=30, angle="General",
                employees=30, size_band="20-49", sender="Hannah Spalding"),
        account(account_id="acc-3", domain="loopstudio.com", clean_name="Loop Studio", tier="Control", score=5,
                angle="General", employees=15, size_band="10-19", hq_city="", hq_state="NY"),
    ]


def contacts3() -> list[dict]:
    return [
        contact(),
        contact(contact_id="con-2", account_id="acc-2", first_name="Omar", last_name="Reyes", role="Founder or executive",
                email="omar@brightfin.com", email_status="catch_all_valid", person_state="NY"),
        contact(contact_id="con-3", account_id="acc-3", first_name="Lee", last_name="Park", role="Operations",
                email="lee@loopstudio.com", email_status="valid", person_state="NY"),
    ]


def make(*, live=False, settings=None, now=NOW, accounts=None, contacts=None, hand_check="handled", transport=None):
    s = settings or make_settings(live_sending=live)
    t = transport or FakeTransport()
    t.route("GET", "/campaigns", CAMPAIGNS)
    t.route("POST", "/crm/v3/objects/companies/search", {"results": []})
    t.route("POST", "/crm/v3/objects/contacts/search", {"results": []})
    t.route("POST", "/crm/v3/objects/deals/search", {"results": []})
    t.route("POST", "/leads/add", fn=added)
    ctx = make_context(s, live=live, now=now, transport=t)
    st = ctx.store
    st.insert("accounts", accounts if accounts is not None else accounts3())
    st.insert("contacts", contacts if contacts is not None else contacts3())
    if hand_check:
        st.insert("hitl_items", [{"item_id": "hc-1", "kind": "hand_check", "status": hand_check,
                                  "created_at": now - timedelta(days=1), "payload": {"iso_week": "2026-W44"}}])
    return ctx, t


def instantly_posts(t: FakeTransport):
    return [r for r in t.requests if r.url.startswith(INSTANTLY) and r.method != "GET"]


def rows(ctx, table, key):
    return {r[key]: r for r in ctx.store.select(table)}


# -- dry-run ---------------------------------------------------------------------------------


def test_dry_run_sends_nothing_and_marks_nothing():
    ctx, t = make()
    out = enrol.run(ctx)
    assert out["status"] == "ok" and out["dry_run"] is True
    assert out["prepared"] == 3 and out["enrolled"] == 0
    assert sum(out["by_owner"].values()) == 3
    assert instantly_posts(t) == []
    assert ctx.guard.writes("instantly", sent=True) == []
    refused = ctx.guard.writes("instantly", sent=False)
    assert refused and all(c.action == "lead.add" and c.target.startswith("US Outbound – ") for c in refused)
    for a in ctx.store.select("accounts"):
        assert a["status"] == "verified"
    assert ctx.store.get("accounts", account_id="acc-1").get("sender") is None
    assert all(not c.get("enrolment_month") and not c.get("instantly_lead_id") for c in ctx.store.select("contacts"))


# -- live -------------------------------------------------------------------------------------


def test_live_posts_leads_only_to_us_outbound_campaigns_with_custom_variables():
    ctx, t = make(live=True)
    out = enrol.run(ctx)
    assert out["status"] == "ok" and out["enrolled"] == 3, out
    posts = instantly_posts(t)
    assert posts and all(r.url == f"{INSTANTLY}/leads/add" for r in posts)
    assert all(c.target.startswith("US Outbound – ") for c in ctx.guard.writes("instantly"))
    by_campaign = {r.json["campaign_id"]: r.json["leads"] for r in posts}
    assert set(by_campaign) == {"c-harry", "c-hannah", "c-sam"}
    emails = {CAMPAIGN_OWNER[cid]: sorted(lead["email"] for lead in leads) for cid, leads in by_campaign.items()}
    # New accounts fill owners in proportion to capacity: Harry first (the most free slots),
    # then Sam, whose share is fuller than Harry's after one; Omar's account keeps Hannah.
    assert emails == {"Harry Dryden": ["jane@acmecreative.com"], "Sam Jackson": ["lee@loopstudio.com"],
                      "Hannah Spalding": ["omar@brightfin.com"]}
    for leads in by_campaign.values():
        for lead in leads:
            cv = lead["custom_variables"]
            assert set(cv) == {f"s{i}_{p}" for i in range(1, 5) for p in ("subject", "body")}
            assert "{{" not in "".join(cv.values())
            assert "on-demand counseling for your team" in cv["s4_body"]  # the signature
    jane = next(lead for lead in by_campaign["c-harry"] if lead["email"] == "jane@acmecreative.com")
    assert jane["first_name"] == "Jane" and jane["company_name"] == "Acme Creative"
    assert EAP_OPENER in jane["custom_variables"]["s1_body"]
    omar = by_campaign["c-hannah"][0]
    for lead in (jane, omar):
        assert f'<a href="{DEMO}">' in lead["custom_variables"]["s2_body"]  # every email's call to action


def test_live_records_enrollment_and_keeps_senders():
    ctx, _ = make(live=True)
    enrol.run(ctx)
    accts = rows(ctx, "accounts", "account_id")
    assert {a["status"] for a in accts.values()} == {"enrolled"}
    assert accts["acc-1"]["sender"] == "Harry Dryden"
    assert accts["acc-2"]["sender"] == "Hannah Spalding"  # set before, never changed
    cons = rows(ctx, "contacts", "contact_id")
    jane, omar = cons["con-1"], cons["con-2"]
    assert jane["enrolment_month"] == "2026-10"
    assert jane["angle"] == "Upgrade the EAP" and jane["copy_version"] == "agencies-v1" and jane["test_id"] is None
    assert jane["instantly_campaign"] == "US Outbound – Harry Dryden"
    assert jane["instantly_lead_id"] == "lead-jane@acmecreative.com"
    assert jane["mailbox"] is None  # Harry has two Active addresses; sync_outcomes records the one that sent
    assert omar["mailbox"] == "hannah@meetspill.org"
    assert omar["copy_version"] == "general-v1"  # Fintech has no row of its own, nor has its group


def test_live_flag_without_live_sending_does_nothing():
    ctx, t = make(live=True, settings=make_settings(live_sending=False))
    out = enrol.run(ctx)
    assert out["status"] == "skipped" and out["reason"] == "live_sending is no"
    assert instantly_posts(t) == []


def test_instantly_skipping_a_lead_leaves_it_unenrolled():
    def partial(req):
        body = added(req)
        body["created_leads"] = [c for c in body["created_leads"] if c["email"] != "lee@loopstudio.com"]
        return body

    t = FakeTransport()
    ctx, _ = make(live=True, transport=t)
    t.route("POST", "/leads/add", fn=partial)
    out = enrol.run(ctx)
    assert out["enrolled"] == 2 and out["skipped"]["not added by Instantly"] == 1
    assert ctx.store.get("accounts", account_id="acc-3")["status"] == "verified"
    assert not ctx.store.get("contacts", contact_id="con-3").get("instantly_lead_id")


def test_instantly_api_error_is_recorded_and_marks_nothing():
    t = FakeTransport()
    ctx, _ = make(live=True, transport=t)
    t.route("POST", "/leads/add", {"error": "server"}, status=500)
    out = enrol.run(ctx)
    assert out["enrolled"] == 0 and len(out["errors"]) == 3  # one per sender's campaign
    assert {a["status"] for a in ctx.store.select("accounts")} == {"verified"}


def test_missing_campaign_is_an_error_not_a_crash():
    t = FakeTransport()
    ctx, _ = make(live=True, transport=t)
    t.route("GET", "/campaigns", {"items": [{"id": "c-hannah", "name": "US Outbound – Hannah Spalding"}]})
    out = enrol.run(ctx)
    assert out["enrolled"] == 1 and any("US Outbound – Harry Dryden" in e for e in out["errors"])
    assert any("US Outbound – Sam Jackson" in e for e in out["errors"])
    assert ctx.store.get("accounts", account_id="acc-1")["status"] == "verified"


# -- test versions -------------------------------------------------------------------------------


V2_SUBJECTS = {**SUBJECTS, 1: "A shorter note for {{company}}"}
TEST_COPY = COPY + (copy_row("agencies-v2", AGENCIES, subjects=V2_SUBJECTS),)


def test_running_test_splits_version_a_accounts_and_records_the_test():
    accts = [account(account_id=f"ag-{i}", domain=f"ag{i}.com", clean_name=f"Agency {i}") for i in range(8)]
    cons = [contact(contact_id=f"c-{i}", account_id=f"ag-{i}", email=f"p{i}@ag{i}.com") for i in range(8)]
    s = make_settings(live_sending=True, tests=(FIRST_TEST,), copy=TEST_COPY)
    ctx, t = make(live=True, settings=s, accounts=accts, contacts=cons)
    enrol.run(ctx)
    leads = {lead["email"]: lead for r in instantly_posts(t) for lead in r.json["leads"]}
    versions = set()
    for i in range(8):
        con = ctx.store.get("contacts", contact_id=f"c-{i}")
        want = "agencies-v1" if queue.test_version(f"ag-{i}", FIRST_TEST.test_id) == "a" else "agencies-v2"
        assert con["test_id"] == FIRST_TEST.test_id and con["copy_version"] == want
        subject = leads[f"p{i}@ag{i}.com"]["custom_variables"]["s1_subject"]
        assert subject.startswith("A shorter note" if want == "agencies-v2" else "Support for the")
        assert con["angle"] == "Upgrade the EAP"  # the account's angle, whichever copy it got
        versions.add(want)
    assert versions == {"agencies-v1", "agencies-v2"}


def test_control_and_other_copy_stay_out_of_the_test():
    s = make_settings(live_sending=True, tests=(FIRST_TEST,), copy=TEST_COPY)
    ctx, _ = make(live=True, settings=s)
    enrol.run(ctx)
    assert ctx.store.get("contacts", contact_id="con-1")["test_id"] == FIRST_TEST.test_id
    assert ctx.store.get("contacts", contact_id="con-2")["test_id"] is None  # General copy
    assert ctx.store.get("contacts", contact_id="con-3")["test_id"] is None  # Control


def test_no_approved_copy_skips_the_account_and_says_what_waits():
    s = make_settings(copy=(COPY[0], copy_row("general-v1", "General", status="draft")))
    ctx, _ = make(settings=s)
    out = enrol.run(ctx)
    assert out["skipped"]["no approved copy"] == 1 and out["prepared"] == 2
    [skip] = [x for x in out["skipped_accounts"] if x["reason"] == "no approved copy"]
    assert skip["account_id"] == "acc-2" and "general-v1 is a draft" in skip["detail"][1]


def test_a_draft_industry_row_falls_back_to_general_and_says_so():
    s = make_settings(live_sending=True, copy=(copy_row("agencies-v1", AGENCIES, status="draft"), COPY[1]))
    ctx, _ = make(live=True, settings=s)
    out = enrol.run(ctx)
    assert out["enrolled"] == 3 and out["copy_sendable"] == 1
    assert {c["copy_version"] for c in ctx.store.select("contacts")} == {"general-v1"}
    assert out["copy_fallbacks"] == {"sent general-v1: agencies-v1 is a draft": 2}


def test_unchecked_copy_is_not_sent():
    s = make_settings(live_sending=True, copy=(copy_row("agencies-v1", AGENCIES, qa=False), COPY[1]))
    ctx, _ = make(live=True, settings=s)
    out = enrol.run(ctx)
    assert {c["copy_version"] for c in ctx.store.select("contacts")} == {"general-v1"}
    assert "agencies-v1 is approved but has not passed QA" in next(iter(out["copy_fallbacks"]))


def test_the_most_specific_row_wins_label_then_role():
    label = copy_row("advertising-v1", "Advertising agencies")
    ops = copy_row("agencies-ops-v1", AGENCIES, role="Operations")
    s = make_settings(live_sending=True, copy=COPY + (ops, label))
    ctx, _ = make(live=True, settings=s)
    enrol.run(ctx)
    cons = rows(ctx, "contacts", "contact_id")
    assert cons["con-1"]["copy_version"] == "advertising-v1"  # its own label beats its group
    assert cons["con-3"]["copy_version"] == "advertising-v1"  # the label beats a role row for the group
    s = make_settings(live_sending=True, copy=COPY + (ops,))
    ctx, _ = make(live=True, settings=s)
    enrol.run(ctx)
    assert rows(ctx, "contacts", "contact_id")["con-3"]["copy_version"] == "agencies-ops-v1"  # Lee is Operations


# -- gates ------------------------------------------------------------------------------------


def test_blackout_date_skips():
    ctx, _ = make(now=datetime(2026, 11, 24, 12, 0, tzinfo=UTC))
    out = enrol.run(ctx)
    assert out["status"] == "skipped" and "blackout" in out["reason"]


def test_weekend_skips():
    ctx, _ = make(now=datetime(2026, 10, 31, 12, 0, tzinfo=UTC))
    assert "not a send day" in enrol.run(ctx)["reason"]


def test_positive_reply_waiting_over_24_hours_pauses_enrollment():
    ctx, _ = make()
    ctx.store.insert("hitl_items", [{"item_id": "r-1", "kind": "reply", "status": "open",
                                     "payload": {"reply_class": "positive"}, "created_at": NOW - timedelta(hours=25)}])
    out = enrol.run(ctx)
    assert out["status"] == "skipped" and "waited over 24 hours" in out["reason"]


def test_only_warm_replies_waiting_pause_enrollment():
    """poll_replies writes a reply item for every class a person answers; only positive and referral pause (SPEC 11)."""
    from us_outbound.replies.poll import KIND

    assert enrol.REPLY_KIND == KIND
    ctx, _ = make()
    ctx.store.insert("hitl_items", [
        {"item_id": f"r-{c}", "kind": "reply", "status": "open", "payload": {"reply_class": c},
         "created_at": NOW - timedelta(hours=30)} for c in ("objection", "not_now", "negative", "other", "wrong_person")
    ])
    assert enrol.run(ctx)["status"] == "ok"
    ctx.store.insert("hitl_items", [{"item_id": "r-ref", "kind": "reply", "status": "escalated",
                                     "payload": {"reply_class": "referral"}, "created_at": NOW - timedelta(hours=30)}])
    assert "waited over 24 hours" in enrol.run(ctx)["reason"]


def test_operator_stop_pauses_enrollment_until_a_live_start():
    ctx, _ = make()
    hb = {"dry_run": True, "status": "ok", "finished_at": NOW - timedelta(hours=2)}
    ctx.store.insert("heartbeats", [{**hb, "run_id": "s-1", "job": "operator_stop", "started_at": NOW - timedelta(hours=2)}])
    out = enrol.run(ctx)
    assert out["status"] == "skipped" and "stopped by an operator" in out["reason"]
    # A dry-run start does not lift the pause; a live one does.
    ctx.store.insert("heartbeats", [{**hb, "run_id": "s-2", "job": "operator_start", "started_at": NOW - timedelta(hours=1)}])
    assert enrol.run(ctx)["status"] == "skipped"
    ctx.store.insert("heartbeats", [{**hb, "run_id": "s-3", "job": "operator_start", "dry_run": False,
                                     "started_at": NOW - timedelta(minutes=30)}])
    assert enrol.run(ctx)["status"] == "ok"


def test_recent_or_handled_replies_do_not_pause():
    ctx, _ = make()
    ctx.store.insert("hitl_items", [
        {"item_id": "r-1", "kind": "reply", "status": "open", "payload": {"reply_class": "positive"},
         "created_at": NOW - timedelta(hours=23)},
        {"item_id": "r-2", "kind": "reply", "status": "handled", "payload": {"reply_class": "positive"},
         "created_at": NOW - timedelta(hours=50)},
    ])
    assert enrol.run(ctx)["status"] == "ok"


@pytest.mark.parametrize("state", [None, "open"])
def test_enrollment_waits_for_the_weekly_hand_check(state):
    ctx, _ = make(hand_check=state)
    out = enrol.run(ctx)
    assert out["status"] == "skipped" and "hand-check (2026-W44)" in out["reason"]


def test_last_weeks_hand_check_does_not_count():
    ctx, _ = make(hand_check=None)
    ctx.store.insert("hitl_items", [{"item_id": "hc-0", "kind": "hand_check", "status": "handled",
                                     "created_at": NOW - timedelta(days=8)}])
    assert enrol.run(ctx)["status"] == "skipped"


def test_accounts_pulled_at_the_hand_check_are_not_enrolled():
    ctx, _ = make(hand_check=None)
    ctx.store.insert("hitl_items", [{"item_id": "hc-1", "kind": "hand_check", "status": "handled",
                                     "created_at": NOW - timedelta(hours=3), "payload": {"pulled_account_ids": ["acc-1"]}}])
    out = enrol.run(ctx)
    assert out["prepared"] == 2 and out["skipped"]["pulled at this week's hand-check"] == 1


# -- the daily number -------------------------------------------------------------------------------


def test_the_weekly_target_limits_the_day():
    # Tuesday: four send days left (Tue–Fri), so a weekly target of 4 gives 1 today.
    ctx, _ = make(settings=make_settings(weekly_enrol_cap=4))
    out = enrol.run(ctx)
    assert out["number"] == 1 and out["prepared"] == 1
    assert out["number_terms"]["binding"] == "weekly_target"
    assert out["limited_by"].startswith("Today: 1, limited by the weekly target")
    assert "0 of 4 enrolled this week" in out["limits"][0] and "4 send days left" in out["limits"][0]


def test_accounts_enrolled_earlier_this_week_count_against_the_target():
    ctx, _ = make(settings=make_settings(weekly_enrol_cap=6))
    monday = NOW - timedelta(days=1)
    ctx.store.insert("accounts", [account(account_id=f"old-{i}", domain=f"old{i}.com", status="enrolled", sender="Sam Jackson")
                                  for i in range(2)])
    ctx.store.insert("contacts", [
        contact(contact_id=f"old-c{i}", account_id=f"old-{i}", email=f"a@old{i}.com", enrolment_month="2026-10",
                instantly_campaign="US Outbound – Sam Jackson", instantly_lead_id=f"l{i}", enrolled_at=monday)
        for i in range(2)
    ] + [  # last week's leads do not count
        contact(contact_id="prev", account_id="old-0", email="b@old0.com", enrolment_month="2026-10",
                instantly_campaign="US Outbound – Sam Jackson", instantly_lead_id="lp", enrolled_at=NOW - timedelta(days=8)),
    ])
    out = enrol.run(ctx)
    assert out["number_terms"]["enrolled_this_week"] == 2
    assert out["number"] == 1 and out["prepared"] == 1  # (6 − 2) ÷ 4 send days left


def test_budgets_never_hold_back_accounts_already_verified():
    """Clay and Apollo are spent before enrollment, so a spent week limits the queue, not the day."""
    ctx, _ = make()
    ctx.store.insert("credit_ledger", [
        {"entry_id": "e1", "system": "clay", "credits": 2100.0, "occurred_at": NOW - timedelta(days=1)},
        {"entry_id": "e2", "system": "clay", "credits": 5000.0, "occurred_at": datetime(2026, 9, 20, tzinfo=UTC)},  # September
        {"entry_id": "e3", "system": "apollo", "credits": 120.0, "occurred_at": NOW - timedelta(hours=2)},
    ])
    out = enrol.run(ctx)
    assert out["prepared"] == 3 and out["number_terms"]["binding"] == "ready_accounts"
    clay = out["number_terms"]["budgets"]["clay"]
    assert (clay["budget"], clay["used"], clay["remaining"]) == (2000.0, 2100.0, -100.0)
    assert any(line.startswith("Clay: 2,100 of 2,000 credits used this month (105%)") and line.endswith("none left this month.")
               for line in out["limits"])
    assert any(line.startswith("Apollo: 120 of 2,000 credits used this month (6%)") for line in out["limits"])


def queue_of(tiers: dict[str, int], **settings) -> tuple:
    """A context whose verified queue holds this many accounts per tier, and the leads enrol posts."""
    accts = []
    for tier, n in tiers.items():
        for i in range(n):
            accts.append(account(account_id=f"{tier}{i}", domain=f"{tier.lower()}{i}.com", tier=tier,
                                 score={"Priority": 60, "Standard": 30, "Control": 0}[tier] + i,
                                 angle="Upgrade the EAP" if tier != "Control" else "General"))
    cons = [contact(contact_id=f"k-{a['account_id']}", account_id=a["account_id"], email=f"x@{a['domain']}") for a in accts]
    ctx, _ = make(accounts=accts, contacts=cons, settings=make_settings(**settings) if settings else None)
    posted: list[dict] = []
    add_leads = ctx.clients.instantly.add_leads
    ctx.clients.instantly.add_leads = lambda campaign, leads: posted.extend(leads) or add_leads(campaign, leads)
    return ctx, posted


def tiers_of(posted: list[dict]) -> list[str]:
    """Each posted lead's tier, read back from its domain ("x@control3.com" -> "Control")."""
    return [lead["email"].split("@")[1].split(".")[0].rstrip("0123456789").capitalize() for lead in posted]


def test_control_share_and_queue_order():
    ctx, posted = queue_of({"Priority": 30, "Control": 10}, weekly_enrol_cap=120)  # 30 today
    out = enrol.run(ctx)
    assert out["number"] == 30 and out["prepared"] == 30
    # In proportion to each owner's pace (cap ÷ 4, rounded up): Harry 15, Hannah 8, Sam 8.
    assert out["by_owner"] == {"Harry Dryden": 14, "Hannah Spalding": 8, "Sam Jackson": 8}
    assert tiers_of(posted).count("Control") == 5  # 15% of 30, rounded half up


def test_a_short_control_tier_is_filled_from_standard():
    ctx, posted = queue_of({"Control": 1, "Standard": 40}, weekly_enrol_cap=120)
    assert enrol.run(ctx)["prepared"] == 30
    assert tiers_of(posted).count("Control") == 1 and len(posted) == 30


def test_short_priority_and_standard_are_filled_from_control():
    ctx, posted = queue_of({"Control": 10, "Priority": 2}, weekly_enrol_cap=12)  # 12 ÷ 4 send days left
    assert enrol.run(ctx)["prepared"] == 3  # 15% of 3 rounds to no Control, but only 2 others wait
    assert sorted(tiers_of(posted)) == ["Control", "Priority", "Priority"]


# -- candidates ---------------------------------------------------------------------------------------


def test_recipient_rules():
    base = account()
    accts, cons = [], []
    cases = {
        "ca": dict(person_state="CA"), "wa": dict(person_state="wa"), "nostate": dict(person_state=""),
        "california": dict(person_state="California"), "washington": dict(person_state="Washington"),
        "ontario": dict(person_state="Ontario"),
        "gmail": dict(email="jane@gmail.com"), "info": dict(email="info@acme{}.com"),
        "invalid": dict(email_status="invalid"), "notfound": dict(email_status="not_found"),
        "flagged": dict(suppressed=True), "enrolled": dict(enrolment_month="2026-09"),
    }
    for i, (name, change) in enumerate(cases.items()):
        aid = f"a-{name}"
        accts.append({**base, "account_id": aid, "domain": f"acme{i}.com"})
        c = contact(contact_id=f"k-{name}", account_id=aid, email=f"jane@acme{i}.com")
        c.update({k: (v.format(i) if isinstance(v, str) and "{}" in v else v) for k, v in change.items()})
        cons.append(c)
    accts.append({**base, "account_id": "a-hash", "domain": "hashco.com"})
    cons.append(contact(contact_id="k-hash", account_id="a-hash", email="jane@hashco.com"))
    ctx, _ = make(accounts=accts, contacts=cons)
    ctx.store.insert("suppression", [{"email_sha256": hash_email("Jane@HashCo.com"), "domain": None, "reason": "unsubscribe"}])
    out = enrol.run(ctx)
    assert out["candidates"] == 0 and out["prepared"] == 0
    assert out["skipped"] == {
        "contact in CA or WA": 4, "contact state unknown": 2, "personal email domain": 1, "shared mailbox": 1,
        "email status invalid": 1, "email status not_found": 1, "contact suppressed": 2, "contact already enrolled": 1,
    }


def test_an_email_suppression_row_suppresses_only_that_email():
    """A row with an email hash records its domain but suppresses only that email (suppression.py)."""
    cons = [
        contact(contact_id="k-jane", email="jane@acmecreative.com", created_at="2026-10-01"),
        contact(contact_id="k-john", email="john@acmecreative.com", created_at="2026-10-02"),
    ]
    ctx, _ = make(accounts=[account()], contacts=cons)
    suppression.add(ctx.store, email="jane@acmecreative.com", domain="acmecreative.com", reason="unsubscribe",
                    source="test", now=NOW)
    domains, hashes = enrol.suppressed(ctx)
    assert "acmecreative.com" not in domains and hash_email("jane@acmecreative.com") in hashes
    assert enrol.contact_block(cons[0], domains, hashes) == "contact suppressed"
    assert enrol.contact_block(cons[1], domains, hashes) is None
    out = enrol.run(ctx)
    assert out["candidates"] == 1 and out["prepared"] == 1


def test_account_filters():
    accts = [
        account(account_id="a-sup", domain="sup.com"),
        account(account_id="a-old", domain="old.com"),  # its suppression expired
        account(account_id="a-alias", domain="newname.com"),
        account(account_id="a-partner", domain="broker.com"),
        account(account_id="a-off", domain="staff.com", industry="Staffing agencies", industry_group="Professional Services"),
        account(account_id="a-held", domain="held.com", tier="Held"),
        account(account_id="a-queued", domain="queued.com", status="queued"),
    ]
    cons = [contact(contact_id=f"k-{a['account_id']}", account_id=a["account_id"], email=f"jane@{a['domain']}") for a in accts]
    ctx, _ = make(accounts=accts, contacts=cons)
    ctx.store.insert("suppression", [
        {"email_sha256": None, "domain": "sup.com", "reason": "Layoffs", "expires_at": NOW + timedelta(days=10)},
        {"email_sha256": None, "domain": "old.com", "reason": "Layoffs", "expires_at": NOW - timedelta(days=1)},
        {"email_sha256": None, "domain": "oldname.com", "reason": "unsubscribe", "expires_at": None},
    ])
    ctx.store.insert("domain_aliases", [{"alias": "oldname.com", "root_domain": "newname.com", "source": "redirect"}])
    ctx.store.insert("partners", [{"domain": "broker.com", "name": "Broker", "reason": "broker"}])
    out = enrol.run(ctx)
    assert out["candidates"] == 1 and out["prepared"] == 1
    assert out["skipped"] == {"domain suppressed": 2, "partner domain": 1, "industry switched off": 1}


def test_contact_picks_the_first_sendable_one():
    cons = [
        contact(contact_id="k-1", email="ceo@gmail.com", created_at="2026-10-01"),
        contact(contact_id="k-2", email="jane@acmecreative.com", created_at="2026-10-02"),
    ]
    ctx, _ = make(live=True, accounts=[account()], contacts=cons)
    enrol.run(ctx)
    assert ctx.store.get("contacts", contact_id="k-2")["instantly_lead_id"]
    assert not ctx.store.get("contacts", contact_id="k-1").get("instantly_lead_id")


# -- sender continuity -------------------------------------------------------------------------------


def test_paused_sender_accounts_wait():
    s = make_settings(mailboxes=(dataclasses.replace(HANNAH, status="Paused"), HARRY_M, SAM, HARRY_T))
    ctx, t = make(settings=s)
    out = enrol.run(ctx)
    assert out["skipped"]["sender paused"] == 1 and out["prepared"] == 2
    assert "Hannah Spalding" not in out["by_owner"]


# -- HubSpot re-check --------------------------------------------------------------------------------


def _company_route(domain: str, result: dict):
    def fn(req):
        values = [f["value"] for g in req.json["filterGroups"] for f in g["filters"]]
        return {"results": [result]} if domain in values else {"results": []}

    return fn


@pytest.mark.parametrize(
    "props, deals, fact",
    [
        ({"lifecyclestage": "customer"}, [], "hubspot_customer"),
        ({"hubspot_owner_id": "owner-someone-else"}, [], "hubspot_other_owner"),
        ({"hubspot_owner_id": "owner-harry"}, [{"id": "d1", "properties": {"hs_is_closed": "false"}}], "hubspot_open_deal"),
    ],
)
def test_hubspot_recheck_excludes_the_account(props, deals, fact):
    t = FakeTransport()
    ctx, _ = make(transport=t)
    t.route("POST", "/crm/v3/objects/companies/search", fn=_company_route("acmecreative.com", {"id": "co-1", "properties": props}))
    t.route("POST", "/crm/v3/objects/deals/search", {"results": deals})
    out = enrol.run(ctx)
    assert out["prepared"] == 2 and [e["account_id"] for e in out["excluded"]] == ["acc-1"]
    acc = ctx.store.get("accounts", account_id="acc-1")
    assert acc["tier"] == "Excluded" and "HubSpot" in acc["tier_reason"]
    [ev] = ctx.store.select("signal_events", {"account_id": "acc-1"})
    assert ev["source"] == "hubspot" and ev["fact"] == fact and ev["value"] is True


def test_closed_deals_and_harrys_companies_are_fine():
    t = FakeTransport()
    ctx, _ = make(transport=t)
    t.route("POST", "/crm/v3/objects/companies/search",
            fn=_company_route("acmecreative.com", {"id": "co-1", "properties": {"hubspot_owner_id": "owner-harry"}}))
    t.route("POST", "/crm/v3/objects/deals/search", {"results": [{"id": "d1", "properties": {"hs_is_closed": "true"}}]})
    out = enrol.run(ctx)
    assert out["prepared"] == 3 and out["excluded"] == []


def test_hubspot_opted_out_contact_excludes():
    t = FakeTransport()
    ctx, _ = make(transport=t)
    t.route("POST", "/crm/v3/objects/contacts/search",
            fn=lambda req: {"results": [{"id": "h1", "properties": {"hs_email_optout": "true"}}]}
            if "jane@acmecreative.com" in str(req.json) else {"results": []})
    out = enrol.run(ctx)
    assert [e["account_id"] for e in out["excluded"]] == ["acc-1"]
    assert ctx.store.get("accounts", account_id="acc-1")["tier"] == "Excluded"


def test_hubspot_error_skips_the_account_without_excluding():
    t = FakeTransport()
    ctx, _ = make(transport=t)
    t.route("POST", "/crm/v3/objects/companies/search", {"message": "boom"}, status=403)
    out = enrol.run(ctx)
    assert out["prepared"] == 0 and out["skipped"]["HubSpot check failed"] == 3
    assert ctx.store.get("accounts", account_id="acc-1")["tier"] == "Priority"


# -- copy ---------------------------------------------------------------------------------------------


def test_render_violation_skips_the_account_and_says_why():
    bodies = {**BODIES, 1: BODIES[1].replace("{{opener}}", "{{opener}} {{nickname}}")}
    bad = make_settings(live_sending=True, copy=(copy_row("agencies-v1", AGENCIES, bodies=bodies), COPY[1]))
    ctx, t = make(live=True, settings=bad, accounts=[account()], contacts=[contact()])
    out = enrol.run(ctx)
    assert out["prepared"] == 0 and out["skipped"]["copy blocked"] == 1
    [skip] = [s for s in out["skipped_accounts"] if s["reason"] == "copy blocked"]
    assert any("agencies-v1: email 1:" in d and "{{nickname}}" in d for d in skip["detail"])
    assert instantly_posts(t) == []


def test_an_opener_that_breaks_a_rule_is_dropped(monkeypatch):
    monkeypatch.setattr(enrol, "account_opener", lambda ctx, a: ("Saw your benefits page mentions unlimited PTO", ""))
    ctx, t = make(live=True, accounts=[account()], contacts=[contact()])
    out = enrol.run(ctx)
    assert out["enrolled"] == 1
    [fallback] = out["opener_fallbacks"]
    assert fallback["account_id"] == "acc-1" and "unlimited" in fallback["reason"]
    [post] = instantly_posts(t)
    body = post.json["leads"][0]["custom_variables"]["s1_body"]
    assert "unlimited" not in body.lower() and "<p>Hi Jane,</p><p>In most agencies" in body


def test_created_ids_match_by_index_then_email():
    leads = [{"email": "a@x.com"}, {"email": "b@x.com"}]
    got = enrol._created_ids({"created_leads": [{"index": 5, "id": "L2", "email": "b@x.com"}, {"index": 0, "id": "L1"}]}, leads)
    assert got == {0: "L1", 1: "L2"}
