"""The weekly capture check (replies/capture.py; W1-2, 19 Oct 2026): Instantly's counts against our events, and the
seed inbox's canary, so a broken reply read can be told from a true zero (code-replies-crm M3, M4; RT §1.6, §4)."""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta

import pytest

from tests.test_cli import Harness
from us_outbound.ops import health, seed
from us_outbound.ops.health import run_job
from us_outbound.replies import capture

C_HANNAH, C_HARRY = "US Outbound – Hannah Spalding", "US Outbound – Harry Dryden"
HANNAH = "hannah@meetspill.org"
SEED = "harry+seed1@spill.chat"  # the 6 Oct seed test's inbox (ops/seed.py)
MON = datetime(2026, 10, 26, 6, 30, tzinfo=UTC)  # Mon 26 Oct 2026, 06:30 UTC = 07:30 UK (GMT from 25 Oct)


# -- the drift rule, hand-computed ---------------------------------------------------------------------------------


def test_the_drift_rule_on_the_plans_worked_cases():
    # Instantly 48 sent, 2 replied, 2 bounced; ours 48, 0, 2: replied drifts because theirs > 0 and ours == 0
    # (|2 - 0| = 2 alone is not more than max(2, 0.2) = 2, so the tolerance would have missed it).
    assert capture.drift({"sent": 48, "replied": 2, "bounced": 2}, {"sent": 48, "replied": 0, "bounced": 2}) == [
        {"measure": "replied", "instantly": 2, "ours": 0}]
    # Ours 47, 2, 2: |48 - 47| = 1 <= max(2, 4.8) = 4.8, so no drift.
    assert capture.drift({"sent": 48, "replied": 2, "bounced": 2}, {"sent": 47, "replied": 2, "bounced": 2}) == []
    # Theirs 100 sent, ours 85: |100 - 85| = 15 > max(2, 10) = 10, so sent drifts.
    assert capture.drift({"sent": 100}, {"sent": 85}) == [{"measure": "sent", "instantly": 100, "ours": 85}]
    # The rule's known blind spot once ours is above 0: theirs replied 3, ours 1, |3 - 1| = 2 is not more than
    # max(2, 0.3) = 2, so no drift. The canary, not the tolerance, guards this case.
    assert capture.drift({"replied": 3}, {"replied": 1}) == []
    # At the edge: |13 - 10| = 3 > max(2, 1.3) = 2, a drift; |12 - 10| = 2 is not.
    assert capture.drift({"bounced": 13}, {"bounced": 10}) == [{"measure": "bounced", "instantly": 13, "ours": 10}]
    assert capture.drift({"bounced": 12}, {"bounced": 10}) == []


def test_the_zero_rule_is_judged_on_our_own_replies_never_on_the_canarys_answer():
    """W1-2 review, round 2. With the canary's answer in Instantly's count and added to ours (canary replied 1):
    Instantly 1, ours 0 + 1 is the canary alone, no drift; Instantly 2 (the canary and a real reply), ours 0 + 1: the
    tolerance sees |1 - 2| = 1, not more than 2, and the zero rule, 2 - 1 = 1 real reply against none of ours, does."""
    assert capture.drift({"sent": 1, "replied": 1}, {"sent": 0, "replied": 0}, canary={"sent": 1, "replied": 1}) == []
    assert capture.drift({"replied": 2}, {"replied": 0}, canary={"replied": 1}) == [
        {"measure": "replied", "instantly": 2, "ours": 1}]
    # Ours above 0: the tolerance alone, on ours + canary (|1 + 1 - 2| = 0).
    assert capture.drift({"replied": 2}, {"replied": 1}, canary={"replied": 1}) == []
    # The canary's send counts on sent: Instantly 49, ours 48 + 1, none; without it |48 - 49| = 1 is inside anyway.
    assert capture.drift({"sent": 49}, {"sent": 48}, canary={"sent": 1}) == []


def test_reconcile_compares_whole_utc_days_as_instantlys_dates_do():
    """Mon 26 Oct 06:30 UTC less 7 days is Mon 19 Oct 06:30; the window opens at that day's midnight UTC, the first
    moment of Instantly's start_date 2026-10-19, and closes now."""
    assert capture.window(MON) == (datetime(2026, 10, 19, tzinfo=UTC), MON)


# -- a world: Instantly's analytics, emails and leads, our events -------------------------------------------------


@pytest.fixture
def h(default_settings, monkeypatch):
    # The canary runs here as it will once Harry's D10 (e) is in (an auto-reply rule on the seed inbox); its default,
    # off, has its own test below.
    monkeypatch.setattr(capture, "SEED_AUTO_REPLY_CONFIRMED", True)
    h = Harness(dataclasses.replace(default_settings,
                                    general=dataclasses.replace(default_settings.general, live_sending=True)))
    h.analytics: dict[str, dict] = {}
    h.emails: list[dict] = []
    for name in (C_HANNAH, C_HARRY):
        h.instantly.add_campaign(name, status=1)

    def analytics(req):
        cid = req.params["id"]
        return [{"campaign_id": cid, **h.analytics.get(cid, {})}]

    def emails(req):
        p = req.params or {}
        return {"items": [e for e in h.emails if e["eaccount"] == p["eaccount"] and e["_type"] == p.get("email_type")
                          and e["timestamp_created"] >= p.get("min_timestamp_created", "")]}

    def added(req):
        created = []
        for row in req.json["leads"]:
            lead = h.instantly.add_lead(req.json["campaign_id"], row["email"])
            lead.update(company_name=row.get("company_name"), status=1)
            created.append({"id": lead["id"], "email": row["email"]})
        return {"leads_uploaded": len(created), "created_leads": created}

    h.transport.route("GET", "/campaigns/analytics", fn=analytics)
    h.transport.route("GET", "api.instantly.ai/api/v2/emails", fn=emails)
    h.transport.route("POST", "/leads/add", fn=added)
    return h


def cid(h, name):
    return h.instantly.by_name(name)["id"]


def at(t: datetime) -> str:
    return t.isoformat().replace("+00:00", "Z")


def ctx_at(h, now: datetime, *, live: bool = True, job: str = capture.JOB):
    ctx = h(job, live)
    ctx.now = now
    return ctx


def events(ctx, type_, n, *, start: datetime, contact=lambda i: f"k{i}", **extra):
    ctx.store.upsert("events", [{"event_id": f"{type_}-{start:%d%H}-{i}", "type": type_, "contact_id": contact(i),
                                 "occurred_at": start + timedelta(minutes=i), **extra} for i in range(n)])


def seed_lead(h, address=SEED, campaign=C_HARRY, status=-2):
    lead = h.instantly.add_lead(cid(h, campaign), address)
    lead.update(company_name=seed.SEED_COMPANY, status=status)
    return lead


# -- reconcile -----------------------------------------------------------------------------------------------------


def test_reconcile_counts_both_sides_in_the_same_units(h):
    ctx = ctx_at(h, MON)
    # Instantly over the window: Hannah's campaign 30 sent / 1 replied / 1 bounced, Harry's 18 / 1 / 1, so 48 / 2 / 2.
    h.analytics = {cid(h, C_HANNAH): {"emails_sent_count": 30, "reply_count_unique": 1, "bounced_count": 1},
                   cid(h, C_HARRY): {"emails_sent_count": 18, "reply_count_unique": 1, "bounced_count": 1}}
    week_ago = MON - timedelta(days=7)
    events(ctx, "sent", 48, start=week_ago + timedelta(hours=1))  # 48 sends inside the 7 days
    events(ctx, "sent", 3, start=week_ago - timedelta(days=1))  # 3 before it: not counted
    # Replies: k1 twice (one lead), k2 once, k3's out-of-office: distinct leads with a human reply = 2.
    events(ctx, "replied", 2, start=MON - timedelta(days=2), contact=lambda i: "k1", reply_class="positive")
    events(ctx, "replied", 1, start=MON - timedelta(days=1), contact=lambda i: "k2")  # not classified yet: counts
    events(ctx, "replied", 1, start=MON - timedelta(hours=5), contact=lambda i: "k3", reply_class="out_of_office")
    events(ctx, "bounced", 2, start=MON - timedelta(days=3))
    out = capture.reconcile(ctx)
    assert out["campaigns"] == 2 and out["instantly"] == {"sent": 48, "replied": 2, "bounced": 2}
    assert out["ours"] == {"sent": 48, "replied": 2, "bounced": 2} and out["drift"] == []
    assert out["units"]["replied"].startswith("leads")
    # Dates, the form Instantly documents (step_analytics sends them so): from 19 Oct, 7 days before Mon 26 Oct, to
    # today; ours from 19 Oct 00:00 UTC (capture.window). The review's note: the full datetimes, if refused, would
    # have made every Monday reconcile:unread.
    [req, *_] = [r for r in h.transport.requests if "/campaigns/analytics" in r.url]
    assert (req.params["start_date"], req.params["end_date"]) == ("2026-10-19", "2026-10-26")
    assert out["since"] == "2026-10-19T00:00:00+00:00"


def test_a_broken_read_is_a_drift_an_error_and_an_unverified_week(h):
    """Instantly saw 2 replies, we recorded none: the summary's errors carry it (heartbeat_check posts them), and
    the week's fact is capture_unverified for the readout."""
    ctx = ctx_at(h, MON)
    h.analytics = {cid(h, C_HANNAH): {"emails_sent_count": 48, "reply_count_unique": 2, "bounced_count": 2}}
    events(ctx, "sent", 48, start=MON - timedelta(days=6))
    events(ctx, "bounced", 2, start=MON - timedelta(days=3))
    seed_lead(h)
    summary = run_job(ctx, capture.run)
    assert summary["reconcile"]["drift"] == [{"measure": "replied", "instantly": 2, "ours": 0}]
    assert summary["errors"][0] == ("capture drift on replied: Instantly 2, ours 0 (leads (distinct; automatic replies "
                                    "and out-of-office left out), last 7 days)")
    assert summary["verified"] is False and "drift:replied" in summary["reasons"]
    [fact] = [r for r in ctx.store.tables["signal_events"] if r["fact"] in (capture.VERIFIED, capture.UNVERIFIED)]
    assert (fact["event_id"], fact["fact"], fact["account_id"]) == ("capture:2026-W44", "capture_unverified", None)
    assert fact["value"]["drift"] == summary["reconcile"]["drift"]
    # heartbeat_check's reading of the run (ops/health.py): one "errors" finding quoting the drift first.
    [hb] = [r for r in ctx.store.tables["heartbeats"] if r["job"] == "capture"]
    [finding] = health.findings("capture", hb)
    assert finding.kind == "errors" and finding.texts[0].startswith("capture drift on replied")


# -- the canary ----------------------------------------------------------------------------------------------------


CANARY = "harry+seed1+canary-20261026@spill.chat"
SENT_AT, BACK_AT = MON + timedelta(hours=8), MON + timedelta(hours=8, minutes=2)  # Mon 26 Oct 14:30 / 14:32 UK


def week_one_answered(h) -> dict:
    """Week 1 run at MON (the canary added at 07:30 UK); Instantly sends it that afternoon in the US window and the
    seed inbox's auto-reply comes back from its own address, tied by Instantly to the canary's lead and thread.
    Instantly's next 7-day totals hold the send (1 sent) and, only if it counts automatic replies after all
    (capture.CANARY_ANSWER_IN_INSTANTLY_REPLIES), the answer (1 replied); we have no event for either (no contact of
    ours)."""
    seed_lead(h)  # the 6 Oct seed lead, unsubscribed since
    week1 = run_job(ctx_at(h, MON), capture.run)
    h.emails += [
        {"id": "S-c", "eaccount": "harry@meetspill.org", "from_address_email": "harry@meetspill.org",
         "to_address_email_list": CANARY, "lead": CANARY, "thread_id": "T-c", "ue_type": 1, "_type": "sent",
         "timestamp_created": at(SENT_AT)},
        {"id": "R-c", "eaccount": "harry@meetspill.org", "from_address_email": "harry@spill.chat",
         "lead": CANARY, "thread_id": "T-c", "ue_type": 2, "_type": "received", "timestamp_created": at(BACK_AT)},
    ]
    h.analytics = {cid(h, C_HARRY): {"emails_sent_count": 1,
                                     "reply_count_unique": 1 if capture.CANARY_ANSWER_IN_INSTANTLY_REPLIES else 0}}
    return week1


def test_the_canary_goes_to_the_seed_inbox_and_its_answer_verifies_the_week_after(h):
    week1 = week_one_answered(h)
    # First run: nothing to check yet, so the week is unverified, but that is no error; this week's canary is added.
    assert week1["canary_check"] == {"status": "none"} and week1["canary_send"]["status"] == "sent"
    assert week1["errors"] == [] and week1["fact"] == "capture_unverified"
    canary = CANARY
    [lead] = [x for x in h.instantly.leads.values() if x["email"] == canary]
    assert lead["campaign"] == cid(h, C_HARRY) and lead["company_name"] == seed.SEED_COMPANY
    # Week 2: Instantly counts the canary's send, and not its answer (an automatic reply, which reply_count_unique
    # leaves out); we have no event for either (no contact of ours), so ours includes the send only: Instantly 1 sent
    # / 0 replied, ours 0 + 1 / 0, no drift.
    week2 = run_job(ctx_at(h, MON + timedelta(days=7)), capture.run)
    check = week2["canary_check"]
    assert (check["status"], check["answers"], check["sent"], check["lead"]) == ("ok", 1, 1, "deleted")
    assert check["reasons"] == {"spill_domain": 1}  # read through the same filter poll_replies uses, and named
    assert week2["reconcile"]["canary_in_window"] == {"sent": 1, "replied": 1}
    assert week2["reconcile"]["ours_includes_canary"] == {"sent": 1}
    assert week2["reconcile"]["drift"] == [] and week2["verified"] is True and week2["fact"] == "capture_verified"
    assert canary not in {x["email"] for x in h.instantly.leads.values()}  # emails 2 to 4 never go
    assert week2["canary_send"]["status"] == "sent"  # the next one
    assert (check["sent_at"], check["answered_at"]) == ("2026-10-26T14:30:00+00:00", "2026-10-26T14:32:00+00:00")


@pytest.mark.parametrize("counted", [False, True])
def test_a_run_that_starts_late_still_counts_the_canary_by_when_it_was_sent(h, monkeypatch, counted):
    """W1-2 review: the canary was counted by when its lead was added (Mon 06:30 UTC), so a run 30 seconds later than
    7 days after the add left it out. Where Instantly counts the answer (counted), that was Instantly replied 1, ours
    0: a false drift and an unverified week. By when the email went: the window is [Mon 26 Oct 00:00, Mon 2 Nov
    06:30:30) UTC, and the send (14:30) and answer (14:32) on 26 Oct are inside it, so ours is 0 + 1 sent, and 0 + 1
    replied where counted, else 0, against Instantly's 1 / 1 or 1 / 0."""
    monkeypatch.setattr(capture, "CANARY_ANSWER_IN_INSTANTLY_REPLIES", counted)
    week_one_answered(h)
    late = run_job(ctx_at(h, MON + timedelta(days=7, seconds=30)), capture.run)
    assert late["reconcile"]["ours_includes_canary"] == ({"sent": 1, "replied": 1} if counted else {"sent": 1})
    assert late["reconcile"]["drift"] == [] and late["errors"] == []
    assert late["verified"] is True and late["fact"] == "capture_verified"


@pytest.mark.parametrize("counted", [False, True])
def test_a_real_reply_we_lost_is_a_drift_even_in_a_week_the_canary_answered(h, monkeypatch, counted):
    """W1-2 review, round 2: the canary's answer was added to our replied count before the zero rule, though
    Instantly's reply_count_unique leaves automatic replies out. A real reply Instantly saw and we never recorded
    then read 1 against 0 + 1: no drift, capture_verified, the very loss reconcile is for hidden. Now the zero rule is
    judged on our own replies alone. Instantly replied 1 (default: the real reply alone; the canary's answer is not in
    it) against ours 0: 1 > 0 and ours 0, a drift. Where Instantly does count the canary (counted): Instantly 2, ours
    0 + 1; 2 - 1 = 1 real reply against none of ours, a drift (the tolerance, |1 - 2| = 1 <= 2, would not see it)."""
    monkeypatch.setattr(capture, "CANARY_ANSWER_IN_INSTANTLY_REPLIES", counted)
    week_one_answered(h)
    h.analytics[cid(h, C_HARRY)]["reply_count_unique"] += 1  # a prospect's reply our read never recorded
    week2 = run_job(ctx_at(h, MON + timedelta(days=7)), capture.run)
    assert week2["canary_check"]["status"] == "ok"  # the canary itself passed
    assert week2["reconcile"]["drift"] == [{"measure": "replied", "instantly": 2 if counted else 1,
                                            "ours": 1 if counted else 0}]
    assert week2["verified"] is False and week2["fact"] == "capture_unverified" and "drift:replied" in week2["reasons"]
    assert week2["errors"][0].startswith(f"capture drift on replied: Instantly {2 if counted else 1}, ours ")
    [fact] = [r for r in h.store.tables["signal_events"] if r["event_id"] == "capture:2026-W45"]
    assert fact["fact"] == "capture_unverified"


def test_instantly_is_taken_not_to_count_the_canarys_answer_by_default():
    assert capture.CANARY_ANSWER_IN_INSTANTLY_REPLIES is False  # no fixture: the module's own default (D10 b, e)


def test_the_canary_counts_only_where_its_emails_fell():
    """canary_in_window, hand-computed: the window is [start, end) = [Mon 26 Oct 06:30, Mon 2 Nov 06:30) UTC."""
    start, end = MON, MON + timedelta(days=7)
    checked = {"status": "ok", "added_at": MON.isoformat(), "answers": 1, "sent": 1,
               "sent_at": SENT_AT.isoformat(), "answered_at": BACK_AT.isoformat()}
    assert capture.canary_in_window(checked, start, end) == {"sent": 1, "replied": 1}
    # Sent at 14:30 on 26 Oct, the window opening at 15:00 that day: neither email is Instantly's any more.
    assert capture.canary_in_window(checked, MON + timedelta(hours=8, minutes=30), end + timedelta(hours=8)) == {}
    # Sent before the window, answered inside it: only the answer counts.
    assert capture.canary_in_window(checked, MON + timedelta(hours=8, minutes=1), end) == {"replied": 1}
    # No times read: the add stands in, a day's grace (SEND_GRACE) before the window: added 06:30 26 Oct, the window
    # opening 06:30:30 is within the grace; opening at 06:31 on 27 Oct (a day and 30 s later) is not.
    bare = {"status": "no_answer", "added_at": MON.isoformat(), "answers": 0, "sent": 1}
    assert capture.canary_in_window(bare, MON + timedelta(seconds=30), end) == {"sent": 1}
    assert capture.canary_in_window(bare, MON + timedelta(days=1, seconds=30), end) == {}
    # A canary that was never sent, or not checked, adds nothing.
    assert capture.canary_in_window({**checked, "status": "not_sent"}, start, end) == {}
    assert capture.canary_in_window({"status": "none"}, start, end) == {}


@pytest.mark.parametrize("counted", [False, True])
def test_a_canary_checked_on_tuesday_still_verifies_the_monday_after(h, monkeypatch, counted):
    """W1-2 review: `capture check` on the Tuesday marked the canary checked, so the next Monday's check found
    nothing ({"status": "none"}): the week was unverified (canary:none) and, the canary left out of reconcile where
    Instantly counts its answer, a false drift on replied (Instantly 1, ours 0). The run now reads the stored check
    back."""
    monkeypatch.setattr(capture, "CANARY_ANSWER_IN_INSTANTLY_REPLIES", counted)
    week_one_answered(h)
    tue = capture.check_only(ctx_at(h, MON + timedelta(days=1), job=capture.CHECK_JOB))
    assert tue["canary_check"]["status"] == "ok" and tue["errors"] == []
    week2 = run_job(ctx_at(h, MON + timedelta(days=7)), capture.run)
    c = week2["canary_check"]
    assert (c["status"], c["earlier"], c["answers"], c["sent"]) == ("ok", True, 1, 1)
    assert c["checked_at"] == (MON + timedelta(days=1)).isoformat()
    assert week2["reconcile"]["ours_includes_canary"] == ({"sent": 1, "replied": 1} if counted else {"sent": 1})
    assert week2["reconcile"]["drift"] == [] and week2["errors"] == []
    assert week2["verified"] is True and week2["fact"] == "capture_verified"
    # Run again the same morning (a retry): the same verdict, not "waiting" on the canary it has just added.
    again = run_job(ctx_at(h, MON + timedelta(days=7, minutes=20)), capture.run)
    assert again["verified"] is True and again["canary_check"]["earlier"] is True
    assert again["canary_send"]["status"] == "already_sent"


def test_a_canary_two_weeks_old_is_not_this_weeks_verdict(h):
    """Week 2's canary could not be added (no new lead): week 3 must not stand on week 1's check. Week 1's canary was
    added Mon 26 Oct 06:30; week 3 runs Mon 9 Nov 06:30, and earlier_check looks back WINDOW + SEND_GRACE = 8 days,
    to Sun 1 Nov 06:30, after the add."""
    week_one_answered(h)
    run_job(ctx_at(h, MON + timedelta(days=7)), capture.run)  # checks week 1's canary, adds week 2's
    h.store.tables["signal_events"] = [r for r in h.store.tables["signal_events"]
                                       if not str(r["event_id"]).endswith(":canary:20261102")]
    h.analytics = {}
    week3 = run_job(ctx_at(h, MON + timedelta(days=14)), capture.run)
    assert week3["canary_check"] == {"status": "none"} and week3["verified"] is False
    assert week3["reconcile"]["ours_includes_canary"] == {}


def test_until_d10e_the_canary_is_off_and_no_error_is_raised(h, monkeypatch):
    """Harry has not yet set the seed inbox's auto-reply (D10 e): until then each week's check would find no answer and
    post an error, so the canary is not sent; the week is unverified, said in its reasons, and nothing is posted."""
    monkeypatch.setattr(capture, "SEED_AUTO_REPLY_CONFIRMED", False)
    seed_lead(h)
    out = run_job(ctx_at(h, MON), capture.run)
    assert out["canary_send"]["status"] == "off" and out["errors"] == []
    assert out["reasons"] == ["canary:none", "canary_send:off"] and out["fact"] == "capture_unverified"
    assert not [r for r in h.transport.requests if r.url.endswith("/leads/add")]


def test_the_canary_is_off_by_default():
    assert capture.SEED_AUTO_REPLY_CONFIRMED is False  # no fixture here: the module's own default


def test_analytics_that_cannot_be_read_are_an_error_and_no_drift_is_judged(h):
    """Instantly answers 400 to the analytics read (say, the date format it takes): its totals are missing, so the
    sum (0 sent) would make a false drift against our 48 sends (|0 - 48| = 48 > max(2, 0)). No drift is judged; one
    error names the first campaign (C_HANNAH, sorted first); the week is unverified; the canary still goes."""
    ctx = ctx_at(h, MON)
    events(ctx, "sent", 48, start=MON - timedelta(days=6))
    h.transport.route("GET", "/campaigns/analytics", body={"message": "bad date"}, status=400)
    seed_lead(h)
    out = run_job(ctx, capture.run)
    assert out["reconcile"]["drift"] == [] and len(out["reconcile"]["unread"]) == 2
    assert out["errors"][0].startswith("capture: Instantly's analytics could not be read for 2 campaign(s)")
    assert f"no drift was judged: {C_HANNAH}: ApiError" in out["errors"][0]
    assert out["verified"] is False and out["reasons"][0] == "reconcile:unread"
    assert out["canary_send"]["status"] == "sent"  # the canary still goes


def test_the_readout_caveat_says_when_the_counts_are_unverified(h):
    ctx = ctx_at(h, MON)
    assert capture.readout_caveat(ctx) == "Reply capture not checked this week: the reply counts are unverified."
    week_one_answered(h)
    assert capture.readout_caveat(ctx_at(h, MON + timedelta(hours=1))) == (
        "Reply capture unverified this week (canary:none): the reply counts may be short.")
    run_job(ctx_at(h, MON + timedelta(days=7)), capture.run)
    assert capture.readout_caveat(ctx_at(h, MON + timedelta(days=7, hours=1))) is None
    # Nine days on, the week-2 fact is stale (FACT_FRESH 8 days): not checked this week.
    assert capture.readout_caveat(ctx_at(h, MON + timedelta(days=16))).startswith("Reply capture not checked")


def test_a_canary_with_no_answer_is_an_error_and_says_whether_it_was_sent(h):
    seed_lead(h)
    run_job(ctx_at(h, MON), capture.run)
    week2 = run_job(ctx_at(h, MON + timedelta(days=7)), capture.run)
    assert week2["canary_check"]["status"] == "not_sent"  # nothing went to the seed inbox at all
    assert "capture canary: the campaign never sent the seed email" in week2["errors"][0]
    assert week2["verified"] is False and "canary:not_sent" in week2["reasons"]


def test_capture_check_the_next_day_and_a_young_canary_waits(h):
    seed_lead(h)
    run_job(ctx_at(h, MON), capture.run)
    # 4 hours later: younger than CANARY_WAIT (20 h), so not judged yet.
    early = capture.check_only(ctx_at(h, MON + timedelta(hours=4), job=capture.CHECK_JOB))
    assert early["canary_check"]["status"] == "waiting" and early["errors"] == []
    h.emails.append({"id": "R-c", "eaccount": "harry@meetspill.org", "from_address_email": "harry@spill.chat",
                     "lead": CANARY, "ue_type": 2, "_type": "received", "timestamp_created": at(MON + timedelta(hours=9))})
    tue = capture.check_only(ctx_at(h, MON + timedelta(days=1), job=capture.CHECK_JOB))
    assert tue["canary_check"]["status"] == "ok" and tue["errors"] == []
    # Checked once: the Monday run after finds nothing left to check from it.
    assert capture.check(ctx_at(h, MON + timedelta(days=7)))["status"] == "none"


def harrys_note(t: datetime, **extra) -> dict:
    """An email from Harry's own address (the seed inbox's, untagged) to one of the mailboxes: not the canary's."""
    return {"id": f"R-h{t:%H%M}", "eaccount": "harry@meetspill.org", "from_address_email": "harry@spill.chat",
            "subject": "quick test", "ue_type": 2, "_type": "received", "timestamp_created": at(t), **extra}


def canary_leads(h) -> list[str]:
    return sorted(x["email"] for x in h.instantly.leads.values() if capture.CANARY_TAG in x["email"])


def test_an_email_from_the_seed_inbox_not_tied_to_the_canary_proves_nothing(h):
    """W1-2 review, round 2: any received email from the seed inbox's untagged address since the add passed. Harry
    wrote "quick test" from harry@spill.chat to harry@meetspill.org at 09:30 UTC on the Monday, before any send; the
    Tuesday check said ok, sent 1, and deleted the canary's lead before it ever went. Now an answer counts only when
    Instantly ties it to the canary (its lead, or the thread of a send to it): not sent, and the lead is kept so the
    email can still go (CANARY_GIVE_UP)."""
    seed_lead(h)
    run_job(ctx_at(h, MON), capture.run)
    h.emails.append(harrys_note(MON + timedelta(hours=3)))
    tue = capture.check_only(ctx_at(h, MON + timedelta(days=1), job=capture.CHECK_JOB))
    c = tue["canary_check"]
    assert (c["status"], c["answers"], c["sent"]) == ("not_sent", 0, 0)
    assert c["lead"].startswith("kept") and canary_leads(h) == [CANARY]
    assert tue["errors"] == ["capture canary: the campaign has not sent the seed email yet (paused, or out of its "
                             "window?); its lead is kept and checked again at the next run"]
    [fact] = [r for r in h.store.tables["signal_events"] if r["fact"] == capture.CANARY_SENT]
    assert fact["value"]["checked_at"] is None  # still to check
    # It went on the Tuesday afternoon and the auto-reply came back, tied to it: the Monday run finds it ok.
    h.emails += [
        {"id": "S-c", "eaccount": "harry@meetspill.org", "from_address_email": "harry@meetspill.org",
         "to_address_email_list": CANARY, "thread_id": "T-c", "ue_type": 1, "_type": "sent",
         "timestamp_created": at(SENT_AT + timedelta(days=1))},
        {"id": "R-c", "eaccount": "harry@meetspill.org", "from_address_email": "harry@spill.chat",
         "thread_id": "T-c", "ue_type": 2, "_type": "received",  # tied by the thread alone (no lead field)
         "timestamp_created": at(BACK_AT + timedelta(days=1))},
    ]
    h.analytics = {cid(h, C_HARRY): {"emails_sent_count": 1}}
    week2 = run_job(ctx_at(h, MON + timedelta(days=7)), capture.run)
    c = week2["canary_check"]
    assert (c["status"], c["answers"], c["sent"], c["lead"]) == ("ok", 1, 1, "deleted")
    assert c["sent_at"] == "2026-10-27T14:30:00+00:00" and week2["verified"] is True
    assert canary_leads(h) == ["harry+seed1+canary-20261102@spill.chat"]  # week 2's, just added


def test_an_email_tied_to_the_canary_but_before_its_send_is_no_answer(h):
    """The lead field names the canary, but the email came at 09:30 UTC, before the send read at 14:30: not an
    answer to it. The send was read, so the canary went (sent 1) and nothing came back: no_answer, lead deleted."""
    seed_lead(h)
    run_job(ctx_at(h, MON), capture.run)
    h.emails += [harrys_note(MON + timedelta(hours=3), lead=CANARY),
                 {"id": "S-c", "eaccount": "harry@meetspill.org", "from_address_email": "harry@meetspill.org",
                  "to_address_email_list": CANARY, "ue_type": 1, "_type": "sent", "timestamp_created": at(SENT_AT)}]
    c = capture.check_only(ctx_at(h, MON + timedelta(days=1), job=capture.CHECK_JOB))["canary_check"]
    assert (c["status"], c["answers"], c["sent"], c["lead"]) == ("no_answer", 0, 1, "deleted")


def test_a_send_to_the_seed_inbox_at_large_is_not_the_canarys(h):
    """A sent email to harry@spill.chat (Harry's own address) is not the canary going: only its tagged address is."""
    seed_lead(h)
    run_job(ctx_at(h, MON), capture.run)
    h.emails.append({"id": "S-h", "eaccount": "harry@meetspill.org", "from_address_email": "harry@meetspill.org",
                     "to_address_email_list": "harry@spill.chat", "ue_type": 3, "_type": "sent",
                     "timestamp_created": at(SENT_AT)})
    c = capture.check_only(ctx_at(h, MON + timedelta(days=1), job=capture.CHECK_JOB))["canary_check"]
    assert (c["status"], c["sent"]) == ("not_sent", 0)


def test_a_canary_never_sent_is_given_up_after_a_week_and_its_lead_deleted(h):
    """Not sent on the Tuesday (a day old): kept. Still not sent at the Monday run, 7 days after the add, which is
    past CANARY_GIVE_UP (6 days): not_sent once and for all, the lead deleted, the canary checked."""
    seed_lead(h)
    run_job(ctx_at(h, MON), capture.run)
    capture.check_only(ctx_at(h, MON + timedelta(days=1), job=capture.CHECK_JOB))
    week2 = run_job(ctx_at(h, MON + timedelta(days=7)), capture.run)
    assert (week2["canary_check"]["status"], week2["canary_check"]["lead"]) == ("not_sent", "deleted")
    assert "capture canary: the campaign never sent the seed email" in week2["errors"][0]
    first = h.store.get("signal_events", event_id="capture:canary:20261026")
    assert first["value"]["checked_at"] == (MON + timedelta(days=7)).isoformat()
    assert canary_leads(h) == ["harry+seed1+canary-20261102@spill.chat"]


def test_every_unchecked_canary_is_checked_and_its_lead_deleted(h):
    """W1-2 review, round 2: check() looked at the latest unchecked canary only, so one left unchecked by a lost
    week kept its lead, and the seed inbox would get emails 2 to 4. Week 1's canary (answered) was not checked: week
    2's canary was added without a check. Week 3 checks both: week 1's ok, week 2's not sent and given up (7 days >=
    CANARY_GIVE_UP); the verdict is the latest (week 2's), week 1's is listed under older; both leads are gone."""
    week_one_answered(h)
    assert capture.send_canary(ctx_at(h, MON + timedelta(days=7)))["status"] == "sent"
    assert canary_leads(h) == [CANARY, "harry+seed1+canary-20261102@spill.chat"]
    week3 = run_job(ctx_at(h, MON + timedelta(days=14)), capture.run)
    c = week3["canary_check"]
    assert (c["address"], c["status"], c["lead"]) == ("harry+seed1+canary-20261102@spill.chat", "not_sent", "deleted")
    assert c["older"] == [{"address": CANARY, "status": "ok", "lead": "deleted"}]
    assert canary_leads(h) == ["harry+seed1+canary-20261109@spill.chat"]  # week 3's, just added
    assert all(r["value"]["checked_at"] for r in h.store.tables["signal_events"]
               if r["fact"] == capture.CANARY_SENT and not r["event_id"].endswith("20261109"))


def test_with_no_seed_lead_the_canary_is_not_set_up_and_nothing_is_sent(h):
    out = run_job(ctx_at(h, MON), capture.run)
    assert out["canary_send"]["status"] == "not_set_up" and out["errors"] == []
    assert "canary_send:not_set_up" in out["reasons"] and out["fact"] == "capture_unverified"
    assert not [r for r in h.transport.requests if r.url.endswith("/leads/add")]


def test_dry_run_reads_and_changes_nothing(h):
    seed_lead(h)
    ctx = ctx_at(h, MON, live=False)
    out = capture.run(ctx)
    assert out["dry_run"] is True and out["canary_send"]["status"] == "dry_run"
    assert not [r for r in h.transport.writes() if "api.instantly.ai" in r.url and not r.url.endswith("/leads/list")]
    assert ctx.store.tables.get("signal_events", []) == []


def test_the_canary_address_is_the_seed_inbox_tagged_by_day():
    assert capture.canary_address("harry@spill.chat", MON) == "harry+canary-20261026@spill.chat"
    assert capture.base_address("Harry+Canary-20261026@spill.chat") == "harry@spill.chat"
    assert capture.base_address("harry+seed1+canary-20261026@spill.chat") == "harry+seed1@spill.chat"
