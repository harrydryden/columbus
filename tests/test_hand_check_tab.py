"""The weekly hand-check on the settings sheet (enrol/hand_check_tab.py; Harry, 12 Oct 2026)."""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime

from tests.fakes import make_context
from tests.test_copy_desk import MemorySheet
from tests.test_hand_check import AGENCIES, SETTINGS, WEEK, _approving, acct, posts, world
from us_outbound import verify
from us_outbound.enrol import hand_check
from us_outbound.enrol import hand_check_tab as tab
from us_outbound.replies import desk


class Sheet(MemorySheet):
    def add_tab(self, sheet_id, name):
        self.tabs.setdefault(name, [])

    def tab_ids(self, sheet_id):
        return {}

    def update_cell(self, sheet_id, name, match, column, value):
        for r in self.tabs.get(name, []):
            if all(str(r.get(k, "")).lower() == str(v).lower() for k, v in match.items()):
                r[column] = value
                return True
        return False


def doubtful_week(n: int = 3):
    """auto_send = no: the week's item holds n companies held for doubtful facts, posted and written to the tab."""
    ctx, t = world()
    _approving(ctx)
    ctx.clients.sheets = Sheet(ctx.guard, {"Overrides": []})
    held = [acct(70 + i, AGENCIES, "queued", employees=None, size_band="", hq_state="") for i in range(n)]
    ctx.store.insert("accounts", held)
    ctx.store.insert("signal_events", [verify.doubt_fact(ctx, a, [verify.NO_STATE]) for a in held])
    assert hand_check.post(ctx)["status"] == "recorded"
    return ctx, t


def sheet_rows(ctx):
    return ctx.clients.sheets.tabs[tab.TAB]


def decide(ctx, domain, decision, **cells):
    for r in sheet_rows(ctx):
        if r["domain"] == domain:
            r.update(decision=decision, **cells)


def test_the_post_writes_one_row_per_company_with_the_reason_and_how_to_fill_it():
    ctx, t = doubtful_week()
    rows = sheet_rows(ctx)
    assert [r["domain"] for r in rows] == ["mar70.com", "mar71.com", "mar72.com"]
    assert rows[0]["id"] == f"{WEEK}:mar70.com" and rows[0]["check"] == tab.DOUBT
    assert rows[0]["why"] == f"{verify.NO_STATE} (type it in right_hq_state, then pick fine)"
    assert all(r["decision"] == "" for r in rows)
    [post] = posts(t)
    assert "Hand-check tab" in post.json["text"] and "railway ssh" not in post.json["text"]


def test_a_rewrite_keeps_the_decisions_already_made():
    ctx, _ = doubtful_week()
    decide(ctx, "mar70.com", "fine", right_hq_state="IL")
    tab.write(ctx, hand_check.current(ctx)["payload"])
    row = next(r for r in sheet_rows(ctx) if r["domain"] == "mar70.com")
    assert (row["decision"], row["right_hq_state"]) == ("fine", "IL")


def test_each_decision_is_applied_once_and_the_week_closes_when_every_row_has_one():
    ctx, t = doubtful_week()
    decide(ctx, "mar70.com", "leave out")
    decide(ctx, "mar71.com", "fine", right_hq_state="IL")
    out = desk.poll_approvals(ctx)["hand_check"]
    assert sorted(out["applied"]) == [f"{WEEK}:mar70.com", f"{WEEK}:mar71.com"] and not out["closed"]
    left = ctx.store.get("accounts", account_id="mar-70")
    assert (left["status"], left["tier"]) == ("disqualified", "Excluded")
    assert ctx.clients.sheets.tabs["Overrides"] == [{"domain": "mar71.com", "field": "hq_state", "value": "IL",
                                                      "note": "from the Hand-check tab"}]
    # mar-70 is left out; mar-71's missing HQ state stays a doubt until the next settings sync brings in its override
    assert [d["account_id"] for d in verify.open_doubts(ctx)] == ["mar-71", "mar-72"]
    applied = {r["domain"]: r["applied"] for r in sheet_rows(ctx)}
    assert applied["mar70.com"].endswith(": left out") and "hq_state added to the Overrides tab" in applied["mar71.com"]
    assert tab.apply(ctx)["applied"] == []  # applied once
    assert hand_check.current(ctx)["status"] == "open"
    decide(ctx, "mar72.com", "wrong industry", right_label="Fintech")
    out = tab.apply(ctx)
    assert out["closed"] is True
    item = hand_check.current(ctx)
    assert item["status"] == "handled" and item["payload"]["pulled_account_ids"] == ["mar-70"]
    fixed = ctx.store.get("accounts", account_id="mar-72")
    assert (fixed["industry"], fixed["label_source"]) == ("Fintech", "approver")


def test_a_company_settled_since_needs_no_decision_and_a_dry_run_changes_nothing():
    ctx, _ = doubtful_week(2)
    ctx.store.update("accounts", {"account_id": "mar-70"}, {"status": "disqualified"})  # the label check left it out
    decide(ctx, "mar71.com", "leave out")
    dry = make_context(ctx.settings, live=False, store=ctx.store, now=ctx.now)
    dry.clients.sheets = ctx.clients.sheets
    assert tab.apply(dry)["would_apply"] == ["mar71.com: leave out"]
    assert ctx.store.get("accounts", account_id="mar-71")["status"] == "queued"
    out = tab.apply(ctx)
    assert out["closed"] is True
    applied = {r["domain"]: r["applied"] for r in sheet_rows(ctx)}
    assert "settled without you (disqualified)" in applied["mar70.com"]


def test_a_week_posted_before_the_tab_existed_gets_its_rows_at_the_next_poll():
    ctx, _ = doubtful_week()
    ctx.clients.sheets.tabs.pop(tab.TAB)
    out = tab.apply(ctx)
    assert out["written"] == 3 and len(sheet_rows(ctx)) == 3


def test_a_wrong_industry_with_no_label_says_so_on_the_row():
    ctx, _ = doubtful_week(1)
    decide(ctx, "mar70.com", "wrong industry")
    tab.apply(ctx)
    assert sheet_rows(ctx)[0]["applied"] == "not done: pick the right industry in right_label"


def test_a_missing_industry_points_to_the_industry_dropdown():
    ctx, _ = world()
    _approving(ctx)
    ctx.clients.sheets = Sheet(ctx.guard, {})
    held = acct(80, AGENCIES, "queued", industry="", employees=None, size_band="")
    ctx.store.insert("accounts", [held])
    ctx.store.insert("signal_events", [verify.doubt_fact(ctx, held, [verify.NO_INDUSTRY, verify.NO_SIZE])])
    hand_check.post(ctx)
    assert sheet_rows(ctx)[0]["why"] == (f"{verify.NO_INDUSTRY}; {verify.NO_SIZE} (type it in right_employees, then "
                                         "pick fine; for the industry, pick wrong industry and the right one in "
                                         "right_label)")


# -- W0, 19 Oct 2026: the tab keeps every week, and the judgements go into the database --------------------------------

W43 = "2026-W43"
EXPORT_AT = datetime(2026, 10, 19, 13, 30, tzinfo=UTC)  # Mon 19 Oct, 14:30 UK: the export the afternoon W0 deploys
HEADER = [*tab.COLUMNS, "reason"]  # the tab as it stands, with a column Harry added on the right


def w43_decision(i: int) -> tuple[str, str]:
    """W43 as Harry judged it: 48 random rows, 38 fine (0-37), 8 leave out (38-45), 2 wrong industry (46, 47)."""
    if i < 38:
        return tab.FINE, ""
    return (tab.LEAVE_OUT, "") if i < 46 else (tab.WRONG_INDUSTRY, "Fintech")


def w43_world(*, live: bool = True):
    """The W43 tab of 19 Oct: 48 random-sample rows and 1 doubtful-facts row (fine), 49 in all, every one decided
    and applied, and the 49 companies in the database; plus the 10 companies W44's sample draws."""
    ctx = make_context(SETTINGS, live=live, now=EXPORT_AT)
    companies = [acct(i, AGENCIES, source="apollo", label_source="rules+model") for i in range(48)]
    companies.append(acct(60, AGENCIES, "queued", source="site_visit", label_source="model", hq_state="GA"))
    companies += [acct(100 + i, AGENCIES) for i in range(10)]
    ctx.store.insert("accounts", companies)
    rows = []
    for i, a in enumerate(companies[:49]):
        decision, right = w43_decision(i) if i < 48 else (tab.FINE, "")
        rows.append({"id": tab.row_id(W43, a["domain"]), "week": W43, "company": a["clean_name"], "domain": a["domain"],
                     "check": tab.SAMPLE if i < 48 else tab.DOUBT, "why": "", "hq_state": a["hq_state"],
                     "size": "20-49 (40 staff)", "industry": a["industry"], "decision": decision,
                     "right_label": right, "right_hq_state": "", "right_employees": "",
                     "note": "a law firm's marketing arm" if i == 40 else "", "applied": f"2026-10-19: {decision}",
                     "reason": ""})
    ctx.clients.sheets = Sheet(ctx.guard, {tab.TAB: rows})
    return ctx, companies


def w44_payload(companies):
    return {"iso_week": "2026-W44", "accounts": companies[49:59], "doubtful": []}


def test_the_next_week_is_added_under_the_last_and_no_row_is_lost():
    """Before, write() rewrote the tab with the new week's rows alone: Mon 26 Oct's W44 post would have dropped the
    49 W43 rows, the only record of the 38 fines. Now 49 + 10 = 59 rows, the 49 exactly as they were (Harry's
    reason column included), by one append and no rewrite."""
    ctx, companies = w43_world()
    before = [dict(r) for r in sheet_rows(ctx)]
    assert tab.write(ctx, w44_payload(companies)) == {"rows": 10, "added": 10, "written": True}
    rows = sheet_rows(ctx)
    assert len(rows) == 59 and rows[:49] == before
    assert [r["week"] for r in rows[49:]] == ["2026-W44"] * 10
    assert [r["domain"] for r in rows[49:]] == [a["domain"] for a in companies[49:59]]
    assert ctx.clients.sheets.writes == [f"append {tab.TAB}"]  # never a replace
    # A rerun the same week adds nothing and keeps a decision already made on a W44 row.
    rows[49]["decision"] = tab.LEAVE_OUT
    assert tab.write(ctx, w44_payload(companies)) == {"rows": 10, "added": 0, "written": True}
    assert len(sheet_rows(ctx)) == 59 and sheet_rows(ctx)[49]["decision"] == tab.LEAVE_OUT
    assert ctx.clients.sheets.writes == [f"append {tab.TAB}"]


def test_the_append_goes_through_the_sheets_api_under_the_header_as_it_stands():
    """The same week through the real client: the tab is read, its header row read, and the 10 rows appended,
    each placed by that header (Harry's reason column left blank); nothing is cleared or overwritten."""
    from urllib.parse import unquote

    from tests.fakes import FakeTransport
    from us_outbound.clients.sheets import Sheets

    ctx, companies = w43_world()
    old = sheet_rows(ctx)
    t = FakeTransport()
    t.route("GET", "values:batchGet", body={"valueRanges": [{"values": [HEADER] + [[r[h] for h in HEADER]
                                                                                   for r in old]}]})
    t.route("GET", "/values/", body={"values": [HEADER]})
    t.route("POST", ":append", body={})
    ctx.clients.sheets = Sheets(ctx.guard, t)
    assert tab.write(ctx, w44_payload(companies))["added"] == 10
    assert [(r.method, unquote(r.url.rsplit("/", 1)[-1])) for r in t.requests] == [
        ("GET", "values:batchGet"), ("GET", "'Hand-check'!1:1"), ("POST", "'Hand-check'!A1:append")]
    values = t.requests[-1].json["values"]
    assert len(values) == 10 and all(len(v) == len(HEADER) for v in values)
    assert values[0][:4] == [tab.row_id("2026-W44", companies[49]["domain"]), "2026-W44", companies[49]["clean_name"],
                             companies[49]["domain"]]
    assert {v[HEADER.index("reason")] for v in values} == {""} and {v[HEADER.index("decision")] for v in values} == {""}


def judgements(ctx):
    return ctx.store.select("signal_events", {"source": tab.SOURCE, "fact": tab.FACT})


def test_export_writes_one_judgement_per_decided_row_and_again_changes_nothing():
    """49 decided rows -> 49 facts. Counted from the facts, the 48 random rows are 38 fine, 8 leave out and 2 wrong
    industry (w43_decision), the W43 tally; the doubtful row is told apart by its check. A second export finds
    the same 49 event ids, so it updates them and adds none."""
    dry, _ = w43_world(live=False)
    out = tab.export(dry)
    assert (out["facts"], out["new"], out["written"]) == (49, 49, False) and judgements(dry) == []

    ctx, companies = w43_world()
    out = tab.export(ctx)
    assert (out["rows"], out["decided"], out["facts"], out["new"], out["written"]) == (49, 49, 49, 49, True)
    assert out["weeks"] == {W43: {tab.FINE: 39, tab.LEAVE_OUT: 8, tab.WRONG_INDUSTRY: 2}} and out["skipped"] == []
    facts = judgements(ctx)
    assert len(facts) == 49
    random = Counter(f["value"]["decision"] for f in facts if f["value"]["check"] == tab.SAMPLE)
    assert random == {tab.FINE: 38, tab.LEAVE_OUT: 8, tab.WRONG_INDUSTRY: 2}
    first = companies[40]
    [one] = [f for f in facts if f["account_id"] == first["account_id"]]
    assert one["event_id"] == f"hand_check:{W43}:{first['account_id']}"
    assert one["value"] == {"week": W43, "row_id": tab.row_id(W43, first["domain"]), "check": tab.SAMPLE,
                            "decision": tab.LEAVE_OUT, "right_label": "", "note": "a law firm's marketing arm",
                            "snapshot": {"tier": "Priority", "score": 55, "industry_group": AGENCIES,
                                         "industry": "Advertising agencies", "size_band": "20-49", "hq_state": "IL",
                                         "source": "apollo", "label_source": "rules+model"}}
    wrong = [f["value"]["right_label"] for f in facts if f["value"]["decision"] == tab.WRONG_INDUSTRY]
    assert wrong == ["Fintech", "Fintech"]

    # Again a week later, after the company was left out (tier Excluded) and Harry changed row 0 to leave out: still
    # 49 facts; row 0's decision is the new one, and every snapshot is the one taken at the first export.
    ctx.store.update("accounts", {"account_id": first["account_id"]}, {"tier": "Excluded"})
    sheet_rows(ctx)[0]["decision"] = tab.LEAVE_OUT
    out = tab.export(ctx)
    assert (out["facts"], out["new"], out["already"]) == (49, 0, 49)
    facts = {f["account_id"]: f for f in judgements(ctx)}
    assert len(facts) == 49
    assert facts[companies[0]["account_id"]]["value"]["decision"] == tab.LEAVE_OUT
    assert facts[first["account_id"]]["value"]["snapshot"]["tier"] == "Priority"
    assert facts[first["account_id"]]["observed_at"] == EXPORT_AT


def test_export_skips_undecided_rows_and_says_why_it_skips_others():
    ctx, companies = w43_world()
    rows = sheet_rows(ctx)
    rows[0]["decision"] = ""  # not judged: not exported
    rows[1]["decision"] = "maybe"  # not a decision
    rows[2]["domain"] = "gone.example"  # no company with this domain (erased since)
    out = tab.export(ctx)
    assert (out["decided"], out["facts"]) == (48, 46)  # 49 - 1 undecided; 48 - 2 skipped
    assert [(s["domain"], s["why"]) for s in out["skipped"]] == [
        (companies[1]["domain"], "the decision must be one of fine, leave out, wrong industry"),
        ("gone.example", "no company with this domain")]
    assert len(judgements(ctx)) == 46


def test_handcheck_export_from_the_command_line(capsys):
    from us_outbound.ops import cli

    dry, _ = w43_world(live=False)
    assert cli.main(["handcheck", "export"], context_factory=lambda job, live, **kw: dry) == 0
    printed = capsys.readouterr().out
    assert "Would write 49 hand-check judgements (49 new) from 49 decided rows of 49." in printed
    assert judgements(dry) == []
    live, _ = w43_world()
    assert cli.main(["handcheck", "export", "--live"], context_factory=lambda job, live_flag, **kw: live) == 0
    assert "Wrote 49 hand-check judgements (49 new)" in capsys.readouterr().out and len(judgements(live)) == 49
