"""The industry crosswalk (ops/crosswalk.py; Harry, 9 Oct 2026): how Apollo's industries, NAICS codes and keyword
tags translate into the Industries tab's labels, measured against the companies whose label is known."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from tests.fakes import make_context
from tests.test_copy_desk import MemorySheet
from tests.test_labels import DEFAULT
from us_outbound.clients.http import ApiError
from us_outbound.ops import cli, crosswalk

AT = datetime(2026, 10, 8, 4, 30, tzinfo=UTC)
MC = "Marketing & Creative Agencies"


def company(ctx, aid, *, naics, industry, tags, model=None, confidence="high", entity="company", corrected=None):
    """One account with its Apollo facts and its label check (and an approver's correction, if any)."""
    ctx.store.insert("accounts", [{"account_id": aid, "domain": f"{aid}.com", "industry": MC, "industry_group": MC}])
    facts = [("naics", naics), ("apollo_industry", industry), ("keywords", tags)]
    ctx.store.insert("signal_events", [
        {"event_id": f"{aid}-{f}", "account_id": aid, "source": "apollo_org", "fact": f, "value": v,
         "observed_at": AT - timedelta(days=3)} for f, v in facts])
    if model is not None:
        ctx.store.insert("signal_events", [{
            "event_id": f"{aid}-v", "account_id": aid, "source": "label_check", "fact": "label_verdict",
            "value": {"rules": MC, "rules_group": MC, "model": model, "confidence": confidence, "entity": entity,
                      "asked": True, "decision": {"source": "model", "action": "verify"}}, "observed_at": AT}])
    if corrected:
        ctx.store.insert("signal_events", [{
            "event_id": f"{aid}-c", "account_id": aid, "source": "label_check", "fact": "label_corrected",
            "value": {"from": MC, "to": corrected, "by": "U_HARRY", "via": "thread"}, "observed_at": AT}])


def world(live=False):
    """Six trade publishers the rules place right; six trade associations and five martech SaaS companies whose
    NAICS codes put them under Marketing & Creative Agencies; an agency an approver moved within the group; and two
    companies the model was not sure of (not known, so not counted)."""
    ctx = make_context(DEFAULT, live=live, now=AT + timedelta(days=1))
    ctx.clients.sheets = Sheet(ctx.guard, {})
    for i in range(6):
        company(ctx, f"pub{i}", naics=["513120"], industry="publishing", tags=["magazine", "b2b media"],
                model="Publishers")
        company(ctx, f"assoc{i}", naics=["561920"], industry="events services", tags=["trade association", "networking"],
                model="none", entity="association")
    for i in range(5):
        company(ctx, f"saas{i}", naics=["541613"], industry="computer software", tags=["marketing automation", "saas"],
                model="Adtech & martech")
    company(ctx, "agency", naics=["541810"], industry="marketing & advertising", tags=["digital marketing"],
            model="Marketing agencies", confidence="medium", corrected="Marketing agencies")
    company(ctx, "unsure1", naics=["541613"], industry="computer software", tags=["saas"], model="Fintech",
            confidence="medium")
    company(ctx, "unsure2", naics=["541613"], industry="computer software", tags=["saas"])
    return ctx


class Sheet(MemorySheet):
    def read_tabs(self, sheet_id, tabs):
        """As the Sheets API answers: a tab the sheet does not have is a 400 (9 Oct 2026: the first `crosswalk --live`
        stopped on it, as the tab was read before it was added)."""
        if missing := [t for t in tabs if t not in self.tabs]:
            raise ApiError("sheets", 400, f"Unable to parse range: {missing[0]}")
        return super().read_tabs(sheet_id, tabs)

    def add_tab(self, sheet_id, tab):
        if self._ok("spreadsheet.addSheet", sheet_id, tab):
            self.tabs.setdefault(tab, [])
            self.writes.append(f"add {tab}")


def rows_by(rows):
    return {(r.kind, r.value): r for r in rows}


def test_only_companies_whose_label_is_known_are_measured():
    known = {k.account_id: k for k in crosswalk.load(world())}
    assert len(known) == 18 and "unsure1" not in known and "unsure2" not in known
    assert (known["assoc0"].label, known["assoc0"].group, known["assoc0"].by) == (crosswalk.OUTSIDE,) * 2 + ("model",)
    assert (known["agency"].label, known["agency"].by) == ("Marketing agencies", "approver")  # the approver's word
    assert known["pub0"].rules == "Publishers" and known["saas0"].rules == MC  # 541613 is on the agencies' row
    assert known["saas0"].codes == ("541613",) and known["saas0"].tags == ("marketing automation", "saas")


def test_each_translation_is_measured_against_the_known_labels():
    ctx = world()
    known = crosswalk.load(ctx)
    rows = rows_by(crosswalk.measure(known, ctx.settings))
    code = rows[(crosswalk.NAICS6, "541613")]  # marketing consulting, on the agencies' umbrella row
    assert (code.tab_says, code.umbrella, code.companies, code.right) == (MC, True, 5, 0)
    assert code.verdict() == "poor: mostly Adtech & martech (100%)"
    pub = rows[(crosswalk.NAICS6, "513120")]
    assert pub.tab_says == MC and pub.verdict() == "reliable"  # it places the group, and every one is in it
    assert rows[(crosswalk.RULES, "Publishers")].verdict() == "reliable"
    umbrella = rows[(crosswalk.RULES, MC)]
    assert (umbrella.companies, umbrella.right) == (12, 1)  # judged on the group: only the agency is in it
    assert umbrella.verdict() == f"poor: mostly {crosswalk.OUTSIDE} (50%)"
    assert rows[(crosswalk.TAG, "trade association")].verdict() == "exclude: almost all are outside our labels"
    assert rows[(crosswalk.INDUSTRY, "computer software")].verdict() == "add to Adtech & martech"
    term = rows[(crosswalk.TERM, "marketing automation (Adtech & martech)")]
    assert (term.companies, term.right, term.verdict()) == (5, 5, "reliable")
    assert rows[(crosswalk.TERM, "digital marketing (Marketing agencies)")].verdict() == "too few to tell"


def test_a_label_inside_a_group_that_only_places_the_group_says_so():
    ctx = world()
    for i in range(5):
        company(ctx, f"ux{i}", naics=["541511"], industry="design", tags=["ux design"], model="Creative & design agencies")
    rows = rows_by(crosswalk.measure(crosswalk.load(ctx), ctx.settings))
    term = rows[(crosswalk.TERM, "ux design (UX & product design agencies)")]
    assert term.verdict() == "group only: it places the group, not the label"


def test_the_summary_and_the_command(capsys):
    ctx = world()
    out = crosswalk.run(ctx)
    assert (out["known"], out["by_approver"], out["by_model"], out["outside_our_labels"]) == (18, 1, 17, 6)
    assert out["rules"] == {"exact_label": 6, "same_group": 7, "other_group": 11, "no_rules_label": 0}
    poor = [r["value"] for r in out["poor"]]
    assert poor[:2] == [MC, "561920"] and "541613" in poor and out["tab_written"] is False  # the most companies first
    assert any(r["value"] == "trade association" for r in out["add"])
    assert ctx.clients.sheets.writes == []  # dry-run writes nothing

    live = world(live=True)
    assert crosswalk.run(live)["tab_written"] is True
    assert live.clients.sheets.writes == ["add Crosswalk", "replace Crosswalk"]
    tab = live.clients.sheets.tabs["Crosswalk"]
    assert list(tab[0]) == crosswalk.COLUMNS and tab[0]["kind"] == crosswalk.RULES
    assert {"kind": crosswalk.NAICS6, "value": "541613", "tab_says": MC, "companies": "5", "share_right": "0%",
            "verdict": "poor: mostly Adtech & martech (100%)", "top_label": "Adtech & martech", "label_share": "100%",
            "top_group": "Technology & Startups", "group_share": "100%"} in tab

    capsys.readouterr()  # the runs' log lines
    cli._crosswalk_report(out)
    text = capsys.readouterr().out
    assert text.startswith("18 companies with a known label (1 corrected by an approver, 17 the model was sure of; "
                           "6 outside our labels).")
    assert "the same label 6 (33%); of the 18 the rules labelled, the same group 7, another group 11" in text
    assert "  naics “541613” → Marketing & Creative Agencies: 5 companies, 0% right; poor: mostly Adtech & martech" in text
