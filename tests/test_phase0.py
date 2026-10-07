"""`us-outbound phase0 check` (ops/phase0.py): the PHASE0-CONFIRM items a live read settles, and the writes on Harry's
own seed lead that settle LEAD_PAUSE_CONFIRMED and the interest status. Each probe says CONFIRMED, DIFFERS or COULD
NOT CHECK; dry-run calls nothing; --seed touches only that seed lead, through the guard, live only; the output never
carries a key or an address. Instantly, HubSpot, Slack and Apollo answer on a FakeTransport.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tests.fakes import make_context
from tests.test_cli import Harness
from us_outbound.clients.instantly import LEAD_UNSUBSCRIBED
from us_outbound.logs import EMAIL_RE
from us_outbound.ops import phase0, seed

C_HANNAH = "US Outbound – Hannah Spalding"
HANNAH = "hannah@meetspill.org"
SEED = "harry+seed1@spill.chat"
SEED2 = "harry+seed2@spill.chat"
DOC = Path(__file__).resolve().parents[1] / "docs" / "phase0-confirm.md"


def at(day: int, hour: int = 15) -> str:
    return datetime(2026, 10, day, hour, 0, tzinfo=UTC).isoformat().replace("+00:00", "Z")


class World(Harness):
    """The CLI harness with a seed lead, a prospect lead, the emails Instantly holds, and the lead writes."""

    def __init__(self, settings, *, honour_until=True, takes_status=True):
        super().__init__(settings)
        self.campaign = self.instantly.add_campaign(C_HANNAH, status=1)
        self.emails: list[dict] = []
        self.honour_until, self.takes_status = honour_until, takes_status
        self.forwards: list[dict] = []
        t = self.transport
        t.route("GET", "api.instantly.ai/api/v2/emails", fn=self.list_emails)
        t.route("POST", "/emails/forward", fn=lambda req: self.forwards.append(req.json) or {"id": "fwd-1"})
        t.route("PATCH", "api.instantly.ai/api/v2/leads/", fn=self.patch_lead)
        t.route("POST", "/leads/update-interest-status", fn=self.interest)

    def lead(self, email, *, company=seed.SEED_COMPANY, status=1, **kw):
        lead = self.instantly.add_lead(self.campaign["id"], email)
        lead.update(company_name=company, status=status, **kw)
        return lead

    def sent(self, to, when, **kw):
        e = {"id": f"e{len(self.emails) + 1}", "eaccount": HANNAH, "from_address_email": HANNAH, "lead": to,
             "to_address_email_list": to, "timestamp_created": when, "timestamp_email": when, "ue_type": 1,
             "campaign_id": self.campaign["id"], "subject": "support for the team",
             "from_address_json": [{"address": HANNAH, "name": "Hannah Spalding"}], **kw}
        self.emails.append(e)
        return e

    def list_emails(self, req):
        path = req.url.split("/api/v2", 1)[1]
        if path != "/emails":  # GET /emails/{id}: the email a forward reads first
            return next(e for e in self.emails if path.endswith("/" + e["id"]))
        p = req.params or {}
        kind = "received" if p.get("email_type") == "received" else "sent"
        items = [e for e in self.emails if e.get("kind", "sent") == kind and e["eaccount"] == p.get("eaccount")]
        if self.honour_until and p.get("max_timestamp_created"):
            items = [e for e in items if e["timestamp_created"] <= p["max_timestamp_created"]]
        return {"items": items, "next_starting_after": None}

    def patch_lead(self, req):
        lead = self.instantly.leads[req.url.rsplit("/", 1)[1]]
        if self.takes_status:
            lead.update(req.json)
        return dict(lead)

    def interest(self, req):
        lead = next(x for x in self.instantly.leads.values() if x["email"] == req.json["lead_email"])
        lead["lt_interest_status"] = req.json["interest_value"]
        return {"message": "Lead interest status update background job submitted"}

    def writes(self):
        return [r for r in self.transport.writes() if "api.instantly.ai" in r.url and "/leads/list" not in r.url]


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(phase0, "SLEEP", lambda s: None)


@pytest.fixture
def w(default_settings):
    return World(default_settings)


def printed(out):
    return [x for x in out.splitlines() if x and not x.startswith("{")]


def _status(rest):
    for status in (phase0.CONFIRMED, phase0.DIFFERS, phase0.NOT_CHECKED):
        if rest.startswith(status):
            return status, rest[len(status):].lstrip(":").strip()
    raise AssertionError(rest)


def result(out, pid):
    """(status, detail) on the line of one probe id."""
    line = next(x for x in printed(out) if x.startswith(pid + " ") and not x.startswith(pid + " (email:"))
    return _status(line[len(pid):].strip())


def seed_result(out, pid):
    """(status, detail) on the line of one seed probe."""
    line = next(x for x in printed(out) if x.startswith(pid + " (email:"))
    return _status(line.split(") ", 1)[1].strip())


# -- dry-run and the parser ---------------------------------------------------------------------------------------


def test_the_command_parses():
    from us_outbound.ops import cli

    args = cli.build_parser().parse_args(["phase0", "check", "--seed", SEED, "--seed", SEED2, "--apollo-credits",
                                          "--live"])
    assert (args.action, args.seed, args.apollo_credits, args.live) == ("check", [SEED, SEED2], True, True)


def test_dry_run_says_what_each_probe_would_call_and_calls_nothing(w, capsys):
    w.lead(SEED)
    assert w.run("phase0", "check", "--seed", SEED) == 0
    lines = printed(capsys.readouterr().out)
    assert lines[0].startswith("Dry-run: nothing is called.")
    assert lines[-1].startswith("Add --live to run them")
    for p in phase0.PROBES + phase0.SEED_PROBES:
        assert any(x.strip().startswith(p.id) and "would call: " in x for x in lines), p.id
    assert w.transport.requests == []  # not even a read
    [beat] = w.beats(phase0.JOB)
    assert beat["status"] == "ok" and beat["dry_run"] is True


def test_every_probe_id_is_in_the_inventory():
    text = DOC.read_text()
    for p in phase0.PROBES + phase0.SEED_PROBES:
        assert f"`{p.id}`" in text, p.id


# -- read-only probes: both outcomes ------------------------------------------------------------------------------


def test_max_timestamp_created_honoured_is_confirmed(w, capsys):
    for day in (20, 21, 22, 23):
        w.sent(SEED, at(day))
    assert w.run("phase0", "check", "--live") == 0
    status, detail = result(capsys.readouterr().out, "INST-EMAILS-UNTIL")
    assert status == phase0.CONFIRMED and "left out the 2 sent after" in detail
    [cut] = [r.params["max_timestamp_created"] for r in w.transport.requests if (r.params or {}).get("max_timestamp_created")]
    assert cut.startswith("2026-10-21")


def test_max_timestamp_created_ignored_differs(default_settings, capsys):
    w = World(default_settings, honour_until=False)
    for day in (20, 21, 22, 23):
        w.sent(SEED, at(day))
    assert w.run("phase0", "check", "--live") == 0
    out = capsys.readouterr().out
    status, detail = result(out, "INST-EMAILS-UNTIL")
    assert status == phase0.DIFFERS and "max_timestamp_created is ignored" in detail
    assert "INST-EMAILS-UNTIL (DIFFERS): clients/instantly.py list_emails: until= has no effect" in out


def test_email_fields_and_from_name(w, capsys):
    w.sent(SEED, at(20))
    w.sent(SEED2, at(21), ue_type=3, from_address_json=[{"address": HANNAH, "name": "Hannah at Spill"}])
    assert w.run("phase0", "check", "--live") == 0
    out = capsys.readouterr().out
    assert result(out, "INST-EMAIL-FIELDS") == (phase0.DIFFERS, "2 sent emails: timestamp_email on 2; ue_type 1 ×1, 3 ×1")
    assert result(out, "INST-FROM-NAME") == (phase0.DIFFERS, "Hannah Spalding's mailbox sent as 'Hannah at Spill'")


def test_a_reply_from_a_seed_inbox_settles_the_campaign_id(w, capsys):
    w.lead(SEED, status=LEAD_UNSUBSCRIBED)
    assert w.run("phase0", "check", "--live") == 0
    out = capsys.readouterr().out
    assert result(out, "INST-REPLY-CAMPAIGN")[0] == phase0.NOT_CHECKED
    assert result(out, "INST-LEAD-UNSUB") == (phase0.CONFIRMED, "1 of 1 seed leads read back unsubscribed (status -2)")
    w.sent(SEED, at(22), kind="received", from_address_email=SEED, is_auto_reply=0)
    assert w.run("phase0", "check", "--live") == 0
    out = capsys.readouterr().out
    assert result(out, "INST-REPLY-CAMPAIGN") == (phase0.CONFIRMED, "1 of 1 replies from a seed inbox carry their "
                                                                   "campaign's id")
    assert result(out, "INST-AUTO-REPLY")[0] == phase0.CONFIRMED


def test_association_ids_confirmed_and_differs(default_settings):
    def labels(table):
        def fn(req):
            src, dst = req.url.split("/associations/", 1)[1].split("/")[:2]
            return {"results": [{"category": "HUBSPOT_DEFINED", "typeId": tid, "label": label}
                                for tid, label in table.get((src, dst), [])]}
        return fn

    good = {k: [(v, "Primary" if k == ("deals", "companies") else None)] for k, v in phase0.ASSOCIATION_TYPE_IDS.items()}
    ctx = make_context(default_settings, live=True)
    ctx.clients.transport.route("GET", "/crm/v4/associations/", fn=labels(good))
    assert phase0.association_ids(phase0.Run(ctx)).status == phase0.CONFIRMED
    bad = {**good, ("deals", "companies"): [(5, None), (341, None)], ("notes", "contacts"): [(201, None)]}
    ctx = make_context(default_settings, live=True)
    ctx.clients.transport.route("GET", "/crm/v4/associations/", fn=labels(bad))
    res = phase0.association_ids(phase0.Run(ctx))
    assert res.status == phase0.DIFFERS
    assert "notes→contacts: 202 is not listed (HubSpot lists [201])" in res.detail
    assert "deals→companies: 5 is labelled None, not Primary" in res.detail


def test_deal_search_by_company_ignored_differs(default_settings):
    ctx = make_context(default_settings, live=True)
    ctx.store.insert("accounts", [{"account_id": "a1", "domain": "acme.com", "hubspot_company_id": "77"}])
    ctx.clients.transport.route("POST", "/crm/v3/objects/deals/search", body={"total": 4210, "results": []})
    res = phase0.deals_by_company(phase0.Run(ctx))
    assert res.status == phase0.DIFFERS and "the filter was ignored" in res.detail
    ctx = make_context(default_settings, live=True)
    ctx.store.insert("accounts", [{"account_id": "a1", "domain": "acme.com", "hubspot_company_id": "77"}])
    ctx.clients.transport.route("POST", "/crm/v3/objects/deals/search", fn=lambda req: {
        "total": 2 if req.json["filterGroups"] else 4210, "results": []})
    res = phase0.deals_by_company(phase0.Run(ctx))
    assert res.status == phase0.CONFIRMED and "2 deals for one company, of 4210" in res.detail


@pytest.mark.parametrize("scopes, status", [
    (",".join(phase0.SLACK_SCOPES), phase0.CONFIRMED),
    ("chat:write,channels:history,reactions:write", phase0.DIFFERS),
])
def test_slack_scopes(default_settings, scopes, status):
    ctx = make_context(default_settings, live=True)
    ctx.clients.transport.route("GET", "auth.test", body={"ok": True, "user_id": "UBOT"},
                                headers={"X-OAuth-Scopes": scopes})
    res = phase0.slack_scopes(phase0.Run(ctx))
    assert res.status == status
    if status == phase0.DIFFERS:
        assert "lacks groups:history" in res.detail and "im:write" in res.detail


@pytest.mark.parametrize("status, outcome", [(404, phase0.CONFIRMED), (422, phase0.DIFFERS)])
def test_enrich_of_an_unknown_company(default_settings, status, outcome):
    ctx = make_context(default_settings, live=True)
    ctx.clients.transport.route("GET", "/organizations/enrich", status=status, body={"error": "x"})
    assert phase0.enrich_not_found(phase0.Run(ctx)).status == outcome


@pytest.mark.parametrize("values, outcome", [
    ([0.12, -0.05, 0.4, 0.0, 1.5, 0.08, 0.2, 0.33, -0.1, 0.06], phase0.CONFIRMED),
    ([12, -5, 40, 0, 150, 8, 20, 33, -10, 6], phase0.DIFFERS),
])
def test_growth_figures_as_stored(default_settings, values, outcome):
    ctx = make_context(default_settings)
    ctx.store.insert("signal_events", [{"event_id": f"g{i}", "account_id": f"a{i}", "source": "apollo_org",
                                         "fact": "headcount_growth_12m", "value": v} for i, v in enumerate(values)])
    assert phase0.growth_fraction(phase0.Run(ctx)).status == outcome


def test_rate_limits_from_the_people_search_headers(default_settings):
    ctx = make_context(default_settings, live=True)
    ctx.clients.transport.route("POST", "/mixed_people/api_search", body={"people": []}, headers={
        "x-rate-limit-minute": "200", "x-rate-limit-hourly": "400", "x-minute-usage": "3", "Content-Type": "x"})
    res = phase0.apollo_rate_limits(phase0.Run(ctx))
    assert res.status == phase0.CONFIRMED and "x-rate-limit-minute 200" in res.detail and "content-type" not in res.detail
    ctx = make_context(default_settings, live=True)
    ctx.clients.transport.route("POST", "/mixed_people/api_search", body={"people": []},
                                headers={"x-rate-limit-minute": "20"})
    assert phase0.apollo_rate_limits(phase0.Run(ctx)).status == phase0.DIFFERS


def test_no_apollo_credit_is_spent_without_the_flag(w, capsys):
    w.store.insert("accounts", [{"account_id": "a1", "domain": "acme.com", "status": "queued", "apollo_org_id": "o1"}])
    assert w.run("phase0", "check", "--live") == 0
    paid = [r for r in w.transport.requests if "/mixed_companies/search" in r.url or "domain=spill.chat" in r.url
            or (r.params or {}).get("domain") == "spill.chat"]
    assert paid == []
    assert result(capsys.readouterr().out, "APO-ENRICH-ONE") == (phase0.NOT_CHECKED, "needs --apollo-credits (1 Apollo "
                                                                                     "credit)")
    assert w.run("phase0", "check", "--live", "--apollo-credits") == 0
    assert [r.url for r in w.transport.requests if "/mixed_companies/search" in r.url]  # the two paid searches


# -- the seed mode: only that seed lead, through the guard, live only ---------------------------------------------


def test_seed_refuses_an_address_that_is_not_ours_before_any_call(w, capsys):
    assert w.run("phase0", "check", "--seed", "dana@harborfinch.com", "--live") == 2
    assert "a seed inbox is one of ours" in capsys.readouterr().err
    assert w.transport.requests == []


def test_seed_refuses_a_contact_we_hold(w, capsys):
    w.store.insert("contacts", [{"contact_id": "k1", "account_id": "a1", "email": SEED}])
    assert w.run("phase0", "check", "--seed", SEED, "--live") == 2
    assert "is a contact we hold" in capsys.readouterr().err
    assert w.writes() == []


def test_seed_pause_pauses_and_resumes_the_seed_lead_alone(w, capsys):
    mine = w.lead(SEED, timestamp_last_contact=at(26))
    other = w.lead("pat@acme.com", company="Acme", status=1)
    w.sent(SEED, at(26))
    assert w.run("phase0", "check", "--seed", SEED, "--live") == 0
    out = capsys.readouterr().out
    assert seed_result(out, "SEED-PAUSE") == (phase0.CONFIRMED, "PATCH /leads/{id} took status 2 (read back paused) and "
                                                                "1 (read back active)")
    assert seed_result(out, "SEED-RESUME")[0] == phase0.NOT_CHECKED
    patches = [r for r in w.writes() if r.method == "PATCH"]
    assert [(r.url.rsplit("/", 1)[1], r.json) for r in patches] == [(mine["id"], {"status": 2}), (mine["id"], {"status": 1})]
    assert other.get("status") == 1 and all(other["id"] not in r.url for r in w.writes())
    assert mine["status"] == 1  # left as it was
    [forward] = w.forwards
    assert forward["to_address_email_list"] == "harry@spill.chat" and forward["reply_to_uuid"] == "e1"
    # The lead is still in its sequence, so it is not marked "Meeting booked".
    assert seed_result(out, "SEED-INTEREST")[0] == phase0.NOT_CHECKED
    assert not [r for r in w.writes() if "update-interest-status" in r.url]
    assert "SEED-PAUSE (email:" in out and "(CONFIRMED, a gate): LEAD_PAUSE_CONFIRMED's first half" in out
    pause = w.beats(phase0.JOB)[-1]["detail"]["seed_pause"]
    assert [p["lead_id"] for p in pause.values()] == [mine["id"]]


def test_a_later_run_reads_whether_the_resumed_lead_went_on(w, capsys):
    mine = w.lead(SEED, timestamp_last_contact=at(26))
    assert w.run("phase0", "check", "--seed", SEED, "--live") == 0
    capsys.readouterr()
    mine["timestamp_last_contact"] = at(27, 18)  # Instantly sent its next step after the pause and resume
    before = len(w.writes())
    assert w.run("phase0", "check", "--seed", SEED, "--live") == 0
    out = capsys.readouterr().out
    assert seed_result(out, "SEED-RESUME") == (phase0.CONFIRMED, "paused and set active on 27 Oct; Instantly emailed it "
                                                                 "again on 27 Oct")
    assert seed_result(out, "SEED-PAUSE")[0] == phase0.CONFIRMED and "by an earlier run" in out
    assert not [r for r in w.writes()[before:] if r.method == "PATCH"]  # not paused a second time
    assert "set LEAD_PAUSE_CONFIRMED = True in clients/instantly.py" in out


def test_a_pause_instantly_ignores_differs_and_the_lead_is_set_active_anyway(default_settings, capsys):
    w = World(default_settings, takes_status=False)
    mine = w.lead(SEED)
    assert w.run("phase0", "check", "--seed", SEED, "--live") == 0
    status, detail = seed_result(capsys.readouterr().out, "SEED-PAUSE")
    assert status == phase0.DIFFERS and "read back status 1, not 2" in detail
    assert [r.json for r in w.writes() if r.method == "PATCH"] == [{"status": 2}, {"status": 1}]
    assert mine["status"] == 1


def test_seed_interest_on_a_finished_seed_lead_marks_it_and_sets_it_back(w, capsys):
    done = w.lead(SEED, status=LEAD_UNSUBSCRIBED, lt_interest_status=None)
    assert w.run("phase0", "check", "--seed", SEED, "--live") == 0
    out = capsys.readouterr().out
    status, detail = seed_result(out, "SEED-INTEREST")
    assert status == phase0.CONFIRMED and "set back to None" in detail
    posts = [r.json["interest_value"] for r in w.writes() if "update-interest-status" in r.url]
    assert posts == [2, None] and done["lt_interest_status"] is None
    assert seed_result(out, "SEED-PAUSE")[0] == phase0.NOT_CHECKED  # unsubscribed: never set active again
    assert not [r for r in w.writes() if r.method == "PATCH"]


def test_an_address_of_ours_that_is_no_seed_lead_is_never_written(w, capsys):
    w.lead(SEED, company="Acme")  # ours, but not a lead `seed send` added
    assert w.run("phase0", "check", "--seed", SEED, "--live") == 0
    out = capsys.readouterr().out
    assert seed_result(out, "SEED-PAUSE")[0] == phase0.NOT_CHECKED
    assert [r for r in w.writes() if r.method != "GET"] == []


def test_the_seed_writes_are_live_only(w):
    w.lead(SEED)
    assert w.run("phase0", "check", "--seed", SEED) == 0
    assert w.transport.requests == []


def test_the_output_carries_no_key_and_no_address(w, capsys):
    w.lead(SEED, timestamp_last_contact=at(26))
    w.lead(SEED2, status=LEAD_UNSUBSCRIBED)
    w.sent(SEED, at(26))
    w.sent(SEED, at(27, 9), kind="received", from_address_email=SEED)
    w.store.insert("accounts", [{"account_id": "a1", "domain": "acme.com", "hubspot_company_id": "77"}])
    w.transport.route("POST", "/crm/v3/objects/deals/search", status=400,
                      body={"message": "bad filter, asked by pat@acme.com"})  # an error that echoes an address
    assert w.run("phase0", "check", "--seed", SEED, "--seed", SEED2, "--live", "--apollo-credits") == 0
    out = "\n".join(printed(capsys.readouterr().out))
    assert result(out, "HS-DEALS-ASSOC")[0] == phase0.DIFFERS
    assert "test-US_OUTBOUND" not in out  # the fake keys every client sends
    assert not EMAIL_RE.search(out), EMAIL_RE.search(out)
    assert "email:" in out  # addresses appear as their hashes


def test_a_probe_that_fails_is_not_checked_and_the_rest_still_run(w, capsys, monkeypatch):
    def boom(r):
        raise RuntimeError("surprise")

    probes = tuple(p if p.id != "APO-USAGE" else phase0.Probe(p.id, p.question, p.calls, boom) for p in phase0.PROBES)
    monkeypatch.setattr(phase0, "PROBES", probes)
    assert w.run("phase0", "check", "--live") == 0
    out = capsys.readouterr().out
    assert result(out, "APO-USAGE") == (phase0.NOT_CHECKED, "the probe failed: RuntimeError: surprise")
    assert result(out, "CLAY-CHECK")[0] == phase0.NOT_CHECKED
    assert re.search(r"\d+ confirmed, \d+ differ, \d+ not checked\.", out)


def test_resume_waits_for_the_next_step_then_differs_when_it_never_goes(default_settings):
    ctx = make_context(default_settings, live=True, job=phase0.JOB)
    r = phase0.Run(ctx)
    lead = {"id": "l1", "email": SEED, "status": 1, "timestamp_last_contact": at(26)}
    r._cache["seed_leads"] = [(C_HANNAH, lead)]
    paused_at = ctx.now
    ctx.store.insert("heartbeats", [{"run_id": "r0", "job": phase0.JOB, "started_at": paused_at, "status": "ok",
                                     "detail": {"seed_pause": {"x": {"lead_id": "l1", "confirmed": True,
                                                                     "paused_at": paused_at.isoformat(),
                                                                     "last_contact": at(26)}}}}])
    res = phase0.seed_resume(r, SEED)
    assert res.status == phase0.NOT_CHECKED and "due about Mon 02 Nov" in res.detail
    later = phase0.Run(make_context(default_settings, live=True, job=phase0.JOB, store=ctx.store,
                                    now=paused_at + timedelta(days=12)))
    later._cache["seed_leads"] = [(C_HANNAH, lead)]
    assert phase0.seed_resume(later, SEED).status == phase0.DIFFERS
