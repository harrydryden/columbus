"""The weekly hand-check (SPEC 11; enrol/hand_check.py): the sample, the hitl item enrol waits for,
the Slack post, and `us-outbound handcheck show|approve` without Slack."""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta

from tests.fakes import FakeTransport, make_context
from tests.test_registry import slack_routes
from tests.test_render import AGENCIES, TECH, make_settings
from tests.test_scoring import EAP_NAMED
from us_outbound import suppression
from us_outbound.enrol import enrol, hand_check

NOW = datetime(2026, 10, 26, 7, 0, tzinfo=UTC)  # Monday 08:00 UK (GMT+0 after 25 Oct), ISO week 2026-W44
WEEK = "2026-W44"
EAP_LINE = "I saw {company} already offers its team an employee assistance program."
# auto_send = yes: the weekly sample is enrol's gate. With auto_send = no (Harry, 2 Oct 2026) only the
# doubtful-fact accounts are posted (tests at the end of this file).
SETTINGS = dataclasses.replace(make_settings(live_sending=True, approver_slack_ids=("U_HARRY",), auto_send=True),
                               signals=(dataclasses.replace(EAP_NAMED, role_openers={"People leader": EAP_LINE}),))


def acct(i: int, group: str, status: str = "verified", **kw) -> dict:
    industry = "Advertising agencies" if group == AGENCIES else "Fintech"
    return {"account_id": f"{group[:3].lower()}-{i:02d}", "domain": f"{group[:3].lower()}{i}.com",
            "clean_name": f"{group[:3]} Co {i}", "hq_city": "Chicago", "hq_state": "IL", "employees": 40,
            "size_band": "20-49", "industry": industry, "industry_group": group, "tier": "Priority", "score": 55,
            "angle": "Upgrade the EAP", "status": status, **kw}


def world(*, live: bool = True, now: datetime = NOW):
    t = slack_routes(FakeTransport())
    ctx = make_context(SETTINGS, live=live, transport=t, now=now)
    accounts = [acct(i, AGENCIES) for i in range(12)] + [acct(20 + i, AGENCIES, "queued") for i in range(3)]
    accounts += [acct(i, TECH) for i in range(2)] + [acct(10 + i, TECH, "queued") for i in range(5)]
    accounts += [
        acct(90, AGENCIES, tier="Held"),  # enrol never takes Held
        acct(91, AGENCIES, status="enrolled"),
        acct(92, AGENCIES, industry="Staffing agencies", industry_group="Professional Services"),  # switched off
        acct(93, TECH, domain="suppressed.com"),
    ]
    ctx.store.insert("accounts", accounts)
    suppression.add(ctx.store, domain="suppressed.com", reason="kill_rule", source="test", now=now)
    ctx.store.insert("contacts", [
        {"contact_id": "k0", "account_id": "mar-00", "role": "People leader", "title": "Head of People",
         "first_name": "Jane", "email": "jane@mar0.com", "email_status": "verified", "person_state": "IL"},
        {"contact_id": "k1", "account_id": "mar-01", "role": "Operations", "title": "Office Manager", "email": ""},
    ])
    ctx.store.insert("signal_events", [{
        "event_id": "f0", "account_id": "mar-00", "source": "clay_careers", "fact": "benefit",
        "value": "Employee assistance program", "quote": "Every employee gets our employee assistance program.",
        "source_url": "https://mar0.com/careers", "observed_at": now - timedelta(days=3)}])
    return ctx, t


def posts(t):
    return [r for r in t.requests if r.url.endswith("chat.postMessage")]


def test_the_job_records_this_week_s_sample_and_posts_it():
    ctx, t = world()
    out = hand_check.post(ctx)
    assert out["status"] == "recorded" and out["iso_week"] == WEEK
    assert out["groups"] == {AGENCIES: 10, TECH: 7}  # 10 per group; Tech has 7 to take
    item = ctx.store.get("hitl_items", item_id=f"hand_check-{WEEK}")
    assert item["kind"] == "hand_check" and item["status"] == "open"
    p = item["payload"]
    assert p["iso_week"] == WEEK and p["per_group"] == 10 and p["posted"] is True
    agencies = p["groups"][AGENCIES]
    assert all(a.startswith("mar-0") or a.startswith("mar-1") for a in agencies)  # verified first: 12 of them
    assert set(p["groups"][TECH]) == {"tec-00", "tec-01", "tec-10", "tec-11", "tec-12", "tec-13", "tec-14"}
    left_out = {"mar-90", "mar-91", "mar-92", "tec-93"}
    assert not left_out & {a for ids in p["groups"].values() for a in ids}
    facts = {a["account_id"]: a for a in p["accounts"]}
    assert set(facts) == {a for ids in p["groups"].values() for a in ids}
    assert facts["tec-10"]["contact"]["note"].startswith("no contact yet")
    # Posted to the alert channel, mentioning Harry, with how to approve without Slack.
    [post] = posts(t)
    assert post.json["channel"] == "C_ALERT"
    text = post.json["text"]
    assert text.startswith("<@U_HARRY> Week 44 check: 17 accounts drawn at random from the queue. Is each one right: "
                           "the name, HQ state, size band, the contact's role and title, and what the opener rests on?")
    assert text.endswith("All fine: `railway ssh -- us-outbound handcheck approve --live`. Any wrong: add "
                         f"`--pull {p['accounts'][0]['domain']}`, or fix it on the Overrides tab. Until then no new "
                         "leads go to Instantly this week.")
    assert "SPEC" not in text and "2026" not in text
    assert item["slack_ts"] == "1.1"
    # Enrollment waits for it.
    assert enrol.hand_check(ctx, ctx.now_et().date()) == (f"this week's hand-check ({WEEK}) is not approved yet", frozenset())


def test_the_draw_is_the_same_all_week_and_a_second_run_posts_nothing():
    ctx, t = world()
    first = hand_check.draw(ctx, WEEK)
    assert hand_check.draw(ctx, WEEK) == first
    assert hand_check.draw(ctx, "2026-W45") != first
    hand_check.post(ctx)
    again = hand_check.post(ctx)
    assert again["status"] == "posted already" and len(posts(t)) == 1
    assert len(ctx.store.select("hitl_items", {"kind": "hand_check"})) == 1


def test_the_facts_harry_checks():
    ctx, _ = world()
    domains, hashes = enrol.suppressed(ctx)
    f = hand_check.facts(ctx, ctx.store.get("accounts", account_id="mar-00"), domains, hashes)
    assert (f["clean_name"], f["hq_state"], f["size_band"], f["industry_group"]) == ("Mar Co 0", "IL", "20-49", AGENCIES)
    assert f["contact"] == {"role": "People leader", "title": "Head of People", "note": ""}
    # The opener enrol would send this contact (enrol/openers.py): the EAP line for a People leader.
    assert f["opener"] == "I saw Mar Co 0 already offers its team an employee assistance program."
    assert (f["opener_arm"], f["opener_source"]) == ("opener", "EAP named / opener_people")
    # A held-out account sends none, and Harry sees the line it would have had.
    ctx.settings = dataclasses.replace(SETTINGS, general=dataclasses.replace(SETTINGS.general, opener_holdout_share=1.0))
    held = hand_check.facts(ctx, ctx.store.get("accounts", account_id="mar-00"), domains, hashes)
    assert held["opener_arm"] == "holdout"
    assert hand_check._opener(held) == ("none (held out; would be: I saw Mar Co 0 already offers its team an "
                                        "employee assistance program.)")
    [ev] = f["evidence"]
    assert (ev["signal"], ev["url"]) == ("EAP named", "https://mar0.com/careers")
    assert ev["quote"] == "Every employee gets our employee assistance program."
    line = hand_check.text({"iso_week": WEEK, "groups": {AGENCIES: ["mar-00"]}, "accounts": [f]}, detailed=False)
    assert ("• Mar Co 0 (mar0.com) · IL · 20-49 · People leader, Head of People · EAP named: “Every employee gets "
            "our employee assistance program.” (https://mar0.com/careers) · id mar-00") in line


def test_a_contact_without_an_email_is_shown_with_why():
    ctx, _ = world()
    domains, hashes = enrol.suppressed(ctx)
    f = hand_check.facts(ctx, ctx.store.get("accounts", account_id="mar-01"), domains, hashes)
    assert f["contact"] == {"role": "Operations", "title": "Office Manager", "note": "not sendable yet: contact has no email"}


def test_a_dry_run_records_the_item_and_the_live_run_posts_it():
    ctx, t = world(live=False)
    out = hand_check.post(ctx)
    assert out["status"] == "recorded"
    assert [p.json["channel"] for p in posts(t)] == ["C_DEV"]
    assert ctx.store.get("hitl_items", item_id=f"hand_check-{WEEK}")["payload"]["posted"] is False
    live = make_context(SETTINGS, live=True, transport=t, now=NOW + timedelta(minutes=5), store=ctx.store)
    assert hand_check.post(live)["status"] == "posted"
    assert [p.json["channel"] for p in posts(t)] == ["C_DEV", "C_ALERT"]
    assert ctx.store.get("hitl_items", item_id=f"hand_check-{WEEK}")["payload"]["posted"] is True


def test_nothing_to_check_records_no_item():
    ctx = make_context(SETTINGS, live=True, transport=slack_routes(FakeTransport()), now=NOW)
    out = hand_check.post(ctx)
    assert out["status"].startswith("nothing to check") and ctx.store.select("hitl_items") == []


# -- without Slack: the handcheck command -----------------------------------------------------------------


def harness():
    from tests.test_cli import Harness

    h = Harness(SETTINGS)
    ctx, _ = world()
    h.store.tables = ctx.store.tables  # the same accounts, contacts and facts
    return h


def test_handcheck_show_prints_the_sample_and_never_records_it(capsys):
    """A9: `handcheck show` only prints; `handcheck approve` records the same draw (seeded with the week)."""
    h = harness()
    assert h.run("handcheck", "show") == 0
    out = capsys.readouterr().out
    assert "Week 44 check: 17 accounts drawn at random from the queue" in out
    assert "Marketing & Creative Agencies (10):" in out and "Contact: " in out and "Evidence: " in out
    assert "All fine: `us-outbound handcheck approve --live`." in out
    assert h.run("handcheck", "show", "--live") == 0
    assert h.store.select("hitl_items", {"kind": "hand_check"}) == []
    shown = hand_check.show(h.last)[1]["groups"]
    assert h.run("handcheck", "approve", "--live") == 0
    [item] = h.store.select("hitl_items", {"kind": "hand_check"})
    assert item["status"] == "handled" and item["payload"]["groups"] == shown  # what show printed


def test_handcheck_approve_marks_the_week_handled_with_the_pulled_accounts(capsys):
    h = harness()
    assert h.run("run", "hand_check_post", "--live") == 0  # recorded and posted
    item = h.store.select("hitl_items", {"kind": "hand_check"})[0]
    sample = item["payload"]["groups"][TECH]
    assert h.run("handcheck", "approve", "--pull", sample[0], "tec1.com") == 0  # dry-run: nothing changes
    assert h.store.get("hitl_items", item_id=item["item_id"])["status"] == "open"
    assert h.run("handcheck", "approve", "--pull", "no-such-account", "--live") == 2
    assert h.run("handcheck", "approve", "--pull", sample[0], "tec1.com", "--live") == 0
    done = h.store.get("hitl_items", item_id=item["item_id"])
    assert done["status"] == "handled" and done["handled_by"]
    assert set(done["payload"]["pulled_account_ids"]) == {sample[0], "tec-01"}
    # enrol honours it: the week is approved and the pulled accounts are left out.
    ctx = h.last
    why, pulled = enrol.hand_check(ctx, ctx.now_et().date())
    assert why is None and pulled == frozenset({sample[0], "tec-01"})
    cands, skipped = enrol.candidates(ctx, pulled)
    assert skipped["pulled at this week's hand-check"] == 2


def test_hand_check_post_runs_as_a_job_from_the_cli():
    h = harness()
    assert h.run("run", "hand_check_post") == 0
    [beat] = h.beats("hand_check_post")
    assert beat["status"] == "ok" and beat["detail"]["status"] == "recorded"


# -- auto_send = no (Harry, 2 Oct 2026): every email is approved in Slack, so no weekly sample -----------------


def _approving(ctx):
    ctx.settings = dataclasses.replace(SETTINGS, general=dataclasses.replace(SETTINGS.general, auto_send=False))


def test_with_auto_send_off_the_job_skips_when_no_account_is_held_for_doubts():
    ctx, t = world()
    _approving(ctx)
    out = hand_check.post(ctx)
    assert out["skipped"] is True and out["reason"].startswith("auto_send = no: every email is approved in Slack")
    assert ctx.store.select("hitl_items", {"kind": "hand_check"}) == [] and posts(t) == []


def test_with_auto_send_off_the_doubtful_accounts_still_go_to_a_person():
    from us_outbound import verify

    ctx, t = world()
    _approving(ctx)
    held = acct(70, AGENCIES, "queued", employees=None, size_band="", hq_state="")
    ctx.store.insert("accounts", [held])
    ctx.store.insert("signal_events", [verify.doubt_fact(ctx, held, ["no HQ state"])])
    out = hand_check.post(ctx)
    assert out["status"] == "recorded" and out["accounts"] == 0 and out["doubtful"] == 1 and out["groups"] == {}
    [post] = posts(t)
    text = post.json["text"]
    assert text.startswith("<@U_HARRY> Week 44 check: 1 account has doubtful Apollo facts, so it won't get a card "
                           "until you look.\n  1. Mar Co 70 (mar70.com) · HQ ? · size unknown · no HQ state · id mar-70\n")
    assert text.endswith("All fine: `railway ssh -- us-outbound handcheck approve --live`. Any wrong: add "
                         "`--pull mar70.com`, or fix it on the Overrides tab. Ignoring this is safe: only these "
                         "accounts wait.")
    assert "SPEC" not in text and "Harry, 2 Oct" not in text


# -- A9: auto_send switched on after a doubtful-only week, and the confirmation --------------------------------


def _doubtful_week(ctx):
    """auto_send = no: the week's item holds only the account with doubtful facts (no sample)."""
    from us_outbound import verify

    _approving(ctx)
    held = acct(70, AGENCIES, "queued", employees=None, size_band="", hq_state="")
    ctx.store.insert("accounts", [held])
    ctx.store.insert("signal_events", [verify.doubt_fact(ctx, held, ["no HQ state"])])
    assert hand_check.post(ctx)["status"] == "recorded"
    return ctx.store.get("hitl_items", item_id=f"hand_check-{WEEK}")


def _auto_send_on(ctx):
    ctx.settings = dataclasses.replace(SETTINGS, general=dataclasses.replace(SETTINGS.general, auto_send=True))


def test_a_doubtful_only_week_does_not_pass_the_gate_once_auto_send_is_yes():
    ctx, t = world()
    item = _doubtful_week(ctx)
    assert item["payload"]["per_group"] == 0 and not enrol.has_sample(item["payload"])
    hand_check.approve(ctx, ["mar70.com"], "harry")
    assert enrol.hand_check(ctx, ctx.now_et().date()) == (None, frozenset({"mar-70"}))  # auto_send = no: fine
    _auto_send_on(ctx)
    why, pulled = enrol.hand_check(ctx, ctx.now_et().date())
    assert why.startswith(f"this week's hand-check ({WEEK}) has no random sample") and pulled == frozenset()
    # hand_check_post records it again with the sample, the pull kept, open until it is approved.
    out = hand_check.post(ctx)
    assert out["status"] == "recorded again with the sample (auto_send is yes now)" and out["accounts"] == 17
    item = ctx.store.get("hitl_items", item_id=f"hand_check-{WEEK}")
    assert (item["status"], item["handled_by"], item["payload"]["per_group"]) == ("open", None, 10)
    assert item["payload"]["pulled_account_ids"] == ["mar-70"]
    assert posts(t)[-1].json["text"].startswith("<@U_HARRY> Week 44 check: 17 accounts drawn at random")
    assert "not approved yet" in enrol.hand_check(ctx, ctx.now_et().date())[0]
    hand_check.approve(ctx, [], "harry")
    assert enrol.hand_check(ctx, ctx.now_et().date()) == (None, frozenset({"mar-70"}))
    assert len(ctx.store.select("hitl_items", {"kind": "hand_check"})) == 1


def test_approve_records_the_sample_itself_when_the_week_has_none():
    ctx, t = world()
    _doubtful_week(ctx)
    hand_check.approve(ctx, [], "harry")
    _auto_send_on(ctx)
    item, payload = hand_check.show(ctx)  # what approve will record; nothing is written
    assert item is None and payload["per_group"] == 10 and len(payload["accounts"]) == 17
    assert ctx.store.get("hitl_items", item_id=f"hand_check-{WEEK}")["payload"]["per_group"] == 0
    dry = make_context(ctx.settings, live=False, transport=t, now=ctx.now, store=ctx.store)
    out = hand_check.approve(dry, [], "harry")
    assert (out["approved"], out["recorded"], out["was"]) == (False, False, "not recorded")
    assert ctx.store.get("hitl_items", item_id=f"hand_check-{WEEK}")["payload"]["per_group"] == 0  # dry: unchanged
    out = hand_check.approve(ctx, [], "harry")
    assert (out["approved"], out["recorded"]) == (True, True)
    item = ctx.store.get("hitl_items", item_id=f"hand_check-{WEEK}")
    assert item["status"] == "handled" and item["payload"]["groups"] == payload["groups"]
    assert enrol.hand_check(ctx, ctx.now_et().date())[0] is None


def test_approve_with_nothing_to_check_says_so():
    ctx = make_context(SETTINGS, live=True, transport=slack_routes(FakeTransport()), now=NOW)
    import pytest

    with pytest.raises(LookupError, match=f"nothing to check for {WEEK}"):
        hand_check.approve(ctx, [], "harry")


def test_the_confirmation_says_what_happens_next_in_plain_words():
    ctx, t = world()
    _doubtful_week(ctx)
    out = hand_check.approve(ctx, [], "harry")  # its HQ state and size are missing: approving cannot supply them
    assert out["message"] == ("Week 44 check approved by harry. The account missing a fact (HQ state, size or "
                              "industry) stays on the check until an Overrides row fills it in.")
    assert posts(t)[-1].json["text"].endswith(out["message"])  # the item was posted, so the channel hears it
    friday = make_context(ctx.settings, live=True, transport=t, now=NOW + timedelta(days=4), store=ctx.store)
    assert hand_check.confirmation(friday, WEEK, "harry", [], 2) == (
        "Week 44 check approved by harry. The 2 accounts with doubtful facts are verified again at 04:30 on Monday.")
    monday = make_context(ctx.settings, live=True, transport=t, now=NOW, store=ctx.store)
    assert hand_check.confirmation(monday, WEEK, "harry", [], 1, 2) == (
        "Week 44 check approved by harry. The account with doubtful facts is verified again at 04:30 tomorrow. "
        "The 2 accounts missing a fact (HQ state, size or industry) stay on the check until an Overrides row fills it in.")
    _auto_send_on(ctx)
    assert hand_check.confirmation(ctx, WEEK, "harry", ["tec-01"], 0) == (
        "Week 44 check approved by harry; pulled: tec-01. New leads can go to Instantly this week.")
    assert "SPEC" not in out["message"]
