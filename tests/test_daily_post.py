"""The daily post (SPEC 11 "Daily post"; learn/daily_post.py): yesterday's sends, replies and outcomes,
the limiter and each sender, mailbox health with the ramp, kill rules, and what waits for approval."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from tests.test_registry import HANNAH, SAM, setup
from us_outbound.context import Secrets
from us_outbound.learn import daily_post
from us_outbound.clients.slack import dry_run_note

DRY = dry_run_note("#us-outbound") + "\n"

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
         "payload": {"iso_week": "2026-W44", "accounts": [{"account_id": "x1"}, {"account_id": "x2"}]}},
        {"item_id": "k1", "kind": "kill_rule", "status": "open", "created_at": MON,
         "payload": {"action": "pause_source", "target": "clay", "reason": "emails found by clay: 4 of 100 sends bounced"}},
    ])
    return ctx, t


def test_parked_companies_have_their_own_line_and_are_not_waiting_for_verification():
    """W1-4: a parked company is out of the queue while a setting keeps it out, so it is not "being prepared"; the
    pipeline gives it its own line by why: 2 for an industry off, 1 for the label check failing. 1 new account
    (Standard) is the one that needs its facts checked."""
    ctx, t = world()
    ctx.store.insert("accounts", [
        {"account_id": "p1", "status": "parked", "status_reason": "industry_off", "tier": "Excluded"},
        {"account_id": "p2", "status": "parked", "status_reason": "industry_off", "tier": "Excluded"},
        {"account_id": "p3", "status": "parked", "status_reason": "label_failed", "tier": "Excluded"},
        {"account_id": "n1", "status": "new", "tier": "Standard"},
    ])
    daily_post.run(ctx)
    [post] = [r for r in t.requests if r.url.endswith("chat.postMessage")]
    text = post.json["text"].splitlines()
    i = text.index("*Pipeline*")
    assert text[i + 2] == "Being prepared: 0 need a contact · 1 need their facts checked"
    assert text[i + 3] == "Parked: 3 (industry off 2, label check failing 1)"
    lines, nums = daily_post.build(ctx)
    assert "  Parked: 3 (industry off 2, label check failing 1)" in lines
    assert (nums["waiting_for_verification"], nums["parked"]) == (1, 3)


def test_a_campaign_that_is_not_active_has_no_capacity_and_the_post_says_why():
    ctx, _ = world(drafts=("Hannah Spalding",))
    lines, _ = daily_post.build(ctx)
    [line] = [x for x in lines if x.startswith("  Sending capacity, Hannah Spalding")]
    assert "no new leads today" in line and "not active in Instantly (draft)" in line
    assert any("Hannah Spalding's campaign is not active in Instantly" in x for x in lines)


def test_the_short_post_says_what_needs_harry_yesterday_today_the_pipeline_and_what_to_watch():
    """Harry, 9 Oct 2026: "a more readable pithier version of the daily report"; 12 Oct 2026: clear to anyone, even
    new to the system. The 09:00 job posts this; the full post is `us-outbound daily --full`."""
    ctx, t = world()
    out = daily_post.run(ctx)
    [post] = [r for r in t.requests if r.url.endswith("chat.postMessage")]
    assert post.json["text"].splitlines() == [
        ":warning: *Daily summary, Tue 27 Oct: 3 things need you*",
        "1. Answer 1 prospect reply (waiting 30 hours): ✅ or `edit: <text>` in its thread in #us-outbound",
        "2. 1 safety pause on (bounce or complaint limit hit): `railway ssh -- us-outbound killrules show`",
        "3. Weekly hand-check: 2 companies still need a decision on the "
        "<https://docs.google.com/spreadsheets/d/sheet-test/edit|Hand-check tab>",
        "",
        "*Yesterday (Mon 26 Oct)*",
        "Sent 8 emails (5 first, 3 follow-ups) · 3 replies (1 interested) · 1 bounce · 1 unsubscribe · 1 demo booked",
        "Found 0 companies and 0 contacts",
        "Interested: Acme Creative (positive reply to hannah@meetspill.org)",
        "• Brightfin (objection): “We already have an EAP through our broker and the team seems happy with it.”",
        "Visited the US site after we emailed: Brightfin",
        "",
        "*Today: no new people added to the emails*",
        "Why: optout_tested is no: the seed-inbox test of Instantly's unsubscribe link is not done, so "
        "nothing is sent; once it is, set optout_tested = yes on the General tab.",
        "This week: 0 of 150 added, 4 send days left",
        "Test mode: `live_sending` is no, so no prospect is emailed.",
        "",
        "*Pipeline*",
        "Ready to email: 0",
        "Being prepared: 0 need a contact · 0 need their facts checked",
        "",
        next(x for x in post.json["text"].splitlines() if x.startswith("Spend: ")),
        "",
        "*Worth a look*",
        "• Safety pause set off: emails found by clay: 4 of 100 sends bounced",
        "",
        "Fine: mailboxes and campaigns. Full report: `railway ssh -- us-outbound daily --full`",
    ]
    assert (out["sent"], out["replies"], out["positive"]) == (8, 3, 1)  # the summary keeps the same numbers (human replies)


def test_the_short_post_flags_a_loud_mailbox_and_covers_friday_to_sunday_on_monday():
    ctx, _ = world(now=datetime(2026, 10, 26, 9, 0, tzinfo=UTC))
    day = daily_post.gather(ctx)
    day.mailboxes.append(("sam@meetspill.org", 0, 40, 2))
    lines = daily_post.compact(ctx, day)
    assert lines[0] == ":warning: *Daily summary, Mon 26 Oct: 3 things need you*"
    assert "*Fri 23 to Sun 25 Oct*" in lines
    assert ("• sam@meetspill.org: 2 of its last 40 emails bounced (5%); over 3% of 100 it is paused automatically"
            in lines)
    assert lines[-1] == "Fine: campaigns. Full report: `railway ssh -- us-outbound daily --full`"


def test_the_short_post_with_nothing_waiting_says_so_first_and_shows_today_s_number():
    ctx, _ = world()
    ctx.store.delete("hitl_items", {"kind": ["reply_approval", "hand_check", "kill_rule"]})
    with_general(ctx, live_sending=True, optout_tested=True)
    lines = daily_post.compact(ctx, daily_post.gather(ctx))
    assert lines[0] == ":bar_chart: *Daily summary, Tue 27 Oct: nothing needs you today*"
    assert lines[1] == ""  # no numbered asks
    today = lines.index(next(x for x in lines if x.startswith("*Today: ")))
    assert lines[today] == "*Today: up to 0 new people added to the emails*"
    assert lines[today + 1] == "Held to that by the companies ready to email"
    assert lines[today + 2].startswith("Room per sender: Hannah 3 · ")
    assert not any(x.startswith("Test mode") for x in lines)


def test_the_needs_lines_say_where_to_act_for_cards_and_the_doubtful_only_hand_check():
    ctx, _ = world()
    ctx.store.delete("hitl_items", {"kind": ["reply_approval", "kill_rule"]})
    ctx.store.update("hitl_items", {"item_id": "h1"}, {"payload": {"iso_week": "2026-W44", "per_group": 0,
                                                                    "doubtful": [{"account_id": "a1"}, {"account_id": "a2"}]}})
    ctx.store.insert("hitl_items", [
        {"item_id": "s1", "kind": "send_approval", "status": "open", "account_id": "a9", "created_at": TUE_9 - timedelta(hours=5),
         "payload": {"state": "waiting", "owner": "Hannah Spalding", "send_day": "2026-10-27", "expires_on": "2026-10-28"}},
    ])
    lines = daily_post.compact(ctx, daily_post.gather(ctx))
    assert lines[:3] == [
        ":warning: *Daily summary, Tue 27 Oct: 2 things need you*",
        "1. Approve 1 email (waiting 5 hours; a card lapses after its next send day): ✅ or ❌ on each card in #us-outbound",
        "2. Weekly hand-check: 2 companies still need a decision on the "
        "<https://docs.google.com/spreadsheets/d/sheet-test/edit|Hand-check tab>",
    ]


def test_the_post_covers_yesterday_the_limiter_mailboxes_kill_rules_and_approvals():
    ctx, t = world()
    out = daily_post.run(ctx)
    [post] = [r for r in t.requests if r.url.endswith("chat.postMessage")]
    lines, _ = daily_post.build(ctx)  # the full post: `us-outbound daily --full`
    assert lines[0] == "*Daily post, Tue 27 Oct*"
    assert lines[1] == ("Yesterday: 8 sent · 3 replies (1 positive) · 1 unsubscribe · 0 companies, 0 contacts found"
                        " · 0 ready to send")
    assert lines[2] == ("*Needs you:* 1 reply (waited 30 hours) · 1 kill-rule hold (`us-outbound killrules show`) · "
                        "this week's hand-check (the Hand-check tab on the settings sheet)")
    assert "*Sent and outcomes* · Yesterday, Mon 26 Oct (UK)" in lines
    assert "  Sent: 8 (step 1 5, step 2 3)" in lines  # Sunday's send is not yesterday's
    assert "  Replies: 3 (not classified 1, objection 1, positive 1) · out of office 1" in lines
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
    assert "Site visits (spill.chat): not read yet (site_visits, daily 06:00)." in lines  # sources/site_visits.py
    assert "Kill rules fired: emails found by clay: 4 of 100 sends bounced" in lines
    assert "  Holds in force: 1 (`us-outbound killrules show`)" in lines
    # A kill-rule hold is listed as a hold, not as waiting for approval.
    assert "Waiting for approval: 2 (hand-checks 1, reply approvals 1); the oldest has waited 30 hours." in lines
    # Posted to the alert channel; the summary keeps the numbers, never the reply text (SPEC 6 purges it).
    assert post.json["channel"] == "C_ALERT"
    assert (out["sent"], out["replies"], out["positive"], out["bounced"], out["demos_booked"]) == (8, 3, 1, 1, 1)
    assert out["out_of_office"] == 1  # an away message is not a reply (replies/kinds.py), as in the readout
    assert "text" not in out and "We already have" not in str(out)


def test_the_post_has_one_retention_line_when_today_s_run_deleted_anything():
    """ops/retention.py (SPEC 6, 13): counts only, and nothing when the run changed nothing."""
    ctx, _ = world()
    lines, nums = daily_post.build(ctx)
    assert not any(x.startswith("Old personal data") for x in lines) and nums["retention"] == {}
    ctx.store.insert("heartbeats", [{"run_id": "ret", "job": "retention", "status": "ok", "dry_run": False,
                                     "started_at": TUE_9 - timedelta(hours=8), "finished_at": TUE_9,
                                     "detail": {"dry_run": False, "leads": {"deleted": 5}}}])
    lines, nums = daily_post.build(ctx)
    assert lines[-1] == "Old personal data deleted today (our retention rule): 5 Instantly leads (31 days after their last step)."
    assert nums["retention"]["leads"] == 5


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
    assert "Answer 1 prospect reply" in logged[0] and "hannah@meetspill.org" not in logged[0]  # emails hashed


def test_dry_run_posts_to_the_dev_channel():
    ctx, t = world(live=False)
    daily_post.run(ctx)
    [post] = [r for r in t.requests if r.url.endswith("chat.postMessage")]
    assert post.json["channel"] == "C_DEV" and post.json["text"].startswith(DRY + ":warning: *Daily summary")


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


def test_the_daily_command_prints_the_short_or_the_full_post_and_posts_nothing(capsys):
    from tests.test_cli import Harness

    h = Harness()
    assert h.run("daily") == 0
    out = capsys.readouterr().out
    assert "*Daily summary, " in out and "*Pipeline*" in out and "*Sources*" not in out
    assert h.run("daily", "--full") == 0
    out = capsys.readouterr().out
    assert "*Daily post, " in out and "*Sources*" in out
    assert not [r for r in h.transport.requests if r.url.endswith("chat.postMessage")]
