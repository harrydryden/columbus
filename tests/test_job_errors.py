"""Errors the jobs caught, and rejected keys (ops/job_errors.py; Harry, 7 Oct 2026): heartbeat_check reads each job's
latest heartbeat detail and says in Slack what went wrong, once a day per job and kind."""

from __future__ import annotations

from datetime import timedelta

import pytest

from tests.test_heartbeat import MON_NOON, beat
from tests.test_watchdog import PING, pings, posts, world
from us_outbound.ops import heartbeat as hb
from us_outbound.ops import job_errors

JOBS = ["enrol", "poll_replies", "poll_approvals", "hubspot_readback", "kill_rules", "settings_sync", "pick_contacts",
        "daily_post", "heartbeat_check", "mailbox_health"]


def at(minutes):
    return MON_NOON - timedelta(minutes=minutes)


def check(ctx):
    return job_errors.check(ctx, JOBS)


def text(t) -> str:
    [post] = posts(t)
    return post["text"]


def test_errors_a_job_carried_on_past_are_posted_once_a_day_with_the_first_quoted():
    ctx, t = world()
    beat(ctx.store, "enrol", at(60), detail={"status": "ok", "errors": [
        "US Outbound – Hannah Spalding: instantly HTTP 502 for /api/v2/leads/add: bad gateway jane@acme.com",
        "US Outbound – Sam Jackson: instantly HTTP 502 for /api/v2/leads/add: bad gateway"],
        "send_approvals": {"errors": ["abc123: the card was not posted (ratelimited)"]}})
    out = check(ctx)
    body = text(t)
    assert body.startswith("<@U_HARRY> Errors the jobs met:")  # the approvers mentioned
    assert ("• enrol (Mon 11:00 UK) carried on past 3 errors; the first: “US Outbound – Hannah Spalding: instantly "
            "HTTP 502 for /api/v2/leads/add: bad gateway email:") in body
    assert "jane@acme.com" not in body  # passed through logs.redact
    assert body.endswith("Each job tries again on its next run. `us-outbound status` lists the jobs that failed; the "
                         "worker's logs in Railway have the rest.")
    assert out["posted"] == ["job_error:enrol:errors:2026-10-26"]
    assert check(ctx)["posted"] == [] and len(posts(t)) == 1  # once a day, a restart included
    ctx.now += timedelta(days=1)
    beat(ctx.store, "enrol", ctx.now - timedelta(minutes=60), detail={"errors": ["again"]})
    check(ctx)
    assert len(posts(t)) == 2 and "the first: “again”" in posts(t)[-1]["text"]


@pytest.mark.parametrize("job, detail, error, says", [
    ("hubspot_readback", {"errors": ["meetings: hubspot HTTP 401 for /crm/v3/objects/meetings/search: expired"]}, None,
     "HubSpot refused our key or this request: if the key was revoked, replace US_OUTBOUND_HUBSPOT_TOKEN in Railway "
     "(Variables), then redeploy; if it is current, the HubSpot plan may not allow this."),
    ("poll_approvals", {"errors": [], "send_approvals": {"errors": [
        "abc1: ApiError: instantly HTTP 403 for /api/v2/campaigns: forbidden"]}}, None,
     "Instantly refused our key or this request: if the key was revoked, replace US_OUTBOUND_INSTANTLY_API_KEY in Railway "
     "(Variables), then redeploy; if it is current, the Instantly plan may not allow this."),
    ("kill_rules", {"vitals_error": "apollo HTTP 401 for /usage_stats: invalid"}, None,
     "Apollo refused our key or this request: if the key was revoked, replace US_OUTBOUND_APOLLO_API_KEY in Railway "
     "(Variables), then redeploy; if it is current, the Apollo plan may not allow this."),
    ("daily_post", {"alert": {"posted": False, "error": "slack HTTP 200 for /api/chat.postMessage: {'ok': False, "
                                                         "'error': 'token_revoked'}"}}, None,
     "Slack refused our key or this request: if the key was revoked, replace US_OUTBOUND_SLACK_BOT_TOKEN in Railway "
     "(Variables), then redeploy; if it is current, the Slack plan may not allow this."),
    ("poll_replies", {"claude_error": "Claude API error 401"}, None,
     "Anthropic (Claude) refused our key or this request: if the key was revoked, replace US_OUTBOUND_CLAUDE_API_KEY in Railway "
     "(Variables), then redeploy; if it is current, the Anthropic (Claude) plan may not allow this."),
    ("settings_sync", None, "RefreshError: ('invalid_grant: Invalid JWT Signature.', {'error': 'invalid_grant'})",
     "Google refused our key or this request: if the key was revoked, replace US_OUTBOUND_GOOGLE_SERVICE_ACCOUNT_JSON in Railway "
     "(Variables), then redeploy; if it is current, the Google plan may not allow this."),
    ("pick_contacts", None, "ApiError: clay HTTP 401 for /v1/sources: unauthorized",
     "Clay refused our key or this request: if the key was revoked, replace US_OUTBOUND_CLAY_API_KEY in Railway "
     "(Variables), then redeploy; if it is current, the Clay plan may not allow this."),
])
def test_a_rejected_key_names_the_railway_variable_to_replace(job, detail, error, says):
    ctx, t = world()
    beat(ctx.store, job, at(30), status="error" if error else "ok", detail=detail, error=error)
    check(ctx)
    body = text(t)
    assert f"• {says} ({job}: “" in body
    assert "carried on past" not in body and "failed (" not in body  # told once, as the key


def test_a_rejected_key_is_told_once_a_day_whichever_jobs_met_it():
    ctx, t = world()
    beat(ctx.store, "hubspot_readback", at(30), detail={"errors": ["deals: hubspot HTTP 401 for /crm: x"]})
    beat(ctx.store, "poll_approvals", at(10), detail={"errors": ["ab12: HubSpot: hubspot HTTP 401 for /crm: x",
                                                               "cd34: instantly HTTP 502 for /api: busy"]})
    out = check(ctx)
    body = text(t)
    assert body.count("HubSpot refused our key") == 1 and "(hubspot_readback, poll_approvals: “" in body
    assert "• poll_approvals (Mon 11:50 UK) carried on past 1 error; the first: “cd34: instantly HTTP 502" in body
    assert out["posted"] == ["key_rejected:hubspot:2026-10-26", "job_error:poll_approvals:errors:2026-10-26"]


def test_the_failure_flags_the_jobs_record():
    ctx, t = world()
    beat(ctx.store, "poll_replies", at(20), detail={"claude_cap_reached": 2, "slack_errors": 3, "retry_later": 4})
    beat(ctx.store, "enrol", at(40), status="error", error="KeyError: 'owner'")
    beat(ctx.store, "kill_rules", at(5), detail={"vitals_error": "instantly HTTP 504 for /accounts: timeout",
                                                 "alert": {"posted": False, "error": "no Slack token: posted to the log"}})
    check(ctx)
    body = text(t)
    assert ('• poll_replies (Mon 11:40 UK): Claude\'s monthly cap is used up, so 2 replies were classified "other" with '
            "no draft. Raise claude_monthly_cap_usd on the General tab (up to $100) and the Anthropic Console spend "
            "limit.") in body
    assert "• poll_replies (Mon 11:40 UK): 3 slack errors" in body
    assert "• enrol failed (Mon 11:20 UK): “KeyError: 'owner'”" in body
    assert "• kill_rules (Mon 11:55 UK): vitals error: “instantly HTTP 504 for /accounts: timeout”" in body
    assert "posted to the log" not in body  # no token is not a Slack failure: the log has the post


def test_a_frequent_job_that_failed_once_is_left_to_its_next_run_unless_its_key_was_rejected():
    ctx, t = world()
    beat(ctx.store, "poll_approvals", at(2), status="error", error="Terminated: stopped by SIGTERM (a redeploy)")
    beat(ctx.store, "poll_replies", at(3), status="error", error="ApiError: instantly HTTP 401 for /api/v2/emails: x")
    check(ctx)
    body = text(t)
    assert "Terminated" not in body and "poll_approvals" not in body
    assert "Instantly refused our key or this request: if the key was revoked, replace US_OUTBOUND_INSTANTLY_API_KEY" in body and "(poll_replies: “" in body


def test_what_is_no_error_is_left_out():
    ctx, t = world()
    beat(ctx.store, "settings_sync", at(30), detail={"errors": 4, "tabs": {"Copy": {"errors": 4}}})  # sheet errors
    beat(ctx.store, "pick_contacts", at(30), detail={"errors": [], "clay": {"errors": 2}})
    beat(ctx.store, "enrol", at(27 * 60), detail={"errors": ["yesterday's"]})  # older than 26 hours
    beat(ctx.store, "heartbeat_check", at(55), detail={"job_errors": {"check_error": "its own"}})
    beat(ctx.store, "poll_approvals", at(30), detail={"errors": ["an earlier run"]})
    beat(ctx.store, "poll_approvals", at(5), detail={"errors": []})  # the latest run is what counts
    out = check(ctx)
    assert out == {"found": [], "posted": [], "post": {"posted": False, "error": None}} and posts(t) == []


def test_unusable_settings_are_one_line_for_every_job_they_stop():
    ctx, t = world()
    for job in ("enrol", "poll_replies", "settings_sync"):
        beat(ctx.store, job, at(10), status="error", detail={"unusable": ["Copy", "General"]},
             error="settings are unusable (Copy, General); fix the sheet, then run `us-outbound settings sync`")
    check(ctx)
    body = text(t)
    assert body.count("settings are unusable") == 1 and "failed (" not in body
    assert ("• The settings are unusable (Copy, General): every job refuses to run until the sheet is fixed. Fix it, "
            "then run `us-outbound sync`.") in body


def test_a_slack_failure_is_not_recorded_so_the_next_hour_tries_again():
    ctx, t = world(posts=False)
    beat(ctx.store, "enrol", at(30), detail={"errors": ["boom"]})
    out = check(ctx)
    assert out["posted"] == [] and out["post"]["posted"] is False and ctx.store.select("events") == []
    t.route("POST", "chat.postMessage", fn=lambda r: {"ok": True, "channel": r.json["channel"], "ts": "2.2"})
    assert check(ctx)["posted"] == ["job_error:enrol:errors:2026-10-26"]


def test_heartbeat_check_runs_it_every_hour_and_a_dead_slack_token_still_pings_fail():
    ctx, t = world(auth=False, posts=False)
    beat(ctx.store, "hubspot_readback", at(10), detail={"errors": ["meetings: hubspot HTTP 401 for /crm: expired"]})
    out = hb.run(ctx)
    assert out["job_errors"]["found"] == ["key_rejected:hubspot"] and out["job_errors"]["posted"] == []
    assert pings(t) == [PING + "/fail"]  # Slack could not take the alert: Healthchecks emails instead
