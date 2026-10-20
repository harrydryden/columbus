"""The enrol job (SPEC 9, 10, 11): gates, the daily number, candidates, HubSpot re-check,
sender continuity, test versions, and dry-run versus live Instantly writes."""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta

import pytest

from tests.fakes import FakeTransport, make_context
from tests.test_ramp import past_ramp
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
    TECH,
    account,
    contact,
    copy_row,
    make_settings,
)
from us_outbound import suppression
from us_outbound.enrol import enrol, openers, queue
from us_outbound.logs import hash_email
from us_outbound.settings.model import Focus

NOW = datetime(2026, 10, 27, 11, 0, tzinfo=UTC)  # Tuesday 11:00 UK, 07:00 ET (ISO week 2026-W44)
INSTANTLY = "https://api.instantly.ai/api/v2"
CAMPAIGNS = {  # status 1: active (`us-outbound start --live` has run)
    "items": [
        {"id": "c-hannah", "name": "US Outbound – Hannah Spalding", "status": 1},
        {"id": "c-sam", "name": "US Outbound – Sam Jackson", "status": 1},
        {"id": "c-harry", "name": "US Outbound – Harry Dryden", "status": 1},
        {"id": "c-eu", "name": "EU Outbound – Anna", "status": 1},
    ]
}
CAMPAIGN_OWNER = {"c-hannah": "Hannah Spalding", "c-sam": "Sam Jackson", "c-harry": "Harry Dryden"}


@pytest.fixture(autouse=True)
def default_openers(monkeypatch):
    """Openers come from enrol/openers.py (tested in test_openers.py); here each angle's default opener."""

    def opener(ctx, a, c, check=None):
        angle = ctx.settings.angle(str(a.get("angle") or ""))
        text = angle.default_opener if angle else ""
        return openers.Opener(text, openers.OPENER if text else openers.NONE, "angle default" if text else ""), ""

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


def make(*, live=False, settings=None, now=NOW, accounts=None, contacts=None, hand_check="handled", transport=None,
         optout_tested=True):
    """The enrol world: leads go straight to Instantly (send approvals were removed: W2-1). The opt-out test is done
    (optout_tested)."""
    s = settings or make_settings(live_sending=live)
    s = dataclasses.replace(s, general=dataclasses.replace(s.general, optout_tested=optout_tested))
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
    past_ramp(st, s.mailboxes, now)  # the mailboxes are past the sending ramp (tests/test_ramp.py covers it)
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
            # The signature's one line: email 4 links the demo page, so the booking line (Harry, 5 Oct 2026).
            assert "Book a call <a href=" in cv["s4_body"] and "on-demand counseling for your team" not in cv["s4_body"]
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


def test_live_flag_without_live_sending_runs_dry():
    """A job is live only with --live and live_sending = yes (bootstrap.resolve_live), so enrol never sees a live
    context without live_sending, and its gate does not check it again."""
    from us_outbound.ops import bootstrap

    s = make_settings(live_sending=False)
    ctx, t = make(live=bootstrap.resolve_live(True, s), settings=s)
    out = enrol.run(ctx)
    assert out["status"] == "ok" and out["dry_run"] is True and out["enrolled"] == 0
    assert instantly_posts(t) == [] and ctx.guard.writes("instantly", sent=True) == []


def test_a_live_run_waits_for_the_opt_out_test_and_a_dry_run_still_previews():
    """A6: until optout_tested = yes nothing is enrolled; a dry run (the scheduler's state until then) still runs."""
    ctx, t = make(live=True, optout_tested=False)
    out = enrol.run(ctx)
    assert out["status"] == "skipped" and out["reason"].startswith("optout_tested is no")
    assert instantly_posts(t) == [] and enrol.gate(ctx, ctx.now_et().date()) == enrol.OPTOUT_UNTESTED
    ctx, t = make(optout_tested=False)
    out = enrol.run(ctx)
    assert out["status"] == "ok" and out["prepared"] == 3 and enrol.gate(ctx, ctx.now_et().date()) is None


def test_instantly_skipping_a_lead_leaves_it_unenrolled():
    def partial(req):
        body = added(req)
        body["created_leads"] = [c for c in body["created_leads"] if c["email"] != "lee@loopstudio.com"]
        return body

    t = FakeTransport()
    ctx, _ = make(live=True, transport=t)
    t.route("POST", "/leads/add", fn=partial)
    t.route("POST", "/leads/list", {"items": []})  # not in the campaign either: Instantly refused it
    out = enrol.run(ctx)
    assert out["enrolled"] == 2 and out["skipped"]["not added by Instantly"] == 1
    assert ctx.store.get("accounts", account_id="acc-3")["status"] == "verified"
    lee = ctx.store.get("contacts", contact_id="con-3")
    assert not lee.get("instantly_lead_id")
    # A3: suppressed, so pick_contacts finds the next person instead of trying Lee every day.
    assert (lee["suppressed"], lee["suppressed_reason"]) == (True, enrol.NOT_ADDED)
    [skip] = [x for x in out["skipped_accounts"] if x["account_id"] == "acc-3"]
    assert skip["detail"] == [f"{enrol.NOT_ADDED}: the contact is suppressed, so pick_contacts finds the next person"]


def test_a_lead_left_out_of_the_summary_but_in_the_campaign_is_recorded():
    """A3: created_leads leaves out a lead the campaign already has; the lookup finds its id."""
    def partial(req):
        body = added(req)
        body["created_leads"] = [c for c in body["created_leads"] if c["email"] != "lee@loopstudio.com"]
        return body

    t = FakeTransport()
    ctx, _ = make(live=True, transport=t)
    t.route("POST", "/leads/add", fn=partial)
    t.route("POST", "/leads/list", fn=lambda req: {"items": [
        {"id": "lead-old", "email": "Lee@LoopStudio.com", "campaign": req.json["campaign"]}]})
    out = enrol.run(ctx)
    assert out["enrolled"] == 3 and "not added by Instantly" not in out["skipped"]
    lee = ctx.store.get("contacts", contact_id="con-3")
    assert (lee["instantly_lead_id"], lee.get("suppressed")) == ("lead-old", None)
    assert ctx.store.get("accounts", account_id="acc-3")["status"] == "enrolled"


def test_a_left_out_lead_whose_campaign_cannot_be_read_waits_for_the_next_run():
    def none_created(req):
        return {**added(req), "created_leads": []}

    t = FakeTransport()
    ctx, _ = make(live=True, transport=t)
    t.route("POST", "/leads/add", fn=none_created)
    t.route("POST", "/leads/list", {"error": "down"}, status=503)
    out = enrol.run(ctx)
    assert out["enrolled"] == 0 and out["skipped"]["not added by Instantly"] == 3
    assert any("could not be looked up" in e for e in out["errors"])
    assert not any(c.get("suppressed") for c in ctx.store.select("contacts"))  # nothing concluded


def test_instantly_api_error_is_recorded_and_marks_nothing():
    t = FakeTransport()
    ctx, _ = make(live=True, transport=t)
    t.route("POST", "/leads/add", {"error": "server"}, status=500)
    out = enrol.run(ctx)
    assert out["enrolled"] == 0 and len(out["errors"]) == 3  # one per sender's campaign
    assert {a["status"] for a in ctx.store.select("accounts")} == {"verified"}


def test_a_missing_campaign_gets_no_leads_and_says_how_to_create_it():
    """A1: an owner with no campaign in Instantly has no capacity in a live run; new accounts go to the others."""
    t = FakeTransport()
    ctx, _ = make(live=True, transport=t)
    t.route("GET", "/campaigns", {"items": [{"id": "c-hannah", "name": "US Outbound – Hannah Spalding", "status": 1}]})
    out = enrol.run(ctx)
    assert out["enrolled"] == 3 and out["by_owner"] == {"Hannah Spalding": 3} and out["errors"] == []
    assert {r.json["campaign_id"] for r in instantly_posts(t)} == {"c-hannah"}
    assert set(out["campaigns_not_sending"]) == {"Harry Dryden", "Sam Jackson"}
    assert ("Harry Dryden's campaign (US Outbound – Harry Dryden) is not in Instantly: `us-outbound campaigns ensure "
            "--live` creates it, then run `us-outbound start --live`.") in out["limited_by"]


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


def checked(source: str = "rules+model") -> list[dict]:
    """The three accounts with their labels decided by the label check (labels.py; Harry, 7 Oct 2026)."""
    return [{**a, "label_source": source} for a in accounts3()]


def test_the_most_specific_row_wins_label_then_role():
    label = copy_row("advertising-v1", "Advertising agencies")
    ops = copy_row("agencies-ops-v1", AGENCIES, role="Operations")
    s = make_settings(live_sending=True, copy=COPY + (ops, label))
    ctx, _ = make(live=True, settings=s, accounts=checked())
    enrol.run(ctx)
    cons = rows(ctx, "contacts", "contact_id")
    assert cons["con-1"]["copy_version"] == "advertising-v1"  # its own label beats its group
    assert cons["con-3"]["copy_version"] == "advertising-v1"  # the label beats a role row for the group
    s = make_settings(live_sending=True, copy=COPY + (ops,))
    ctx, _ = make(live=True, settings=s, accounts=checked())
    enrol.run(ctx)
    assert rows(ctx, "contacts", "contact_id")["con-3"]["copy_version"] == "agencies-ops-v1"  # Lee is Operations


@pytest.mark.parametrize("source, jane, lee", [
    ("rules+model", "advertising-v1", "advertising-v1"),  # confirmed: the label's own rows
    ("model", "advertising-v1", "advertising-v1"),
    ("override", "advertising-v1", "advertising-v1"),
    ("approver", "advertising-v1", "advertising-v1"),
    ("umbrella", "agencies-v1", "agencies-ops-v1"),  # a doubt within the group: the group's rows
    ("rules", "agencies-v1", "agencies-ops-v1"),
    (None, "agencies-v1", "agencies-ops-v1"),  # not checked yet: the group's (Harry, 7 Oct 2026)
    ("disputed", "general-v1", "general-v1"),  # a doubt across groups: General, whatever the label has approved
])
def test_the_label_check_says_how_specific_the_copy_may_be(source, jane, lee):
    """labels.copy_level (Harry, 7 Oct 2026): a doubtful label never carries a label's pitch."""
    label = copy_row("advertising-v1", "Advertising agencies")
    ops = copy_row("agencies-ops-v1", AGENCIES, role="Operations")
    s = make_settings(live_sending=True, copy=COPY + (ops, label))
    ctx, _ = make(live=True, settings=s, accounts=checked(source))
    enrol.run(ctx)
    cons = rows(ctx, "contacts", "contact_id")
    assert (cons["con-1"]["copy_version"], cons["con-3"]["copy_version"]) == (jane, lee)
    a = accounts3()[0]
    targets = enrol.copy_targets({**a, "label_source": source}, "Operations", s)
    assert targets[-2:] == [("General", "Operations"), ("General", "")]
    assert len(targets) == {"rules+model": 6, "umbrella": 4, "disputed": 2}.get(source, len(targets))


def test_with_no_copy_at_its_level_the_reason_says_which_level():
    s = make_settings(copy=(copy_row("advertising-v1", "Advertising agencies"),))  # the label's own row only
    accounts = [{**accounts3()[0], "label_source": "disputed"}]
    ctx, _ = make(settings=s, accounts=accounts, contacts=contacts3()[:1])
    out = enrol.run(ctx)
    [skip] = out["skipped_accounts"]
    assert skip["detail"][0] == ("no approved copy that has passed QA for General (the label check left Advertising "
                                 "agencies in doubt)")


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
    assert out["prepared"] == 2 and out["skipped"][enrol.HAND_CHECK_SKIP] == 1


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
    """Apollo is spent before enrollment, so a spent month limits the queue, not the day. (Clay's budget went with
    Clay, W2-1: a clay row on the ledger counts for nothing.)"""
    ctx, _ = make()
    ctx.store.insert("credit_ledger", [
        {"entry_id": "e1", "system": "clay", "credits": 2100.0, "occurred_at": NOW - timedelta(days=1)},
        {"entry_id": "e2", "system": "apollo", "credits": 2100.0, "occurred_at": NOW - timedelta(hours=2)},
        {"entry_id": "e3", "system": "apollo", "credits": 5000.0, "occurred_at": datetime(2026, 9, 20, tzinfo=UTC)},
    ])
    out = enrol.run(ctx)
    assert out["prepared"] == 3 and out["number_terms"]["binding"] == "ready_accounts"
    assert set(out["number_terms"]["budgets"]) == {"apollo"}
    apollo = out["number_terms"]["budgets"]["apollo"]
    # October's 2,100 against 2,000 (September's 5,000 is another month): 100 over.
    assert (apollo["budget"], apollo["used"], apollo["remaining"]) == (2000.0, 2100.0, -100.0)
    assert any(line.startswith("Apollo: 2,100 of 2,000 credits used this month (105%)")
               and line.endswith("none left this month.") for line in out["limits"])
    assert not any(line.startswith("Clay") for line in out["limits"])


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


def test_control_share_and_queue_order_while_explore_share_is_0():
    """explore_share 0 (no General row, as the live sheet is on deploy): the Control tier keeps control_share of the
    day first, as before W1-6, so the comparison arm does not stop before the random arm starts (the whole-wave
    review, 20 Oct 2026). 30 Priority and 10 Control accounts, 30 today (weekly 120 ÷ 4 send days left): 5 Control
    (15% of 30 is 4.5, rounded half up) and 25 Priority. Each contact is stamped ranked: the arm column is the random
    arm's, and tier_at_enrol says Control."""
    ctx, posted = queue_of({"Priority": 30, "Control": 10}, weekly_enrol_cap=120)
    out = enrol.run(ctx)
    assert out["number"] == 30 and out["prepared"] == 30
    # In proportion to each owner's pace (cap ÷ 4, rounded up): Harry 15, Hannah 8, Sam 8.
    assert out["by_owner"] == {"Harry Dryden": 14, "Hannah Spalding": 8, "Sam Jackson": 8}
    assert tiers_of(posted).count("Control") == 5
    assert out["control"] == {"share": 0.15, "ready": 10, "target": 5, "prepared": 5}


def test_a_short_control_tier_is_filled_from_standard():
    ctx, posted = queue_of({"Control": 1, "Standard": 40}, weekly_enrol_cap=120)
    assert enrol.run(ctx)["prepared"] == 30
    assert tiers_of(posted).count("Control") == 1 and len(posted) == 30


def test_the_random_arm_replaces_the_control_tier_once_explore_share_is_set():
    """explore_share 0.15 with no random-arm account ready (pick has not drawn one yet): the Control tier has no lane
    of its own, so the 30 are the 30 Priority in queue order, and the summary says the Control lane is off."""
    ctx, posted = queue_of({"Priority": 30, "Control": 10}, weekly_enrol_cap=120, explore_share=0.15)
    out = enrol.run(ctx)
    assert out["prepared"] == 30 and tiers_of(posted).count("Control") == 0
    assert out["control"] is None and out["explore"]["target"] == 0


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
    """A HubSpot outage (500) is a per-candidate failure: each account is skipped, never excluded, and counted in the
    summary's failures. (Before W1-6 this test sent a 403, which is the wrong key, not an outage: it now ends the run,
    test_a_hubspot_401_or_403_ends_the_run_with_an_error.)"""
    t = FakeTransport()
    ctx, _ = make(transport=t)
    t.route("POST", "/crm/v3/objects/companies/search", {"message": "boom"}, status=500)
    out = enrol.run(ctx)
    assert out["prepared"] == 0 and out["skipped"]["HubSpot check failed"] == 3
    assert out["failures"] == {"HubSpot check failed": 3} and out["candidates"] == 3
    assert ctx.store.get("accounts", account_id="acc-1")["tier"] == "Priority"


@pytest.mark.parametrize("status", [401, 403])
def test_a_hubspot_401_or_403_ends_the_run_with_an_error(status):
    """M2 (code-contacts-enrol; enrol.py 718-721 before W1-6): the wrong key failed every account as "HubSpot check
    failed" and the day read as an ok run that enrolled nobody. An AuthError now ends the run before any add, so its
    heartbeat is an error and job_errors reports it."""
    from us_outbound.clients.http import AuthError
    from us_outbound.ops import health

    t = FakeTransport()
    ctx, _ = make(live=True, settings=make_settings(live_sending=True), transport=t)
    t.route("POST", "/crm/v3/objects/companies/search", {"message": "expired"}, status=status)
    with pytest.raises(AuthError):
        health.run_job(ctx, enrol.run)
    [row] = ctx.store.select("heartbeats")
    assert row["status"] == "error" and row["error"].startswith("AuthError: hubspot HTTP")
    assert instantly_posts(t) == []  # prepare runs before any add
    assert {a["status"] for a in ctx.store.select("accounts")} == {"verified"}


def test_hubspot_down_for_every_candidate_is_a_job_errors_finding():
    """25 candidates, HubSpot answering 500 to each: enrolled 0, failures {"HubSpot check failed": 25} beside
    candidates 25, so job_errors raises a failures finding (25 >= 3 and 25 >= 50% of 25) instead of the day reading as
    an ok run with errors == []. Today's number: weekly_enrol_cap 100 ÷ 4 send days left on Tuesday = 25."""
    from us_outbound.ops import health

    ctx, posted, _ = explore_world(0, 25)
    ctx.clients.transport.route("POST", "/crm/v3/objects/companies/search", {"message": "down"}, status=500)
    out = enrol.run(ctx)
    assert (out["number"], out["candidates"], out["enrolled"], out["prepared"]) == (25, 25, 0, 0)
    assert out["failures"] == {"HubSpot check failed": 25} and out["errors"] == []
    [finding] = health.findings("enrol", {"status": "ok", "detail": out})
    assert finding.kind == "failures" and finding.count == 25


def test_copy_blocks_count_as_failures():
    bodies = {**BODIES, 1: BODIES[1].replace("{{opener}}", "{{opener}} {{nickname}}")}
    bad = make_settings(copy=(copy_row("agencies-v1", AGENCIES, bodies=bodies), COPY[1]))
    ctx, _ = make(settings=bad, accounts=[account()], contacts=[contact()])
    out = enrol.run(ctx)
    assert out["failures"] == {"copy blocked": 1} and out["candidates"] == 1


# -- an add Instantly never answers (W1-6; code-contacts-enrol M3) ------------------------------------------------


def test_an_unanswered_add_is_looked_up_twice_and_the_leads_found_are_recorded(monkeypatch):
    """A ReadTimeout on Harry's add (4 leads); the campaign lists none of them 30 s later and 3 of them after another
    60 s: those 3 are recorded with today's enrolled_at, the 4th stays verified with its contact kept, one errors
    line, two lookups; Hannah's add (2 leads) goes ahead and is recorded. Before W1-6 the OSError escaped run(), after
    the add may have happened, and nothing of the run was recorded."""
    import requests

    waits: list[float] = []
    monkeypatch.setattr(enrol, "_sleep", waits.append)
    harry = [account(account_id=f"h{i}", domain=f"h{i}.com", sender="Harry Dryden") for i in range(4)]
    hannah = [account(account_id=f"n{i}", domain=f"n{i}.com", sender="Hannah Spalding") for i in range(2)]
    cons = [contact(contact_id=f"k-{a['account_id']}", account_id=a["account_id"], email=f"jo@{a['domain']}")
            for a in harry + hannah]
    t = FakeTransport()
    ctx, _ = make(live=True, settings=make_settings(live_sending=True), transport=t, accounts=harry + hannah,
                  contacts=cons)

    def add(req):
        if req.json["campaign_id"] == "c-harry":
            raise requests.ReadTimeout("read timed out")
        return added(req)

    lookups: list[str] = []

    def listed(req):
        lookups.append(req.json["campaign"])
        if req.json["campaign"] == "c-harry" and lookups.count("c-harry") >= 2:
            return {"items": [{"id": f"lead-h{i}", "email": f"jo@h{i}.com", "campaign": "c-harry"} for i in range(3)]}
        return {"items": []}

    t.route("POST", "/leads/add", fn=add)
    t.route("POST", "/leads/list", fn=listed)
    out = enrol.run(ctx)
    assert waits == [30, 60] and lookups == ["c-harry", "c-harry"]
    assert out["enrolled"] == 5 and out["by_owner"] == {"Harry Dryden": 3, "Hannah Spalding": 2}
    assert out["skipped"][enrol.ADD_UNKNOWN] == 1
    [line] = out["errors"]
    assert line.startswith("US Outbound – Harry Dryden: Instantly did not answer the add (ReadTimeout")
    assert "3 of 4 leads found" in line
    cons = rows(ctx, "contacts", "contact_id")
    for i in range(3):
        assert (cons[f"k-h{i}"]["instantly_lead_id"], cons[f"k-h{i}"]["enrolled_at"]) == (f"lead-h{i}", ctx.now)
    assert cons["k-h3"].get("instantly_lead_id") is None and not cons["k-h3"].get("suppressed")
    accts = rows(ctx, "accounts", "account_id")
    assert accts["h3"]["status"] == "verified" and {accts[f"n{i}"]["status"] for i in range(2)} == {"enrolled"}
    assert {accts[f"h{i}"]["status"] for i in range(3)} == {"enrolled"}


def test_an_unanswered_add_found_at_the_first_lookup_is_not_looked_up_again(monkeypatch):
    import requests

    waits: list[float] = []
    monkeypatch.setattr(enrol, "_sleep", waits.append)
    t = FakeTransport()
    ctx, _ = make(live=True, settings=make_settings(live_sending=True), transport=t, accounts=[account()],
                  contacts=[contact()])
    t.route("POST", "/leads/add", fn=lambda req: (_ for _ in ()).throw(requests.ConnectionError("reset")))
    t.route("POST", "/leads/list", fn=lambda req: {"items": [
        {"id": "lead-j", "email": "jane@acmecreative.com", "campaign": req.json["campaign"]}]})
    out = enrol.run(ctx)
    assert waits == [30] and out["enrolled"] == 1 and len(out["errors"]) == 1
    assert ctx.store.get("contacts", contact_id="con-1")["instantly_lead_id"] == "lead-j"


def test_an_unanswered_add_keeps_the_owner_so_the_retry_goes_to_the_same_campaign(monkeypatch):
    """The round-1 review's probe. A new account (no sender) whose add got no answer, and was not listed after the two
    lookups, though Instantly had taken it: the next run must add it to the same campaign, not to whichever owner the
    pace then favours, or Instantly leaves it out (skip_if_in_workspace), the other campaign's list does not have it,
    and the contact is wrongly suppressed as not added while its lead runs its sequence unrecorded.

    Run 1, Tuesday: only Hannah has an Active mailbox, so Acme goes to her (the only owner with free slots). Her add
    times out; the lead did land in c-hannah, but the campaign lists nothing yet. Acme keeps Hannah as its sender and
    stays verified, its contact kept.

    Run 2, Wednesday: every mailbox Active. Free and pace: Hannah 8 and ceil(30 / 4) = 8, Harry 15 and ceil(60 / 4) =
    15, Sam 8 and 8, so each owner's share of pace left is 1.0, and the tie goes to the most free: Harry. A new
    account with no sender would go to Harry; Acme (first in queue order: score 60) goes to Hannah, its sender. Her add
    leaves the lead out as in the workspace already; c-hannah lists it, so it is recorded with that lead id, and
    mark_not_added is never called. Fresh (score 50, no sender) shows what the pace gives a new account: Harry (after
    Acme took one of Hannah's slots her share is 7 / 8, Harry's and Sam's 1.0, and Harry is the most free)."""
    import requests

    from tests.test_render import HARRY_M, HARRY_T

    monkeypatch.setattr(enrol, "_sleep", lambda s: None)
    not_added: list = []
    real_mark = enrol.mark_not_added
    monkeypatch.setattr(enrol, "mark_not_added", lambda ctx, cid: not_added.append(cid) or real_mark(ctx, cid))
    workspace: dict[str, tuple[str, str]] = {}  # email -> (campaign id, lead id): the leads Instantly holds
    lagging = [True]  # run 1: the campaign does not list the lead yet

    def add(req):
        cid = req.json["campaign_id"]
        created, skipped = [], 0
        for i, lead in enumerate(req.json["leads"]):
            if lead["email"] in workspace:  # skip_if_in_workspace (instantly.LEAD_IMPORT_OPTIONS)
                skipped += 1
                continue
            workspace[lead["email"]] = (cid, f"lead-{lead['email']}")
            created.append({"index": i, "id": f"lead-{lead['email']}", "email": lead["email"]})
        if cid == "c-hannah" and lagging[0]:
            raise requests.ReadTimeout("read timed out")  # taken, but no answer came
        return {"status": "success", "total_sent": len(req.json["leads"]), "leads_uploaded": len(created),
                "skipped_count": skipped, "created_leads": created}

    def listed(req):
        if lagging[0]:
            return {"items": []}
        return {"items": [{"id": lead_id, "email": email, "campaign": cid}
                          for email, (cid, lead_id) in workspace.items() if cid == req.json["campaign"]]}

    paused = [dataclasses.replace(m, status="Paused") for m in (HARRY_M, SAM, HARRY_T)]
    s1 = make_settings(live_sending=True, mailboxes=(HANNAH, *paused))
    fresh = account(account_id="acc-f", domain="freshco.com", clean_name="Freshco", score=50)
    t = FakeTransport()
    ctx, _ = make(live=True, settings=s1, transport=t, accounts=[account()], contacts=[contact()])
    t.route("POST", "/leads/add", fn=add)
    t.route("POST", "/leads/list", fn=listed)
    out = enrol.run(ctx)
    assert out["enrolled"] == 0 and out["skipped"] == {enrol.ADD_UNKNOWN: 1} and len(out["errors"]) == 1
    acme = ctx.store.get("accounts", account_id="acc-1")
    assert (acme["status"], acme.get("sender")) == ("verified", "Hannah Spalding")
    jane = ctx.store.get("contacts", contact_id="con-1")
    assert jane.get("instantly_lead_id") is None and not jane.get("suppressed")
    assert workspace == {"jane@acmecreative.com": ("c-hannah", "lead-jane@acmecreative.com")}

    lagging[0] = False
    ctx.store.insert("accounts", [fresh])
    ctx.store.insert("contacts", [contact(contact_id="con-f", account_id="acc-f", email="jo@freshco.com")])
    s2 = make_settings(live_sending=True)
    s2 = dataclasses.replace(s2, general=dataclasses.replace(s2.general, optout_tested=True))
    from tests.fakes import make_context as fresh_context

    ctx2 = fresh_context(s2, live=True, now=NOW + timedelta(days=1), transport=t, store=ctx.store)
    before = len(t.requests)
    out = enrol.run(ctx2)
    sent_to = {lead["email"]: r.json["campaign_id"] for r in t.requests[before:] if r.url.endswith("/leads/add")
               for lead in r.json["leads"]}
    assert sent_to == {"jane@acmecreative.com": "c-hannah", "jo@freshco.com": "c-harry"}
    assert out["enrolled"] == 2 and out["by_owner"] == {"Hannah Spalding": 1, "Harry Dryden": 1}
    assert not_added == [] and out["skipped"] == {}
    jane = ctx2.store.get("contacts", contact_id="con-1")
    assert (jane["instantly_lead_id"], jane["instantly_campaign"]) == (
        "lead-jane@acmecreative.com", "US Outbound – Hannah Spalding")
    assert not jane.get("suppressed") and jane.get("suppressed_reason") is None
    acme = ctx2.store.get("accounts", account_id="acc-1")
    assert (acme["status"], acme["sender"]) == ("enrolled", "Hannah Spalding")
    assert ctx2.store.get("accounts", account_id="acc-f")["sender"] == "Harry Dryden"


def test_an_unanswered_add_whose_leads_are_found_resumes_a_completed_campaign(monkeypatch):
    """Instantly marks a campaign with no lead left to email completed (capacity.resume_if_completed); a lead added
    to it then waits unsent. The leads an unanswered add is found to have added are in such a campaign as much as an
    answered add's, so it is activated again. Harry's campaign is completed (status 3); his add times out and the
    first lookup lists the lead: one activate call for c-harry, and the lead is recorded."""
    import requests

    from tests.test_render import HARRY_M, HARRY_T

    monkeypatch.setattr(enrol, "_sleep", lambda s: None)
    t = FakeTransport()
    only_harry = (dataclasses.replace(HANNAH, status="Paused"), HARRY_M, dataclasses.replace(SAM, status="Paused"),
                  HARRY_T)
    ctx, _ = make(live=True, settings=make_settings(live_sending=True, mailboxes=only_harry), transport=t,
                  accounts=[account()], contacts=[contact()])
    completed = {"items": [dict(c, status=3) if c["id"] == "c-harry" else c for c in CAMPAIGNS["items"]]}
    t.route("GET", "/campaigns", completed)
    t.route("POST", "/activate", {})
    t.route("POST", "/leads/add", fn=lambda req: (_ for _ in ()).throw(requests.ReadTimeout("read timed out")))
    t.route("POST", "/leads/list", fn=lambda req: {"items": [
        {"id": "lead-j", "email": "jane@acmecreative.com", "campaign": req.json["campaign"]}]})
    out = enrol.run(ctx)
    assert out["enrolled"] == 1 and len(out["errors"]) == 1  # the unanswered add's line only
    assert [r.url.removeprefix(INSTANTLY) for r in t.requests if r.url.endswith("/activate")] == [
        "/campaigns/c-harry/activate"]
    assert ctx.store.get("contacts", contact_id="con-1")["instantly_lead_id"] == "lead-j"


# -- the random explore arm (W1-6; Harry's D7: rank by score plus a random arm in place of the Control tier) ---------


def explore_world(n_random: int, n_ranked: int, *, live: bool = False, **general):
    """A queue of n_random accounts pick drew into the random arm (accounts.arm = random, low scores) and n_ranked in
    score order (Priority, scores 60 up), each with a sendable contact. weekly_enrol_cap 100 on Tuesday, 4 send days
    left: today's number is 25. Returns (ctx, the leads posted, the accounts in queue order)."""
    general = {"weekly_enrol_cap": 100, **general}
    accts = [account(account_id=f"rnd{i}", domain=f"rnd{i}.com", tier="Control", score=i, angle="General",
                     arm=queue.RANDOM) for i in range(n_random)]
    accts += [account(account_id=f"rank{i:02d}", domain=f"rank{i:02d}.com", score=60 + i) for i in range(n_ranked)]
    cons = [contact(contact_id=f"k-{a['account_id']}", account_id=a["account_id"], email=f"x@{a['domain']}")
            for a in accts]
    s = make_settings(live_sending=live, **general)
    ctx, _ = make(live=live, settings=s, accounts=accts, contacts=cons)
    posted: list[dict] = []
    add_leads = ctx.clients.instantly.add_leads
    ctx.clients.instantly.add_leads = lambda campaign, leads: posted.extend(leads) or add_leads(campaign, leads)
    return ctx, posted, sorted(accts, key=lambda a: queue.order_key(a, s))


def ids_of(posted: list[dict]) -> list[str]:
    return [lead["email"].split("@")[1].removesuffix(".com") for lead in posted]


def test_explore_share_0_the_default_walks_today_s_queue_order():
    """explore_share 0 (no General row): no random walk, so the walk is today's (ef2ce5f's): control_share of the 25
    from the Control tier first (0.15 × 25 = 3.75 → 4), then the first 21 others in queue order, and every contact is
    stamped ranked. 60 ranked and 5 random-arm accounts ready (drawn while the share was above 0); rnd0 is Priority
    with the top score, so it is first in queue order and walked there; the other four (Control, scores 1-4) are the
    Control tier's 4. Each contact is ranked, not random: none was chosen at random, so the readout must not count it
    in the random arm (the round-1 review found rnd0 stamped random, and summary.explore.prepared 1, at share 0)."""
    ctx, posted, ordered = explore_world(5, 60, live=True)
    ctx.store.update("accounts", {"account_id": "rnd0"}, {"tier": "Priority", "score": 999})
    ordered = sorted(ctx.store.select("accounts"), key=lambda a: queue.order_key(a, ctx.settings))
    assert ctx.settings.general.explore_share == 0.0 and ordered[0]["account_id"] == "rnd0"
    out = enrol.run(ctx)
    assert (out["number"], out["prepared"], out["enrolled"]) == (25, 25, 25)
    assert sorted(ids_of(posted)) == sorted([f"rnd{i}" for i in range(1, 5)] + [a["account_id"] for a in ordered[:21]])
    assert out["explore"] == {"share": 0.0, "drawn_ready": 0, "target": 0, "prepared": 0,
                              "realised": None}
    assert out["control"] == {"share": 0.15, "ready": 4, "target": 4, "prepared": 4}
    enrolled = {c["account_id"]: c["arm"] for c in ctx.store.select("contacts") if c.get("instantly_lead_id")}
    assert len(enrolled) == 25 and "rnd0" in enrolled and set(enrolled.values()) == {queue.RANKED}
    assert ctx.store.get("accounts", account_id="rnd0")["arm"] == queue.RANDOM  # pick's draw is left as it was


def test_the_random_arm_takes_its_share_and_the_rest_go_in_queue_order():
    """explore_share 0.15, n = 25, 5 random-arm accounts ready and 60 ranked: n_random = explore_count(25, 0.15, 5) =
    round(3.75) = 4, the first 4 random ones in queue order (scores 4, 3, 2, 1); the other 21 are the top 21 ranked
    (scores 119 down to 99). Each contact carries its account's arm."""
    ctx, posted, ordered = explore_world(5, 60, live=True, explore_share=0.15)
    out = enrol.run(ctx)
    assert (out["prepared"], out["enrolled"]) == (25, 25)
    assert out["explore"] == {"share": 0.15, "drawn_ready": 5, "target": 4, "prepared": 4,
                              "realised": 0.16}
    want = {f"rnd{i}" for i in (4, 3, 2, 1)} | {f"rank{i:02d}" for i in range(59, 38, -1)}
    assert set(ids_of(posted)) == want
    arms = {c["account_id"]: c["arm"] for c in ctx.store.select("contacts") if c.get("instantly_lead_id")}
    assert sorted(a for a, arm in arms.items() if arm == queue.RANDOM) == ["rnd1", "rnd2", "rnd3", "rnd4"]
    assert sum(arm == queue.RANKED for arm in arms.values()) == 21


def test_no_random_arm_ready_means_every_place_goes_in_order():
    ctx, posted, ordered = explore_world(0, 60, explore_share=0.15)
    out = enrol.run(ctx)
    assert out["prepared"] == 25 and out["explore"]["target"] == 0
    assert sorted(ids_of(posted)) == sorted(a["account_id"] for a in ordered[:25])


def test_a_short_random_arm_is_filled_from_the_ranked_and_a_short_ranked_from_the_random():
    # 1 random-arm account and 40 ranked, n = 30 (weekly 120 ÷ 4): explore_count(30, 0.15, 1) = min(5, 1) = 1, so 1
    # random and 29 ranked.
    ctx, posted, _ = explore_world(1, 40, explore_share=0.15, weekly_enrol_cap=120)
    out = enrol.run(ctx)
    assert out["prepared"] == 30 and sum(i.startswith("rnd") for i in ids_of(posted)) == 1
    # The shortfall is reported: the realised share, 1 of 30, against the 0.15 asked (the whole-wave review).
    assert (out["explore"]["target"], out["explore"]["realised"]) == (1, 0.033)
    # 10 random-arm accounts and 2 ranked, n = 3 (weekly 12 ÷ 4): explore_count(3, 0.15, 10) = 0 (0.45 rounds to 0
    # and 3 < 4), the 2 ranked, then the shortfall of 1 from the random arm: 3 prepared.
    ctx, posted, _ = explore_world(10, 2, explore_share=0.15, weekly_enrol_cap=12)
    out = enrol.run(ctx)
    assert out["prepared"] == 3 and sorted(i[:3] for i in ids_of(posted)) == ["ran", "ran", "rnd"]


def test_the_random_walk_ignores_the_focus_quota_but_counts_against_it():
    """Focus 100% Technology & Startups. The random-arm accounts are agencies (pick drew them within the Focus shares
    of their day): the random walk takes 4 of them whatever the quota says, and the ranked walk then takes the Focus
    group's 21 (rank accounts are agencies too, held by the quota, then filled at the end): 25 prepared."""
    focus = (Focus(TECH, 1.0),)
    ctx, posted, _ = explore_world(5, 60, explore_share=0.15, focus=focus)
    out = enrol.run(ctx)
    assert out["prepared"] == 25 and sum(i.startswith("rnd") for i in ids_of(posted)) == 4


def test_the_random_walk_counts_against_the_focus_quota():
    """Focus 50% Technology & Startups, 50% Marketing & Creative Agencies; weekly_enrol_cap 100 on Tuesday (4 send
    days left), nothing enrolled this week: each group's target is 50 and today's quota ceil(50 / 4) = 13; n = 25.
    The random arm: 5 Tech accounts; explore_count(25, 0.15, 5) = round(3.75) = 4, walked first (scores 4, 3, 2, 1),
    and each takes one of Tech's 13: 9 left. The ranked walk then wants 25 - 4 = 21, in queue order: 30 Tech accounts
    (scores 200-229, first) and 30 agencies (scores 60-89): Tech's 9 (229 down to 221), the other Tech ones held by
    the quota, then 12 agencies (89 down to 78) and the day is full; nothing is left for the held fill. Done: Tech 4 +
    9 = 13 of its 13, agencies 12. Had the random walk not counted, the ranked walk would have taken 13 Tech and only 8
    agencies, giving Tech 17 of the day's 25 against a 50% share."""
    focus = (Focus(TECH, 0.5), Focus(AGENCIES, 0.5))
    tech = {"industry": "Fintech", "industry_group": TECH, "angle": "General"}
    accts = [account(account_id=f"rnd{i}", domain=f"rnd{i}.com", tier="Control", score=i, arm=queue.RANDOM, **tech)
             for i in range(5)]
    accts += [account(account_id=f"tech{i:02d}", domain=f"tech{i:02d}.com", score=200 + i, **tech) for i in range(30)]
    accts += [account(account_id=f"agen{i:02d}", domain=f"agen{i:02d}.com", score=60 + i) for i in range(30)]
    cons = [contact(contact_id=f"k-{a['account_id']}", account_id=a["account_id"], email=f"x@{a['domain']}")
            for a in accts]
    s = make_settings(weekly_enrol_cap=100, explore_share=0.15, focus=focus)
    ctx, t = make(settings=s, accounts=accts, contacts=cons)
    posted: list[dict] = []
    add_leads = ctx.clients.instantly.add_leads
    ctx.clients.instantly.add_leads = lambda campaign, leads: posted.extend(leads) or add_leads(campaign, leads)
    out = enrol.run(ctx)
    assert (out["number"], out["prepared"]) == (25, 25)
    assert out["explore"] == {"share": 0.15, "drawn_ready": 5, "target": 4, "prepared": 4,
                              "realised": 0.16}
    got = set(ids_of(posted))
    assert got == ({f"rnd{i}" for i in (4, 3, 2, 1)} | {f"tech{i:02d}" for i in range(29, 20, -1)}
                   | {f"agen{i:02d}" for i in range(29, 17, -1)})
    assert out["focus"] == f"Focus this week: {TECH} 13 of 50, {AGENCIES} 12 of 50."


def test_the_failures_are_the_skips_job_errors_reads():
    """job_errors reads the run's failures by reason (FAILURE_REASONS): 3 HubSpot failures and 1 copy block are
    {HubSpot 3, copy 1}; an ordinary skip is no failure. (A second contact's "second contact: <reason>" skip went
    with second contacts, W2-1.)"""
    skipped = {"HubSpot check failed": 3, "copy blocked": 1, "sender full today": 7}
    assert enrol._failures(skipped) == {"HubSpot check failed": 3, "copy blocked": 1}
    assert enrol._failures({"sender full today": 7}) == {}


# -- what a contact was enrolled as (W1-6; code-contacts-enrol L6) ------------------------------------------------


def test_each_contact_is_stamped_with_what_it_was_enrolled_as():
    import hashlib

    from us_outbound import labels

    ctx, t = make(live=True)
    # A relabel and resize land while the run is adding (verify_accounts, a hand-check correction): the account moves,
    # the contact keeps what it was prepared and sent as. The stamps come from the account as prepared, not from a
    # read of it when the contact is recorded.
    add_leads = ctx.clients.instantly.add_leads

    def add_then_relabel(campaign, leads):
        ctx.store.update("accounts", {"account_id": "acc-1"}, {"industry": "Fintech", "industry_group": TECH,
                                                               "size_band": "100-249", "label_source": "model"})
        return add_leads(campaign, leads)

    ctx.clients.instantly.add_leads = add_then_relabel
    enrol.run(ctx)
    assert ctx.store.get("accounts", account_id="acc-1")["industry_group"] == TECH
    posts = {lead["email"]: lead for r in instantly_posts(t) for lead in r.json["leads"]}
    jane = ctx.store.get("contacts", contact_id="con-1")
    cv = posts["jane@acmecreative.com"]["custom_variables"]
    assert jane["arm"] == queue.RANKED  # accounts.arm unset (picked before the arm existed) reads as ranked
    assert jane["sham_arm"] == queue.sham_arm("acc-1") and jane["sham_arm"] in ("a", "b")
    assert (jane["industry_at_enrol"], jane["industry_group_at_enrol"], jane["size_band_at_enrol"]) == (
        "Advertising agencies", AGENCIES, "50-99")
    # No label check recorded on the account: the group's copy (labels.copy_level; the label_source comment).
    assert jane["copy_level"] == labels.GROUP_COPY == "group"
    assert jane["email1_subject"] == cv["s1_subject"] == "Support for the Acme Creative team"
    want = [hashlib.sha256(cv[f"s{n}_body"].encode()).hexdigest() for n in (1, 2, 3, 4)]
    assert jane["email_hashes"] == want and len(set(want)) == 4


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
    bad = openers.Opener("Saw your benefits page mentions unlimited PTO", openers.OPENER, "Progressive benefits / opener")
    monkeypatch.setattr(enrol, "account_opener", lambda ctx, a, c, check=None: (bad, ""))
    ctx, t = make(live=True, accounts=[account()], contacts=[contact()])
    out = enrol.run(ctx)
    assert out["enrolled"] == 1
    [fallback] = out["opener_fallbacks"]
    assert fallback["account_id"] == "acc-1" and "unlimited" in fallback["reason"]
    [post] = instantly_posts(t)
    body = post.json["leads"][0]["custom_variables"]["s1_body"]
    assert "unlimited" not in body.lower() and "<p>Hi Jane,</p><p>In most agencies" in body
    # The contact records that it went out with no opener (the readout's arm), not as an opener.
    assert ctx.store.get("contacts", contact_id=contact()["contact_id"])["opener_arm"] == openers.NONE


def test_created_ids_match_by_index_then_email():
    leads = [{"email": "a@x.com"}, {"email": "b@x.com"}]
    got = enrol._created_ids({"created_leads": [{"index": 5, "id": "L2", "email": "b@x.com"}, {"index": 0, "id": "L1"}]}, leads)
    assert got == {0: "L1", 1: "L2"}


# -- which contact (Harry, 1 Oct 2026: the best-ranked sendable one) ----------------------------------------


def _with_default_roles(**general):
    from us_outbound.settings.defaults import default_tabs
    from us_outbound.settings.validate import validate_all

    return dataclasses.replace(make_settings(**general), roles=validate_all(default_tabs())[0].roles)


RANKED_CONTACTS = [
    contact(contact_id="k-cfo", email="cfo@acmecreative.com", title="CFO", role="Finance", created_at="2026-09-01"),
    contact(contact_id="k-hr", email="hr@acmecreative.com", title="HR Manager", created_at="2026-10-01"),
    contact(contact_id="k-om", email="om@acmecreative.com", title="Office Manager", role="Operations",
            created_at="2026-10-02"),
    contact(contact_id="k-hop", email="hop@acmecreative.com", title="Head of People", created_at="2026-10-03"),
    contact(contact_id="k-ceo", email="ceo@acmecreative.com", title="CEO", role="Founder or executive",
            created_at="2026-10-04"),
]


@pytest.mark.parametrize("employees, band, chosen", [
    (30, "20-49", "k-ceo"),  # 10-49: the founder first, though created last
    (120, "100-249", "k-hop"),  # 50-249: the senior People leader first
])
def test_enrol_takes_the_best_ranked_sendable_contact(employees, band, chosen):
    ctx, _ = make(live=True, settings=_with_default_roles(live_sending=True),
                  accounts=[account(employees=employees, size_band=band)], contacts=[dict(c) for c in RANKED_CONTACTS])
    assert enrol.run(ctx)["enrolled"] == 1
    assert [c["contact_id"] for c in ctx.store.select("contacts") if c.get("instantly_lead_id")] == [chosen]


def test_enrol_passes_over_an_unsendable_best_contact():
    s, a = _with_default_roles(), account(employees=30, size_band="20-49")
    cons = [
        contact(contact_id="k-om", email="om@acmecreative.com", title="Office Manager", created_at="2026-10-01"),
        contact(contact_id="k-ceo", email="ceo@acmecreative.com", title="CEO", person_state="CA", created_at="2026-10-02"),
        contact(contact_id="k-hop", email="hop@acmecreative.com", title="Head of People", created_at="2026-10-03"),
    ]
    assert enrol.pick_contact(cons, set(), set(), a, s) == (cons[2], "")
    assert enrol.pick_contact(cons[1:2], set(), set(), a, s) == (None, "contact in CA or WA")
    # Without a Roles tab or an account, the first created, as before.
    assert enrol.pick_contact(cons, set(), set())[0]["contact_id"] == "k-om"
    assert enrol.pick_contact(cons, set(), set(), a, make_settings())[0]["contact_id"] == "k-om"


# -- the cohort stamp (config_version.py; Harry, 7 Oct 2026) ---------------------------------------------------------


def test_enrol_stamps_config_version_code_sha_and_copy_hash(monkeypatch):
    monkeypatch.setenv("RAILWAY_GIT_COMMIT_SHA", "a4c5c27f00ba7e1234567890")
    ctx, _ = make()  # a dry run records the version too (a database write), and stamps no contact
    out = enrol.run(ctx)
    [version] = ctx.store.select("config_versions")
    assert out["config_version"] == version["config_version"] and version["code_sha"] == "a4c5c27f00ba"
    assert not any(c.get("config_version") for c in ctx.store.select("contacts"))
    ctx, _ = make(live=True)
    enrol.run(ctx)
    [version] = ctx.store.select("config_versions")
    cons = rows(ctx, "contacts", "contact_id")
    for c in cons.values():
        assert (c["config_version"], c["code_sha"]) == (version["config_version"], "a4c5c27f00ba")
        assert c["copy_hash"] == version["copy_hashes"][c["copy_version"]]
    assert (version["first_seen"], version["run_id"]) == (NOW, ctx.run_id)
    ctx.now += timedelta(days=1)  # the next run, on the same settings and code: no second row
    assert enrol.run(ctx)["config_version"] == version["config_version"]
    assert len(ctx.store.select("config_versions")) == 1


# -- the size range at enrol (the whole-wave review, 20 Oct 2026: D1 narrows max_employees 350 -> 249) ---------------


def test_account_block_holds_the_general_size_range_as_it_is_today():
    """verify judges size once; a range narrowed later must still keep the accounts it no longer takes out of the
    queue. 300 staff at max 249: blocked; at max 350 (the sheet before D1): not. With no count, the band decides
    (250-499 is outside 10 to 249); with neither, verify already left it out, so nothing is added here. An Overrides
    row of 120 staff brings it back, as it does at verify."""
    from us_outbound.settings.model import Override

    narrow, wide = make_settings(max_employees=249), make_settings(max_employees=350)
    big = account(employees=300, size_band="250-499")
    assert enrol.account_block(big, narrow, (), (), frozenset()) == enrol.SIZE_SKIP
    assert enrol.account_block(big, wide, (), (), frozenset()) is None
    assert enrol.account_block(account(), narrow, (), (), frozenset()) is None  # 64 staff
    assert enrol.account_block(account(employees=None, size_band="250-499"), narrow, (), (), frozenset()) \
        == enrol.SIZE_SKIP
    assert enrol.account_block(account(employees=None, size_band="100-249"), narrow, (), (), frozenset()) is None
    assert enrol.account_block(account(employees=None, size_band=None), narrow, (), (), frozenset()) is None
    assert enrol.account_block(account(employees=5, size_band="1-9"), narrow, (), (), frozenset()) == enrol.SIZE_SKIP
    fixed = make_settings(max_employees=249, overrides=(Override("acmecreative.com", "employees", "120"),))
    assert enrol.account_block(big, fixed, (), (), frozenset()) is None


def test_accounts_verified_at_a_wider_range_are_not_enrolled_or_picked_for_once_it_narrows():
    """The review's scenario 2: 10 companies of 300 staff and 10 of 64 were verified and given contacts at max 350;
    the sheet then goes to 249. Enrol takes the 10 of 64 staff only (weekly 100 ÷ 4 send days left = 25 today, so the
    number is the 10 ready), each 300-staff account is skipped for its size, and pick_contacts neither counts them
    ready nor reveals for them."""
    from us_outbound.contacts import pick

    accts = [account(account_id=f"big{i}", domain=f"big{i}.com", employees=300, size_band="250-499", score=90 + i)
             for i in range(10)]
    accts += [account(account_id=f"fit{i}", domain=f"fit{i}.com", score=60 + i) for i in range(10)]
    accts += [account(account_id=f"new{i}", domain=f"new{i}.com", employees=300, size_band="250-499")
              for i in range(3)]  # no contact yet: pick would reveal for them
    cons = [contact(contact_id=f"k-{a['account_id']}", account_id=a["account_id"], email=f"x@{a['domain']}")
            for a in accts[:20]]
    ctx, _ = make(live=True, accounts=accts, contacts=cons,
                  settings=make_settings(live_sending=True, weekly_enrol_cap=100, max_employees=249))
    need, ready = pick.waiting(ctx)
    assert need == [] and ready == 10
    out = enrol.run(ctx)
    assert (out["number"], out["enrolled"]) == (10, 10)
    enrolled = {c["account_id"] for c in ctx.store.select("contacts") if c.get("instantly_lead_id")}
    assert enrolled == {f"fit{i}" for i in range(10)}
    assert {c["size_band_at_enrol"] for c in ctx.store.select("contacts") if c.get("instantly_lead_id")} == {"50-99"}


def test_thirty_ready_and_25_today_go_straight_to_instantly_with_no_send_approval():
    """W2-1 (Harry's D6): send approvals and auto_send are gone, so each day's leads go straight to Instantly. 30
    Priority accounts are ready; today's number is 25: weekly_enrol_cap 100 over the 4 send days left (Tue 27 Oct,
    none enrolled yet this ISO week) is 25, under sending capacity (31) and the 30 ready. All 25 are added and
    enrolled, none is held, and no send_approval item is written; the 25 are the top scores in queue order, p29 down
    to p5 (score 60 + i)."""
    accts = [account(account_id=f"p{i}", domain=f"p{i}.com", score=60 + i) for i in range(30)]
    cons = [contact(contact_id=f"k-p{i}", account_id=f"p{i}", email=f"x@p{i}.com") for i in range(30)]
    ctx, t = make(live=True, settings=make_settings(live_sending=True, weekly_enrol_cap=100), accounts=accts,
                  contacts=cons)
    out = enrol.run(ctx)
    assert (out["status"], out["number"], out["prepared"], out["enrolled"]) == ("ok", 25, 25, 25)
    assert sum(len(r.json["leads"]) for r in instantly_posts(t) if r.url.endswith("/leads/add")) == 25
    assert not {"send_approvals", "auto_send", "second_contacts"} & set(out)
    assert [i for i in ctx.store.select("hitl_items") if i["kind"] == "send_approval"] == []
    enrolled = {a["account_id"] for a in ctx.store.select("accounts") if a["status"] == "enrolled"}
    assert enrolled == {f"p{i}" for i in range(5, 30)}
    assert {c["contact_slot"] for c in ctx.store.select("contacts") if c.get("instantly_lead_id")} == {1}
