"""The send path and its gates (enrol/enrol.py, enrol/approvals/, limits.py): one eligibility check for
enrol's candidates and a send approval's re-check, the campaign's status in Instantly, holds that keep a ✅
valid against blocks that close the card, HubSpot at the ✅, and leads Instantly leaves out of its summary.

The worlds are tests/test_enrol.py's and tests/test_send_approvals.py's: the real clients and the guard over a
FakeTransport, with Slack's threads and reactions on the same transport.
"""

from __future__ import annotations

from datetime import timedelta

from tests.test_enrol import NOW
from tests.test_render import account, contact
from us_outbound import suppression
from us_outbound.base import holds
from us_outbound.enrol import approvals, enrol
from us_outbound.logs import hash_email

# -- A4: one eligibility check -----------------------------------------------------------------------------


def _parity_world():
    """One account per gate candidates applies, each with one contact; and one that passes every gate."""
    from tests.test_send_approvals import world

    cases = {
        "ok": ({}, {}),
        "dom": ({"domain": "suppressed-co.com"}, {}),
        "alias": ({"domain": "newname.com"}, {}),
        "expired": ({"domain": "expired-co.com"}, {}),
        "partner": ({"domain": "broker.com"}, {}),
        "off": ({"industry": "Staffing agencies", "industry_group": "Professional Services"}, {}),
        "pulled": ({}, {}),
        "stopped": ({"industry": "Fintech", "industry_group": "Technology & Startups"}, {}),
        "source": ({}, {"email_source": "clay"}),
        "hash": ({}, {}),
        "emaildom": ({}, {"email": "jane@suppressed-mail.com"}),
        "ca": ({}, {"person_state": "CA"}),
        "flag": ({}, {"suppressed": True}),
        "status": ({}, {"email_status": "invalid"}),
    }
    accts, cons = [], []
    for name, (a, c) in cases.items():
        domain = a.pop("domain", f"{name}-co.com")
        accts.append(account(account_id=f"acc-{name}", domain=domain, clean_name=f"Co {name}", **a))
        cons.append(contact(contact_id=f"con-{name}", account_id=f"acc-{name}",
                            **{"email": f"jane@{domain}", "email_source": "apollo", **c}))
    ctx, t, sl = world(accounts=accts, contacts=cons)
    ctx.store.insert("suppression", [
        {"email_sha256": None, "domain": "suppressed-co.com", "reason": "unsubscribe", "expires_at": None},
        {"email_sha256": None, "domain": "oldname.com", "reason": "unsubscribe", "expires_at": None},
        {"email_sha256": None, "domain": "expired-co.com", "reason": "Layoffs", "expires_at": NOW - timedelta(days=1)},
        {"email_sha256": None, "domain": "suppressed-mail.com", "reason": "unsubscribe", "expires_at": None},
        {"email_sha256": hash_email("jane@hash-co.com"), "domain": None, "reason": "unsubscribe", "expires_at": None},
    ])
    ctx.store.insert("domain_aliases", [{"alias": "oldname.com", "root_domain": "newname.com", "source": "redirect"}])
    ctx.store.insert("partners", [{"domain": "broker.com", "name": "Broker", "reason": "broker"}])
    ctx.store.insert("hitl_items", [
        {"item_id": "hc-w44", "kind": "hand_check", "status": "handled", "created_at": NOW - timedelta(hours=3),
         "payload": {"iso_week": "2026-W44", "pulled_account_ids": ["acc-pulled"]}},
        {"item_id": "kr-1", "kind": holds.KIND, "status": "open", "created_at": NOW,
         "payload": {"action": holds.STOP_GROUP, "target": "Technology & Startups", "reason": "low replies"}},
        {"item_id": "kr-2", "kind": holds.KIND, "status": "open", "created_at": NOW,
         "payload": {"action": holds.PAUSE_SOURCE, "target": "clay", "reason": "bounces"}},
    ])
    return ctx, t, sl, accts, cons


def _card_for(a, c) -> approvals.Item:
    return approvals.Item({"item_id": f"it-{a['account_id']}", "account_id": a["account_id"],
                           "contact_id": c["contact_id"],
                           "payload": {"company": a["clean_name"], "contact": {"first_name": c["first_name"],
                                                                                "last_name": c["last_name"]},
                                       "lead": {"email": c["email"]}}})


def test_candidates_and_the_recheck_agree_on_every_gate():
    ctx, t, sl, accts, cons = _parity_world()
    _, pulled = enrol.hand_check(ctx, ctx.now_et().date())
    cands, skipped = enrol.candidates(ctx, pulled)
    assert sorted(c.account["account_id"] for c in cands) == ["acc-expired", "acc-ok"]  # expired: holds nothing
    assert skipped == {
        "domain suppressed": 2, "partner domain": 1, "industry switched off": 1, "pulled at this week's hand-check": 1,
        "industry group stopped by a kill rule": 1, "email source paused by a kill rule": 1, "contact suppressed": 2,
        "email domain suppressed": 1, "contact in CA or WA": 1, "email status invalid": 1,
    }
    # The re-check of a card for each account says the same, account by account.
    got = {}
    for a, c in zip(accts, cons):
        why = approvals.eligibility(ctx, _card_for(a, c), a, c)
        got[a["account_id"]] = why.split(": ", 1)[1] if why else ""
    assert got["acc-ok"] == "" and got["acc-expired"] == ""
    reasons = [r for r in got.values() if r]
    from collections import Counter

    assert Counter(reasons) == skipped
    assert approvals.eligibility(ctx, _card_for(accts[1], cons[1]), accts[1], cons[1]) == "Co dom: domain suppressed"
    assert approvals.eligibility(ctx, _card_for(accts[-1], cons[-1]), accts[-1], cons[-1]) == (
        "Jane Doe: email status invalid")


def test_the_recheck_reads_suppression_by_key_not_the_whole_table():
    """A7: a ✅ looks up its own domain, its aliases and its email; never the whole suppression table."""
    ctx, t, sl, accts, cons = _parity_world()
    seen = []
    select = ctx.store.select

    def spy(table, where=None):
        if table in ("suppression", "domain_aliases"):
            seen.append((table, where))
        return select(table, where)

    ctx.store.select = spy
    a, c = accts[2], cons[2]  # newname.com, suppressed through its alias oldname.com
    assert approvals.eligibility(ctx, _card_for(a, c), a, c) == "Co alias: domain suppressed"
    assert seen and all(where for _, where in seen)  # every read is by key
    assert ("domain_aliases", {"root_domain": ["newname.com"]}) in seen
    assert enrol.suppressed_for(ctx, ["NewName.com", "other.com"], []) == ({"newname.com"}, set())
    assert enrol.suppressed_for(ctx, [], [hash_email("jane@hash-co.com"), "nothing"]) == (
        set(), {hash_email("jane@hash-co.com")})
    # The same answer as the whole-table read candidates makes.
    domains, hashes = enrol.suppressed(ctx)
    for a in accts:
        assert (a["domain"] in domains) == bool(enrol.suppressed_for(ctx, [a["domain"]], [])[0])


def test_a_card_for_an_account_pulled_or_stopped_since_is_blocked():
    """A4: the re-check now applies the kill rules and the hand-check's pulls, as candidates do."""
    from tests.test_send_approvals import HARRY_ID, item_for, poll, proposed

    ctx, t, sl, _ = proposed()
    ctx.store.insert("hitl_items", [
        {"item_id": "kr-1", "kind": holds.KIND, "status": "open", "created_at": NOW,
         "payload": {"action": holds.STOP_GROUP, "target": "Marketing & Creative Agencies", "reason": "low replies"}}])
    sl.react("white_check_mark", HARRY_ID, ts=item_for(ctx, "acc-1")["slack_ts"])
    out = poll(ctx)
    assert out["outcomes"] == {"blocked": 1}
    assert item_for(ctx, "acc-1")["payload"]["reason"] == "Acme Creative: industry group stopped by a kill rule"
    suppression.add(ctx.store, domain="loopstudio.com", reason="unsubscribe", source="test", now=NOW)
    sl.react("white_check_mark", HARRY_ID, ts=item_for(ctx, "acc-3")["slack_ts"])
    poll(ctx)
    assert item_for(ctx, "acc-3")["payload"]["reason"] == "Loop Studio: domain suppressed"


# -- A1: a campaign that is not active in Instantly takes no leads ------------------------------------------------


def _hannah_draft(t, status=0):
    from tests.test_enrol import CAMPAIGNS

    items = [dict(c, status=status) if c["id"] == "c-hannah" else c for c in CAMPAIGNS["items"]]
    t.route("GET", "/campaigns", {"items": items})


DRAFT = "Hannah Spalding's campaign is not active in Instantly (draft): run `us-outbound start --live`"


def test_live_auto_send_adds_no_lead_to_a_campaign_that_is_not_active():
    from tests.fakes import FakeTransport
    from tests.test_enrol import instantly_posts, make

    t = FakeTransport()
    ctx, _ = make(live=True, transport=t)
    _hannah_draft(t)
    out = enrol.run(ctx)
    assert {r.json["campaign_id"] for r in instantly_posts(t)} == {"c-harry", "c-sam"}
    assert "Hannah Spalding" not in out["by_owner"] and out["enrolled"] == 2
    assert out["campaigns_not_sending"] == {"Hannah Spalding": DRAFT}
    assert out["limited_by"].endswith(f" {DRAFT}.")
    assert f"Sending capacity, Hannah Spalding: no new leads today: {DRAFT}." in out["limits"]
    assert not any("Add a mailbox for Hannah" in line for line in out["limits"])
    [skip] = [x for x in out["skipped_accounts"] if x["account_id"] == "acc-2"]  # Omar's account keeps Hannah
    assert skip["reason"] == "sender's campaign not sending" and skip["detail"] == [
        f"{DRAFT}; the account waits for Hannah Spalding"]
    assert ctx.store.get("accounts", account_id="acc-2")["status"] == "verified"


def test_live_approvals_propose_no_card_for_a_campaign_that_is_not_active():
    from tests.test_send_approvals import items, world

    ctx, t, sl = world()
    _hannah_draft(t, status=2)
    out = enrol.run(ctx)
    assert out["send_approvals"]["posted"] == 2 and "Hannah Spalding" not in out["by_owner"]
    assert {r["account_id"] for r in items(ctx)} == {"acc-1", "acc-3"}
    assert "Hannah Spalding's campaign is not active in Instantly (paused)" in out["limited_by"]


def test_a_dry_run_still_previews_and_says_a_live_run_would_not():
    from tests.fakes import FakeTransport
    from tests.test_enrol import make

    t = FakeTransport()
    ctx, _ = make(transport=t)
    _hannah_draft(t)
    out = enrol.run(ctx)
    assert out["dry_run"] is True and out["prepared"] == 3 and out["by_owner"]["Hannah Spalding"] == 1
    assert out["campaigns_not_sending"] == {"Hannah Spalding": DRAFT}
    assert out["limited_by"].endswith(f" Counted anyway in this dry run, but a live run would not: {DRAFT}.")
    line = next(x for x in out["limits"] if x.startswith("Sending capacity, Hannah Spalding:"))
    assert line.endswith(f"; a live run would give none: {DRAFT}.") and "8 new leads today" in line


def test_when_instantly_cannot_be_read_nothing_is_proposed_or_added():
    from tests.fakes import FakeTransport
    from tests.test_enrol import instantly_posts, make
    from tests.test_send_approvals import items, world

    ctx, t, sl = world()
    t.route("GET", "/campaigns", {"error": "down"}, status=503)
    out = enrol.run(ctx)
    assert out["status"] == "ok" and out["number"] == 0 and out["send_approvals"]["posted"] == 0 and items(ctx) == []
    assert "Instantly could not be read (ApiError" in out["limited_by"]
    assert set(out["campaigns_not_sending"]) == {"Harry Dryden", "Hannah Spalding", "Sam Jackson"}
    t = FakeTransport()
    ctx, _ = make(live=True, transport=t)
    t.route("GET", "/campaigns", {"error": "down"}, status=503)
    assert enrol.run(ctx)["enrolled"] == 0 and instantly_posts(t) == []


def test_limits_reads_instantly_only_when_asked():
    from tests.fakes import FakeTransport
    from tests.test_enrol import make
    from us_outbound import limits

    t = FakeTransport()
    ctx, _ = make(live=True, transport=t)
    _hannah_draft(t)
    lim = limits.today(ctx, ctx.now_et().date(), ready_accounts=10)  # status, the daily post and golive
    assert lim.not_sending == {} and not [r for r in t.requests if "/campaigns" in r.url]
    lim = limits.today(ctx, ctx.now_et().date(), ready_accounts=10, campaigns=True)
    assert lim.not_sending == {"Hannah Spalding": DRAFT} and lim.senders["Hannah Spalding"].free == 0
    assert lim.terms["senders"]["Hannah Spalding"]["not_sending"] == DRAFT


# -- A2: holds keep the card open and the ✅ valid; blocks close it -------------------------------------------------


def _approved(account_id="acc-1", **kw):
    """Three cards posted (live, auto_send = no), and Harry's ✅ on one of them."""
    from tests.test_send_approvals import HARRY_ID, item_for, proposed

    ctx, t, sl, _ = proposed(**kw)
    row = item_for(ctx, account_id)
    sl.react("white_check_mark", HARRY_ID, ts=row["slack_ts"])
    return ctx, t, sl, row


def _held_notes(sl, row) -> list[str]:
    return [p["text"] for p in sl.thread(row["slack_ts"]) if p["text"].startswith("⏸")]


def _stop(ctx, job="operator_stop", at=None):
    ctx.store.insert("heartbeats", [{"run_id": f"{job}-{len(ctx.store.select('heartbeats'))}", "job": job,
                                     "dry_run": False, "status": "ok", "started_at": at or ctx.now,
                                     "finished_at": at or ctx.now}])


def test_a_hold_posts_one_note_per_reason_and_the_tick_goes_through_once_every_hold_clears():
    from tests.test_enrol import instantly_posts
    from tests.test_send_approvals import item_for, poll

    ctx, t, sl, row = _approved()
    _stop(ctx)
    out = poll(ctx)
    assert out["outcomes"] == {} and out["held"][0]["item"] == row["item_id"][:8] and instantly_posts(t) == []
    [note] = _held_notes(sl, row)
    assert note.startswith("⏸ Approved by <@U_HARRY>, but not added yet: enrollment is stopped by an operator")
    assert note.endswith("It goes through by itself once that clears, until the card expires at the end of "
                         "Wed 28 Oct (UK). ❌ still stops it.")
    for _ in range(3):  # every 5 minutes: no note again
        poll(ctx)
    assert len(_held_notes(sl, row)) == 1
    # A second reason while the first still holds: one note for it, once.
    ctx.store.insert("hitl_items", [{"item_id": "r-1", "kind": "reply", "status": "open", "created_at": NOW - timedelta(hours=30),
                                     "payload": {"reply_class": "positive"}}])
    poll(ctx)
    poll(ctx)
    notes = _held_notes(sl, row)
    assert len(notes) == 2 and "new emails wait while 1 positive reply has waited over 24 hours" in notes[1]
    p = item_for(ctx, "acc-1")["payload"]
    assert p["held"]["noted"] == ["operator_stop", "reply_pause"] and p["held"]["by"] == "U_HARRY"
    assert (item_for(ctx, "acc-1")["status"], p["state"], p["outcome"]) == ("open", "waiting", "")
    # The first clears; the second still holds.
    _stop(ctx, "operator_start")
    assert poll(ctx)["outcomes"] == {} and instantly_posts(t) == []
    ctx.store.update("hitl_items", {"item_id": "r-1"}, {"status": "handled"})
    assert poll(ctx)["outcomes"] == {"approved": 1} and len(instantly_posts(t)) == 1
    row = item_for(ctx, "acc-1")
    assert (row["status"], row["handled_by"], row["payload"]["outcome"]) == ("handled", "U_HARRY", "approved")


def test_a_held_send_reply_and_a_held_cli_approval_go_through_too():
    """The ✅ on the card is read every run; a "send" reply is read once, so the held approval is kept for it."""
    from tests.test_enrol import instantly_posts
    from tests.test_send_approvals import HARRY_ID, item_for, poll, proposed

    ctx, t, sl, _ = proposed()
    _stop(ctx)
    sl.say(HARRY_ID, "send", item_for(ctx, "acc-2")["slack_ts"])
    poll(ctx)
    assert item_for(ctx, "acc-2")["payload"]["held"]["via"] == "thread"
    approvals.approve(ctx, item_for(ctx, "acc-3")["item_id"])  # the command line, approved_by "cli"
    assert item_for(ctx, "acc-3")["payload"]["held"]["by"] == "cli"
    poll(ctx)
    assert instantly_posts(t) == []
    _stop(ctx, "operator_start")
    out = approvals.poll(ctx, None)  # no Slack token: the held approvals still go through
    assert out["outcomes"] == {"approved": 2} and len(instantly_posts(t)) == 2
    assert {item_for(ctx, a)["handled_by"] for a in ("acc-2", "acc-3")} == {HARRY_ID, "cli"}


def test_a_cross_after_a_held_tick_stops_it():
    from tests.test_enrol import instantly_posts
    from tests.test_send_approvals import HARRY_ID, item_for, poll

    ctx, t, sl, row = _approved()
    _stop(ctx)
    poll(ctx)
    sl.react("x", HARRY_ID, ts=row["slack_ts"])
    poll(ctx)
    p = item_for(ctx, "acc-1")["payload"]
    assert p["state"] == "rejected" and "held" not in p
    _stop(ctx, "operator_start")
    poll(ctx)
    assert instantly_posts(t) == [] and item_for(ctx, "acc-1")["payload"]["state"] == "rejected"


def test_a_held_card_that_never_clears_expires_and_says_so():
    from datetime import UTC, datetime

    from tests.test_send_approvals import at, item_for, poll

    ctx, t, sl, row = _approved()
    _stop(ctx)
    poll(ctx)
    at(ctx, datetime(2026, 10, 28, 23, 55, tzinfo=UTC))
    out = poll(ctx)  # Thu 00:00 UK
    assert out["outcomes"] == {"expired": 3}
    reason = item_for(ctx, "acc-1")["payload"]["reason"]
    assert reason.startswith("approved, but still held at the end of Wed 28 Oct (UK): enrollment is stopped by an operator")
    assert item_for(ctx, "acc-2")["payload"]["reason"] == "not approved by the end of Wed 28 Oct (UK)"


def test_holds_from_instantly_the_campaign_and_the_opt_out_test():
    import dataclasses

    from tests.test_enrol import CAMPAIGNS, instantly_posts
    from tests.test_send_approvals import item_for, poll

    ctx, t, sl, row = _approved()
    ctx.settings = dataclasses.replace(ctx.settings, general=dataclasses.replace(ctx.settings.general, optout_tested=False))
    poll(ctx)
    assert _held_notes(sl, row)[-1].startswith("⏸ Approved by <@U_HARRY>, but not added yet: optout_tested is no")
    ctx.settings = dataclasses.replace(ctx.settings, general=dataclasses.replace(ctx.settings.general, optout_tested=True))
    t.route("GET", "/campaigns", {"error": "down"}, status=503)
    poll(ctx)
    assert "Instantly could not be read (ApiError" in _held_notes(sl, row)[-1]
    harry_paused = [dict(c, status=2) if c["id"] == "c-harry" else c for c in CAMPAIGNS["items"]]
    t.route("GET", "/campaigns", {"items": harry_paused})
    poll(ctx)
    assert _held_notes(sl, row)[-1].endswith(
        "not added yet: Harry Dryden's campaign is not active in Instantly (paused): run `us-outbound start --live`. "
        "It goes through by itself once that clears, until the card expires at the end of Wed 28 Oct (UK). "
        "❌ still stops it.")
    assert item_for(ctx, "acc-1")["payload"]["held"]["noted"] == ["optout_tested", "instantly", "campaign"]
    assert instantly_posts(t) == [] and item_for(ctx, "acc-1")["status"] == "open"
    t.route("GET", "/campaigns", CAMPAIGNS)
    assert poll(ctx)["outcomes"] == {"approved": 1}


def test_a_blackout_on_instantlys_next_send_day_holds_but_a_weekend_does_not():
    import dataclasses
    from datetime import UTC, date, datetime

    from tests.test_send_approvals import item_for
    from us_outbound.settings.model import DateRange

    ctx, t, sl, row = _approved()
    item = approvals.Item(item_for(ctx, "acc-1"))
    # Tuesday 11:00 UK is 07:00 ET, before the window: Instantly sends today.
    assert approvals.instantly_send_day(ctx) == date(2026, 10, 27)
    ctx.now = datetime(2026, 10, 30, 21, 30, tzinfo=UTC)  # Friday 17:30 ET, after the window: Monday
    assert approvals.instantly_send_day(ctx) == date(2026, 11, 2)
    ctx.now = datetime(2026, 10, 31, 12, 0, tzinfo=UTC)  # Saturday: Monday too, and nothing holds
    assert approvals.instantly_send_day(ctx) == date(2026, 11, 2)
    assert approvals.recheck(ctx, item).holds == {}
    blackout = (DateRange(date(2026, 11, 2), date(2026, 11, 2)),)
    ctx.settings = dataclasses.replace(ctx.settings, general=dataclasses.replace(ctx.settings.general,
                                                                                  blackout_dates=blackout))
    held = approvals.recheck(ctx, item).holds
    assert held == {"blackout": "Mon 2 Nov is a blackout date, and Instantly (which does not know our blackout dates) "
                                "would send email 1 then"}


def test_blocks_still_close_the_card_while_holds_keep_it_open():
    from tests.test_send_approvals import item_for, poll

    ctx, t, sl, row = _approved()
    _stop(ctx)
    ctx.store.update("accounts", {"account_id": "acc-1"}, {"tier": "Held", "tier_reason": "Held: no HQ state"})
    out = poll(ctx)
    assert out["outcomes"] == {"blocked": 1}
    row = item_for(ctx, "acc-1")
    assert (row["status"], row["payload"]["outcome"]) == ("handled", "blocked")
    assert row["payload"]["reason"] == "Acme Creative is Held now (Held: no HQ state)"


# -- A5: HubSpot at the ✅ ------------------------------------------------------------------------------------------


def test_a_hubspot_exclusion_at_the_tick_closes_the_card_and_excludes_the_account():
    from tests.test_enrol import _company_route, instantly_posts
    from tests.test_send_approvals import item_for, poll
    from us_outbound.scoring import tiers

    ctx, t, sl, row = _approved()
    t.route("POST", "/crm/v3/objects/companies/search",
            fn=_company_route("acmecreative.com", {"id": "co-1", "properties": {"lifecyclestage": "customer"}}))
    out = poll(ctx)
    assert out["outcomes"] == {"blocked": 1} and instantly_posts(t) == []
    row = item_for(ctx, "acc-1")
    assert row["payload"]["reason"] == "Acme Creative: a customer in HubSpot"
    acme = ctx.store.get("accounts", account_id="acc-1")
    assert (acme["tier"], acme["tier_reason"]) == ("Excluded", "a customer in HubSpot")
    [fact] = [e for e in ctx.store.select("signal_events", {"account_id": "acc-1"}) if e["source"] == "hubspot"]
    assert fact["fact"] == "hubspot_customer" and fact["fact"] in dict(tiers.HUBSPOT_EXCLUSIONS)


def test_a_hubspot_error_at_the_tick_holds():
    from tests.test_enrol import instantly_posts
    from tests.test_send_approvals import item_for, poll

    ctx, t, sl, row = _approved()
    t.route("POST", "/crm/v3/objects/contacts/search", {"message": "boom"}, status=500)
    out = poll(ctx)
    assert out["outcomes"] == {} and instantly_posts(t) == []
    assert "HubSpot could not be read for the re-check" in _held_notes(sl, row)[0]
    assert item_for(ctx, "acc-1")["payload"]["held"]["noted"] == ["hubspot"]
    t.route("POST", "/crm/v3/objects/contacts/search", {"results": []})
    assert poll(ctx)["outcomes"] == {"approved": 1}


def test_a_dry_cli_approval_reports_the_holds_and_changes_nothing():
    from tests.test_send_approvals import at, item_for, proposed

    ctx, t, sl, _ = proposed()
    before = item_for(ctx, "acc-1")
    at(ctx, ctx.now, live=False)
    import dataclasses

    ctx.settings = dataclasses.replace(ctx.settings, general=dataclasses.replace(ctx.settings.general, live_sending=False))
    out = approvals.approve(ctx, before["item_id"])
    assert out["dry_run"] is True and out["added"] is False and out["held"] == ["live_sending is no"]
    assert item_for(ctx, "acc-1") == before


# -- A3: a lead Instantly leaves out of its add summary is looked up, never lost ------------------------------------


def _adds(t):
    return [r for r in t.requests if r.url.endswith("/leads/add")]


def _none_created(t):
    from tests.test_enrol import added

    t.route("POST", "/leads/add", fn=lambda req: {**added(req), "created_leads": []})


def _in_campaign(t, *emails, lead_id="lead-found"):
    t.route("POST", "/leads/list", fn=lambda req: {"items": [
        {"id": lead_id, "email": e, "campaign": req.json["campaign"]} for e in emails]})


def test_a_tick_whose_lead_is_in_the_campaign_after_all_is_recorded_and_approved():
    from tests.test_send_approvals import item_for, poll

    ctx, t, sl, row = _approved()
    _none_created(t)
    _in_campaign(t, "Jane@AcmeCreative.com")
    assert poll(ctx)["outcomes"] == {"approved": 1}
    row = item_for(ctx, "acc-1")
    assert (row["status"], row["payload"]["outcome"], row["payload"]["added"]["lead_id"]) == (
        "handled", "approved", "lead-found")
    jane = ctx.store.get("contacts", contact_id="con-1")
    assert (jane["instantly_lead_id"], jane["enrolment_month"]) == ("lead-found", "2026-10")


def test_a_tick_whose_lead_instantly_refused_suppresses_the_contact_and_blocks():
    from tests.test_send_approvals import item_for, poll

    ctx, t, sl, row = _approved()
    _none_created(t)
    _in_campaign(t, "someone-else@acmecreative.com")
    assert poll(ctx)["outcomes"] == {"blocked": 1} and len(_adds(t)) == 1
    row = item_for(ctx, "acc-1")
    assert (row["status"], row["payload"]["outcome"], row["payload"]["reason"]) == ("handled", "blocked", enrol.NOT_ADDED)
    jane = ctx.store.get("contacts", contact_id="con-1")
    assert (jane["suppressed"], jane["suppressed_reason"], jane.get("instantly_lead_id")) == (True, enrol.NOT_ADDED, None)
    assert ctx.store.get("accounts", account_id="acc-1")["status"] == "verified"  # the account goes on
    assert any("you'll get a card for the next person at Acme Creative" in x for x in sl.texts())
    _, skipped = enrol.candidates(ctx, frozenset())
    assert skipped["contact suppressed"] == 1  # pick_contacts finds the next person at 05:30


def test_a_tick_whose_lookup_fails_is_resolved_by_the_stuck_pass():
    from tests.test_send_approvals import HARRY_ID, item_for, poll

    ctx, t, sl, row = _approved()
    _none_created(t)
    t.route("POST", "/leads/list", {"error": "down"}, status=503)
    out = poll(ctx)
    assert out["outcomes"] == {} and "could not be read" in out["not_added"][0]["why"][0]
    assert item_for(ctx, "acc-1")["status"] == "sending"  # nothing concluded yet
    poll(ctx)  # 5 minutes later: an add may still be going, so it waits
    assert item_for(ctx, "acc-1")["status"] == "sending" and len(_adds(t)) == 1
    _in_campaign(t, "jane@acmecreative.com")
    out = poll(ctx, minutes=10)
    assert out["outcomes"] == {"approved": 1} and len(_adds(t)) == 1  # found, never added twice
    row = item_for(ctx, "acc-1")
    assert (row["status"], row["handled_by"], row["payload"]["added"]["lead_id"]) == ("handled", HARRY_ID, "lead-found")


def test_a_tick_whose_lookup_fails_and_whose_lead_is_not_there_later_was_refused():
    """Instantly answered the add without creating the lead; a later lookup does not find it: refused, so the
    contact is suppressed and the card closes, as if the lookup had worked at once."""
    from tests.test_send_approvals import item_for, poll

    ctx, t, sl, row = _approved()
    _none_created(t)
    t.route("POST", "/leads/list", {"error": "down"}, status=503)
    poll(ctx)
    assert item_for(ctx, "acc-1")["payload"]["sending"]["answered"] is True
    _in_campaign(t, "someone-else@acmecreative.com")
    out = poll(ctx, minutes=15)
    assert out["outcomes"] == {"blocked": 1} and len(_adds(t)) == 1
    row = item_for(ctx, "acc-1")
    assert (row["status"], row["handled_by"], row["payload"]["reason"]) == ("handled", "U_HARRY", enrol.NOT_ADDED)
    assert ctx.store.get("contacts", contact_id="con-1")["suppressed_reason"] == enrol.NOT_ADDED


def test_an_add_left_sending_whose_lead_is_in_the_campaign_resolves_itself():
    from tests.test_send_approvals import HARRY_ID, item_for, poll, proposed

    ctx, t, sl, _ = proposed()
    row = item_for(ctx, "acc-1")
    sending = {"at": (NOW - timedelta(minutes=20)).isoformat(), "by": HARRY_ID, "via": "✅"}
    ctx.store.update("hitl_items", {"item_id": row["item_id"]},
                     {"status": "sending", "payload": {**row["payload"], "state": "sending", "sending": sending}})
    _in_campaign(t, "jane@acmecreative.com", lead_id="lead-before")
    out = poll(ctx)
    assert out["outcomes"] == {"approved": 1} and out["unsure"] == [] and _adds(t) == []
    row = item_for(ctx, "acc-1")
    assert (row["status"], row["handled_by"], row["payload"]["outcome"]) == ("handled", HARRY_ID, "approved")
    assert ctx.store.get("contacts", contact_id="con-1")["instantly_lead_id"] == "lead-before"
    assert ctx.store.get("events", event_id=f"send-approval:{row['item_id']}")["approval"] == "approved"
