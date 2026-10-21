"""Errors the jobs caught, and rejected keys (ops/health.py; Harry, 7 Oct 2026): heartbeat_check reads each job's
latest heartbeat detail and says in Slack what went wrong, once a day per job and kind."""

from __future__ import annotations

from datetime import timedelta

import pytest

from tests.fakes import TEST_SHEET_ID
from tests.test_heartbeat import MON_NOON, beat
from tests.test_watchdog import PING, pings, posts, world
from us_outbound.ops import health as hb
from us_outbound.base import say
from us_outbound.ops import health

SHEET = f"https://docs.google.com/spreadsheets/d/{TEST_SHEET_ID}/edit"  # say.sheet_link, no tab ids here
JOBS = ["enrol", "poll_replies", "poll_approvals", "hubspot_readback", "kill_rules", "settings_sync", "pick_contacts",
        "daily_post", "heartbeat_check", "mailbox_health"]


def at(minutes):
    return MON_NOON - timedelta(minutes=minutes)


def check(ctx):
    return health.report_errors(ctx, JOBS)


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
    assert body.startswith("<@U_HARRY> :warning: *Some automatic jobs ran into problems*")  # approvers mentioned
    assert ("• *Adding people to the email campaign* (Mon 11:00 UK) finished, but skipped past 3 errors. The first: "
            "Instantly's server had an error (HTTP 502), a problem on their side that usually clears by itself") in body
    assert "jane@acme.com" not in body  # passed through logs.redact
    assert body.endswith("_Each job tries again at its next run. More detail: the worker's logs in Railway, or "
                         "`railway ssh -- us-outbound status` for each job's last run._")
    assert out["posted"] == ["job_error:enrol:errors:2026-10-26"]
    assert check(ctx)["posted"] == [] and len(posts(t)) == 1  # once a day, a restart included
    ctx.now += timedelta(days=1)
    beat(ctx.store, "enrol", ctx.now - timedelta(minutes=60), detail={"errors": ["again"]})
    check(ctx)
    assert len(posts(t)) == 2 and "The first: “again”" in posts(t)[-1]["text"]


def test_a_daily_job_s_failure_is_told_on_its_own_day_and_not_again_after_midnight():
    """read_pages failed at 03:45 on Thu 8 Oct 2026 and was told at 09:05; at 00:05 on Friday it was still the latest
    run, and keyed on the new day it was told again. The key is the day its run began."""
    ctx, t = world()
    jobs = [*JOBS, "read_pages"]
    nul = "UntranslatableCharacter: unsupported Unicode escape sequence DETAIL: \\u0000 cannot be converted to text."
    beat(ctx.store, "read_pages", MON_NOON - timedelta(hours=8, minutes=15), status="error", error=nul)
    assert health.report_errors(ctx, jobs)["posted"] == ["job_error:read_pages:failed:2026-10-26"]
    assert "• *Reading company websites* failed (Mon 03:45 UK): “UntranslatableCharacter" in text(t)
    ctx.now = MON_NOON + timedelta(hours=12, minutes=5)  # Tuesday 00:05 UK: Monday's run is still the latest
    out = health.report_errors(ctx, jobs)
    assert out["found"] == ["job_error:read_pages:failed"] and out["posted"] == [] and len(posts(t)) == 1
    beat(ctx.store, "read_pages", MON_NOON + timedelta(hours=15, minutes=45), status="error", error=nul)
    ctx.now = MON_NOON + timedelta(hours=16, minutes=5)  # Tuesday's run failed too: told on Tuesday
    assert health.report_errors(ctx, jobs)["posted"] == ["job_error:read_pages:failed:2026-10-27"]
    assert len(posts(t)) == 2


@pytest.mark.parametrize("job, detail, error, name, var", [
    ("hubspot_readback", {"errors": ["meetings: hubspot HTTP 401 for /crm/v3/objects/meetings/search: expired"]}, None,
     "HubSpot", "US_OUTBOUND_HUBSPOT_TOKEN"),
    ("poll_approvals", {"errors": [], "send_approvals": {"errors": [
        "abc1: ApiError: instantly HTTP 403 for /api/v2/campaigns: forbidden"]}}, None,
     "Instantly", "US_OUTBOUND_INSTANTLY_API_KEY"),
    ("kill_rules", {"vitals_error": "apollo HTTP 401 for /usage_stats: invalid"}, None,
     "Apollo", "US_OUTBOUND_APOLLO_API_KEY"),
    ("daily_post", {"alert": {"posted": False, "error": "slack HTTP 200 for /api/chat.postMessage: {'ok': False, "
                                                         "'error': 'token_revoked'}"}}, None,
     "Slack", "US_OUTBOUND_SLACK_BOT_TOKEN"),
    ("poll_replies", {"claude_error": "Claude API error 401"}, None,
     "Anthropic (Claude)", "US_OUTBOUND_CLAUDE_API_KEY"),
    ("settings_sync", None, "RefreshError: ('invalid_grant: Invalid JWT Signature.', {'error': 'invalid_grant'})",
     "Google", "US_OUTBOUND_GOOGLE_SERVICE_ACCOUNT_JSON"),
])
def test_a_rejected_key_names_the_railway_variable_to_replace(job, detail, error, name, var):
    ctx, t = world()
    beat(ctx.store, job, at(30), status="error" if error else "ok", detail=detail, error=error)
    check(ctx)
    body = text(t)
    assert f"• :rotating_light: *{name} turned down our key* ({say.job_name(job)}): “" in body
    assert (f"*What to do:* if the key was revoked or has expired, put a new one in `{var}` under Variables in "
            f"Railway, then redeploy. If the key is current, our {name} plan may not allow this request.") in body
    assert "skipped past" not in body and "failed (" not in body  # told once, as the key


def test_a_rejected_key_is_told_once_a_day_whichever_jobs_met_it():
    ctx, t = world()
    beat(ctx.store, "hubspot_readback", at(30), detail={"errors": ["deals: hubspot HTTP 401 for /crm: x"]})
    beat(ctx.store, "poll_approvals", at(10), detail={"errors": ["ab12: HubSpot: hubspot HTTP 401 for /crm: x",
                                                               "cd34: instantly HTTP 404 for /api: no such lead"]})
    out = check(ctx)
    body = text(t)
    assert body.count("HubSpot turned down our key") == 1
    assert "*HubSpot turned down our key* (updating HubSpot and checking the approvals in Slack): “" in body
    assert ("• *Checking the approvals in Slack* (Mon 11:50 UK) finished, but skipped past 1 error. The first: “cd34: "
            "instantly HTTP 404") in body
    assert out["posted"] == ["key_rejected:hubspot:2026-10-26", "job_error:poll_approvals:errors:2026-10-26"]


def test_the_failure_flags_the_jobs_record():
    ctx, t = world()
    beat(ctx.store, "poll_replies", at(20), detail={"claude_cap_reached": 2, "slack_errors": 3, "retry_later": 4})
    beat(ctx.store, "enrol", at(40), status="error", error="KeyError: 'owner'")
    beat(ctx.store, "kill_rules", at(5), detail={"vitals_error": "instantly HTTP 504 for /accounts: timeout",
                                                 "alert": {"posted": False, "error": "no Slack token: posted to the log"}})
    check(ctx)
    body = text(t)
    assert ("• *Replies are going without draft answers* (checking for replies, Mon 11:40 UK): Claude's monthly "
            "spending cap is used up, so 2 replies were filed as “other” with no draft.") in body
    assert ("*What to do:* raise `claude_monthly_cap_usd` (now $10, at most $100) on the "
            f"<{SHEET}|General tab>, and the spend limit in the Anthropic Console to match.") in body
    assert "• *Checking for replies* (Mon 11:40 UK): 3 Slack posts failed" in body
    assert "• *Adding people to the email campaign* failed (Mon 11:20 UK): “KeyError: 'owner'”" in body
    assert "Checking bounce and complaint limits" not in body  # an hourly job's one outage: its next run tries again
    assert "posted to the log" not in body  # no token is not a Slack failure: the log has the post


def test_a_frequent_job_that_failed_once_is_left_to_its_next_run_unless_its_key_was_rejected():
    ctx, t = world()
    beat(ctx.store, "poll_approvals", at(2), status="error", error="Terminated: stopped by SIGTERM (a redeploy)")
    beat(ctx.store, "poll_replies", at(3), status="error", error="ApiError: instantly HTTP 401 for /api/v2/emails: x")
    check(ctx)
    body = text(t)
    assert "Terminated" not in body and "poll_approvals" not in body
    assert "*Instantly turned down our key* (checking for replies): “" in body
    assert "put a new one in `US_OUTBOUND_INSTANTLY_API_KEY` under Variables in Railway" in body


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
    assert body.count("The jobs have stopped") == 1 and "failed (" not in body
    assert ("• :rotating_light: *The jobs have stopped (since Mon 11:50 UK)*. The settings sheet has errors on the "
            f"<{SHEET}|Copy tab> and <{SHEET}|General tab>, so every job but the safety jobs refuses to start (the "
            "safety jobs run on a fallback that only pauses).") in body
    assert "   2. Run `railway ssh -- us-outbound sync` to read the sheet now." in body
    assert "each time the worker starts, and at 02:00 UK daily and 11:30 UK on weekdays" in body
    assert "Each job tries again" not in body  # it does not, until the sheet is fixed


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


def test_unusable_settings_show_the_first_errors_and_point_to_the_sync_s_own_message():
    ctx, t = world()
    beat(ctx.store, "settings_sync", at(600), detail={"rejected": ["Industries"], "unusable": ["Industries"],
                                                      "errors": 2, "alerted": True})
    beat(ctx.store, "enrol", at(10), status="error", detail={"unusable": ["Industries"]},
         error="settings are unusable (Industries); fix the sheet, then run `us-outbound settings sync`")
    check(ctx)
    body = text(t)
    assert f"on the <{SHEET}|Industries tab>, so every job but the safety jobs refuses to start (the safety jobs run on a fallback that only pauses)." in body
    assert "   The first error:\n   ◦ Industries row 1:" in body  # read from the saved version: no logs needed
    assert ("   Every error on the sheet is listed in the settings sync's message of Mon 02:00 UK (“The settings sheet "
            "has errors…”).") in body
    assert f"   1. Correct this on the <{SHEET}|Industries tab>." in body


def test_a_post_a_job_could_not_make_is_told_with_its_slack_error():
    ctx, t = world()
    beat(ctx.store, "daily_post", at(30), detail={"alert": {"posted": False, "error": "channel_not_found"}})
    check(ctx)
    assert ("• *The daily summary* (Mon 11:30 UK) could not post its message to Slack: “channel_not_found”"
            in text(t))


def test_an_outage_a_frequent_job_meets_twice_running_is_posted_in_plain_words():
    """14 Oct 2026: one Instantly 502 in the hourly kill_rules run reached Slack, quoting the error page's HTML."""
    page = "ApiError: instantly HTTP 502 for /api/v2/accounts/warmup-analytics: <!DOCTYPE html> <html lang=\"en\">"
    ctx, t = world()
    beat(ctx.store, "kill_rules", at(65), detail={"vitals_error": page}, run_id="before")
    beat(ctx.store, "kill_rules", at(5), detail={"vitals_error": page})
    check(ctx)
    body = text(t)
    assert ("• *Checking bounce and complaint limits* (Mon 11:55 UK) could not read the mailboxes' health from "
            "Instantly: Instantly's server had an error (HTTP 502), a problem on their side that usually clears by "
            "itself") in body
    assert "DOCTYPE" not in body

    ctx, t = world()  # the run before went through: nothing to post
    beat(ctx.store, "kill_rules", at(65), detail={"vitals_error": None}, run_id="before")
    beat(ctx.store, "kill_rules", at(5), detail={"vitals_error": page})
    assert check(ctx)["posted"] == [] and not t.requests


# -- the per-item contract and the failure-rate rule (W1-1, 19 Oct 2026) -----------------------------------------


@pytest.mark.parametrize("detail, found", [
    # 25 of 25 HubSpot checks failed inside a run that exited 0: 25 >= 3 and 25 >= 0.5 x 25.
    ({"candidates": 25, "failures": {"HubSpot check failed": 25}}, True),
    ({"candidates": 25, "failures": {"HubSpot check failed": 2}}, False),  # under FAILURES_MIN (3)
    ({"candidates": 4, "failures": {"x": 3}}, True),  # 3 >= 3 and 3 >= 0.5 x 4 = 2
    ({"candidates": 6, "failures": {"x": 3}}, True),  # exactly half: 3 >= 0.5 x 6 = 3 (at least half, not more)
    ({"candidates": 25, "failures": {"x": 12}}, False),  # 12 < 0.5 x 25 = 12.5
    ({"candidates": 25, "failures": {"x": 13}}, True),
])
def test_one_reason_failing_enough_items_is_a_finding(detail, found):
    got = [f for f in health.findings("enrol", {"status": "ok", "started_at": MON_NOON, "detail": detail})
           if f.kind == health.FAILURES]
    assert bool(got) is found
    if found:
        assert got[0].count == next(iter(detail["failures"].values()))


def test_the_per_item_failures_are_posted_once_a_day():
    ctx, t = world()
    beat(ctx.store, "enrol", at(60), detail={"enrolled": 0, "candidates": 25, "failures": {"HubSpot check failed": 25}})
    assert check(ctx)["posted"] == ["job_error:enrol:failures:2026-10-26"]
    assert "HubSpot check failed (25 of 25)" in text(t)  # the reason and its count; the wording is not pinned


def test_a_text_under_a_key_named_error_is_a_finding_but_not_a_post_result_or_a_dry_run_note():
    """The hand-check tab's pass inside poll_approvals put its failure under "error" (hand_check_tab_failed 208 times
    in 17 hours, 19 Oct 2026) and nothing read it."""
    run = {"status": "ok", "started_at": MON_NOON}
    bad_range = "ApiError: sheets HTTP 400 for /values:batchUpdate: bad range"
    hit = health.findings("poll_approvals", {**run, "detail": {"hand_check_tab": {"applied": [], "error": bad_range}}})
    assert [(f.kind, f.texts) for f in hit] == [("error", [bad_range])]
    assert [f.kind for f in health.findings("x", {**run, "detail": {
        "alert": {"posted": False, "error": "unknown outcome: ReadTimeout"}}})] == ["slack_post"]  # read as a post
    for detail in ({"definitions": {"error": "dry-run: no model asked"}}, {"error": None}, {"error": ""}):
        assert health.findings("x", {**run, "detail": detail}) == []


def _readback_runs(store, failed_at: tuple[int, ...]):
    """hubspot_readback every 15 minutes: 12 runs, 11:45 back to 09:00, failing at the given positions."""
    for i in range(12):
        failed = i in failed_at
        beat(store, "hubspot_readback", at(15 * (i + 1)), status="error" if failed else "ok",
             error="ApiError: hubspot HTTP 502 for /crm/v3/objects/meetings/search" if failed else None,
             run_id=f"hr-{i}")


def test_a_frequent_job_failing_3_of_its_last_12_runs_is_posted_once_a_day():
    """B13: each failure of a job that runs every 15 minutes waits for the next run (a transient error, or missed if
    it keeps failing), so one failing a run in four was never told. 3 errors in its last 12 runs is a finding; 2 is
    not. The latest run went through, so nothing else is told."""
    ctx, t = world()
    _readback_runs(ctx.store, (1, 5, 9))
    assert check(ctx)["posted"] == ["job_error:hubspot_readback:failure_rate:2026-10-26"]
    finding = health.failure_rates(ctx, JOBS)[0]
    assert (finding.job, finding.count, finding.at) == ("hubspot_readback", 3, at(30))  # the latest error: 11:30
    assert "3 of its last 12 runs" in text(t)
    assert check(ctx)["posted"] == []  # once a day
    ctx2, t2 = world()
    _readback_runs(ctx2.store, (1, 5))
    assert check(ctx2)["posted"] == [] and not posts(t2)


def test_a_daily_job_is_not_held_to_the_failure_rate_rule():
    """The rule is for frequent jobs (EXPECTED under a day): mailbox_health runs once a day and is told on its first
    failure instead (failed). Here it was run 12 times in the last 24 hours (by hand, every 2 hours, all inside
    LOOKBACK's 26), 3 of them errors: a frequent job with that record is a finding (3 >= RATE_ERRORS in 12 runs);
    this daily one is not."""
    from us_outbound.ops.health import DAILY, EXPECTED

    assert EXPECTED["mailbox_health"] >= DAILY
    ctx, _ = world()
    for i in range(12):
        beat(ctx.store, "mailbox_health", at(120 * i + 30), status="error" if i in (1, 5, 9) else "ok",
             error="ApiError: instantly HTTP 502" if i in (1, 5, 9) else None, run_id=f"mh-{i}")
    assert health.failure_rates(ctx, ["mailbox_health"]) == []
    assert health.failure_rates(ctx, JOBS) == []


def test_unusable_settings_are_not_told_again_as_each_job_s_failure_rate():
    ctx, t = world()
    for i in range(4):
        beat(ctx.store, "poll_replies", at(15 * (i + 1)), status="error", detail={"unusable": ["Copy"]},
             error="settings are unusable (Copy); fix the sheet, then run `us-outbound settings sync`", run_id=f"p{i}")
    assert health.failure_rates(ctx, JOBS) == []
