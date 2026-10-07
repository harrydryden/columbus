"""The outside watchdog (ops/watchdog.py; Harry, 7 Oct 2026): heartbeat_check pings Healthchecks.io every hour, and
/fail when a job is missed or failed or Slack cannot take alerts, so a dead worker, database or Slack token still
reaches Harry by email."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from tests.fakes import TEST_SHEET_ID, FakeTransport, make_context
from tests.test_heartbeat import MON_NOON, beat
from us_outbound.clients.db import MemoryStore
from us_outbound.clients.guard import Boundaries, Guard, GuardViolation, Op
from us_outbound.context import Secrets, boundaries_for
from us_outbound.ops import bootstrap, cli, watchdog
from us_outbound.ops import heartbeat as hb
from us_outbound.settings.model import General, Settings

SECRET_ID = "7d1e0c2a-5b6f-4c3d-9e8f-test"
PING = f"https://hc-ping.com/{SECRET_ID}"
SETTINGS = Settings(general=General(approver_slack_ids=("U_HARRY",)))
DSN = "postgresql://us_outbound@db.test:5432/railway"


def slack(t: FakeTransport, *, auth: bool = True, posts: bool = True) -> FakeTransport:
    t.route("GET", "conversations.list", {"ok": True, "channels": [{"id": "C_DEV", "name": "us-outbound-dev"},
                                                                  {"id": "C_ALERT", "name": "us-outbound"}]})
    t.route("GET", "auth.test", {"ok": True, "user_id": "U_BOT"} if auth else {"ok": False, "error": "invalid_auth"})
    t.route("POST", "chat.postMessage",
            fn=lambda r: {"ok": True, "channel": r.json["channel"], "ts": "1.1"} if posts else
            {"ok": False, "error": "invalid_auth"})
    return t


def world(now=MON_NOON, *, live=True, url=PING, auth=True, posts=True, settings=SETTINGS):
    """heartbeat_check's context as bootstrap builds it: the ping URL read through the secrets client into the guard."""
    t = slack(FakeTransport(), auth=auth, posts=posts)
    ctx = make_context(settings, job="heartbeat_check", now=now, transport=t, live=live)
    ctx.clients.secrets = Secrets(ctx.guard, fetch=lambda n: url if n == "US_OUTBOUND_WATCHDOG_URL" else f"test-{n}")
    ping_url = watchdog.read(ctx.clients.secrets)[0]
    ctx.guard.configure(bounds=boundaries_for(settings, TEST_SHEET_ID, job="heartbeat_check", watchdog_url=ping_url))
    return ctx, t


def pings(t: FakeTransport) -> list[str]:
    return [r.url for r in t.requests if "hc-ping.com" in r.url]


def posts(t: FakeTransport) -> list[dict]:
    return [r.json for r in t.requests if r.url.endswith("chat.postMessage")]


# -- the URL ---------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("value, url, why", [
    ("", "", "US_OUTBOUND_WATCHDOG_URL is not set"),
    (f"{PING}/", PING, ""),
    (f"http://hc-ping.com/{SECRET_ID}", "", "US_OUTBOUND_WATCHDOG_URL is not an https ping URL"),
    ("hc-ping.com/abc", "", "US_OUTBOUND_WATCHDOG_URL is not an https ping URL"),
    (f"{PING}?rid=1", "", "US_OUTBOUND_WATCHDOG_URL is not an https ping URL"),
    ("https://user:pw@hc-ping.com/abc", "", "US_OUTBOUND_WATCHDOG_URL is not an https ping URL"),
])
def test_the_url_is_read_through_the_secrets_client(value, url, why):
    guard = Guard()
    assert watchdog.read(Secrets(guard, fetch=lambda n: value)) == (url, why)
    assert [(c.system, c.target) for c in guard.calls] == [("secrets", "US_OUTBOUND_WATCHDOG_URL")]


def test_the_guard_allows_a_get_of_exactly_the_url_and_its_fail_form():
    guard = Guard(live=False, bounds=Boundaries(watchdog_url=PING))
    for url in (PING, PING + "/fail"):
        assert guard.authorize("watchdog", Op("get", target="ok", write=True, detail={"url": url})) is True  # dry-run too
    for op in (Op("get", target="ok", write=True, detail={"url": PING + "/start"}),
               Op("get", target="ok", write=True, detail={"url": "https://hc-ping.com/someone-else"}),
               Op("post", target="ok", write=True, detail={"url": PING})):
        with pytest.raises(GuardViolation) as err:
            guard.authorize("watchdog", op)
        assert SECRET_ID not in str(err.value)
    with pytest.raises(GuardViolation):  # no URL configured: nothing at all
        Guard(bounds=Boundaries()).authorize("watchdog", Op("get", write=True, detail={"url": ""}))


def test_only_heartbeat_check_s_context_reads_the_url():
    env = {"DATABASE_URL": DSN, "US_OUTBOUND_WATCHDOG_URL": PING}

    def build(job):
        return bootstrap.build_context(job, False, env=env, store=MemoryStore(Guard()), transport=FakeTransport(),
                                       load_settings=lambda s: (SETTINGS, {}), google_credentials=object())

    ctx = build("heartbeat_check")
    assert ctx.guard.bounds.watchdog_url == PING and ctx.clients.watchdog.url == PING
    other = build("mailbox_health")
    assert other.guard.bounds.watchdog_url == "" and other.clients.watchdog is None
    assert not [c for c in other.guard.calls if c.target == "US_OUTBOUND_WATCHDOG_URL"]


# -- the hourly ping -------------------------------------------------------------------------------------------


def test_a_healthy_hour_pings_the_url_and_logs_no_secret(capsys):
    ctx, t = world()
    out = hb.run(ctx)
    assert pings(t) == [PING] and out["watchdog"] == {"pinged": True, "fail": False, "status": 200, "reasons": []}
    assert out["missed"] == [] and out["failed"] == [] and out["slack"] == "ok"
    [req] = [r for r in t.requests if "hc-ping.com" in r.url]
    assert req.method == "GET" and not any(k.lower() in ("authorization", "x-api-key") for k in req.headers)
    assert SECRET_ID not in capsys.readouterr().out


def test_a_missed_job_pings_fail_after_the_slack_alert():
    ctx, t = world()
    beat(ctx.store, "poll_replies", MON_NOON - timedelta(minutes=50))  # 4 minutes past its 45
    out = hb.run(ctx)
    assert out["missed"] == ["poll_replies"] and pings(t) == [PING + "/fail"]
    assert out["watchdog"]["reasons"] == ["missed: poll_replies"]
    assert "poll_replies" in posts(t)[0]["text"]


def test_a_daily_job_whose_latest_run_failed_pings_fail_but_a_poll_that_failed_once_does_not():
    eight = MON_NOON - timedelta(hours=4)  # 08:00 UK: today's 07:00 run failed, yesterday's was fine
    ctx, t = world(eight)
    beat(ctx.store, "mailbox_health", eight - timedelta(hours=1), status="error", error="Boom")  # next try: tomorrow
    beat(ctx.store, "mailbox_health", eight - timedelta(hours=25), run_id="yesterday")
    out = hb.run(ctx)
    assert out["missed"] == [] and out["failed"] == ["mailbox_health"] and pings(t) == [PING + "/fail"]

    ctx, t = world()
    beat(ctx.store, "poll_replies", MON_NOON - timedelta(minutes=20), run_id="ok")
    beat(ctx.store, "poll_replies", MON_NOON - timedelta(minutes=5), status="error", run_id="blip", error="Boom")
    out = hb.run(ctx)
    assert out["missed"] == [] and out["failed"] == [] and pings(t) == [PING]  # tried again within 15 minutes


def test_a_monthly_job_s_failure_fails_the_ping_for_a_day_not_a_month():
    """The 1st's lookalikes run failed: /fail that day, then the check is free to say something new."""
    oct_1 = datetime(2026, 10, 1, 1, 30, tzinfo=UTC)
    for hours, want in ((3, [PING + "/fail"]), (27, [PING])):
        ctx, t = world(oct_1 + timedelta(hours=hours))
        beat(ctx.store, "lookalikes", datetime(2026, 9, 1, 1, 30, tzinfo=UTC), run_id="september")
        beat(ctx.store, "lookalikes", oct_1, status="error", run_id="october", error="Boom")
        out = hb.run(ctx)
        assert out["missed"] == [] and pings(t) == want


def test_a_dead_slack_token_still_reaches_harry_through_the_fail_ping(capsys):
    """Slack revoked the token: nothing can be posted, so the /fail ping (an email from Healthchecks) says so."""
    ctx, t = world(auth=False, posts=False)
    beat(ctx.store, "poll_replies", MON_NOON - timedelta(minutes=50))
    out = hb.run(ctx)
    assert out["missed"] == ["poll_replies"] and out["slack"] == "Slack rejected the bot token (invalid_auth)"
    assert pings(t) == [PING + "/fail"]
    assert out["watchdog"]["reasons"] == ["missed: poll_replies", "Slack rejected the bot token (invalid_auth)"]
    assert '"event": "slack_post_failed"' in capsys.readouterr().out  # the missed alert could not go
    ctx, t = world(auth=False, posts=False)  # and with nothing missed, the token alone fails the check
    assert hb.run(ctx)["watchdog"]["fail"] is True and pings(t) == [PING + "/fail"]


def test_slack_out_of_order_is_not_called_a_rejected_token():
    ctx, t = world()
    t.route("GET", "auth.test", {"error": "busy"}, status=503)
    assert hb.run(ctx)["slack"] == "Slack could not check the bot token (busy)" and pings(t) == [PING + "/fail"]


def test_a_live_run_with_no_slack_token_pings_fail():
    ctx, t = world()
    ctx.clients.secrets = Secrets(ctx.guard, fetch=lambda n: "" if "SLACK" in n else PING)
    out = hb.run(ctx)
    assert out["slack"] == "no Slack token (US_OUTBOUND_SLACK_BOT_TOKEN is not set)" and pings(t) == [PING + "/fail"]


def test_dry_run_pings_too_and_its_alerts_go_to_the_dev_channel():
    ctx, t = world(live=False)
    beat(ctx.store, "poll_replies", MON_NOON - timedelta(minutes=50))
    hb.run(ctx)
    assert pings(t) == [PING + "/fail"] and posts(t)[0]["channel"] == "C_DEV"


def test_an_unset_url_is_logged_once_and_skipped(capsys):
    ctx, t = world(url="")
    out = hb.run(ctx)
    assert pings(t) == [] and out["watchdog"] == {"pinged": False, "fail": False,
                                                   "off": "US_OUTBOUND_WATCHDOG_URL is not set"}
    assert capsys.readouterr().out.count('"event": "watchdog_off"') == 1


@pytest.mark.parametrize("answer, error", [
    ({"status": 500}, "Healthchecks answered HTTP 500"),
    ({"fn": "raise"}, "ConnectionError"),
])
def test_a_ping_that_fails_is_logged_and_never_fails_the_job(answer, error, capsys):
    ctx, t = world()
    if "fn" in answer:
        def down(req):
            raise ConnectionError(f"HTTPSConnectionPool(host='hc-ping.com'): Max retries exceeded with url: {req.url}")

        t.route("GET", "hc-ping.com", fn=down)
    else:
        t.route("GET", "hc-ping.com", status=answer["status"])
    out = hb.run_job(ctx, hb.run)
    assert out["watchdog"] == {"pinged": False, "fail": False, "ping_error": error}
    assert ctx.store.get("heartbeats", run_id=ctx.run_id)["status"] == "ok"
    logged = capsys.readouterr().out
    assert '"event": "watchdog_ping_failed"' in logged and SECRET_ID not in logged


def test_a_heartbeat_check_that_cannot_read_the_heartbeats_pings_fail_then_fails(monkeypatch):
    ctx, t = world()

    def broken(store):
        raise RuntimeError("the database is down")

    monkeypatch.setattr(hb, "latest_runs", broken)
    with pytest.raises(RuntimeError):
        hb.run_job(ctx, hb.run)
    assert pings(t) == [PING + "/fail"]
    assert ctx.store.get("heartbeats", run_id=ctx.run_id)["status"] == "error"


# -- unusable settings -----------------------------------------------------------------------------------------


def test_heartbeat_check_runs_on_the_defaults_when_the_settings_are_unusable_and_still_alerts_and_pings():
    """Every other job refuses (and leaves an error heartbeat); heartbeat_check starts on the General defaults, live
    with --live alone, so the alert reaches #us-outbound and the ping says /fail."""
    assert "heartbeat_check" in bootstrap.DEFAULTS_OK and "heartbeat_check" in bootstrap.ALERTS_ON_DEFAULTS
    t = slack(FakeTransport())
    store = MemoryStore(Guard())
    env = {"DATABASE_URL": DSN, "US_OUTBOUND_SLACK_BOT_TOKEN": "xoxb-test", "US_OUTBOUND_WATCHDOG_URL": PING}
    now = datetime.now(UTC)
    store.upsert("heartbeats", [{"run_id": "s1", "job": "settings_sync", "started_at": now - timedelta(minutes=30),
                                 "finished_at": now - timedelta(minutes=30), "status": "error", "dry_run": True,
                                 "detail": {"unusable": ["General"]},
                                 "error": "settings are unusable (General); fix the sheet, then run `us-outbound "
                                          "settings sync`"}])

    def factory(job, live_flag, operator=False):
        return bootstrap.build_context(job, live_flag, operator=operator, env=env, store=store, transport=t,
                                       load_settings=lambda s: (None, {"General": ["row 3: bad"]}),
                                       google_credentials=object())

    with pytest.raises(bootstrap.SettingsUnusable):
        factory("daily_post", True)
    assert factory("heartbeat_check", False).dry_run  # without --live it stays dry
    assert cli.main(["run", "heartbeat_check", "--live"], context_factory=factory) == 0
    [post] = posts(t)
    assert post["channel"] == "C_ALERT"  # live on the defaults: not the dev channel
    assert "The settings are unusable (General): every job refuses to run until the sheet is fixed" in post["text"]
    assert pings(t) == [PING + "/fail"]
    [row] = [r for r in store.select("heartbeats") if r["job"] == "heartbeat_check"]
    assert row["status"] == "ok" and row["dry_run"] is False and row["detail"]["failed"] == ["settings_sync"]
