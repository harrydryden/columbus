"""`us-outbound golive` (ops/golive.py): Harry's read-only go/no-go check before the first sends."""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from tests.fakes import FakeTransport, make_context
from tests.test_registry import FakeInstantly, StubSheets, slack_routes, warm_account
from tests.test_render import COPY, MAILBOXES, account, contact, make_settings
from us_outbound.context import Secrets
from us_outbound.ops import bootstrap, cli, golive
from us_outbound.ops import schedule as schedule_
from us_outbound.registry import mailboxes as reg

NOW = datetime(2026, 10, 5, 7, 30, tzinfo=UTC)  # Mon 5 Oct 2026, 08:30 UK: the pilot's first morning
WEEK = "2026-W41"


class Factory:
    """A golive context over one store and transport, as bootstrap.build_context would give it."""

    def __init__(self, settings, *, instantly_accounts=None):
        self.settings = settings
        self.transport = slack_routes(FakeTransport())
        self.instantly = FakeInstantly(self.transport, instantly_accounts)
        self.ctx = make_context(settings, transport=self.transport, now=NOW)
        self.ctx.clients.sheets = StubSheets(self.ctx.guard, {})
        self.calls = []

    def __call__(self, job, live_flag, operator=False):
        self.calls.append((job, live_flag, operator))
        return self.ctx


def default_settings():
    from us_outbound.settings.defaults import default_tabs
    from us_outbound.settings.validate import validate_all

    settings, errors = validate_all(default_tabs())
    assert not any(errors.values())
    return settings


def lines_of(out: str) -> dict[str, str]:
    """check name -> its PASS/WARN/FAIL line."""
    found = {}
    for line in out.splitlines():
        head = line[:5].strip()
        if head in ("PASS", "WARN", "FAIL"):
            found[line[6:].split(":", 1)[0]] = line
    return found


def test_golive_against_the_default_settings_is_a_no_go(capsys):
    f = Factory(default_settings())
    assert cli.main(["golive"], context_factory=f) == 1
    out = capsys.readouterr().out
    assert f.calls == [("golive", False, False)]  # dry-run; not an operator command
    report = [line for line in out.splitlines() if not line.startswith('{"event"')]  # the log comes first
    assert report[0] == "Go-live check, Mon 05 Oct 2026 08:30 UK (read-only: nothing was changed)"
    got = lines_of(out)
    assert got["Settings"].startswith("PASS")
    assert got["Copy"].startswith("FAIL  Copy: 28 of 28 active industries have no approved copy")  # Legal on
    [fintech] = [line for line in out.splitlines() if line.strip().startswith("Fintech: nothing sendable for")]
    # One line per copy role: "HR manager" contacts get the People leader copy (the Roles tab's copy_role).
    assert all(r in fintech for r in ("People leader", "Founder or executive", "Operations"))
    assert "HR manager" not in fintech
    assert all(f"(fintech-{v}-v1 is a draft" in fintech or f"; fintech-{v}-v1 is a draft" in fintech
               for v in ("people", "founder", "ops"))
    assert got["Mailboxes"] == "FAIL  Mailboxes: no Active mailbox: nothing can send"
    assert "hannah@meetspill.org: Warming, not on a sending list" in out
    assert got["live_sending"].startswith("FAIL  live_sending: no")
    assert got["Approvers"].startswith("FAIL  Approvers: approver_slack_ids is blank")
    assert got["Slack"] == "PASS  Slack: token set; the bot can see #us-outbound"
    assert got["Campaigns"].startswith("FAIL  Campaigns: 3 missing")
    assert got["Apollo budget"].startswith("PASS  Apollo budget: 2,000 of 2,000 credits left this month")
    assert got["Queue"] == "FAIL  Queue: no verified account has a sendable contact"
    assert got["Hand-check"].startswith(f"FAIL  Hand-check: this week's hand-check ({WEEK}) has not been posted")
    assert got["Enrollment"].startswith("PASS")
    assert got["Jobs"].startswith("PASS")  # every job a live send needs is built and scheduled
    assert got["clay_verification"].startswith("WARN  clay_verification: 'skip'")  # the pilot (Harry, 1 Oct 2026)
    assert got["Opt-out tested"].startswith("FAIL  Opt-out tested: seed-inbox test of {{unsubscribe}} not done")
    assert out.splitlines()[-1].startswith("NO-GO: 8 FAIL, 1 WARN, 5 PASS.")
    # Read-only: no write was attempted anywhere.
    assert f.ctx.guard.writes() == [] and f.ctx.store.select("heartbeats") == []


def ready_world(monkeypatch):
    """Everything in place for the first sends, as Harry should find it on Monday."""
    g = dict(live_sending=True, approver_slack_ids=("U_HARRY",), optout_tested=True)
    settings = make_settings(copy=COPY, **g)
    f = Factory(settings, instantly_accounts={m.address: warm_account(m.address) for m in MAILBOXES})
    owners = {"Hannah Spalding": ["hannah@meetspill.org"], "Sam Jackson": ["sam@meetspill.org"],
              "Harry Dryden": ["harry@meetspill.org", "harry@tryspill.org"]}
    for owner, boxes in owners.items():  # created by `campaigns ensure --live`, at the ramp's limits
        f.instantly.standard(reg.campaign_name(owner), boxes, 10 * len(boxes))
    st = f.ctx.store
    accounts = [account(account_id=f"acc-{i}", domain=f"a{i}.com") for i in range(40)]
    st.insert("accounts", accounts)
    st.insert("contacts", [contact(contact_id=f"con-{i}", account_id=f"acc-{i}", email=f"jane@a{i}.com") for i in range(40)])
    st.insert("hitl_items", [{"item_id": f"hand_check-{WEEK}", "kind": "hand_check", "status": "handled",
                              "created_at": NOW - timedelta(hours=1), "payload": {"iso_week": WEEK, "pulled_account_ids": []}}])
    for job in ("sync_outcomes", "poll_replies", "poll_approvals"):
        monkeypatch.setitem(cli.JOBS, job, "tests.test_golive:noop")
    monkeypatch.setattr(schedule_, "SCHEDULE", tuple(dataclasses.replace(j, enabled=True) if j.cron else j
                                                     for j in schedule_.SCHEDULE))
    return f


def noop(ctx):
    return {}


def test_golive_is_a_go_when_every_blocker_is_cleared(monkeypatch, capsys):
    f = ready_world(monkeypatch)
    assert cli.main(["golive"], context_factory=f) == 0
    out = capsys.readouterr().out
    got = lines_of(out)
    assert not [line for line in got.values() if line.startswith("FAIL")], out
    assert got["Mailboxes"] == "PASS  Mailboxes: 4 of 4 Active and warm; 40 sends a day today on the ramp"
    assert ("hannah@meetspill.org: Active and warm (score 98); ramp week 1 (no send yet): 10 a day of its 30; "
            "20 from a week after its first send") in out
    assert got["Campaigns"].startswith("PASS  Campaigns: all 3 exist and match")
    # Agencies have their own row; the other active industries use the General row.
    assert got["Copy"].startswith("WARN  Copy: 2 of 5 active industries have their own approved copy; 3 use the General row")
    assert "Fintech: falls back to General (general-v1) for People leader, Founder or executive, Operations" in out
    assert got["Queue"].startswith("PASS  Queue: 40 ready (verified with a sendable contact)")
    assert got["Hand-check"] == f"PASS  Hand-check: {WEEK} approved"
    assert got["clay_verification"].startswith("WARN  clay_verification: 'skip'")  # the pilot (Harry, 1 Oct 2026)
    assert out.splitlines()[-1].startswith("GO: 0 FAIL, 2 WARN")


def test_campaigns_still_at_30_a_day_drift_from_the_ramp(monkeypatch, capsys):
    f = ready_world(monkeypatch)
    f.instantly.by_name("US Outbound – Hannah Spalding")["daily_limit"] = 30
    assert cli.main(["golive"], context_factory=f) == 1
    out = capsys.readouterr().out
    assert lines_of(out)["Campaigns"].startswith("FAIL  Campaigns: 1 drifted; run `us-outbound campaigns ensure --fix --live`")
    assert "US Outbound – Hannah Spalding: daily_limit 30, expected 10" in out


def test_an_active_mailbox_that_is_not_warm_fails(monkeypatch, capsys):
    f = ready_world(monkeypatch)
    f.instantly.accounts["sam@meetspill.org"].update(warmup_status=0)
    assert cli.main(["golive"], context_factory=f) == 1
    out = capsys.readouterr().out
    assert lines_of(out)["Mailboxes"].startswith("FAIL  Mailboxes: 3 of 4 Active and warm")
    assert "sam@meetspill.org: Active on the sheet, but Instantly does not show it warm (warmup off" in out


def test_no_slack_token_and_no_instantly_key_fail_with_the_reason(capsys):
    f = Factory(default_settings())
    f.ctx.clients.secrets = Secrets(f.ctx.guard, fetch=lambda name: "")
    assert cli.main(["golive"], context_factory=f) == 1
    got = lines_of(capsys.readouterr().out)
    assert got["Slack"].startswith("FAIL  Slack: US_OUTBOUND_SLACK_BOT_TOKEN is not set")
    assert got["Mailboxes"].startswith("FAIL  Mailboxes: cannot read Instantly: set US_OUTBOUND_INSTANTLY_API_KEY")
    assert got["Campaigns"].startswith("FAIL  Campaigns: cannot read Instantly")


def test_unusable_settings_are_a_fail(capsys):
    def factory(job, live_flag, operator=False):
        raise bootstrap.SettingsUnusable({"General": ["General row 3, value: must be yes or no"]})

    assert cli.main(["golive"], context_factory=factory) == 1
    out = capsys.readouterr().out
    assert "FAIL  Settings: unusable: fix the sheet, then `us-outbound settings sync`" in out
    assert "General: General row 3, value: must be yes or no" in out and "NO-GO: 1 FAIL" in out


@pytest.mark.parametrize("value, status", [(True, "PASS"), ("yes", "PASS"), ("no", "WARN"), (False, "WARN")])
def test_clay_verification_when_the_build_has_it(value, status):
    ctx = SimpleNamespace(settings=SimpleNamespace(general=SimpleNamespace(clay_verification=value)))
    assert golive.check_clay_verification(ctx).status == status


def test_the_optout_key_is_on_the_general_tab():
    from us_outbound.settings.defaults import default_tabs

    row = next(r for r in default_tabs()["General"] if r["key"] == "optout_tested")
    assert row["value"] == "no" and "seed inbox" in row["note"]
    assert default_settings().general.optout_tested is False


def test_golive_parses():
    assert cli.build_parser().parse_args(["golive"]).command == "golive"
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["golive", "--live"])
