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
SETTINGS = dataclasses.replace(make_settings(live_sending=True, approver_slack_ids=("U_HARRY",)), signals=(EAP_NAMED,))


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
    assert text.startswith(f"<@U_HARRY> Weekly hand-check {WEEK}") and "`us-outbound handcheck approve --live`" in text
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
    assert f["opener"] == "I saw your team already has an employee assistance program."
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


def test_handcheck_show_prints_the_sample_and_records_it_only_when_live(capsys):
    h = harness()
    assert h.run("handcheck", "show") == 0
    out = capsys.readouterr().out
    assert f"Weekly hand-check {WEEK}" in out and "Marketing & Creative Agencies (10):" in out
    assert "Contact: " in out and "Evidence: " in out
    assert "Not recorded yet" in out and h.store.select("hitl_items", {"kind": "hand_check"}) == []
    assert h.run("handcheck", "show", "--live") == 0
    assert "Item hand_check-2026-W44: open." in capsys.readouterr().out
    assert len(h.store.select("hitl_items", {"kind": "hand_check"})) == 1


def test_handcheck_approve_marks_the_week_handled_with_the_pulled_accounts(capsys):
    h = harness()
    assert h.run("handcheck", "approve", "--live") == 2  # nothing recorded yet
    assert "handcheck show --live" in capsys.readouterr().err
    assert h.run("handcheck", "show", "--live") == 0
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
