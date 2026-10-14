"""The weekly hand-check on the settings sheet (enrol/hand_check_tab.py; Harry, 12 Oct 2026)."""

from __future__ import annotations

from tests.fakes import make_context
from tests.test_copy_desk import MemorySheet
from tests.test_hand_check import AGENCIES, WEEK, _approving, acct, posts, world
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
