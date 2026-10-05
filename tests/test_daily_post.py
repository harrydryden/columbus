"""The daily post (SPEC 11 "Daily post"; learn/daily_post.py): yesterday's sends, replies and outcomes,
the limiter and each sender, mailbox health with the ramp, kill rules, and what waits for approval."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from tests.test_registry import HANNAH, SAM, setup
from us_outbound.context import Secrets
from us_outbound.learn import daily_post

TUE_9 = datetime(2026, 10, 27, 9, 0, tzinfo=UTC)  # 09:00 UK (GMT), Tue 27 Oct
MON = datetime(2026, 10, 26, 15, 0, tzinfo=UTC)  # Monday, inside the send window


def ev(i, type_, **kw):
    return {"event_id": f"e{i}", "type": type_, "occurred_at": kw.pop("at", MON + timedelta(minutes=i)), **kw}


def world(now=TUE_9, live=True, drafts=(), **kw):
    """drafts: owners whose campaign is still a draft; the others are active (`us-outbound start --live` has run)."""
    ctx, t, inst, sheets = setup(now=now, live=live, ramp_done=False, **kw)
    for owner in ("Hannah Spalding", "Sam Jackson", "Harry Dryden"):
        inst.add_campaign(f"US Outbound – {owner}", status=0 if owner in drafts else 1)
    ctx.store.insert("accounts", [
        {"account_id": "a1", "clean_name": "Acme Creative", "status": "engaged"},
        {"account_id": "a2", "clean_name": "Brightfin", "status": "enrolled"},
        {"account_id": "a3", "clean_name": "Loop Studio", "status": "enrolled"},
        {"account_id": "a4", "clean_name": "Northwind", "status": "enrolled"},
    ])
    events = [ev(i, "sent", mailbox=HANNAH, step=1, account_id=f"x{i}", contact_id=f"k{i}") for i in range(5)]
    events += [ev(10 + i, "sent", mailbox=SAM, step=2, account_id=f"y{i}", contact_id=f"m{i}") for i in range(3)]
    events += [
        ev(20, "replied", account_id="a1", reply_class="positive", mailbox=HANNAH),
        ev(21, "replied", account_id="a2", reply_class="objection", mailbox=HANNAH,
           reply_text="We already have an EAP through our broker and the team seems happy with it."),
        ev(22, "replied", account_id="a3", reply_class="out_of_office", mailbox=SAM),
        ev(23, "replied", account_id="a4", reply_class=None, mailbox=SAM),
        ev(24, "bounced", contact_id="k0", step=1),
        ev(25, "unsubscribed", contact_id="k1", account_id="x1"),
        ev(26, "meeting_booked", account_id="a1"),
        ev(27, "site_visit", account_id="a2"),
        ev(30, "sent", mailbox=HANNAH, step=1, account_id="old", contact_id="old", at=MON - timedelta(days=1)),  # Sunday
    ]
    ctx.store.insert("events", events)
    ctx.store.insert("hitl_items", [
        {"item_id": "r1", "kind": "reply_approval", "status": "open", "created_at": now - timedelta(hours=30)},
        {"item_id": "h1", "kind": "hand_check", "status": "open", "created_at": now - timedelta(hours=1),
         "payload": {"iso_week": "2026-W44"}},
        {"item_id": "k1", "kind": "kill_rule", "status": "open", "created_at": MON,
         "payload": {"action": "pause_source", "target": "clay", "reason": "emails found by clay: 4 of 100 sends bounced"}},
    ])
    return ctx, t


def test_a_campaign_that_is_not_active_has_no_capacity_and_the_post_says_why():
    ctx, _ = world(drafts=("Hannah Spalding",))
    lines, _ = daily_post.build(ctx)
    [line] = [x for x in lines if x.startswith("  Sending capacity, Hannah Spalding")]
    assert "no new leads today" in line and "not active in Instantly (draft)" in line
    assert any("Hannah Spalding's campaign is not active in Instantly" in x for x in lines)


def test_the_post_covers_yesterday_the_limiter_mailboxes_kill_rules_and_approvals():
    ctx, t = world()
    out = daily_post.run(ctx)
    [post] = [r for r in t.requests if r.url.endswith("chat.postMessage")]
    text = post.json["text"]
    lines = text.splitlines()
    assert lines[0] == "*Daily post, Tue 27 Oct*"
    assert lines[1] == ("Yesterday: 8 sent · 4 replies (1 positive) · 1 unsubscribe · 0 companies, 0 contacts found"
                        " · 0 ready to send")
    assert lines[2] == ("*Needs you:* 1 reply (waited 30 hours) · 1 kill-rule hold (`us-outbound killrules show`) · "
                        "this week's hand-check (`us-outbound handcheck show`)")
    assert "*Sent and outcomes* · Yesterday, Mon 26 Oct (UK)" in lines
    assert "  Sent: 8 (step 1 5, step 2 3)" in lines  # Sunday's send is not yesterday's
    assert "  Replies: 4 (not classified 1, objection 1, out_of_office 1, positive 1)" in lines
    assert "  Positive or referral: 1 · Acme Creative (positive, hannah@meetspill.org)" in lines
    assert "  Bounces: 1 · Unsubscribes: 1 · Demos booked: 1 · Demos held: 0" in lines
    assert ("  • Brightfin (objection): “We already have an EAP through our broker and the team seems happy with it.”"
            in lines)
    assert "Out of office: Loop Studio" in lines
    assert "Warm accounts (enrolled, visited the US site): Brightfin" in lines
    # The limiter and each sender (limits.py), with the ramp: Hannah first sent on Monday.
    assert any(line.startswith("Today: 0, limited by ready accounts") for line in lines)
    assert any(line.startswith("  Sending capacity, Hannah Spalding: 3 new leads today, 10 sends a day") for line in lines)
    # enrol's own gate (this test context is live): the opt-out test is not done, so nothing is sent
    assert any(line.startswith("  Enrollment waits: optout_tested is no") for line in lines)
    assert any(line.startswith("  Apollo: 0 of 2,000 credits used this month") for line in lines)
    assert ("  • hannah@meetspill.org (Active): 5 sent; bounces 1 of its last 6 sends (16.7%); ramp week 1 "
            "(first send Sun 25 Oct): 10 a day of its 30; 20 from Sun 01 Nov") in lines
    assert "Funding (Apollo enrich, Technology & Startups): no account enriched yet (apollo_enrich, weekdays 04:10)." in lines
    assert "Kill rules fired: emails found by clay: 4 of 100 sends bounced" in lines
    assert "  Holds in force: 1 (`us-outbound killrules show`)" in lines
    # A kill-rule hold is listed as a hold, not as waiting for approval.
    assert "Waiting for approval: 2 (hand-checks 1, reply approvals 1); the oldest has waited 30 hours." in lines
    # Posted to the alert channel; the summary keeps the numbers, never the reply text (SPEC 6 purges it).
    assert post.json["channel"] == "C_ALERT"
    assert (out["sent"], out["replies"], out["positive"], out["bounced"], out["demos_booked"]) == (8, 4, 1, 1, 1)
    assert "text" not in out and "We already have" not in str(out)


def test_monday_s_post_covers_friday_to_sunday():
    ctx, _ = world(now=datetime(2026, 10, 26, 9, 0, tzinfo=UTC))
    start, end, label = daily_post.period(ctx)
    assert label == "Since Fri 23 Oct" and (end - start) == timedelta(days=3)


def test_with_no_slack_token_the_post_goes_to_the_log(capsys):
    ctx, t = world()
    ctx.clients.secrets = Secrets(ctx.guard, fetch=lambda name: "" if "SLACK" in name else f"test-{name}")
    out = daily_post.run(ctx)
    assert out["alert"]["posted"] is False and out["alert"]["error"] == "no Slack token: posted to the log"
    logged = [line for line in capsys.readouterr().out.splitlines() if '"event": "slack_off"' in line]
    assert len(logged) == 1  # the whole post, line by line, not clipped to 200 characters
    assert "Waiting for approval: 2" in logged[0] and "hannah@meetspill.org" not in logged[0]  # emails hashed


def test_dry_run_posts_to_the_dev_channel():
    ctx, t = world(live=False)
    daily_post.run(ctx)
    [post] = [r for r in t.requests if r.url.endswith("chat.postMessage")]
    assert post.json["channel"] == "C_DEV" and post.json["text"].startswith("[dry-run → #us-outbound] *Daily post")


def test_daily_post_runs_as_a_job_from_the_cli():
    from tests.test_cli import Harness

    h = Harness()
    assert h.run("dry-run", "daily_post") == 0
    [beat] = h.beats("daily_post")
    assert beat["status"] == "ok" and beat["detail"]["sent"] == 0 and "text" not in beat["detail"]


# -- why enrollment waits, and what needs Harry (4 Oct 2026) -------------------------------------------------------


def with_general(ctx, **values):
    import dataclasses

    ctx.settings = dataclasses.replace(ctx.settings, general=dataclasses.replace(ctx.settings.general, **values))


def waits(lines):
    return [line for line in lines if line.startswith(("  Enrollment waits", "  Running dry"))]


def test_enrollment_waits_on_enrol_s_own_gates_and_the_hand_check_only_with_auto_send_on():
    ctx, _ = world()
    with_general(ctx, live_sending=True, approver_slack_ids=("U_HARRY",), optout_tested=True)
    lines, _ = daily_post.build(ctx)
    # auto_send = no: the open hand-check does not hold anything back; the positive reply waiting 30 hours does.
    assert waits(lines) == ["  Enrollment waits: 1 positive reply has waited over 24 hours for approval."]
    ctx.store.delete("hitl_items", {"item_id": "r1"})
    lines, _ = daily_post.build(ctx)
    assert waits(lines) == []
    with_general(ctx, auto_send=True)  # now enrol waits for the weekly hand-check
    lines, _ = daily_post.build(ctx)
    assert waits(lines) == ["  Enrollment waits: this week's hand-check (2026-W44) is not approved yet."]


def test_with_live_sending_no_the_post_says_enrol_runs_dry():
    ctx, _ = world(live=False)
    ctx.store.delete("hitl_items", {"item_id": "r1"})
    lines, _ = daily_post.build(ctx)
    assert waits(lines) == ["  Running dry: live_sending is no, so enrol reaches nobody (a few cards go to the dev "
                            "channel as a preview)."]


def test_needs_you_says_nothing_when_nothing_waits():
    ctx, _ = world()
    ctx.store.delete("hitl_items", {"kind": ["reply_approval", "hand_check", "kill_rule"]})
    lines, nums = daily_post.build(ctx)
    assert lines[2] == "*Needs you:* nothing"
    assert nums["needs_you"] == {"send_approvals": 0, "replies": 0, "kill_rule_holds": 0, "hand_checks": 0}
    assert "Waiting for approval: nothing." in lines


def test_reply_items_are_labelled_replies_and_a_lapsed_card_needs_nobody():
    ctx, _ = world()
    ctx.store.insert("hitl_items", [
        {"item_id": "r2", "kind": "reply", "status": "escalated", "created_at": TUE_9 - timedelta(hours=3),
         "payload": {"reply_class": "objection"}},
        {"item_id": "s1", "kind": "send_approval", "status": "open", "account_id": "a9", "created_at": MON,
         "payload": {"state": "waiting", "owner": "Hannah Spalding", "send_day": "2026-10-23",
                     "expires_on": "2026-10-26"}},  # lapsed at the end of Monday: poll_approvals closes it
    ])
    lines, nums = daily_post.build(ctx)
    assert lines[2].startswith("*Needs you:* 2 replies (the oldest has waited 30 hours) · 1 kill-rule hold")
    assert nums["needs_you"]["send_approvals"] == 0
    [line] = [x for x in lines if x.startswith("Waiting for approval:")]
    assert line.startswith("Waiting for approval: 4 (") and "replies 1" in line and "reply approvals 1" in line
    assert "send approvals 1" in line and "kill-rule" not in line
