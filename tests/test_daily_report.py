"""The daily post's funnel sections (learn/daily_report.py; Harry, 2 Oct 2026): the headline, Approvals from
the send-approval contract (and silence before it exists), Found, Pipeline, and To improve with its thresholds."""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta

from tests.test_daily_post import MON, TUE_9, ev, world
from tests.test_registry import HANNAH
from us_outbound.learn import daily_post, daily_report
from us_outbound.settings.model import Focus

MON_9 = datetime(2026, 10, 26, 9, 0, tzinfo=UTC)  # 09:00 UK, Mon 26 Oct: the post covers Fri 23 to Sun 25
SECTIONS = ("*Sent and outcomes*", "*Approvals*", "*Found*", "*Pipeline*", "*To improve*", "*Today's number*",
            "*Sources*", "*Mailboxes*", "*Kill rules and items waiting*")


def with_auto_send(ctx, value):
    ctx.settings = dataclasses.replace(ctx.settings, general=dataclasses.replace(ctx.settings.general, auto_send=value))


def acct(aid, **kw):
    return {"account_id": aid, "domain": f"{aid}.com", "clean_name": f"Company {aid}", "first_seen": MON - timedelta(days=20),
            **kw}


def person(cid, aid, **kw):
    return {"contact_id": cid, "account_id": aid, "email": f"pat.lee@{aid}.com", "email_status": "verified",
            "email_source": "apollo", "person_state": "NY", "role": "People leader",
            "created_at": MON - timedelta(days=10), **kw}


def funnel(now=TUE_9, auto_send=None, approvals=True):
    """world() plus a pipeline: new, queued, verified, ready, waiting, enrolled and finished accounts."""
    ctx, t = world(now=now)
    ctx.store.insert("accounts", [
        # Found yesterday: two from Apollo, one named; tiers as the rescore left them.
        acct("n1", source="apollo", tier="Priority", status="queued", size_band="20-49", first_seen=MON),
        acct("n2", source="apollo", tier="Control", status="new", size_band="50-99", first_seen=MON),
        acct("n3", source="named", status="new", first_seen=MON + timedelta(hours=1)),
        acct("old", source="apollo", tier="Control", status="new", size_band="10-19"),  # found before yesterday
        acct("q1", source="apollo", tier="Standard", status="queued"),  # no size band; doubtful facts
        acct("e1", source="apollo", tier="Excluded", status="new"),  # not in the queue
        # Ready: verified, in a queue tier, with a sendable contact.
        acct("r1", tier="Priority", status="verified", industry_group="Legal Teams", size_band="20-49"),
        acct("r2", tier="Standard", status="verified", industry_group="Legal Teams", size_band="50-99"),
        acct("r3", tier="Control", status="verified", size_band="20-49"),
        acct("r4", tier="Standard", status="verified", size_band="20-49"),  # waiting for a ✅
        # Verified, waiting for a contact; at w2 nobody suitable was found yesterday.
        acct("w1", tier="Standard", status="verified", size_band="20-49"),
        acct("w2", tier="Standard", status="verified"),
        # In sequence, finished, stopped.
        acct("s1", tier="Standard", status="enrolled"),
        acct("s2", tier="Standard", status="enrolled"),
        acct("s3", tier="Standard", status="engaged"),
    ])
    ctx.store.insert("contacts", [
        person("c_r1", "r1", created_at=MON + timedelta(hours=2)),
        person("c_r2", "r2", role="Founder or executive"),
        person("c_r3", "r3"),
        person("c_r4", "r4"),
        person("c_q1", "q1", email_source="clay", role="Founder or executive", created_at=MON + timedelta(hours=3)),
        person("c_s1", "s1", enrolled_at=datetime(2026, 10, 23, 14, 0, tzinfo=UTC), enrolment_month="2026-10"),
        person("c_s2", "s2", enrolled_at=datetime(2026, 9, 25, 14, 0, tzinfo=UTC), enrolment_month="2026-09"),
        person("c_s3", "s3", enrolled_at=datetime(2026, 10, 20, 14, 0, tzinfo=UTC), enrolment_month="2026-10"),
    ])
    ctx.store.insert("events", [ev(40, "replied", contact_id="c_s3", account_id="s3", reply_class="positive",
                                   at=datetime(2026, 10, 22, 15, 0, tzinfo=UTC))])
    ctx.store.insert("signal_events", [
        {"event_id": "f1", "account_id": "w2", "source": "pick_contacts", "fact": "contact_pick",
         "value": {"outcome": "none", "reason": "company size unknown"}, "observed_at": MON},
        {"event_id": "f2", "account_id": "zz", "source": "pick_contacts", "fact": "contact_pick",
         "value": {"outcome": "none", "reason": "no sendable email among the top 2 (email status unverified, contact in CA "
                                                "or WA)"}, "observed_at": MON},
        {"event_id": "f3", "account_id": "r1", "source": "pick_contacts", "fact": "contact_pick",
         "value": {"outcome": "picked"}, "observed_at": MON},
        {"event_id": "d1", "account_id": "q1", "source": "verify_accounts", "fact": "doubtful_facts",
         "value": {"reasons": ["employee count unknown"]}, "observed_at": MON},
    ])
    ctx.store.insert("heartbeats", [
        {"run_id": "v1", "job": "verify_accounts", "status": "ok", "started_at": MON.replace(hour=4, minute=30),
         "detail": {"verified": 4}},
        {"run_id": "v0", "job": "verify_accounts", "status": "ok", "started_at": MON - timedelta(days=3),
         "detail": {"verified": 9}},  # Friday's run, not yesterday's
    ])
    if approvals:
        ctx.store.insert("hitl_items", [
            {"item_id": "ap1", "kind": "send_approval", "status": "open", "account_id": "r4", "contact_id": "c_r4",
             "created_at": now - timedelta(hours=5), "payload": {"state": "waiting", "copy_version": "LEG-1"}},
            {"item_id": "ap2", "kind": "send_approval", "status": "open", "account_id": "y9", "contact_id": "c_y9",
             "created_at": now - timedelta(hours=2), "payload": {"state": "editing"}},
            {"item_id": "ap3", "kind": "send_approval", "status": "sending", "account_id": "y10", "contact_id": "c_y10",
             "created_at": now - timedelta(hours=1), "payload": {}},
            {"item_id": "ap4", "kind": "send_approval", "status": "handled", "account_id": "y11", "contact_id": "c_y11",
             "created_at": now - timedelta(days=2), "handled_at": now - timedelta(days=2), "payload": {}},
        ])
        decisions = ["approved"] * 3 + ["approved_edited", "contact_rejected", "company_rejected", "expired"]
        ctx.store.insert("events", [ev(50 + i, "send_approval", approval=a, approved_by="U_HARRY", account_id=f"y{i}",
                                       contact_id=f"c_y{i}") for i, a in enumerate(decisions)]
                         + [ev(60, "send_approval", approval="approved", at=MON - timedelta(days=1))])  # Sunday
    if auto_send is not None:
        with_auto_send(ctx, auto_send)
    return ctx, t


def section(lines, title):
    """The lines of the section whose title starts with `title`, title first, up to the blank line."""
    i = next(n for n, line in enumerate(lines) if line.startswith(title))
    end = next((n for n in range(i, len(lines)) if not lines[n]), len(lines))
    return lines[i:end]


# -- the headline and the order ---------------------------------------------------------------------


def test_the_headline_and_the_sections_top_to_bottom():
    ctx, t = funnel(auto_send=False)
    out = daily_post.run(ctx)
    [post] = [r for r in t.requests if r.url.endswith("chat.postMessage")]
    lines = post.json["text"].splitlines()
    assert lines[1] == ("Yesterday: 8 sent · 4 replies (1 positive) · 1 unsubscribe · 3 companies, 2 contacts found"
                        " · 3 ready to send")
    at = [next(n for n, line in enumerate(lines) if line.startswith(s)) for s in SECTIONS]
    assert at == sorted(at)
    assert all(lines[n - 1] == "" for n in at)  # a blank line before each title
    assert (out["found_companies"], out["found_contacts"], out["ready_to_send"]) == (3, 2, 3)
    assert "Company r1" not in "\n".join(section(lines, "*Pipeline*") + section(lines, "*Found*"))  # aggregates only


# -- Approvals ---------------------------------------------------------------------------------------


def test_approvals_from_the_contract():
    ctx, _ = funnel(auto_send=False)
    lines, nums = daily_post.build(ctx)
    assert section(lines, "*Approvals*") == [
        "*Approvals* · auto_send: no, so every email waits for a ✅ in Slack",
        "  Approved 4 (1 after an edit) · contact declined 1 · company dropped 1 · expired 1 · blocked 0",
        "  Waiting now: 2 (editing 1, waiting 1); the oldest has waited 5 hours · sending 1.",
    ]
    assert (nums["approved"], nums["approved_edited"], nums["approvals_waiting"], nums["auto_send"]) == (4, 1, 2, False)
    # The waiting accounts are not ready again, and the waiting items list says what they are.
    assert "  Ready to send: 3 (Priority 1, Standard 1, Control 1); 3 more wait for a ✅" in lines
    assert any(line.startswith("Waiting for approval: 5 (") and "send approvals 2" in line for line in lines)


def test_auto_send_yes_still_reports_the_approvals_made_before_it_was_switched_on():
    ctx, _ = funnel(auto_send=True)
    lines, nums = daily_post.build(ctx)
    assert section(lines, "*Approvals*")[0] == "*Approvals* · auto_send: yes, so emails go without a ✅"
    assert nums["auto_send"] is True


def test_the_default_is_auto_send_no():
    ctx, _ = funnel()
    lines, _ = daily_post.build(ctx)
    assert lines[section_index(lines, "*Approvals*")] == "*Approvals* · auto_send: no, so every email waits for a ✅ in Slack"


def section_index(lines, title):
    return next(n for n, line in enumerate(lines) if line.startswith(title))


def test_no_approvals_section_with_auto_send_on_and_no_approvals():
    ctx, _ = funnel(auto_send=True, approvals=False)
    lines, nums = daily_post.build(ctx)
    assert not any(line.startswith("*Approvals*") for line in lines)
    assert "approved" not in nums and "auto_send" not in nums
    assert not any("❌" in line for line in lines)


# -- Found and Pipeline --------------------------------------------------------------------------------


def test_found_counts_the_period():
    ctx, _ = funnel()
    lines, nums = daily_post.build(ctx)
    assert section(lines, "*Found*") == [
        "*Found* · Yesterday, Mon 26 Oct",
        "  Companies: 3 new (apollo 2, named 1); tiers now: Priority 1, Control 1, not scored yet 1",
        "  Verified: 4",
        "  Contacts: 2 new (apollo 1, clay 1; Founder or executive 1, People leader 1)",
        "  No suitable contact: 2 accounts (company size unknown 1, no sendable email among the top candidates 1)",
    ]
    assert nums["found_by_source"] == {"apollo": 2, "named": 1} and nums["verified_in_period"] == 4


def test_found_says_when_verify_did_not_run_and_when_clay_is_required():
    ctx, _ = funnel(approvals=False)
    ctx.store.delete("heartbeats", {"job": "verify_accounts"})
    lines, _ = daily_post.build(ctx)
    assert "  Verified: 0 (verify_accounts did not run)" in lines
    ctx.settings = dataclasses.replace(ctx.settings, general=dataclasses.replace(ctx.settings.general,
                                                                                  clay_verification="required"))
    lines, _ = daily_post.build(ctx)
    assert "  Verified: 0 (clay_verification is required, so accounts wait for Clay)" in lines


def test_pipeline_counts_now():
    ctx, _ = funnel()
    lines, nums = daily_post.build(ctx)
    pace = nums["supply_pace"]
    assert pace == min(11, 38)  # the smaller of sending capacity and the weekly target (limits.py)
    assert section(lines, "*Pipeline*") == [
        "*Pipeline* · now",
        "  Ready to send: 3 (Priority 1, Standard 1, Control 1); 3 more wait for a ✅",
        "    By group: Legal Teams 2, no group 1",
        f"  Supply: {round(3 / pace, 1)} send days at {pace} a day",
        "  Waiting for a contact: 2 (1 where nobody suitable was found; searched again after 14 days)",
        "  Waiting for verification: 5 (1 with doubtful Apollo facts, for the hand-check)",
        "  In sequence: 1 account with follow-ups to come · finished 1 · stopped 1 (reply, bounce or unsubscribe)",
    ]
    assert (nums["ready_by_tier"], nums["waiting_for_contact"], nums["waiting_for_verification"]) == (
        {"Priority": 1, "Standard": 1, "Control": 1}, 2, 5)
    assert (nums["in_sequence"], nums["finished"], nums["stopped"]) == (1, 1, 1)
    # Today's number stays the enrol job's own (limits.py, from its candidates, r4 among them).
    assert nums["ready_accounts"] == 4


def test_pipeline_by_the_focus_tab_s_groups():
    ctx, _ = funnel()
    ctx.settings = dataclasses.replace(ctx.settings, focus=(Focus("Legal Teams", 0.4), Focus("Retail", 0.2)))
    lines, _ = daily_post.build(ctx)
    assert "    Focus: Legal Teams 2, Retail 0, other 1" in lines


def test_monday_covers_friday_to_sunday():
    ctx, _ = world(now=MON_9)
    fri, sat, thu = datetime(2026, 10, 23, 15, 0, tzinfo=UTC), datetime(2026, 10, 24, 15, 0, tzinfo=UTC), \
        datetime(2026, 10, 22, 15, 0, tzinfo=UTC)
    ctx.store.insert("events", [ev(70, "sent", mailbox=HANNAH, step=1, account_id="f1", contact_id="kf", at=fri),
                                ev(71, "unsubscribed", contact_id="kf", account_id="f1", at=sat),
                                ev(72, "sent", mailbox=HANNAH, step=1, account_id="t1", contact_id="kt", at=thu)])
    ctx.store.insert("accounts", [acct("sat", source="apollo", status="new", first_seen=sat)])
    ctx.store.insert("contacts", [person("c_sat", "sat", created_at=sat)])
    lines, nums = daily_post.build(ctx)
    # Friday's and Sunday's sends (world's Monday sends are later today); Thursday's is not in it.
    assert lines[1] == ("Since Fri 23 Oct: 2 sent · 0 replies (0 positive) · 1 unsubscribe · 1 company, 1 contact found"
                        " · 0 ready to send")
    assert "*Found* · Since Fri 23 Oct" in lines and "*Sent and outcomes* · Since Fri 23 Oct (UK)" in lines
    assert nums["found_companies"] == 1 and nums["found_contacts"] == 1


# -- To improve ------------------------------------------------------------------------------------------


DECISIONS = (
    # (approval, industry_group, role, opener_source, copy_version)
    [("contact_rejected", "Legal Teams", "People leader", "New People leader / opener_self", "LEG-PL-1"),
     ("contact_rejected", "Legal Teams", "People leader", "opener_generic_people", "LEG-PL-1"),
     ("company_rejected", "Technology & Startups", "Founder or executive", "New People leader / opener_self", "G-1")]
    + [("approved_edited", "Legal Teams", "People leader", "opener_generic_people", "LEG-PL-1")] * 3
    + [("approved", "Legal Teams", "People leader", "opener_generic_people", "LEG-PL-1")] * 4
    + [("approved", "Technology & Startups", "Founder or executive", "opener_generic_founder", "G-1")] * 10
)


def learning(opener=52, holdout=50, clay=10, decisions=DECISIONS):
    """world() plus 90 days of step-1 sends by opener arm and email source, replies, bounces and approval decisions.

    The last two opener contacts bounced, so `opener` - 2 were emailed."""
    ctx, t = world()
    sent_at = TUE_9 - timedelta(days=20)
    contacts, events = [], []
    n = 100
    groups = [("o", opener, {"opener_arm": "opener", "angle": "Hiring"}),
              ("h", holdout, {"opener_arm": "holdout", "angle": "General"}),
              ("c", clay, {"opener_arm": "none", "email_source": "clay"})]
    for prefix, count, fields in groups:
        for i in range(count):
            cid = f"{prefix}{i}"
            contacts.append(person(cid, f"a{cid}", copy_version="G-1", **fields))
            events.append(ev(n, "sent", mailbox=HANNAH, step=1, account_id=f"a{cid}", contact_id=cid, at=sent_at))
            n += 1
    for cid in (f"o{opener - 1}", f"o{opener - 2}", "c0"):
        events.append(ev(n, "bounced", contact_id=cid, step=1, at=sent_at + timedelta(minutes=5)))
        n += 1
    for cid, cls in (("o0", "positive"), ("o1", "referral"), ("o2", "objection"), ("o3", "objection"),
                     ("h0", "negative"), ("h1", "out_of_office")):
        events.append(ev(n, "replied", contact_id=cid, account_id=f"a{cid}", reply_class=cls,
                         at=sent_at + timedelta(days=1)))
        n += 1
    items = []
    for i, (approval, group, role, opener_source, copy_version) in enumerate(decisions):
        items.append({"item_id": f"p{i}", "kind": "send_approval", "status": "handled", "account_id": f"ap{i}",
                      "contact_id": f"cp{i}", "created_at": TUE_9 - timedelta(days=6),
                      "payload": {"industry_group": group, "role": role, "opener_source": opener_source,
                                  "copy_version": copy_version, "edited": approval == "approved_edited"}})
        events.append(ev(n, "send_approval", approval=approval, approved_by="U_HARRY", account_id=f"ap{i}",
                         contact_id=f"cp{i}", at=TUE_9 - timedelta(days=5)))
        n += 1
    ctx.store.insert("contacts", contacts)
    ctx.store.insert("events", events)
    ctx.store.insert("hitl_items", items)
    return ctx, t


def test_to_improve_past_its_thresholds():
    ctx, _ = learning()
    lines, nums = daily_post.build(ctx)
    assert section(lines, "*To improve*") == [
        "*To improve* · last 90 days",
        "  ❌ 3 of 20 send approvals declined, most in Legal Teams 2 of 9, People leader 2 of 9, "
        "opener New People leader / opener_self 2 of 2: check targeting and scoring there.",
        "  Edited before approval: LEG-PL-1 3 of 7: fix the template on the Copy tab.",
        "  Replies, opener vs holdout: 4 of 50 (8.0%) vs 1 of 50 (2.0%); positive 2 vs 0. Counts, not conclusions.",
        "  Bounces by email source: apollo 2 of 102 (2.0%), clay 10 emailed, too few; "
        "the kill rule pauses a source over 3%.",
        "  Replies by angle: General 1 of 50, Hiring 4 of 50.",
    ]
    assert nums["improve_lines"] == 5


def test_to_improve_below_its_thresholds_says_what_it_needs_once():
    ctx, _ = learning(opener=12, holdout=5, clay=0, decisions=DECISIONS[:5])
    lines, _ = daily_post.build(ctx)
    assert section(lines, "*To improve*") == [
        "*To improve* · last 90 days",
        "  Too early to compare: ❌ by segment, 5 of 20 decisions; opener vs holdout, 10 and 5 of 50 emailed each; "
        "angles and copy versions, 50 emailed each, two at least; bounces by source, 17 of 50 emailed.",
    ]


def test_enough_decisions_but_too_few_declines_name_no_segment():
    few = [d for d in DECISIONS if d[0] != "company_rejected"]
    few += [("approved", "Technology & Startups", "Founder or executive", "opener_generic_founder", "G-1")]
    ctx, _ = learning(decisions=few)
    lines, _ = daily_post.build(ctx)
    body = section(lines, "*To improve*")
    assert not any("❌" in line for line in body)  # 2 of 20 declined: nothing to say, and nothing missing
    assert body[1] == "  Edited before approval: LEG-PL-1 3 of 7: fix the template on the Copy tab."


def test_to_improve_before_any_send():
    ctx, _ = funnel(approvals=False)
    ctx.store.delete("events", {"type": "sent"})
    lines, _ = daily_post.build(ctx)
    body = section(lines, "*To improve*")
    assert body[-1] == "  Too early to compare: no email sent in the last 90 days."
    assert body[1].startswith("  Data gaps: no size band at 1 of 6 verified accounts (16.7%) and 2 of 5 waiting for "
                              "verification; nobody suitable at 1 verified account, mostly company size unknown (1).")


def test_to_improve_keeps_to_six_lines_with_the_data_gaps_and_tier_mix():
    ctx, _ = learning()
    month = TUE_9 - timedelta(days=2)
    ctx.store.insert("accounts", [acct(f"q{i}", tier="Control", status="queued", size_band="20-49", first_seen=month)
                                  for i in range(20)] + [acct("v0", tier="Control", status="verified")])
    lines, _ = daily_post.build(ctx)
    body = section(lines, "*To improve*")[1:]
    assert len(body) == daily_report.MAX_IMPROVE
    assert body[4] == ("  Data gaps: no size band at 1 of 1 verified accounts (100.0%) and 0 of 20 waiting for "
                       "verification. The page reader's coverage is under Sources.")
    assert body[5] == ("  Tier mix of this month's queue (21 accounts): Priority 0%, Standard 0%, Control 100%; each should "
                       "be 5% to 40%: review the thresholds and signal weights.")
    assert not any(line.startswith("  Replies by angle") for line in body)  # last in line, so the first left out


def test_the_reason_drops_per_account_detail():
    assert daily_report.reason("no sendable email among the top 2 (email status unverified (2))") == (
        "no sendable email among the top candidates")
    assert daily_report.reason("company size unknown") == "company size unknown"
    assert daily_report.reason("") == "no reason recorded"
