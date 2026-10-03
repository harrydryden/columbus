"""Industry focus and named companies from the sheet (Harry, 30 Sep 2026): the Focus and Named
accounts tabs, their validation, the front door, the "named" source, and focus in the enrol job."""

from __future__ import annotations

import copy
from datetime import UTC, datetime, timedelta

import pytest

from tests.fakes import make_context
from tests.test_enrol import make
from tests.test_render import account, contact, make_settings
from us_outbound import accounts, suppression
from us_outbound.clients.http import ApiError
from us_outbound.enrol import enrol, focus
from us_outbound.scoring.score import score_account
from us_outbound.settings.defaults import default_tabs
from us_outbound.settings.model import Focus, NamedAccount
from us_outbound.settings.validate import parse_share, validate_all
from us_outbound.sources import named

NOW = datetime(2026, 10, 27, 11, 0, tzinfo=UTC)
BASE = default_tabs()


def errors_on(tabs, tab):
    return validate_all(tabs)[1][tab]


# -- validation ------------------------------------------------------------------------------------


def test_shares_are_written_as_percentages_or_fractions():
    assert parse_share("60%") == 0.6 and parse_share("0.4") == 0.4 and parse_share(" 100 % ") == 1.0
    for bad in ("60", "sixty", "-5%", "120%"):
        with pytest.raises(ValueError):
            parse_share(bad)


def test_a_valid_focus_tab():
    t = copy.deepcopy(BASE)
    t["Focus"] = [{"industry_group": "legal teams", "share": "40%", "note": ""},
                  {"industry_group": "Marketing & Creative Agencies", "share": "0.35", "note": "Q4 push"}]
    t["Industries"] = [dict(r, active="yes") if r["industry_group"] == "Legal Teams" else r for r in t["Industries"]]
    settings, errors = validate_all(t)
    assert not any(errors.values()), errors
    assert settings.focus == (Focus("Legal Teams", 0.4, ""), Focus("Marketing & Creative Agencies", 0.35, "Q4 push"))


def test_focus_rows_must_name_an_active_group_and_stay_within_100_percent():
    t = copy.deepcopy(BASE)
    t["Focus"] = [{"industry_group": "Marketing & Creative Agencis", "share": "50%", "note": ""}]
    [e] = errors_on(t, "Focus")
    assert "not an industry_group on the Industries tab" in e.message and "Marketing & Creative Agencies" in e.message
    t["Focus"] = [{"industry_group": "Hospitality", "share": "50%", "note": ""}]  # on the tab, switched off in v1
    [e] = errors_on(t, "Focus")
    assert "has no active industry" in e.message
    t["Focus"] = [{"industry_group": "Technology & Startups", "share": "70%", "note": ""},
                  {"industry_group": "Marketing & Creative Agencies", "share": "40%", "note": ""}]
    [e] = errors_on(t, "Focus")
    assert "add up to 110%" in e.message
    t["Focus"] = [{"industry_group": "Technology & Startups", "share": "20%", "note": ""},
                  {"industry_group": "technology & startups", "share": "20%", "note": ""}]
    assert "already on row" in errors_on(t, "Focus")[0].message


def test_named_accounts_are_company_root_domains():
    t = copy.deepcopy(BASE)
    t["Named accounts"] = [
        {"domain": "acmecreative.com", "name": "Acme Creative", "note": "met at a conference"},
        {"domain": "www.brightfin.com", "name": "", "note": ""},
        {"domain": "gmail.com", "name": "", "note": ""},
        {"domain": "spill.chat", "name": "", "note": ""},
        {"domain": "ACMECREATIVE.com", "name": "", "note": ""},
    ]
    errs = {e.row: e.message for e in errors_on(t, "Named accounts")}
    assert "root domain like acme.com" in errs[3] and "personal email domain" in errs[4]
    assert "spill.chat is Spill" in errs[5] and "already on row 2" in errs[6]


def test_a_sheet_without_the_new_tabs_still_validates():
    t = {k: v for k, v in copy.deepcopy(BASE).items() if k not in ("Focus", "Named accounts")}
    settings, errors = validate_all(t)
    assert settings is not None and not any(errors.values())
    assert settings.focus == () and settings.named_accounts == ()
    t.pop("Tests")
    assert "the sheet has no Tests tab" in validate_all(t)[1]["Tests"][0].message  # the old tabs are still required


def test_sync_reads_a_missing_optional_tab_as_empty(monkeypatch):
    from tests.test_settings_sync import StubSheets, StubSlack
    from us_outbound.settings import sync

    monkeypatch.setattr(sync, "_rescore", lambda ctx: True)

    class OldSheet(StubSheets):
        def read_tabs(self, sheet_id, tabs):
            missing = [t for t in tabs if t not in self.tabs]
            if missing:
                raise ApiError("sheets", 400, {"error": {"message": f"Unable to parse range: '{missing[0]}'"}}, "batchGet")
            return super().read_tabs(sheet_id, tabs)

    old = {k: v for k, v in default_tabs().items() if k not in ("Focus", "Named accounts")}
    settings, _ = validate_all(default_tabs())
    ctx = make_context(settings, now=NOW, job="settings_sync")
    ctx.clients.sheets, ctx.clients.slack = OldSheet(old), StubSlack()
    out = sync.run(ctx)
    assert out["unusable"] == [] and out["rejected"] == [] and out["tabs"]["Focus"]["status"] == "unchanged"
    assert sync.load_current(ctx.store)[0] is not None


# -- the front door and the "named" source ------------------------------------------------------


def named_ctx(*rows: NamedAccount):
    return make_context(make_settings(named_accounts=rows), now=NOW)


def test_the_front_door_finds_by_root_domain_and_alias_and_refuses_suppressed_and_partners():
    ctx = named_ctx()
    st = ctx.store
    st.insert("accounts", [{"account_id": "a1", "domain": "acme.com", "status": "verified"}])
    st.insert("domain_aliases", [{"alias": "acme-old.com", "root_domain": "acme.com", "source": "test", "added_at": NOW}])
    st.insert("partners", [{"domain": "brokerco.com", "reason": "broker", "added_at": NOW}])
    suppression.add(st, domain="optedout.com", reason="test", source="test", now=NOW)
    assert accounts.admit(st, "https://www.acme.com/about", source="named", now=NOW).account_id == "a1"
    assert accounts.admit(st, "acme-old.com", source="named", now=NOW).outcome == "exists"
    assert accounts.admit(st, "brokerco.com", source="named", now=NOW).outcome == "a partner, never prospected"
    assert accounts.admit(st, "optedout.com", source="named", now=NOW).outcome == "suppressed"
    new = accounts.admit(st, "newco.io", source="named", now=NOW, name="NewCo, Inc.")
    assert new.outcome == "created"
    row = st.get("accounts", account_id=new.account_id)
    assert (row["domain"], row["clean_name"], row["source"], row["status"]) == ("newco.io", "NewCo", "named", "new")


def test_named_accounts_are_marked_once_and_unmarked_when_taken_off():
    ctx = named_ctx(NamedAccount("newco.io", "NewCo", "Harry met them"), NamedAccount("acme.com"))
    ctx.store.insert("accounts", [{"account_id": "a1", "domain": "acme.com", "status": "verified"}])
    out = named.run(ctx)
    assert out["created"] == ["newco.io"] and out["named"] == 2 and out["marked"] == 2
    facts = ctx.store.select("signal_events", {"source": "named"})
    assert {f["value"] for f in facts} == {True} and len(facts) == 2
    assert named.run(ctx)["marked"] == 0  # nothing changed, nothing written
    ctx.settings = make_settings(named_accounts=(NamedAccount("newco.io"),))
    ctx.now = NOW + timedelta(days=1)
    assert named.run(ctx)["unmarked"] == 1
    latest = max((f for f in ctx.store.select("signal_events", {"account_id": "a1"})), key=lambda f: f["observed_at"])
    assert latest["value"] is False


def test_the_named_signal_scores_a_named_account():
    settings, _ = validate_all(default_tabs())
    events = [{"event_id": "e1", "account_id": "a1", "source": "named", "fact": "named", "value": True,
               "quote": "On the Named accounts tab", "observed_at": NOW}]
    acct = {"account_id": "a1", "domain": "acme.com", "hq_state": "NY"}
    base = score_account(acct, [], settings, NOW.date()).score
    assert base == 0  # October, and the Q4 plan-year window is off (design review Appendix A, 1 Oct 2026)
    r = score_account(acct, events, settings, NOW.date())
    assert "Named by Harry" in [m.signal.signal for m in r.matches] and r.score == base + 30
    events.append({"event_id": "e2", "account_id": "a1", "source": "named", "fact": "named", "value": False,
                   "observed_at": NOW + timedelta(hours=1)})
    assert score_account(acct, events, settings, NOW.date()).score == base


# -- focus in the enrol job ---------------------------------------------------------------------------


def two_groups(n_agency: int, n_tech: int):
    accts, cons = [], []
    for i in range(n_agency):
        accts.append(account(account_id=f"ag{i}", domain=f"agency{i}.com", score=90 - i))
    for i in range(n_tech):
        accts.append(account(account_id=f"te{i}", domain=f"tech{i}.com", industry="Fintech",
                             industry_group="Technology & Startups", score=50 - i))
    for a in accts:
        cons.append(contact(contact_id=f"k-{a['account_id']}", account_id=a["account_id"], email=f"x@{a['domain']}"))
    return accts, cons


def test_focus_gives_each_group_its_share_of_the_day():
    # 120 a week over the 4 send days left is 30 today. Tech has 70% of the week, so 21 of today's 30
    # go to Tech even though every agency account scores higher.
    accts, cons = two_groups(40, 40)
    s = make_settings(weekly_enrol_cap=120, focus=(Focus("Technology & Startups", 0.7),))
    ctx, _ = make(settings=s, accounts=accts, contacts=cons)
    out = enrol.run(ctx)
    assert out["prepared"] == 30
    assert out["focus"] == "Focus this week: Technology & Startups 21 of 84, other 9 of 36."


def test_a_short_share_passes_to_the_next_accounts_in_queue_order():
    accts, cons = two_groups(40, 5)  # Tech wants 21 today but has only 5 ready
    s = make_settings(weekly_enrol_cap=120, focus=(Focus("Technology & Startups", 0.7),))
    ctx, _ = make(settings=s, accounts=accts, contacts=cons)
    out = enrol.run(ctx)
    assert out["prepared"] == 30
    assert out["focus"] == "Focus this week: Technology & Startups 5 of 84, other 25 of 36."


def test_no_focus_tab_means_plain_queue_order():
    accts, cons = two_groups(40, 40)
    ctx, _ = make(settings=make_settings(weekly_enrol_cap=120), accounts=accts, contacts=cons)
    out = enrol.run(ctx)
    assert out["prepared"] == 30 and out["focus"] is None


def test_the_quota_counts_this_week_s_enrolments_by_group():
    accts, cons = two_groups(3, 3)
    ctx = make_context(make_settings(focus=(Focus("Technology & Startups", 0.5),)), now=NOW)
    ctx.store.insert("accounts", [dict(a, status="enrolled") for a in accts])
    ctx.store.insert("contacts", [dict(c, enrolled_at=NOW - timedelta(days=1)) for c in cons[:4]])  # 3 agencies, 1 tech
    q = focus.today(ctx, days_left=4)
    assert q.targets == {"Technology & Startups": 75, "other": 75}
    assert dict(q.done) == {"other": 3, "Technology & Startups": 1}
    assert q.left == {"Technology & Startups": 19, "other": 18}  # (75 − 1) ÷ 4 and (75 − 3) ÷ 4, rounded up
