"""The three go-live checks `us-outbound status` keeps (ops/live_checks.py; W2-1, 20 Oct 2026): Copy coverage, the
campaigns, the watchdog. They were ops/golive.py's; these are its tests of them, run through status and directly."""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta

from tests.fakes import FakeTransport, make_context
from tests.test_registry import FakeInstantly, StubSheets, slack_routes, warm_account
from tests.test_render import COPY, MAILBOXES, account, contact, make_settings
from us_outbound.context import Secrets
from us_outbound.ops import cli, live_checks
from us_outbound.registry import mailboxes as reg

NOW = datetime(2026, 10, 5, 7, 30, tzinfo=UTC)  # Mon 5 Oct 2026, 08:30 UK: the pilot's first morning
WEEK = "2026-W41"
PING = "https://hc-ping.com/5a1b2c3d-golive"  # US_OUTBOUND_WATCHDOG_URL (ops/watchdog.py)
HARRY = "US Outbound – Harry Dryden"


def secrets_with(guard, watchdog=""):
    """The test keys, and the watchdog's ping URL as given ("" = unset)."""
    return Secrets(guard, fetch=lambda n: watchdog if n == "US_OUTBOUND_WATCHDOG_URL" else f"test-{n}")


class Factory:
    """A status context over one store and transport, as bootstrap.build_context would give it."""

    def __init__(self, settings, *, instantly_accounts=None):
        self.settings = settings
        self.transport = slack_routes(FakeTransport())
        self.instantly = FakeInstantly(self.transport, instantly_accounts)
        self.ctx = make_context(settings, transport=self.transport, now=NOW)
        self.ctx.clients.sheets = StubSheets(self.ctx.guard, {})
        self.ctx.clients.secrets = secrets_with(self.ctx.guard)  # no outside watchdog until a test sets one

    def __call__(self, job, live_flag, operator=False):
        return self.ctx


def default_settings():
    from us_outbound.settings.defaults import default_tabs
    from us_outbound.settings.validate import validate_all

    settings, errors = validate_all(default_tabs())
    assert not any(errors.values())
    return settings


def ready_world():
    """Everything in place for sending, as Harry should find it on a Monday."""
    g = dict(live_sending=True, approver_slack_ids=("U_HARRY",), optout_tested=True,
             hubspot_pipeline_id="pipe-spill3", hubspot_deal_stage_id="stage-first")
    f = Factory(make_settings(copy=COPY, **g),
                instantly_accounts={m.address: warm_account(m.address) for m in MAILBOXES})
    owners = {"Hannah Spalding": ["hannah@meetspill.org"], "Sam Jackson": ["sam@meetspill.org"],
              "Harry Dryden": ["harry@meetspill.org", "harry@tryspill.org"]}
    for owner, boxes in owners.items():  # created by `campaigns ensure --live`, at the ramp's limits, and
        f.instantly.standard(reg.campaign_name(owner), boxes, 10 * len(boxes), status=1)  # activated by start --live
    st = f.ctx.store
    st.insert("accounts", [account(account_id=f"acc-{i}", domain=f"a{i}.com") for i in range(40)])
    st.insert("contacts", [contact(contact_id=f"con-{i}", account_id=f"acc-{i}", email=f"jane@a{i}.com")
                           for i in range(40)])
    st.insert("hitl_items", [{"item_id": f"hand_check-{WEEK}", "kind": "hand_check", "status": "handled",
                              "created_at": NOW - timedelta(hours=1), "payload": {"iso_week": WEEK}}])
    f.ctx.clients.secrets = secrets_with(f.ctx.guard, PING)  # Healthchecks.io's check is set up
    return f


def lines_of(checks: list[live_checks.Check]) -> dict[str, str]:
    """check name -> its "STATUS name: reason" line."""
    return {c.name: f"{c.status:<5} {c.name}: {c.reason}" for c in checks}


def with_general(f, **values):
    f.ctx.settings = dataclasses.replace(f.ctx.settings, general=dataclasses.replace(f.ctx.settings.general, **values))


def test_against_the_default_settings_the_three_checks_fail_or_warn():
    f = Factory(default_settings())
    got = lines_of(live_checks.run_checks(f.ctx))
    assert list(got) == ["Copy", "Campaigns", "Watchdog"]
    assert got["Copy"].startswith("FAIL  Copy: 28 of 28 active industries have no approved copy")  # Legal on
    assert got["Copy"].endswith("approve rows on the Copy tab (status approved, approved_by), then `us-outbound sync`")
    assert got["Campaigns"].startswith("FAIL  Campaigns: no campaign yet: every owner waits for a warm mailbox")
    assert got["Watchdog"] == ("WARN  Watchdog: no outside watchdog: set US_OUTBOUND_WATCHDOG_URL "
                               "(docs/railway-setup.md, step h)")
    copy = next(c for c in live_checks.run_checks(f.ctx) if c.name == "Copy")
    assert copy.detail[-1] == "… and 22 more industries like these"  # 28 industries, six shown
    [tech] = [d for d in copy.detail if d.startswith("Technology & Startups: nothing sendable")]
    # One line per copy role: "HR manager" contacts get the People leader copy (the Roles tab's copy_role).
    assert all(r in tech for r in ("People leader", "Founder or executive", "Operations")) and "HR manager" not in tech
    assert f.ctx.guard.writes() == []  # read-only


def test_status_prints_the_three_checks_when_every_blocker_is_cleared(capsys):
    f = ready_world()
    assert cli.main(["status"], context_factory=f) == 0
    out = capsys.readouterr().out.splitlines()
    i = out.index("Go-live checks:")
    # Agencies have their own row; the other four active industries use the General row.
    assert out[i + 1] == ("  WARN  Copy: 2 of 5 active industries have their own approved copy; 3 use the General "
                          "row")
    assert "          Fintech: falls back to General (general-v1) for People leader, Founder or executive, Operations" \
        in out
    assert "  PASS  Campaigns: all 3 exist, match and are active" in out
    assert ("  PASS  Watchdog: set: heartbeat_check pings it every hour, and Healthchecks.io emails if it stops"
            in out)


def test_campaigns_still_at_30_a_day_drift_from_the_ramp():
    f = ready_world()
    f.instantly.by_name("US Outbound – Hannah Spalding")["daily_limit"] = 30
    c = live_checks.check_campaigns(f.ctx)
    assert lines_of([c])["Campaigns"].startswith(
        "FAIL  Campaigns: 1 drifted; run `us-outbound campaigns ensure --fix --live`")
    assert "US Outbound – Hannah Spalding: daily_limit 30, expected 10" in c.detail


def test_an_owner_whose_mailboxes_are_still_warming_waits_without_blocking():
    # 2 Oct 2026: Hannah's and Sam's mailboxes were warm, Harry's two were not, so his campaign could not be
    # created yet. `start` skips him, and the check warns instead of failing.
    f = ready_world()
    harry = {"harry@meetspill.org", "harry@tryspill.org"}
    f.ctx.settings = dataclasses.replace(f.ctx.settings, mailboxes=tuple(
        dataclasses.replace(m, status="Warming") if m.address in harry else m for m in f.ctx.settings.mailboxes))
    del f.instantly.campaigns[f.instantly.by_name(HARRY)["id"]]
    c = live_checks.check_campaigns(f.ctx)
    assert lines_of([c])["Campaigns"] == "WARN  Campaigns: 2 exist and match; 1 waits for a warm mailbox"
    assert f"{HARRY}: waits for a warm mailbox (created by mailbox_health once one is Active)" in c.detail


def test_live_sending_yes_needs_every_sending_owner_s_campaign_active():
    f = ready_world()
    f.instantly.by_name("US Outbound – Hannah Spalding")["status"] = 2  # paused, e.g. after `stop --live`
    c = live_checks.check_campaigns(f.ctx)
    assert lines_of([c])["Campaigns"] == ("FAIL  Campaigns: 1 not active in Instantly while live_sending = yes, so "
                                          "enrol gives its owner nothing to send: run `us-outbound start --live`")
    assert ("US Outbound – Hannah Spalding: matches (paused); not sending: `us-outbound start --live` activates it"
            in c.detail)


def test_with_live_sending_no_a_paused_campaign_is_detail_only():
    f = ready_world()
    for camp in f.instantly.campaigns.values():
        camp["status"] = 2
    with_general(f, live_sending=False)
    c = live_checks.check_campaigns(f.ctx)
    assert lines_of([c])["Campaigns"] == ("PASS  Campaigns: all 3 exist and match; `us-outbound start --live` "
                                          "activates them")


def test_live_sending_no_does_not_stop_an_active_campaign():
    f = ready_world()  # every campaign active
    with_general(f, live_sending=False)
    assert lines_of([live_checks.check_campaigns(f.ctx)])["Campaigns"] == (
        "WARN  Campaigns: 3 exist and match; 3 active in Instantly; live_sending = no does not stop Instantly: to "
        "stop sending run `us-outbound stop --live`")


def test_a_completed_campaign_passes_and_the_next_lead_resumes_it():
    """Instantly marks a campaign with no lead left "completed" (status 3); the next lead added resumes it."""
    f = ready_world()
    f.instantly.by_name(HARRY)["status"] = 3
    c = live_checks.check_campaigns(f.ctx)
    assert c.status == live_checks.PASS
    assert f"{HARRY}: matches (completed); no lead left to email, and the next lead added resumes it" in c.detail


def test_a_watchdog_url_that_is_not_https_warns():
    """Harry, 7 Oct 2026: the ping URL carries the check's secret id, so it is only ever sent over https."""
    f = ready_world()
    f.ctx.clients.secrets = secrets_with(f.ctx.guard, "http://hc-ping.com/5a1b2c3d")
    got = lines_of([live_checks.check_watchdog(f.ctx)])
    assert got["Watchdog"] == ("WARN  Watchdog: US_OUTBOUND_WATCHDOG_URL is not an https ping URL: paste the check's "
                               "ping URL from Healthchecks.io (docs/railway-setup.md, step h)")
    assert "5a1b2c3d" not in str(got)


def test_drift_held_while_leads_are_in_flight_warns_rather_than_fails():
    """Harry, 7 Oct 2026: the steps of a campaign with leads in flight are not rewritten under them, so a code change
    to the step template is held (registry/mailboxes.IN_FLIGHT_KEYS): consistent for those leads, so a WARN."""
    f = ready_world()
    hannah = f.instantly.by_name("US Outbound – Hannah Spalding")
    variant = hannah["sequences"][0]["steps"][0]["variants"][0]
    variant["body"] = variant["body"].replace("Not relevant?", "To stop hearing from us,")
    f.ctx.store.update("contacts", {"contact_id": "con-0"}, {
        "instantly_campaign": "US Outbound – Hannah Spalding", "instantly_lead_id": "L0",
        "enrolled_at": NOW - timedelta(days=2)})
    f.ctx.store.update("accounts", {"account_id": "acc-0"}, {"status": "enrolled", "sender": "Hannah Spalding"})
    before = len(f.ctx.guard.writes())
    c = live_checks.check_campaigns(f.ctx)
    assert lines_of([c])["Campaigns"] == (
        "WARN  Campaigns: 2 exist and match; 1 with drift held while leads are in flight: `us-outbound campaigns ensure "
        "--fix --in-flight --live` applies it to them, or it waits until `us-outbound cohorts in-flight` shows 0")
    assert any(d.startswith("Hannah Spalding's campaign: its settings in Instantly differ from ours (email 1's "
                            "template), but changing them now would also change the emails of 1 person part-way "
                            "through.") for d in c.detail)
    assert len(f.ctx.guard.writes()) == before  # read-only still


def test_one_broken_check_never_hides_the_others(monkeypatch):
    def boom(ctx):
        raise RuntimeError("the sheet went away")

    monkeypatch.setattr(live_checks, "CHECKS", (("Copy", boom), *live_checks.CHECKS[1:]))
    got = lines_of(live_checks.run_checks(ready_world().ctx))
    assert got["Copy"] == "FAIL  Copy: could not check: RuntimeError: the sheet went away"
    assert got["Campaigns"].startswith("PASS") and got["Watchdog"].startswith("PASS")
