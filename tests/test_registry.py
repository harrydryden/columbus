"""Mailbox registry and sender campaigns (SPEC 5, 9, 13): add, pause, retire, health, and campaign drift.

FakeInstantly, StubSheets and slack_routes are reused by test_cli.py and test_erase.py.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest

from tests.fakes import TEST_SHEET_ID, FakeTransport, make_context
from us_outbound.clients.guard import Op
from us_outbound.clients.instantly import (
    CAMPAIGN_SETTINGS,
    UNSUBSCRIBE_TAG,
    campaign_settings,
    instantly_schedule,
)
from us_outbound.registry import mailboxes as reg
from us_outbound.settings.defaults import COLUMNS
from us_outbound.settings.model import General, Mailbox, Settings

NOW = datetime(2026, 10, 27, 12, 0, tzinfo=UTC)  # Tuesday
TODAY = date(2026, 10, 27)
HANNAH, SAM, HARRY, HARRY2 = "hannah@meetspill.org", "sam@meetspill.org", "harry@meetspill.org", "harry@tryspill.org"
C_HANNAH, C_SAM, C_HARRY = "US Outbound – Hannah Spalding", "US Outbound – Sam Jackson", "US Outbound – Harry Dryden"
C_EU = "EU Outbound – Anna"

GENERAL = General(
    hubspot_pipeline_id="pipe-spill3", hubspot_deal_stage_id="stage-first", hubspot_owner_id="owner-harry",
    approver_slack_ids=("U_HARRY",),
)


def mailbox(address: str, owner: str, status: str = "Active", **kw: Any) -> Mailbox:
    kw.setdefault("added_on", date(2026, 9, 1))
    return Mailbox(address, address.split("@")[1], owner, status, 30, **kw)


MAILBOXES = (
    mailbox(HANNAH, "Hannah Spalding"),
    mailbox(HARRY, "Harry Dryden"),
    mailbox(SAM, "Sam Jackson"),
    mailbox(HARRY2, "Harry Dryden"),
)
SETTINGS = Settings(general=GENERAL, mailboxes=MAILBOXES)
MAILBOX_HEADERS = list(COLUMNS["Mailboxes"])


# -- fakes -----------------------------------------------------------------------------------


def warm_account(email: str, *, score: int = 98, warmup: int = 1, started: str = "2026-08-01T09:00:00.000Z") -> dict:
    return {"email": email, "status": 1, "warmup_status": warmup, "stat_warmup_score": score,
            "timestamp_warmup_start": started, "daily_limit": 30}


class FakeInstantly:
    """An in-memory Instantly API v2 behind a FakeTransport (campaigns, accounts, leads)."""

    def __init__(self, transport: FakeTransport, accounts: dict[str, dict] | None = None):
        self.accounts = accounts if accounts is not None else {m.address: warm_account(m.address) for m in MAILBOXES}
        self.campaigns: dict[str, dict] = {}
        self.leads: dict[str, dict] = {}
        self.daily: list[dict] = []  # GET /accounts/analytics/daily rows
        self.sending_status: dict[str, dict] = {}  # campaign id -> GET /campaigns/{id}/sending-status
        self._n = 0
        for method in ("GET", "POST", "PATCH", "DELETE"):
            transport.route(method, "api.instantly.ai", fn=self.handle)

    def add_campaign(self, name: str, **fields: Any) -> dict:
        self._n += 1
        c = {"id": f"c{self._n}", "name": name, "status": 2, **fields}
        self.campaigns[c["id"]] = c
        return c

    def standard(self, name: str, accounts: list[str], limit: int, **over: Any) -> dict:
        """A campaign exactly as ensure_campaigns would create it."""
        from us_outbound.clients.instantly import sequences

        return self.add_campaign(name, **{**campaign_settings(), "campaign_schedule": instantly_schedule(),
                                          "sequences": sequences(reg.campaign_steps()), "email_list": accounts,
                                          "daily_limit": limit, **over})

    def add_lead(self, campaign_id: str, email: str) -> dict:
        self._n += 1
        lead = {"id": f"l{self._n}", "campaign": campaign_id, "email": email}
        self.leads[lead["id"]] = lead
        return lead

    def by_name(self, name: str) -> dict | None:
        return next((c for c in self.campaigns.values() if c["name"] == name), None)

    def handle(self, req) -> Any:
        path = req.url.split("/api/v2", 1)[1]
        m = req.method
        parts = path.strip("/").split("/")
        if parts[0] == "campaigns":
            if m == "GET" and len(parts) == 1:
                search = (req.params or {}).get("search", "")
                items = [dict(c) for c in self.campaigns.values() if search in c["name"]]
                return {"items": items, "next_starting_after": None}
            if m == "POST" and len(parts) == 1:
                self._n += 1
                c = {**req.json, "id": f"c{self._n}", "status": 0}
                self.campaigns[c["id"]] = c
                return dict(c)
            c = self.campaigns[parts[1]]
            if m == "GET" and parts[-1] == "sending-status":
                return dict(self.sending_status.get(c["id"], {}))
            if m == "GET":
                return dict(c)
            if m == "PATCH":
                c.update(req.json)
                return dict(c)
            if m == "POST" and parts[-1] == "pause":
                c["status"] = 2
            if m == "POST" and parts[-1] == "activate":
                c["status"] = 1
            return {"id": c["id"]}
        if parts[0] == "accounts":
            if m == "GET" and parts[1:] == ["analytics", "daily"]:
                wanted = set((req.params or {}).get("emails") or ())
                return [dict(r) for r in self.daily if r["email_account"] in wanted]
            if m == "GET":
                return dict(self.accounts.get(parts[1], {}))
            if m == "PATCH":
                self.accounts[parts[1]].update(req.json)
                return dict(self.accounts[parts[1]])
            if parts[-1] == "warmup-analytics":
                return {"aggregate_data": {}}
            if parts[-1] == "enable":
                for e in req.json["emails"]:
                    self.accounts[e]["warmup_status"] = 1
                return {}
        if parts[0] == "leads":
            if m == "POST" and parts[-1] == "list":
                return {"items": [dict(x) for x in self.leads.values() if x["campaign"] == req.json["campaign"]],
                        "next_starting_after": None}
            if m == "GET":
                return dict(self.leads.get(parts[1], {}))
            if m == "DELETE":
                self.leads.pop(parts[1], None)
                return {}
        raise AssertionError(f"unexpected Instantly call {m} {path}")


class StubSheets:
    """The settings sheet in memory, with the guard's dry-run rule applied to writes."""

    def __init__(self, guard, tabs: dict[str, list[dict[str, str]]]):
        self.guard, self.tabs = guard, tabs

    def read_tabs(self, sheet_id, tabs):
        return {t: [dict(r) for r in self.tabs.get(t, [])] for t in tabs}

    def append_rows(self, sheet_id, tab, rows):
        if self.guard.authorize("sheets", Op("values.append", target=sheet_id, write=True, detail={"tab": tab})):
            self.tabs.setdefault(tab, []).extend(dict(r) for r in rows)

    def update_cell(self, sheet_id, tab, match, column, value):
        hits = [r for r in self.tabs.get(tab, []) if all(r.get(k) == v for k, v in match.items())]
        if not hits:
            return False
        if self.guard.authorize("sheets", Op("values.update", target=sheet_id, write=True, detail={"tab": tab})):
            hits[0][column] = value
        return True


def sheet_rows(settings: Settings) -> list[dict[str, str]]:
    return [reg._row(m) for m in settings.mailboxes]


def slack_routes(t: FakeTransport) -> FakeTransport:
    t.route("GET", "conversations.list", body={"ok": True, "channels": [
        {"id": "C_DEV", "name": "us-outbound-dev"}, {"id": "C_ALERT", "name": "us-outbound"}]})
    t.route("POST", "chat.postMessage", fn=lambda req: {"ok": True, "channel": req.json["channel"], "ts": "1.1"})
    return t


def setup(settings: Settings = SETTINGS, *, live: bool = True, accounts: dict | None = None, now: datetime = NOW,
          ramp_done: bool = True):
    """ramp_done: the mailboxes are past the sending ramp, so caps are the sheet's (tests/test_ramp.py covers the ramp)."""
    t = slack_routes(FakeTransport())
    inst = FakeInstantly(t, accounts)
    ctx = make_context(settings, live=live, transport=t, now=now)
    if ramp_done:
        from tests.test_ramp import past_ramp

        past_ramp(ctx.store, settings.mailboxes, now)
    sheets = StubSheets(ctx.guard, {"Mailboxes": sheet_rows(settings)})
    ctx.clients.sheets = sheets
    return ctx, t, inst, sheets


def instantly_writes(t: FakeTransport) -> list:
    return [r for r in t.writes() if "api.instantly.ai" in r.url and not r.url.endswith(("/leads/list", "/warmup-analytics"))]


def row(sheets: StubSheets, address: str) -> dict:
    return next(r for r in sheets.tabs["Mailboxes"] if r["address"] == address)


# -- warm or warming -----------------------------------------------------------------------


@pytest.mark.parametrize(
    "warmup, added_on, expected",
    [
        ({"found": True, "warmup_enabled": True, "status": "active", "warmup_score": 95}, None, "Active"),
        ({"found": True, "warmup_enabled": True, "status": "active", "health_score": 90}, None, "Active"),
        ({"found": True, "warmup_enabled": True, "status": "active", "warmup_score": 60,
          "warmup_started_at": "2026-10-20T00:00:00Z"}, None, "Warming"),
        ({"found": True, "warmup_enabled": True, "status": "active", "warmup_score": 60,
          "warmup_started_at": "2026-10-06T00:00:00Z"}, None, "Active"),  # 21 days of warmup
        ({"found": True, "warmup_enabled": True, "status": "active", "warmup_score": 60}, date(2026, 10, 6), "Active"),
        ({"found": True, "warmup_enabled": False, "status": "active", "warmup_score": 99}, None, "Warming"),
        ({"found": True, "warmup_enabled": True, "status": "connection_error", "warmup_score": 99}, None, "Warming"),
        ({"found": False}, None, "Warming"),
    ],
)
def test_warmup_to_status(warmup, added_on, expected):
    assert reg.warmup_to_status(warmup, today=TODAY, added_on=added_on) == expected


# -- campaigns -------------------------------------------------------------------------------


def test_ensure_campaigns_creates_three_paused_campaigns():
    ctx, t, inst, _ = setup()
    inst.add_campaign(C_EU, status=1, email_list=["anna@eu.example"])
    out = reg.ensure_campaigns(ctx)
    assert sorted(out["created"]) == sorted([C_HANNAH, C_SAM, C_HARRY])
    ours = {c["name"]: c for c in inst.campaigns.values() if c["name"].startswith("US Outbound – ")}
    assert set(ours) == {C_HANNAH, C_SAM, C_HARRY}
    assert all(c["status"] == 0 for c in ours.values())  # draft: never activated
    assert not [r for r in t.requests if r.url.endswith("/activate")]
    assert sorted(ours[C_HARRY]["email_list"]) == [HARRY, HARRY2] and ours[C_HARRY]["daily_limit"] == 60
    assert ours[C_HANNAH]["email_list"] == [HANNAH] and ours[C_HANNAH]["daily_limit"] == 30
    for c in ours.values():
        assert {k: c[k] for k in CAMPAIGN_SETTINGS} == CAMPAIGN_SETTINGS
        assert c["open_tracking"] is False and c["link_tracking"] is False and c["text_only"] is False
        steps = c["sequences"][0]["steps"]
        assert [s["variants"][0]["subject"] for s in steps] == [f"{{{{s{i}_subject}}}}" for i in range(1, 5)]
    assert inst.by_name(C_EU)["status"] == 1  # the European campaign is untouched
    assert not [r for r in t.requests if "/c1" in r.url]
    # A second run creates nothing and finds no drift.
    again = reg.ensure_campaigns(ctx)
    assert again["created"] == [] and sorted(again["ok"]) == sorted([C_HANNAH, C_SAM, C_HARRY]) and again["drift"] == {}


def test_ensure_campaigns_reports_drift_and_fixes_it_only_when_asked():
    ctx, t, inst, _ = setup()
    inst.standard(C_HANNAH, [HANNAH], 30, open_tracking=True)
    inst.standard(C_SAM, [SAM, "old@meetspill.org"], 60)
    inst.standard(C_HARRY, [HARRY, HARRY2], 60)
    inst.add_campaign("US Outbound – Somebody Gone", status=2)
    out = reg.ensure_campaigns(ctx)
    assert set(out["drift"]) == {C_HANNAH, C_SAM}
    assert out["drift"][C_HANNAH] == {"open_tracking": [False, True]}
    assert set(out["drift"][C_SAM]) == {"email_list", "daily_limit"}
    assert out["unknown"] == ["US Outbound – Somebody Gone"]
    assert instantly_writes(t) == []  # reporting only
    fixed = reg.ensure_campaigns(ctx, fix=True)
    assert sorted(fixed["fixed"]) == [C_HANNAH, C_SAM]
    assert inst.by_name(C_HANNAH)["open_tracking"] is False
    assert inst.by_name(C_SAM)["email_list"] == [SAM] and inst.by_name(C_SAM)["daily_limit"] == 30
    assert inst.by_name("US Outbound – Somebody Gone") == {"id": "c4", "name": "US Outbound – Somebody Gone", "status": 2}
    assert reg.ensure_campaigns(ctx)["drift"] == {}


def test_owner_without_an_active_mailbox_waits():
    settings = dataclasses.replace(SETTINGS, mailboxes=(mailbox(SAM, "Sam Jackson", "Warming"),))
    ctx, t, inst, _ = setup(settings)
    out = reg.ensure_campaigns(ctx)
    assert out["pending"] == [C_SAM] and inst.campaigns == {}


# -- registry commands -------------------------------------------------------------------------


def test_mailbox_add_warm_new_owner_gets_a_campaign():
    new = "maria@meetspill.org"
    accounts = {**{m.address: warm_account(m.address) for m in MAILBOXES}, new: warm_account(new)}
    ctx, t, inst, sheets = setup(accounts=accounts)
    out = reg.mailbox_add(ctx, new, owner="Maria Lopez")
    added = row(sheets, new)
    assert added["status"] == "Active" and added["owner_name"] == "Maria Lopez" and added["added_on"] == "2026-10-27"
    assert added["daily_cap"] == "30" and added["signature"] == "Maria Lopez\nSpill\nspill.chat/us"
    assert set(added) == set(MAILBOX_HEADERS)
    c = inst.by_name("US Outbound – Maria Lopez")
    assert c["email_list"] == [new] and c["status"] == 0
    assert out["campaign_action"] == "created (paused)" and out["warmup_turned_on"] is False


def test_mailbox_add_cold_mailbox_warms_first():
    new = "sam2@meetspill.org"
    accounts = {**{m.address: warm_account(m.address) for m in MAILBOXES},
                new: warm_account(new, score=10, warmup=0, started="")}
    ctx, t, inst, sheets = setup(accounts=accounts)
    inst.standard(C_SAM, [SAM], 30)
    out = reg.mailbox_add(ctx, new, owner="Sam Jackson")
    assert row(sheets, new)["status"] == "Warming"
    assert out["warmup_turned_on"] is True and inst.accounts[new]["warmup_status"] == 1
    assert inst.by_name(C_SAM)["email_list"] == [SAM]  # joins the sending list once Active


def test_mailbox_add_refusals():
    ctx, t, inst, sheets = setup()
    with pytest.raises(reg.MailboxError, match="spill.chat never sends"):
        reg.mailbox_add(ctx, "harry@spill.chat", owner="Harry Dryden")
    with pytest.raises(reg.MailboxError, match="spill.chat never sends"):
        reg.mailbox_add(ctx, "harry@mail.spill.chat", owner="Harry Dryden")
    with pytest.raises(reg.MailboxError, match="already on the Mailboxes tab"):
        reg.mailbox_add(ctx, HANNAH.upper(), owner="Hannah Spalding")
    with pytest.raises(reg.MailboxError, match="not connected in Instantly"):
        reg.mailbox_add(ctx, "nobody@meetspill.org", owner="No Body")
    with pytest.raises(reg.MailboxError, match="owner"):
        reg.mailbox_add(ctx, "x@meetspill.org", owner=" ")
    assert len(sheets.tabs["Mailboxes"]) == 4


def test_mailbox_pause_takes_it_off_the_sending_list():
    ctx, t, inst, sheets = setup()
    inst.standard(C_HARRY, [HARRY, HARRY2], 60)
    inst.standard(C_HANNAH, [HANNAH], 30, status=1)
    inst.add_campaign(C_EU, status=1, email_list=["anna@eu.example"])
    out = reg.mailbox_pause(ctx, HARRY)
    assert row(sheets, HARRY)["status"] == "Paused" and out["campaign_action"] == "sending list updated"
    assert inst.by_name(C_HARRY)["email_list"] == [HARRY2] and inst.by_name(C_HARRY)["daily_limit"] == 30
    reg.mailbox_pause(ctx, HANNAH)
    assert inst.by_name(C_HANNAH)["status"] == 2  # no Active address left: the campaign waits
    assert inst.by_name(C_HANNAH)["email_list"] == [HANNAH]
    assert inst.by_name(C_EU)["status"] == 1
    assert not [r for r in t.requests if f"/{inst.by_name(C_EU)['id']}" in r.url]
    assert not [r for r in t.requests if "warmup" in r.url and r.method == "POST" and "disable" in r.url]


def test_mailbox_pause_refuses_when_the_sheet_has_no_row_for_it():
    ctx, t, inst, sheets = setup()
    inst.standard(C_HARRY, [HARRY, HARRY2], 60)
    sheets.tabs["Mailboxes"] = [r for r in sheets.tabs["Mailboxes"] if r["address"] != HARRY]
    with pytest.raises(reg.MailboxError, match="no row with that address"):
        reg.mailbox_pause(ctx, HARRY)
    assert instantly_writes(t) == []
    assert inst.by_name(C_HARRY)["email_list"] == [HARRY, HARRY2]


def test_mailbox_retire_pauses_and_sets_the_wait():
    ctx, t, inst, sheets = setup()
    inst.standard(C_SAM, [SAM], 30)
    out = reg.mailbox_retire(ctx, SAM)
    assert row(sheets, SAM)["status"] == "Paused" and row(sheets, SAM)["retire_after"] == "2026-11-26"
    assert out["retire_after"] == "2026-11-26" and inst.by_name(C_SAM)["status"] == 2


def test_registry_commands_in_dry_run_change_nothing():
    ctx, t, inst, sheets = setup(live=False)
    inst.standard(C_HARRY, [HARRY, HARRY2], 60)
    before = [dict(r) for r in sheets.tabs["Mailboxes"]]
    reg.mailbox_pause(ctx, HARRY)
    reg.mailbox_retire(ctx, SAM)
    assert sheets.tabs["Mailboxes"] == before
    assert instantly_writes(t) == []
    assert inst.by_name(C_HARRY)["email_list"] == [HARRY, HARRY2]
    assert ctx.guard.writes("sheets", sent=False) and not ctx.guard.writes("sheets", sent=True)


# -- mailbox_health ---------------------------------------------------------------------------------


def test_mailbox_health_promotes_retires_and_reports():
    settings = dataclasses.replace(SETTINGS, mailboxes=(
        mailbox(HANNAH, "Hannah Spalding", "Warming", added_on=date(2026, 10, 20)),
        mailbox(HARRY, "Harry Dryden"),
        mailbox(SAM, "Sam Jackson", "Paused", retire_after=date(2026, 10, 20)),
        mailbox(HARRY2, "Harry Dryden", "Paused", retire_after=date(2026, 11, 20)),
    ))
    accounts = {m.address: warm_account(m.address) for m in MAILBOXES}
    accounts[HARRY]["warmup_status"] = 0  # warmup was turned off by hand
    ctx, t, inst, sheets = setup(settings, accounts=accounts)
    inst.standard(C_HARRY, [HARRY], 30)
    ctx.store.insert("events", [{"event_id": "e1", "type": "sent", "mailbox": SAM, "occurred_at": NOW - timedelta(days=40)}])
    out = reg.mailbox_health(ctx)
    assert out["promoted"] == [HANNAH] and row(sheets, HANNAH)["status"] == "Active"
    assert out["retired"] == [SAM] and row(sheets, SAM)["status"] == "Retired"
    assert row(sheets, HARRY2)["status"] == "Paused"  # its retire date has not come
    assert out["warmup_turned_on"] == [HARRY] and accounts[HARRY]["warmup_status"] == 1
    assert inst.by_name(C_HANNAH)["email_list"] == [HANNAH]  # created on promotion
    assert out["campaigns"]["drift"] == {}
    [post] = [r.json for r in t.requests if r.url.endswith("chat.postMessage")]
    assert post["channel"] == "C_ALERT" and "Promoted to Active: hannah@meetspill.org" in post["text"]
    account_reads = [r.url for r in t.requests if "/accounts/" in r.url and r.method == "GET"
                     and "/accounts/analytics/" not in r.url]
    assert all(any(a in u for a in (HANNAH, HARRY, SAM, HARRY2)) for u in account_reads)
    [daily] = [r for r in t.requests if "/accounts/analytics/daily" in r.url]  # filtered to the registry
    assert set(daily.params["emails"]) <= {HANNAH, HARRY, SAM, HARRY2}


def test_mailbox_health_waits_to_retire_a_recently_used_mailbox():
    settings = dataclasses.replace(SETTINGS, mailboxes=(
        mailbox(HARRY, "Harry Dryden"), mailbox(SAM, "Sam Jackson", "Paused", retire_after=date(2026, 10, 20))))
    ctx, t, inst, sheets = setup(settings)
    ctx.store.insert("contacts", [{"contact_id": "k1", "mailbox": SAM, "last_step_at": NOW - timedelta(days=5)}])
    out = reg.mailbox_health(ctx)
    assert out["retired"] == [] and out["waiting_to_retire"] == [SAM]
    assert row(sheets, SAM)["status"] == "Paused"


def test_mailbox_health_dry_run_proposes_only():
    settings = dataclasses.replace(SETTINGS, mailboxes=(mailbox(HANNAH, "Hannah Spalding", "Warming"),))
    ctx, t, inst, sheets = setup(settings, live=False)
    out = reg.mailbox_health(ctx)
    assert out["promoted"] == [HANNAH] and row(sheets, HANNAH)["status"] == "Warming"
    assert inst.campaigns == {} and instantly_writes(t) == []
    [post] = [r.json for r in t.requests if r.url.endswith("chat.postMessage")]
    assert post["channel"] == "C_DEV" and "dry-run" in post["text"]


def test_sheet_id_is_the_test_sheet():
    ctx, *_ = setup()
    assert ctx.guard.bounds.settings_sheet_id == TEST_SHEET_ID


def test_a_waiting_campaign_is_not_drift():
    settings = dataclasses.replace(SETTINGS, mailboxes=(mailbox(HANNAH, "Hannah Spalding", "Paused"),))
    ctx, t, inst, _ = setup(settings)
    inst.standard(C_HANNAH, [HANNAH], 30, status=2)
    out = reg.ensure_campaigns(ctx)
    assert out["ok"] == [C_HANNAH] and out["drift"] == {}


# -- what Instantly reports back (Harry, 30 Sep 2026) -------------------------------------------------


def test_mailbox_health_sets_instantly_limits_to_the_sheet_caps():
    accounts = {m.address: warm_account(m.address) for m in MAILBOXES}
    accounts[HANNAH]["daily_limit"] = 50  # someone raised it in Instantly
    ctx, t, inst, sheets = setup(accounts=accounts)
    out = reg.mailbox_health(ctx)
    assert out["limit_set"] == {HANNAH: {"from": 50, "to": 30}} and accounts[HANNAH]["daily_limit"] == 30
    [patch] = [r for r in t.requests if r.method == "PATCH" and "/accounts/" in r.url]
    assert patch.json == {"daily_limit": 30}
    [post] = [r.json for r in t.requests if r.url.endswith("chat.postMessage")]
    assert "Set hannah@meetspill.org's Instantly daily limit from 50 to 30" in post["text"]


def test_mailbox_health_in_dry_run_only_reports_a_limit_it_would_set():
    accounts = {m.address: warm_account(m.address) for m in MAILBOXES}
    accounts[SAM]["daily_limit"] = 20
    ctx, t, inst, sheets = setup(live=False, accounts=accounts)
    out = reg.mailbox_health(ctx)
    assert out["limit_set"] == {SAM: {"from": 20, "to": 30}} and accounts[SAM]["daily_limit"] == 20
    [post] = [r.json for r in t.requests if r.url.endswith("chat.postMessage")]
    assert "Would set sam@meetspill.org's Instantly daily limit from 20 to 30" in post["text"]


def test_mailbox_health_records_sends_and_why_a_campaign_is_held_back():
    ctx, t, inst, sheets = setup()
    c = inst.standard(C_HARRY, [HARRY, HARRY2], 60)
    inst.daily = [{"date": "2026-10-26", "email_account": HARRY, "sent": 30},
                  {"date": "2026-10-26", "email_account": HARRY2, "sent": 29}]
    inst.sending_status[c["id"]] = {"not_sending_status": 3}
    out = reg.mailbox_health(ctx)
    assert out["sent_by_day"][HARRY] == {"2026-10-26": 30} and out["sent_by_day"][HARRY2] == {"2026-10-26": 29}
    assert out["campaign_status"]["Harry Dryden"] == {"code": 3, "meaning": "the campaign reached its daily limit",
                                                      "at_limit": True}
    [post] = [r.json for r in t.requests if r.url.endswith("chat.postMessage")]
    assert "Instantly says US Outbound – Harry Dryden is held back: the campaign reached its daily limit" in post["text"]


def test_every_step_ends_with_instantly_s_unsubscribe_link():
    # Harry, 1 Oct 2026: the opt-out is Instantly's own link, in the step template after the lead's
    # rendered body (Instantly fills merge tags in the template, not inside a custom variable).
    html, text = reg.campaign_steps(), reg.campaign_steps(text_only=True)
    assert [s["subject"] for s in html] == [f"{{{{s{i}_subject}}}}" for i in range(1, 5)]
    for i, (h, t) in enumerate(zip(html, text), start=1):
        assert h["body"].startswith(f"{{{{s{i}_body}}}}<p><a href=\"{UNSUBSCRIBE_TAG}\">")
        assert t["body"] == f"{{{{s{i}_body}}}}\n\nTo stop hearing from us, unsubscribe here: {UNSUBSCRIBE_TAG}"
    assert CAMPAIGN_SETTINGS["insert_unsubscribe_header"] is True  # and the mail client's one-click button
