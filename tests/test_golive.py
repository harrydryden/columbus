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
    assert got["Copy"].endswith("approve rows on the Copy tab (status approved, approved_by), then `us-outbound sync`")
    assert "… and 22 more industries like these" in out  # the detail stops at six industries
    [tech] = [line for line in out.splitlines() if line.strip().startswith("Technology & Startups: nothing sendable")]
    # One line per copy role: "HR manager" contacts get the People leader copy (the Roles tab's copy_role).
    assert all(r in tech for r in ("People leader", "Founder or executive", "Operations"))
    assert "HR manager" not in tech
    assert all(f"technology-startups-{v}-v1 is a draft" in tech for v in ("people", "founder", "ops"))
    assert got["Mailboxes"] == "FAIL  Mailboxes: no Active mailbox: nothing can send"
    assert "hannah@meetspill.org: Warming, not on a sending list" in out
    # Every FAIL that asks for a sheet edit says to sync it in (jobs read the synced copy of the sheet).
    assert got["live_sending"] == ("FAIL  live_sending: no: set live_sending = yes on the General tab once Harry signs "
                                   "off, then `us-outbound sync`")
    assert all(line.endswith(", then `us-outbound sync`") for name, line in got.items()
               if line.startswith("FAIL") and "tab" in line), got
    assert got["Approvers"] == ("FAIL  Approvers: approver_slack_ids is blank: nobody can approve an email or a reply "
                                "in Slack; set it on the General tab, then `us-outbound sync`")
    assert got["Slack"] == "PASS  Slack: token set; the bot can see #us-outbound"
    assert got["Campaigns"].startswith("FAIL  Campaigns: no campaign yet: every owner waits for a warm mailbox")
    assert got["Apollo budget"].startswith("PASS  Apollo budget: 2,000 of 2,000 credits left this month")
    assert got["Queue"] == "FAIL  Queue: no verified account has a sendable contact"
    # auto_send = no by default (Harry, 2 Oct 2026): every email waits for approval, so no hand-check line.
    assert got["auto_send"] == "PASS  auto_send: auto_send = no: every email waits for approval in Slack"
    assert "Hand-check" not in got
    # The second-contact switch, off by default (Harry, 6 Oct 2026): a line for information.
    assert got["Second contact"] == ("PASS  Second contact: off (General second_contact = no): one person per "
                                     "company.")
    assert got["Enrollment"].startswith("PASS")
    assert got["Jobs"].startswith("PASS")  # every job a live send needs is built and scheduled
    assert got["clay_verification"] == "PASS  clay_verification: skip: accounts are verified on Apollo data and HubSpot"
    assert got["HubSpot ids"] == ("WARN  HubSpot ids: hubspot_pipeline_id, hubspot_deal_stage_id, hubspot_owner_id "
                                  "blank: a positive reply creates no HubSpot deal; run `us-outbound hubspot ids` and "
                                  "paste them on the General tab, then `us-outbound sync`")
    assert got["Opt-out tested"].startswith("FAIL  Opt-out tested: seed-inbox test of the unsubscribe link not done")
    assert got["Opt-out tested"].endswith("set optout_tested = yes on the General tab, then `us-outbound sync`")
    assert out.splitlines()[-1] == ("NO-GO: 7 FAIL, 1 WARN, 8 PASS. Fix every FAIL, then run `us-outbound golive` "
                                    "again.")
    assert not [line for line in report if "SPEC" in line]  # plain words for Harry
    # Read-only: no write was attempted anywhere.
    assert f.ctx.guard.writes() == [] and f.ctx.store.select("heartbeats") == []


def ready_world(monkeypatch):
    """Everything in place for the first sends, as Harry should find it on Monday."""
    g = dict(live_sending=True, approver_slack_ids=("U_HARRY",), optout_tested=True,
             hubspot_pipeline_id="pipe-spill3", hubspot_deal_stage_id="stage-first")
    settings = make_settings(copy=COPY, **g)
    f = Factory(settings, instantly_accounts={m.address: warm_account(m.address) for m in MAILBOXES})
    owners = {"Hannah Spalding": ["hannah@meetspill.org"], "Sam Jackson": ["sam@meetspill.org"],
              "Harry Dryden": ["harry@meetspill.org", "harry@tryspill.org"]}
    for owner, boxes in owners.items():  # created by `campaigns ensure --live`, at the ramp's limits, and
        f.instantly.standard(reg.campaign_name(owner), boxes, 10 * len(boxes), status=1)  # activated by start --live
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
    assert got["Campaigns"] == "PASS  Campaigns: all 3 exist, match and are active"
    # Agencies have their own row; the other active industries use the General row.
    assert got["Copy"].startswith("WARN  Copy: 2 of 5 active industries have their own approved copy; 3 use the General row")
    assert "Fintech: falls back to General (general-v1) for People leader, Founder or executive, Operations" in out
    assert got["Queue"].startswith("PASS  Queue: 40 ready (verified with a sendable contact)")
    assert got["auto_send"] == "PASS  auto_send: auto_send = no: every email waits for approval in Slack"
    assert "Hand-check" not in got  # every email is approved in Slack
    assert got["clay_verification"] == "PASS  clay_verification: skip: accounts are verified on Apollo data and HubSpot"
    assert got["HubSpot ids"] == "PASS  HubSpot ids: pipeline, deal stage and owner set: a positive reply creates a deal"
    assert out.splitlines()[-1] == ("GO: 0 FAIL, 1 WARN, 15 PASS. Next: `us-outbound start --live`; cards arrive in "
                                    "#us-outbound after enrol at 12:00 UK.")


def test_with_auto_send_on_the_hand_check_is_needed_and_the_switch_warns(monkeypatch, capsys):
    f = ready_world(monkeypatch)
    f.ctx.settings = dataclasses.replace(f.settings, general=dataclasses.replace(f.settings.general, auto_send=True))
    assert cli.main(["golive"], context_factory=f) == 0
    got = lines_of(capsys.readouterr().out)
    assert got["auto_send"] == "WARN  auto_send: auto_send = yes: emails are added without approval"
    assert got["Hand-check"] == f"PASS  Hand-check: {WEEK} approved"
    f.ctx.store.update("hitl_items", {"item_id": f"hand_check-{WEEK}"}, {"status": "open"})
    assert cli.main(["golive"], context_factory=f) == 1
    out = capsys.readouterr().out
    assert lines_of(out)["Hand-check"].startswith(f"FAIL  Hand-check: this week's hand-check ({WEEK}) is not approved yet")


def test_campaigns_still_at_30_a_day_drift_from_the_ramp(monkeypatch, capsys):
    f = ready_world(monkeypatch)
    f.instantly.by_name("US Outbound – Hannah Spalding")["daily_limit"] = 30
    assert cli.main(["golive"], context_factory=f) == 1
    out = capsys.readouterr().out
    assert lines_of(out)["Campaigns"].startswith("FAIL  Campaigns: 1 drifted; run `us-outbound campaigns ensure --fix --live`")
    assert "US Outbound – Hannah Spalding: daily_limit 30, expected 10" in out


def test_an_owner_whose_mailboxes_are_still_warming_waits_without_blocking(monkeypatch, capsys):
    # 2 Oct 2026: Hannah's and Sam's mailboxes were warm, Harry's two were not, so his campaign could not be
    # created yet. `start` skips him, and golive warns instead of failing.
    f = ready_world(monkeypatch)
    harry = {"harry@meetspill.org", "harry@tryspill.org"}
    f.ctx.settings = dataclasses.replace(f.ctx.settings, mailboxes=tuple(
        dataclasses.replace(m, status="Warming") if m.address in harry else m for m in f.ctx.settings.mailboxes))
    del f.instantly.campaigns[f.instantly.by_name("US Outbound – Harry Dryden")["id"]]
    assert cli.main(["golive"], context_factory=f) == 0
    out = capsys.readouterr().out
    assert lines_of(out)["Campaigns"] == "WARN  Campaigns: 2 exist and match; 1 waits for a warm mailbox"
    assert "US Outbound – Harry Dryden: waits for a warm mailbox" in out


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
    assert "FAIL  Settings: unusable: fix the sheet, then `us-outbound sync`" in out
    assert "General: General row 3, value: must be yes or no" in out and "NO-GO: 1 FAIL" in out


@pytest.mark.parametrize("value, line", [
    ("skip", "PASS  clay_verification: skip: accounts are verified on Apollo data and HubSpot"),
    ("required", "WARN  clay_verification: required: accounts wait for Clay's verification, which is not built yet, so "
                 "no new account is verified; set it to skip on the General tab, then `us-outbound sync`"),
])
def test_clay_verification(value, line):
    ctx = SimpleNamespace(settings=SimpleNamespace(general=SimpleNamespace(clay_verification=value)))
    c = golive.check_clay_verification(ctx)
    assert f"{c.status:<5} {c.name}: {c.reason}" == line


def test_the_optout_key_is_on_the_general_tab():
    from us_outbound.settings.defaults import default_tabs

    row = next(r for r in default_tabs()["General"] if r["key"] == "optout_tested")
    assert row["value"] == "no" and "seed inbox" in row["note"]
    assert default_settings().general.optout_tested is False


def test_golive_parses():
    assert cli.build_parser().parse_args(["golive"]).command == "golive"
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["golive", "--live"])


# -- the lines Harry reads before Monday (4 Oct 2026) -----------------------------------------------------------------


def with_general(f, **values):
    f.ctx.settings = dataclasses.replace(f.ctx.settings, general=dataclasses.replace(f.ctx.settings.general, **values))


def test_live_sending_yes_needs_every_sending_owner_s_campaign_active(monkeypatch, capsys):
    f = ready_world(monkeypatch)
    f.instantly.by_name("US Outbound – Hannah Spalding")["status"] = 2  # paused, e.g. after `stop --live`
    assert cli.main(["golive"], context_factory=f) == 1
    out = capsys.readouterr().out
    assert lines_of(out)["Campaigns"] == ("FAIL  Campaigns: 1 not active in Instantly while live_sending = yes, so enrol "
                                          "gives its owner nothing to send: run `us-outbound start --live`")
    assert "US Outbound – Hannah Spalding: matches (paused); not sending: `us-outbound start --live` activates it" in out


def test_with_live_sending_no_a_paused_campaign_is_detail_only(monkeypatch, capsys):
    f = ready_world(monkeypatch)
    for c in f.instantly.campaigns.values():
        c["status"] = 2
    with_general(f, live_sending=False)
    cli.main(["golive"], context_factory=f)
    out = capsys.readouterr().out
    assert lines_of(out)["Campaigns"] == ("PASS  Campaigns: all 3 exist and match; `us-outbound start --live` "
                                          "activates them")
    assert "US Outbound – Sam Jackson: matches (paused); not sending: `us-outbound start --live` activates it" in out


def test_live_sending_no_does_not_stop_an_active_campaign(monkeypatch, capsys):
    f = ready_world(monkeypatch)  # every campaign active
    with_general(f, live_sending=False)
    assert cli.main(["golive"], context_factory=f) == 1  # live_sending FAILs
    got = lines_of(capsys.readouterr().out)
    assert got["Campaigns"] == ("WARN  Campaigns: 3 exist and match; 3 active in Instantly; live_sending = no does not "
                                "stop Instantly: to stop sending run `us-outbound stop --live`")


def test_the_go_footer_with_auto_send_on_says_leads_go_straight_to_instantly(monkeypatch, capsys):
    f = ready_world(monkeypatch)
    with_general(f, auto_send=True)
    assert cli.main(["golive"], context_factory=f) == 0
    assert capsys.readouterr().out.splitlines()[-1].endswith(
        "Next: `us-outbound start --live`; enrol adds the day's leads to Instantly at 12:00 UK.")


def test_the_queue_counts_the_cards_waiting_for_a_tick_as_enrol_does(monkeypatch, capsys):
    f = ready_world(monkeypatch)
    later = (NOW + timedelta(days=1)).date().isoformat()
    f.ctx.store.insert("hitl_items", [
        {"item_id": f"sa-{i}", "kind": "send_approval", "status": "open", "account_id": f"acc-{i}",
         "contact_id": f"con-{i}", "created_at": NOW - timedelta(hours=20),
         "payload": {"state": "waiting", "owner": "Hannah Spalding", "send_day": "2026-10-02", "expires_on": later}}
        for i in range(2)])
    assert cli.main(["golive"], context_factory=f) == 0
    queue = lines_of(capsys.readouterr().out)["Queue"]
    assert queue.startswith("PASS  Queue: 38 ready (verified with a sendable contact)")  # their accounts wait
    assert queue.endswith("; 2 cards wait for a ✅")


def test_hubspot_ids_warn_until_they_are_pasted(monkeypatch, capsys):
    f = ready_world(monkeypatch)
    with_general(f, hubspot_deal_stage_id="")
    cli.main(["golive"], context_factory=f)
    assert lines_of(capsys.readouterr().out)["HubSpot ids"].startswith(
        "WARN  HubSpot ids: hubspot_deal_stage_id blank: a positive reply creates no HubSpot deal")
