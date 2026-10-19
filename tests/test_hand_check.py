"""The weekly hand-check (SPEC 11; Harry's D5, 19 Oct 2026; enrol/hand_check.py): the draw from the accounts the
coming send days would take, the hitl item, the Monday notice, what enrol holds (hand_check_gate), the reminder, the
readout's line, and `us-outbound handcheck show|approve` without the tab."""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta

import pytest

from tests.fakes import FakeTransport, make_context
from tests.test_registry import slack_routes
from tests.test_render import AGENCIES, TECH, make_settings
from tests.test_scoring import EAP_NAMED
from us_outbound import suppression
from us_outbound.contacts import pick
from us_outbound.enrol import enrol, hand_check
from us_outbound.settings.model import State

NOW = datetime(2026, 10, 26, 7, 0, tzinfo=UTC)  # Monday 07:00 UK (GMT+0 after 25 Oct), 03:00 ET: ISO week 2026-W44
WEEK = "2026-W44"
EAP_LINE = "I saw {company} already offers its team an employee assistance program."
# auto_send = yes and hand_check_gate = yes (both as on the live sheet before D5 is set there): the gate is the
# whole day's, as SPEC 11 was built. The D5 tests switch hand_check_gate to no.
SETTINGS = dataclasses.replace(make_settings(live_sending=True, approver_slack_ids=("U_HARRY",), auto_send=True),
                               signals=(dataclasses.replace(EAP_NAMED, role_openers={"People leader": EAP_LINE}),),
                               states=(State("IL", True), State("GA", True)))


def general(settings=SETTINGS, **kw):
    return dataclasses.replace(settings, general=dataclasses.replace(settings.general, **kw))


def acct(i: int, group: str = AGENCIES, status: str = "verified", **kw) -> dict:
    industry = "Advertising agencies" if group == AGENCIES else "Fintech"
    return {"account_id": f"{group[:3].lower()}-{i:03d}", "domain": f"{group[:3].lower()}{i}.com",
            "clean_name": f"{group[:3]} Co {i}", "hq_city": "Chicago", "hq_state": "IL", "employees": 40,
            "size_band": "20-49", "industry": industry, "industry_group": group, "tier": "Priority",
            "score": 1000 - i,  # queue order (queue.order_key: score first, all else equal) is i's order
            "angle": "Upgrade the EAP", "status": status, **kw}


def person(a: dict, **kw) -> dict:
    return {"contact_id": f"k-{a['account_id']}", "account_id": a["account_id"], "role": "People leader",
            "title": "Head of People", "first_name": "Jane", "email": f"jane@{a['domain']}", "email_status": "verified",
            "person_state": "IL", **kw}


def world(n: int = 14, *, contacts: int | None = None, live: bool = True, now: datetime = NOW, settings=SETTINGS):
    """n verified Agencies accounts in queue order mar-000, mar-001, ...; the first `contacts` of them (all, by
    default) with a sendable contact, so they are ready (pick.ready_accounts). Plus four enrol never takes."""
    t = slack_routes(FakeTransport())
    ctx = make_context(settings, live=live, transport=t, now=now)
    accounts = [acct(i) for i in range(n)]
    accounts += [
        acct(900, tier="Held"),  # enrol never takes Held
        acct(901, status="enrolled"),
        acct(902, industry="Staffing agencies", industry_group="Professional Services"),  # switched off
        acct(903, TECH, domain="suppressed.com"),
    ]
    ctx.store.insert("accounts", accounts)
    ctx.store.insert("contacts", [person(a) for a in accounts[: n if contacts is None else contacts]]
                     + [person(a) for a in accounts[n:]])
    suppression.add(ctx.store, domain="suppressed.com", reason="kill_rule", source="test", now=now)
    return ctx, t


def posts(t):
    return [r for r in t.requests if r.url.endswith("chat.postMessage")]


def ids(rows):
    return [r["account_id"] for r in rows]


# -- the draw: from the accounts the coming send days would take (B5) ----------------------------------------------


def test_the_sample_is_drawn_after_today_s_number_so_monday_s_own_enrolment_is_never_held():
    """90 ready, today's number 30, weekly_enrol_cap 150: the pool is positions 31 to 30 + 150 = 180, of which 90 exist,
    so positions 31-90 (mar-030 to mar-089), 60 accounts. The 10 drawn come from those; none of the first 30, which
    enrol takes today, is ever held."""
    ctx, _ = world(90)
    ready = pick.ready_accounts(ctx)
    assert ids(ready) == [f"mar-{i:03d}" for i in range(90)]  # the four enrol never takes are not ready
    pool = hand_check.pool(ctx, 30)
    assert ids(pool) == [f"mar-{i:03d}" for i in range(30, 90)]
    sample = hand_check.draw(ctx, WEEK, number=30)
    assert len(sample) == 10 and set(ids(sample)) <= set(ids(pool))
    assert not set(ids(sample)) & {f"mar-{i:03d}" for i in range(30)}
    # The cap bounds the far end: weekly_enrol_cap 40 gives positions 31 to 70, mar-030 to mar-069.
    ctx.settings = general(weekly_enrol_cap=40)
    assert ids(hand_check.pool(ctx, 30)) == [f"mar-{i:03d}" for i in range(30, 70)]


def test_every_sampled_account_has_a_contact_and_the_size_is_the_sheet_s():
    """300 verified accounts, 150 of them with a sendable contact: only those 150 are ready, so every sampled one has
    a contact (before, the draw took queued and contactless accounts too). hand_check_sample 20 gives 20, 10 gives
    10, and a pool of 6 gives 6."""
    ctx, _ = world(300, contacts=150)
    with_contact = {f"mar-{i:03d}" for i in range(150)}
    ctx.settings = general(hand_check_sample=20)
    sample = hand_check.draw(ctx, WEEK, number=0)
    assert len(sample) == 20 and set(ids(sample)) <= with_contact
    ctx.settings = general(hand_check_sample=10)
    assert len(hand_check.draw(ctx, WEEK, number=0)) == 10
    small, _ = world(6)
    assert sorted(ids(hand_check.draw(small, WEEK, number=0))) == [f"mar-{i:03d}" for i in range(6)]


def test_the_draw_is_the_same_all_week_and_another_week_draws_again():
    ctx, _ = world(40)
    first = ids(hand_check.draw(ctx, WEEK, number=0))
    assert ids(hand_check.draw(ctx, WEEK, number=0)) == first
    assert ids(hand_check.draw(ctx, "2026-W45", number=0)) != first


def test_today_s_number_is_enrol_s_with_the_campaigns_not_read():
    from us_outbound.enrol import today

    ctx, t = world(20)
    assert hand_check.today_number(ctx) == today.read(ctx, campaigns=False).limits.number
    assert not [r for r in t.requests if "instantly" in r.url]  # no Instantly call


# -- the job ----------------------------------------------------------------------------------------------


@pytest.fixture
def number(monkeypatch):
    """Today's number for the draw (enrol/today.py, tested above): 2, so the pool starts at position 3."""
    monkeypatch.setattr(hand_check, "today_number", lambda ctx, **kw: 2)


def test_the_job_records_this_week_s_sample_writes_the_tab_and_posts_the_notice(number):
    """14 ready, today's number 2: the pool is positions 3-14 (12 accounts), the sample hand_check_sample = 10 of them."""
    from tests.test_hand_check_tab import Sheet, sheet_rows

    ctx, t = world()
    ctx.clients.sheets = Sheet(ctx.guard, {})
    out = hand_check.run(ctx)
    assert (out["status"], out["iso_week"], out["accounts"]) == ("recorded", WEEK, 10)
    assert (out["number_today"], out["positions"], out["pool"], out["sample_size"]) == (2, [3, 152], 12, 10)
    item = ctx.store.get("hitl_items", item_id=f"hand_check-{WEEK}")
    assert item["kind"] == "hand_check" and item["status"] == "open"
    p = item["payload"]
    assert p["drawn_at"] == NOW.isoformat() and p["gate_version"].endswith(":legacy") and p["posted"] is True
    sampled = {a["account_id"] for a in p["accounts"]}
    assert len(sampled) == 10 and not sampled & {"mar-000", "mar-001"}  # today's two are never held
    # The tab: one row per company, the new columns filled from the facts.
    rows = sheet_rows(ctx)
    assert len(rows) == 10 and {r["check"] for r in rows} == {"random sample"}
    first = next(r for r in rows if r["domain"] == p["accounts"][0]["domain"])
    assert (first["week"], first["group"], first["state"], first["contact"], first["title"]) == (
        WEEK, AGENCIES, "IL", "People leader", "Head of People")
    assert first["gate_version"] == p["gate_version"] and first["decision"] == ""
    # The Monday notice: with the whole-day gate (hand_check_gate yes), enrol waits for it.
    [post] = posts(t)
    assert post.json["channel"] == "C_ALERT"
    lines = post.json["text"].splitlines()
    assert lines[0] == ("<@U_HARRY> :warning: *Weekly hand-check: 10 companies to look at before this week's new "
                        "emails go out*")
    assert lines[-1] == "_Why it matters: until every company picked at random has a decision, no new leads go to " \
                        "Instantly this week._"
    assert item["slack_ts"] == "1.1"
    assert enrol.hand_check(ctx, ctx.now_et().date()) == (f"this week's hand-check ({WEEK}) is not approved yet",
                                                          frozenset())
    again = hand_check.run(ctx)
    assert again["status"] == "posted already" and len(posts(t)) == 1


def test_with_the_gate_off_the_notice_says_only_these_companies_wait(number):
    ctx, t = world(settings=general(hand_check_gate=False))
    hand_check.run(ctx)
    text = posts(t)[0].json["text"]
    assert text.splitlines()[0] == "<@U_HARRY> :warning: *Weekly hand-check: 10 companies to look at*"
    assert text.endswith("_Why it matters: only these companies wait for you, for 7 days at most; everyone else is "
                         "emailed as usual._")


def test_a_dry_run_records_the_item_and_the_live_run_posts_it(number):
    ctx, t = world(live=False)
    assert hand_check.run(ctx)["status"] == "recorded"
    assert [p.json["channel"] for p in posts(t)] == ["C_DEV"]
    assert ctx.store.get("hitl_items", item_id=f"hand_check-{WEEK}")["payload"]["posted"] is False
    live = make_context(SETTINGS, live=True, transport=t, now=NOW + timedelta(minutes=5), store=ctx.store)
    assert hand_check.run(live)["status"] == "posted"
    assert [p.json["channel"] for p in posts(t)] == ["C_DEV", "C_ALERT"]


def test_a_supply_bound_monday_under_the_gate_is_checked_from_today_s_accounts_and_can_be_approved(monkeypatch):
    """The W1-5 review's blocking case: hand_check_gate yes and auto_send yes (the live sheet), 5 ready and today's
    number 5. The coming days' pool, positions 6 to 105, is empty; before, the draw recorded nothing, enrol said "has
    not been posted" all week, and `handcheck approve` raised. Now, as the day waits for the check anyway, the pool
    tops up from position 1: positions 1 to 5 + 100 = 105, of which the 5 exist, so all 5 are drawn
    (min(hand_check_sample 10, 5)). Enrol waits for that check as the gate always did ("not approved yet", never
    "has not been posted"); approving it lets the day go: today's number (Tuesday, 100 a week over 4 send days left =
    25) is above the 5 ready, so all 5 are enrolled."""
    monkeypatch.setattr(hand_check, "today_number", lambda ctx, **kw: 5)
    ctx, _, _ = enrol_world(gate=True, n=5)
    out = hand_check.run(ctx)
    assert (out["status"], out["positions"], out["pool"], out["accounts"]) == ("recorded", [1, 105], 5, 5)
    item = hand_check.current(ctx)
    assert sorted(hand_check.sample_ids(item)) == [f"h-{i:02d}" for i in range(5)] and item["status"] == "open"
    skipped = enrol.run(ctx)
    assert (skipped["status"], skipped["reason"]) == ("skipped", "this week's hand-check (2026-W44) is not approved yet")
    done = hand_check.approve(ctx, [], "harry")
    assert done["approved"] is True and hand_check.current(ctx)["status"] == "handled"
    went = enrol.run(ctx)
    assert (went["status"], went["enrolled"]) == ("ok", 5) and enrolled(ctx) == {f"h-{i:02d}" for i in range(5)}


def test_a_supply_bound_monday_with_the_gate_off_records_nothing_to_check_and_holds_nobody(monkeypatch):
    """hand_check_gate no, 5 ready, today's number 5: no top-up (it would hold today's own accounts), so the pool,
    positions 6 to 105, is empty and no company waits for doubtful facts. The week is recorded handled, payload
    nothing_to_check, by the draw: enrol holds nobody and enrols all 5 (25 > 5, as above); `handcheck approve`
    returns rather than raising; the readout says 0 picked."""
    monkeypatch.setattr(hand_check, "today_number", lambda ctx, **kw: 5)
    ctx, _, _ = enrol_world(gate=False, n=5)
    out = hand_check.run(ctx)
    assert out["status"].startswith("nothing to check") and (out["positions"], out["pool"]) == ([6, 105], 0)
    item = ctx.store.get("hitl_items", item_id="hand_check-2026-W44")
    assert (item["status"], item["handled_by"], item["payload"]["nothing_to_check"]) == ("handled", "hand_check_draw", True)
    assert item["payload"]["accounts"] == [] and hand_check.held(ctx) == frozenset()
    went = enrol.run(ctx)
    assert (went["status"], went["enrolled"]) == ("ok", 5) and enrol.HAND_CHECK_SKIP not in went["skipped"]
    assert hand_check.approve(ctx, [], "harry")["approved"] is True
    monday = datetime(2026, 10, 26, 0, 0, tzinfo=UTC)
    lines, nums = hand_check.readout_lines(ctx, monday)
    assert lines[-1] == "  Week 44: 0 picked: no company was ready to email beyond that day's number."
    assert (nums["drawn"], nums["nothing_to_check"]) == (0, True)


def test_a_week_with_no_company_ready_at_all_lets_the_gated_day_go(monkeypatch):
    """hand_check_gate yes, nothing ready (the four companies world() adds are never taken) and none held for doubtful
    facts: even topped up, the pool is empty. A live draw records the week handled, so enrol.hand_check has no reason
    to wait (before: "has not been posted", all week). A dry run records nothing: it never opens the gate."""
    monkeypatch.setattr(hand_check, "today_number", lambda ctx, **kw: 0)
    dry, _ = world(0, live=False)
    assert hand_check.run(dry)["status"].startswith("nothing to check") and dry.store.select("hitl_items") == []
    assert enrol.hand_check(dry, dry.now_et().date())[0] == f"this week's hand-check ({WEEK}) has not been posted"
    ctx, _ = world(0)
    out = hand_check.run(ctx)
    assert out["status"].startswith("nothing to check") and out["item_id"] == f"hand_check-{WEEK}"
    assert enrol.hand_check(ctx, ctx.now_et().date()) == (None, frozenset())
    assert hand_check.run(ctx)["status"].startswith("nothing to check")  # drawn again: still nothing, nothing written
    assert len(ctx.store.select("hitl_items")) == 1


def test_a_week_recorded_with_nothing_to_check_is_drawn_again_when_the_queue_has_grown(monkeypatch):
    """hand_check_gate no. Monday 05:45, today's number 14 of 14 ready: nothing to check, recorded handled. Run again
    later (on demand) with today's number 2: positions 3 to 152, 12 accounts, so 10 are drawn and the week's item is
    open with them, keeping the pulls the empty week had."""
    number = {"n": 14}
    monkeypatch.setattr(hand_check, "today_number", lambda ctx, **kw: number["n"])
    ctx, _ = world(settings=general(hand_check_gate=False))
    assert hand_check.run(ctx)["status"].startswith("nothing to check")
    ctx.store.update("hitl_items", {"item_id": f"hand_check-{WEEK}"},
                     {"payload": {**hand_check.current(ctx)["payload"], "pulled_account_ids": ["mar-900"]}})
    number["n"] = 2
    out = hand_check.run(ctx)
    assert (out["status"], out["accounts"], out["positions"]) == ("recorded", 10, [3, 152])
    item = hand_check.current(ctx)
    assert item["status"] == "open" and not item["payload"].get("nothing_to_check")
    assert item["payload"]["pulled_account_ids"] == ["mar-900"]
    assert len(hand_check.held(ctx)) == 10


def test_a_failed_number_reaches_heartbeat_check(monkeypatch):
    """today.read fails: the draw goes on at the weekly pace (150 / 5 send days = 30) and its summary carries
    number_error, a text under a key ending _error, which heartbeat_check reports (ops/job_errors)."""
    from us_outbound.enrol import today
    from us_outbound.ops import job_errors

    def broken(*a, **k):
        raise RuntimeError("database gone away")

    monkeypatch.setattr(today, "read", broken)
    ctx, _ = world(3)
    out = hand_check.run(ctx)
    assert out["number_today"] == 30
    assert out["number_error"] == ("today's number could not be worked out (RuntimeError: database gone away); the "
                                   "draw used 30, the weekly pace")
    run = {"job": hand_check.JOB, "status": "ok", "started_at": NOW, "finished_at": NOW, "detail": out}
    assert [f.kind for f in job_errors.findings(hand_check.JOB, run)] == ["number_error"]


def test_the_facts_harry_checks():
    ctx, _ = world()
    ctx.store.insert("signal_events", [{
        "event_id": "f0", "account_id": "mar-000", "source": "clay_careers", "fact": "benefit",
        "value": "Employee assistance program", "quote": "Every employee gets our employee assistance program.",
        "source_url": "https://mar0.com/careers", "observed_at": NOW - timedelta(days=3)}])
    ctx.settings = general(opener_holdout_share=0.0)  # every contact gets its opener (the holdout is tested below)
    domains, hashes = enrol.suppressed(ctx)
    f = hand_check.facts(ctx, ctx.store.get("accounts", account_id="mar-000"), domains, hashes)
    assert (f["clean_name"], f["hq_state"], f["size_band"], f["industry_group"]) == ("Mar Co 0", "IL", "20-49", AGENCIES)
    assert f["contact"] == {"role": "People leader", "title": "Head of People", "note": ""}
    assert f["opener"] == "I saw Mar Co 0 already offers its team an employee assistance program."
    assert (f["opener_arm"], f["opener_source"]) == ("opener", "EAP named / opener_people")
    ctx.settings = general(opener_holdout_share=1.0)
    held = hand_check.facts(ctx, ctx.store.get("accounts", account_id="mar-000"), domains, hashes)
    assert hand_check._opener(held) == ("none (held out; would be: I saw Mar Co 0 already offers its team an "
                                        "employee assistance program.)")


# -- what enrol holds: hand_check_gate (D5) --------------------------------------------------------------------


def judge(ctx, week, account_id, decision, **value):
    """A judgement fact, as hand_check_tab.apply writes one for an applied row."""
    ctx.store.upsert("signal_events", [{
        "event_id": hand_check.event_id(week, account_id), "account_id": account_id, "source": hand_check.SOURCE,
        "fact": hand_check.FACT, "value": {"week": week, "check": hand_check.SAMPLE, "decision": decision, **value},
        "quote": "", "source_url": "", "observed_at": ctx.now}])


def drawn(ctx, week, account_ids, at, **payload):
    ctx.store.upsert("hitl_items", [{
        "item_id": f"hand_check-{week}", "kind": "hand_check", "status": "open", "created_at": at,
        "payload": {"iso_week": week, "drawn_at": at.isoformat(), "pulled_account_ids": [],
                    "accounts": [{"account_id": a, "domain": f"{a.replace('-', '')}.com"} for a in account_ids],
                    **payload}}])


def enrol_world(*, gate: bool, n: int = 68):
    """n ready accounts h-00 to h-(n-1) in queue order (68 by default), weekly_enrol_cap 100, auto_send yes, live;
    Tuesday 27 Oct 07:00 ET (W44). Slack answers, so the draw can post. Their HQ state, IL, is active, so a company
    the tab puts back (rescored) stays in a queue tier."""
    from tests.test_enrol import NOW as TUESDAY, make
    from tests.test_render import account, contact

    accounts = [account(account_id=f"h-{i:02d}", domain=f"h{i:02d}.com", clean_name=f"H Co {i}", score=100 - i)
                for i in range(n)]
    contacts = [contact(contact_id=f"c-{i:02d}", account_id=f"h-{i:02d}", email=f"jane@h{i:02d}.com")
                for i in range(n)]
    s = dataclasses.replace(make_settings(live_sending=True, weekly_enrol_cap=100, hand_check_gate=gate),
                            states=(State("IL", True),))
    ctx, t = make(live=True, settings=s, accounts=accounts, contacts=contacts, hand_check=None,
                  transport=slack_routes(FakeTransport()))
    return ctx, t, TUESDAY


def enrolled(ctx):
    return {c["account_id"] for c in ctx.store.select("contacts") if c.get("instantly_lead_id")}


def test_with_the_gate_off_enrol_holds_only_the_unjudged_sample_and_sends_the_rest():
    """hand_check_gate = no. The sample is the top 10 of the queue (h-00 to h-09), drawn yesterday: 3 judged (h-00 and
    h-01 fine, h-02 left out: disqualified and pulled), 7 unjudged (h-03 to h-09), held. Ready: 68 - 1 left out - 7
    held = 60. Today's number: Tuesday, 4 send days left of 100 a week, 0 enrolled yet: 100 / 4 = 25, below the
    senders' room and the 60 ready, so 25 are enrolled: h-00, h-01 and h-10 to h-32 in queue order, none of the 8."""
    ctx, _, tuesday = enrol_world(gate=False)
    sample = [f"h-{i:02d}" for i in range(10)]
    drawn(ctx, "2026-W44", sample, tuesday - timedelta(days=1), pulled_account_ids=["h-02"])
    judge(ctx, "2026-W44", "h-00", "fine")
    judge(ctx, "2026-W44", "h-01", "fine")
    judge(ctx, "2026-W44", "h-02", "leave out", reason="media_publisher")
    ctx.store.update("accounts", {"account_id": "h-02"}, {"status": "disqualified", "status_reason": "hand_check"})
    out = enrol.run(ctx)
    assert out["status"] == "ok" and out["number"] == 25 and out["number_terms"]["binding"] == "weekly_target"
    assert out["candidates"] == 60 and out["enrolled"] == 25
    assert out["skipped"][enrol.HAND_CHECK_SKIP] == 7  # h-02 is not verified, so it is not a candidate at all
    assert enrolled(ctx) == {"h-00", "h-01", *(f"h-{i:02d}" for i in range(10, 33))}
    assert not enrolled(ctx) & {f"h-{i:02d}" for i in range(2, 10)}


def test_with_the_gate_off_a_week_with_no_draw_holds_nobody():
    """No hand-check recorded this week (the draw failed, or found nothing): with the gate on enrol would wait all
    week ("has not been posted"); with it off nothing waits. 68 ready, 25 a day as above."""
    ctx, _, _ = enrol_world(gate=False)
    out = enrol.run(ctx)
    assert (out["status"], out["enrolled"]) == ("ok", 25) and enrol.HAND_CHECK_SKIP not in out["skipped"]
    on, _, _ = enrol_world(gate=True)
    assert enrol.run(on)["reason"] == "this week's hand-check (2026-W44) has not been posted"


def test_today_s_number_falls_back_to_the_weekly_pace_when_it_cannot_be_read(monkeypatch):
    """A read that fails: weekly_enrol_cap 150 over 5 send days is 30, the most Monday could take at that pace."""
    from us_outbound.enrol import today

    def broken(*a, **k):
        raise RuntimeError("database gone away")

    monkeypatch.setattr(today, "read", broken)
    ctx, _ = world(3)
    assert hand_check.today_number(ctx) == 30


def test_with_the_gate_on_enrol_waits_for_the_whole_check_as_before():
    ctx, _, tuesday = enrol_world(gate=True)
    drawn(ctx, "2026-W44", [f"h-{i:02d}" for i in range(10)], tuesday - timedelta(days=1))
    out = enrol.run(ctx)
    assert out["status"] == "skipped" and out["reason"] == "this week's hand-check (2026-W44) is not approved yet"
    assert enrolled(ctx) == set()


def test_unjudged_companies_are_released_seven_days_after_the_draw():
    """Drawn Monday 19 Oct 05:45 UK (W43), 2 of 10 never judged (h-08, h-09). Tuesday 27 Oct 11:00 UK is 8 days on:
    both are candidates again (not held) and the readout of W43 lists them as unjudged, sent. 6 days 23 hours after
    a draw they are still held."""
    ctx, _, tuesday = enrol_world(gate=False)
    sample = [f"h-{i:02d}" for i in range(10)]
    monday = datetime(2026, 10, 19, 4, 45, tzinfo=UTC)  # 05:45 BST
    drawn(ctx, "2026-W43", sample, monday)
    for a in sample[:8]:
        judge(ctx, "2026-W43", a, "fine")
    assert tuesday - monday == timedelta(days=8, hours=6, minutes=15)
    assert hand_check.held(ctx) == frozenset()
    why, out = enrol.hand_check(ctx, tuesday.astimezone(UTC).date())
    assert why is None and out == frozenset()
    lines, nums = hand_check.readout_lines(ctx, monday)
    assert lines[-1] == "  Week 43: 10 picked; 8 judged: 8 fine, 0 left out, 0 wrong industry; 2 unjudged, sent."
    assert (nums["unjudged"], nums["released"]) == (2, True)
    ctx.now = monday + timedelta(days=6, hours=23)
    assert hand_check.held(ctx) == frozenset({"h-08", "h-09"})
    assert hand_check.readout_lines(ctx, monday)[0][-1].endswith("2 unjudged, still held.")
    ctx.now = monday + timedelta(days=7)
    assert hand_check.held(ctx) == frozenset()


def test_a_week_closed_by_hand_holds_nothing():
    ctx, _, tuesday = enrol_world(gate=False)
    drawn(ctx, "2026-W44", ["h-00", "h-01"], tuesday - timedelta(hours=3))
    assert hand_check.held(ctx) == frozenset({"h-00", "h-01"})
    ctx.store.update("hitl_items", {"item_id": "hand_check-2026-W44"}, {"status": "handled"})
    assert hand_check.held(ctx) == frozenset()


def test_the_readout_counts_each_decision_and_the_reasons():
    ctx, _ = world()
    drawn(ctx, "2026-W43", [f"mar-{i:03d}" for i in range(5)], datetime(2026, 10, 19, 4, 45, tzinfo=UTC))
    judge(ctx, "2026-W43", "mar-000", "fine")
    judge(ctx, "2026-W43", "mar-001", "fine")
    judge(ctx, "2026-W43", "mar-002", "leave out", reason="media_publisher")
    judge(ctx, "2026-W43", "mar-003", "leave out", reason="media_publisher")
    judge(ctx, "2026-W43", "mar-004", "wrong industry", right_label="Fintech")
    lines, nums = hand_check.readout_lines(ctx, datetime(2026, 10, 19, 0, 0, tzinfo=UTC))
    assert lines[-2:] == ["  Week 43: 5 picked; 5 judged: 2 fine, 2 left out, 1 wrong industry.",
                          "  Left out because: media_publisher 2."]
    assert nums["decisions"] == {"fine": 2, "leave out": 2, "wrong industry": 1} and nums["unjudged"] == 0
    assert hand_check.tally(ctx, "2026-W43") == (2, 5)
    none, _ = hand_check.readout_lines(ctx, datetime(2026, 10, 12, 0, 0, tzinfo=UTC))
    assert none[-1] == "  Week 42: no hand-check was drawn."


def test_the_monday_readout_carries_the_hand_check_section():
    from us_outbound.learn import readout

    ctx, _ = world(now=datetime(2026, 10, 26, 7, 30, tzinfo=UTC))
    drawn(ctx, "2026-W43", ["mar-000", "mar-001"], datetime(2026, 10, 19, 4, 45, tzinfo=UTC))
    judge(ctx, "2026-W43", "mar-000", "fine")
    lines, nums = readout.build(ctx)
    assert "  Week 43: 2 picked; 1 judged: 1 fine, 0 left out, 0 wrong industry; 1 unjudged, sent." in lines
    assert nums["hand_check"]["judged"] == 1


# -- without the tab: the handcheck command ----------------------------------------------------------------------


def harness(monkeypatch):
    from tests.test_cli import Harness

    monkeypatch.setattr(hand_check, "today_number", lambda ctx, **kw: 2)
    h = Harness(SETTINGS)
    ctx, _ = world()
    h.store.tables = ctx.store.tables  # the same accounts and contacts
    return h


def test_handcheck_show_prints_the_sample_and_never_records_it(monkeypatch, capsys):
    h = harness(monkeypatch)
    assert h.run("handcheck", "show", "--live") == 0
    out = capsys.readouterr().out
    assert "Week 44: 10 companies picked at random from those about to be emailed (positions 3-152 of the queue)." in out
    assert " · Marketing & Creative Agencies · IL · 20-49 · People leader · " in out
    assert "Not recorded yet: `us-outbound handcheck approve --live` records this sample and approves it." in out
    assert h.store.select("hitl_items", {"kind": "hand_check"}) == []
    shown = [a["account_id"] for a in hand_check.show(h.last)[1]["accounts"]]
    assert h.run("handcheck", "approve", "--live") == 0
    [item] = h.store.select("hitl_items", {"kind": "hand_check"})
    assert item["status"] == "handled" and [a["account_id"] for a in item["payload"]["accounts"]] == shown


def test_handcheck_approve_marks_the_week_handled_with_the_pulled_accounts(monkeypatch):
    h = harness(monkeypatch)
    assert h.run("run", "hand_check_draw", "--live") == 0
    [beat] = h.beats("hand_check_draw")
    assert beat["status"] == "ok" and beat["detail"]["status"] == "recorded"
    item = h.store.select("hitl_items", {"kind": "hand_check"})[0]
    first = item["payload"]["accounts"][0]["account_id"]
    assert h.run("handcheck", "approve", "--pull", first, "mar13.com") == 0  # dry-run: nothing changes
    assert h.store.get("hitl_items", item_id=item["item_id"])["status"] == "open"
    assert h.run("handcheck", "approve", "--pull", "no-such-account", "--live") == 2
    assert h.run("handcheck", "approve", "--pull", first, "mar13.com", "--live") == 0
    done = h.store.get("hitl_items", item_id=item["item_id"])
    assert done["status"] == "handled" and done["handled_by"]
    assert set(done["payload"]["pulled_account_ids"]) == {first, "mar-013"}
    why, pulled = enrol.hand_check(h.last, h.last.now_et().date())
    assert why is None and pulled == frozenset({first, "mar-013"})
    _, skipped = enrol.candidates(h.last, pulled)
    assert skipped[enrol.HAND_CHECK_SKIP] == 2


def test_approve_with_nothing_to_check_records_the_week_handled(monkeypatch):
    """No account at all, and no draw yet: before, approve raised LookupError and a gated week could not be unblocked.
    Now it records the week as nothing to check, handled by harry, and enrol has nothing to wait for. A dry run says
    the same and writes nothing."""
    monkeypatch.setattr(hand_check, "today_number", lambda ctx, **kw: 0)
    dry = make_context(SETTINGS, live=False, transport=slack_routes(FakeTransport()), now=NOW)
    assert hand_check.approve(dry, [], "harry")["approved"] is False and dry.store.select("hitl_items") == []
    ctx = make_context(SETTINGS, live=True, transport=slack_routes(FakeTransport()), now=NOW)
    out = hand_check.approve(ctx, [], "harry")
    assert (out["approved"], out["recorded"], out["checked"]) == (True, True, 0)
    item = hand_check.current(ctx)
    assert (item["status"], item["handled_by"], item["payload"]["nothing_to_check"]) == ("handled", "harry", True)
    assert enrol.hand_check(ctx, ctx.now_et().date()) == (None, frozenset())


def test_the_confirmation_says_what_happens_next_in_plain_words():
    ctx, _ = world()
    assert hand_check.confirmation(ctx, WEEK, "harry", ["mar-001"], 0) == (
        ":white_check_mark: *Weekly hand-check done by harry* (Week 44)\nLeft out: mar-001\n"
        "New leads can go to Instantly this week.")
    ctx.settings = general(hand_check_gate=False)
    friday = make_context(ctx.settings, now=NOW + timedelta(days=4))
    assert hand_check.confirmation(friday, WEEK, "harry", [], 2).splitlines()[1:] == [
        "The 2 companies with doubtful facts are checked again at 04:30 on Monday, then cleared to email if their "
        "facts hold."]
