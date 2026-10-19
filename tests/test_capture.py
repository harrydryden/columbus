"""The weekly capture check (replies/capture.py; W1-2, 19 Oct 2026): Instantly's counts against our events, and the
seed inbox's canary, so a broken reply read can be told from a true zero (code-replies-crm M3, M4; RT §1.6, §4)."""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta

import pytest

from tests.test_cli import Harness
from us_outbound.ops import job_errors, seed
from us_outbound.ops.heartbeat import run_job
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


# -- a world: Instantly's analytics, emails and leads, our events -------------------------------------------------


@pytest.fixture
def h(default_settings):
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
    [req, *_] = [r for r in h.transport.requests if "/campaigns/analytics" in r.url]
    assert (req.params["start_date"], req.params["end_date"]) == ("2026-10-19T06:30:00Z", "2026-10-26T06:30:00Z")


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
    # heartbeat_check's reading of the run (ops/job_errors.py): one "errors" finding quoting the drift first.
    [hb] = [r for r in ctx.store.tables["heartbeats"] if r["job"] == "capture"]
    [finding] = job_errors.findings("capture", hb)
    assert finding.kind == "errors" and finding.texts[0].startswith("capture drift on replied")


# -- the canary ----------------------------------------------------------------------------------------------------


def test_the_canary_goes_to_the_seed_inbox_and_its_answer_verifies_the_week_after(h):
    seed_lead(h)  # the 6 Oct seed lead, unsubscribed since
    week1 = run_job(ctx_at(h, MON), capture.run)
    # First run: nothing to check yet, so the week is unverified, but that is no error; this week's canary is added.
    assert week1["canary_check"] == {"status": "none"} and week1["canary_send"]["status"] == "sent"
    assert week1["errors"] == [] and week1["fact"] == "capture_unverified"
    canary = "harry+seed1+canary-20261026@spill.chat"
    [lead] = [x for x in h.instantly.leads.values() if x["email"] == canary]
    assert lead["campaign"] == cid(h, C_HARRY) and lead["company_name"] == seed.SEED_COMPANY
    # Instantly sends it Monday afternoon; the seed inbox's auto-reply comes back from its own address.
    sent_at, back_at = MON + timedelta(hours=8), MON + timedelta(hours=8, minutes=2)
    h.emails += [
        {"id": "S-c", "eaccount": "harry@meetspill.org", "from_address_email": "harry@meetspill.org",
         "to_address_email_list": canary, "ue_type": 1, "_type": "sent", "timestamp_created": at(sent_at)},
        {"id": "R-c", "eaccount": "harry@meetspill.org", "from_address_email": "harry@spill.chat",
         "ue_type": 2, "_type": "received", "timestamp_created": at(back_at)},
    ]
    # Week 2: Instantly counts the canary's send and its answer; we have no event for either (no contact of ours),
    # so ours includes the canary: Instantly 1 sent / 1 replied, ours 0 + 1 / 0 + 1, no drift.
    h.analytics = {cid(h, C_HARRY): {"emails_sent_count": 1, "reply_count_unique": 1}}
    week2 = run_job(ctx_at(h, MON + timedelta(days=7)), capture.run)
    check = week2["canary_check"]
    assert (check["status"], check["answers"], check["sent"], check["lead"]) == ("ok", 1, 1, "deleted")
    assert check["reasons"] == {"spill_domain": 1}  # read through the same filter poll_replies uses, and named
    assert week2["reconcile"]["ours_includes_canary"] == {"sent": 1, "replied": 1}
    assert week2["reconcile"]["drift"] == [] and week2["verified"] is True and week2["fact"] == "capture_verified"
    assert canary not in {x["email"] for x in h.instantly.leads.values()}  # emails 2 to 4 never go
    assert week2["canary_send"]["status"] == "sent"  # the next one


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
                     "ue_type": 2, "_type": "received", "timestamp_created": at(MON + timedelta(hours=9))})
    tue = capture.check_only(ctx_at(h, MON + timedelta(days=1), job=capture.CHECK_JOB))
    assert tue["canary_check"]["status"] == "ok" and tue["errors"] == []
    # Checked once: the Monday run after finds nothing left to check from it.
    assert capture.check(ctx_at(h, MON + timedelta(days=7)))["status"] == "none"


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
