"""The weekly hand-check on the settings sheet (enrol/hand_check_tab.py; Harry, 12 Oct 2026; W1-5, 19 Oct 2026): the
rows, each decision applied and re-applied when it changes, one judgement fact per row, the week's close, the reminder,
the old rows adopted, the header moved, and W0's append and export."""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime, timedelta

import pytest

from tests.fakes import make_context
from tests.test_copy_desk import MemorySheet
from tests.test_hand_check import NOW, SETTINGS, WEEK, acct, general, posts, world
from us_outbound import verify
from us_outbound.clients.http import ApiError
from us_outbound.enrol import enrol, hand_check
from us_outbound.enrol import hand_check_tab as tab
from us_outbound.ops import job_errors
from us_outbound.replies import desk


class Sheet(MemorySheet):
    """The settings sheet in memory, with the calls the Hand-check tab makes besides MemorySheet's."""

    def __init__(self, guard, tabs):
        super().__init__(guard, tabs)
        self.validation: dict[str, dict] = {}

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

    def set_validation(self, sheet_id, name, headers, dropdowns):
        if self._ok("spreadsheet.setDataValidation", sheet_id, name):
            self.validation[name] = {"headers": list(headers), "dropdowns": dict(dropdowns)}


class FlakySheet(Sheet):
    """update_rows answers 503 on the calls numbered in fail (1 is the first)."""

    def __init__(self, guard, tabs, fail):
        super().__init__(guard, tabs)
        self.fail, self.calls = set(fail), 0

    def update_rows(self, sheet_id, name, key, updates):
        self.calls += 1
        if self.calls in self.fail:
            raise ApiError("sheets", 503, "The service is currently unavailable.", "https://sheets.googleapis.com/")
        return super().update_rows(sheet_id, name, key, updates)


def sheet_rows(ctx):
    return ctx.clients.sheets.tabs[tab.TAB]


def decide(ctx, where, decision, **cells):
    for r in sheet_rows(ctx):
        if r["domain"] == where:
            r.update(decision=decision, **cells)


def judgements(ctx):
    return ctx.store.select("signal_events", {"source": tab.SOURCE, "fact": tab.FACT})


def facts_of(ctx, fact):
    return [e for e in ctx.store.select("signal_events") if e.get("fact") == fact]


def drawn_week(monkeypatch, n=14, *, settings=SETTINGS, sheet=Sheet, **kw):
    """The week's draw recorded, written to the tab and posted: today's number 2, so 10 of mar-002 to mar-013."""
    monkeypatch.setattr(hand_check, "today_number", lambda ctx: 2)
    ctx, t = world(n, settings=settings)
    ctx.clients.sheets = sheet(ctx.guard, {"Overrides": []}, **kw)
    assert hand_check.run(ctx)["status"] == "recorded"
    return ctx, t


def sample_domains(ctx):
    return [r["domain"] for r in sheet_rows(ctx) if r["check"] == tab.SAMPLE]


def account(ctx, domain):
    return ctx.store.get("accounts", domain=domain)


# -- each decision, one fact a row ------------------------------------------------------------------------------


def test_ten_decisions_make_ten_facts_and_a_changed_one_is_applied_again(monkeypatch):
    """10 rows: 7 fine, 2 leave out (media_publisher), 1 wrong industry with right_label Fintech. Each is applied and
    leaves one judgement fact: 10 facts. The 2 left out are disqualified with status_reason hand_check. tally counts
    the random rows judged fine against those judged: (7, 10). Then one leave out changed to fine: it is verified
    again, its fact says fine, and there are still 10 facts."""
    ctx, t = drawn_week(monkeypatch)
    d = sample_domains(ctx)
    for x in d[:7]:
        decide(ctx, x, "fine", contact_ok="yes", opener_ok="no")
    for x in d[7:9]:
        decide(ctx, x, "leave out", reason="media publisher")  # words with a space count as the code
    decide(ctx, d[9], "wrong industry", right_label="Fintech")
    out = desk.poll_approvals(ctx)["hand_check"]
    assert sorted(out["applied"]) == sorted(tab.row_id(WEEK, x) for x in d) and out["hand_check_errors"] == []
    assert len(judgements(ctx)) == 10
    for x in d[7:9]:
        a = account(ctx, x)
        assert (a["status"], a["status_reason"], a["tier"]) == ("disqualified", "hand_check", "Excluded")
        assert a["tier_reason"] == f"hand_check: media_publisher ({WEEK})"
    fixed = account(ctx, d[9])
    assert (fixed["industry"], fixed["label_source"]) == ("Fintech", "approver")
    assert hand_check.tally(ctx, WEEK) == (7, 10)
    one = next(f["value"] for f in judgements(ctx) if f["account_id"] == account(ctx, d[0])["account_id"])
    assert (one["decision"], one["contact_ok"], one["opener_ok"], one["reason"]) == ("fine", "yes", "no", "")
    assert one["gate_version"] == hand_check.current(ctx)["payload"]["gate_version"]
    assert one["snapshot"]["tier"] == "Priority"
    # Every sampled row is applied: the week closed, the two left out are its pulls, and the channel was told.
    item = hand_check.current(ctx)
    pulled = sorted(account(ctx, x)["account_id"] for x in d[7:9])
    assert out["closed"] == [WEEK] and item["status"] == "handled"
    assert sorted(item["payload"]["pulled_account_ids"]) == pulled and item["payload"]["released"] == []
    assert posts(t)[-1].json["text"].startswith(":white_check_mark: *Weekly hand-check done by the Hand-check tab*")
    # Applied once: the next poll does nothing.
    assert tab.apply(ctx)["applied"] == []
    # Harry changes his mind on one leave out.
    decide(ctx, d[7], "fine", reason="")
    out = tab.apply(ctx)
    assert out["applied"] == [tab.row_id(WEEK, d[7])]
    back = account(ctx, d[7])
    # Scored again at once: no signal evidence gives score 0, below standard_threshold (20), so Control, a queue tier
    # (IL is active), and the next enrol can take it.
    assert (back["status"], back["status_reason"], back["tier"], back["score"]) == ("verified", None, "Control", 0)
    assert len(judgements(ctx)) == 10 and hand_check.tally(ctx, WEEK) == (8, 10)
    assert back["account_id"] not in hand_check.current(ctx)["payload"]["pulled_account_ids"]
    applied = next(r["applied"] for r in sheet_rows(ctx) if r["domain"] == d[7])
    assert applied.startswith(f"{NOW.date().isoformat()}: fine; back to verified · ")


def test_a_wrong_industry_without_a_label_waits_and_is_applied_once_filled(monkeypatch):
    """Probe 1 of the review (B1): before, the row got "not done" in applied, the week closed, and filling right_label
    later did nothing. Now applied stays blank, the note says what to fill, 9 facts, and the week stays open; once
    right_label is filled the row is applied: 10 facts, the industry corrected, the week closed."""
    ctx, _ = drawn_week(monkeypatch)
    d = sample_domains(ctx)
    for x in d[1:]:
        decide(ctx, x, "fine")
    decide(ctx, d[0], "wrong industry", note="they make software")
    out = tab.apply(ctx)
    row = next(r for r in sheet_rows(ctx) if r["domain"] == d[0])
    assert row["applied"] == "" and row["note"] == "they make software — not done: pick the right industry in right_label"
    assert out["not_done"] == [row["id"]] and out["closed"] == [] and len(judgements(ctx)) == 9
    assert hand_check.current(ctx)["status"] == "open"
    assert tab.apply(ctx)["applied"] == [] and ctx.clients.sheets.writes.count(f"update {tab.TAB}") == 10  # no rewrite
    row["right_label"] = "Fintech"
    out = tab.apply(ctx)
    assert out["applied"] == [row["id"]] and out["closed"] == [WEEK] and len(judgements(ctx)) == 10
    assert account(ctx, d[0])["industry"] == "Fintech"
    assert row["note"] == "they make software" and " · " in row["applied"]
    [corrected] = facts_of(ctx, "label_corrected")
    assert corrected["value"]["from"] == "Advertising agencies"


def test_a_mistyped_leave_out_is_not_applied_and_the_company_stays_held(monkeypatch):
    """Probe 2 (B1): "leave-out" is not one of the decisions. Before, the week closed and the company was emailed. Now
    nothing is applied, the note names the decisions, the company stays verified, and, with hand_check_gate no, it is
    held (enrol leaves it out) until it is judged or 7 days pass."""
    ctx, _ = drawn_week(monkeypatch, settings=general(hand_check_gate=False))
    d = sample_domains(ctx)
    for x in d[1:]:
        decide(ctx, x, "fine")
    decide(ctx, d[0], "leave-out")
    out = tab.apply(ctx)
    row = next(r for r in sheet_rows(ctx) if r["domain"] == d[0])
    assert row["note"] == "not done: the decision must be one of fine, leave out, wrong industry" and row["applied"] == ""
    assert out["closed"] == [] and account(ctx, d[0])["status"] == "verified"
    aid = account(ctx, d[0])["account_id"]
    assert hand_check.held(ctx) == frozenset({aid})
    why, out_ids = enrol.hand_check(ctx, ctx.now_et().date())
    assert why is None and out_ids == frozenset({aid})
    ctx.now = NOW + timedelta(days=7)
    assert hand_check.held(ctx) == frozenset()


def test_a_leave_out_whose_company_is_gone_closes_the_week_without_an_error(monkeypatch):
    """Probe 3 (B2): before, hand_check.approve raised on the unknown domain at every poll (208 times in 17 hours on
    15 Oct) and the week never closed. Now the row says so, counts as settled, and the week closes once the rest are
    done, with no error."""
    ctx, _ = drawn_week(monkeypatch)
    d = sample_domains(ctx)
    for x in d[1:]:
        decide(ctx, x, "fine")
    decide(ctx, d[0], "leave out", domain="gone-company.com")
    out = tab.apply(ctx)
    assert out["hand_check_errors"] == [] and out["closed"] == [WEEK]
    row = next(r for r in sheet_rows(ctx) if r["domain"] == "gone-company.com")
    assert row["note"] == "not done: no company with this domain" and row["applied"] == ""
    assert hand_check.current(ctx)["status"] == "handled" and len(judgements(ctx)) == 9


def test_a_fine_leaves_its_judgement(monkeypatch):
    """Probe 4 (B4): before, a fine on a sample row wrote nothing to the database."""
    ctx, _ = drawn_week(monkeypatch)
    decide(ctx, sample_domains(ctx)[0], "fine")
    tab.apply(ctx)
    [fact] = judgements(ctx)
    assert fact["value"]["decision"] == "fine" and fact["event_id"] == hand_check.event_id(WEEK, fact["account_id"])


def test_a_sheets_failure_on_row_five_stops_the_pass_and_the_next_poll_goes_on(monkeypatch):
    """Probe 5 (B3): before, one failing row made every earlier row re-apply every 5 minutes (3 doubt_cleared facts
    after 3 failing polls). Now each row's applied cell is written as soon as it is done: a 503 on the 5th write leaves
    rows 1-4 applied once and one error; the next poll applies rows 5-10. One judgement fact a row, and one
    label_corrected fact for the one wrong industry, however often the pass ran."""
    ctx, _ = drawn_week(monkeypatch, sheet=FlakySheet, fail=[5])
    d = sample_domains(ctx)
    for x in d[:9]:
        decide(ctx, x, "fine")
    decide(ctx, d[9], "wrong industry", right_label="Fintech")
    ids = [r["id"] for r in sheet_rows(ctx) if r["check"] == tab.SAMPLE]
    first = tab.apply(ctx)
    assert first["applied"] == ids[:5] and len(first["hand_check_errors"]) == 1  # the 5th was done, not marked
    assert "could not be written (ApiError" in first["hand_check_errors"][0] and first["closed"] == []
    assert [r["applied"] != "" for r in sheet_rows(ctx)] == [True] * 4 + [False] * 6
    second = tab.apply(ctx)
    assert second["applied"] == ids[4:] and second["hand_check_errors"] == [] and second["closed"] == [WEEK]
    assert len(judgements(ctx)) == 10 and len(facts_of(ctx, "label_corrected")) == 1
    assert tab.apply(ctx)["applied"] == []


def test_a_left_out_company_already_emailed_is_not_changed(monkeypatch):
    ctx, _ = drawn_week(monkeypatch)
    x = sample_domains(ctx)[0]
    ctx.store.update("accounts", {"domain": x}, {"status": "enrolled"})
    decide(ctx, x, "leave out", reason="subsidiary")
    tab.apply(ctx)
    assert account(ctx, x)["status"] == "enrolled"
    [fact] = judgements(ctx)
    assert (fact["value"]["decision"], fact["value"]["reason"]) == ("leave out", "subsidiary")
    assert "emailed already, so only the judgement is recorded" in next(r for r in sheet_rows(ctx) if r["domain"] == x)["applied"]


def test_an_unknown_reason_is_read_as_other_and_its_words_kept(monkeypatch):
    ctx, _ = drawn_week(monkeypatch)
    x = sample_domains(ctx)[0]
    decide(ctx, x, "leave out", reason="a franchise of a bigger chain")
    tab.apply(ctx)
    [fact] = judgements(ctx)
    assert (fact["value"]["reason"], fact["value"]["reason_text"]) == ("other", "a franchise of a bigger chain")
    assert account(ctx, x)["tier_reason"] == f"hand_check: other ({WEEK})"


def test_a_dry_run_says_what_it_would_apply_and_changes_nothing(monkeypatch):
    ctx, _ = drawn_week(monkeypatch)
    x = sample_domains(ctx)[0]
    decide(ctx, x, "leave out")
    dry = make_context(ctx.settings, live=False, store=ctx.store, now=ctx.now)
    dry.clients.sheets = ctx.clients.sheets
    assert tab.apply(dry)["would_apply"] == [f"{x}: leave out"]
    assert account(ctx, x)["status"] == "verified" and judgements(ctx) == []


def test_the_errors_reach_heartbeat_check(monkeypatch):
    """hand_check_errors is a list under a key ending _errors, which ops/job_errors finds anywhere in the detail."""
    run = {"job": "poll_approvals", "status": "ok", "started_at": NOW, "finished_at": NOW,
           "detail": {"hand_check": {"applied": [], "hand_check_errors": ["W44:x.com: the tab could not be written"]}}}
    [found] = job_errors.findings("poll_approvals", run)
    assert found.kind == "errors" and found.texts == ["W44:x.com: the tab could not be written"]


# -- the week: closing, the reminder, two weeks on the tab --------------------------------------------------------


def test_one_reminder_from_wednesday_09_00_while_rows_wait(monkeypatch):
    """Drawn Monday 26 Oct; no decision. Tuesday: no reminder. Wednesday 28 Oct 09:05 UK: one, mentioning Harry, with
    the 10 waiting. 09:10 and Thursday: none more."""
    ctx, t = drawn_week(monkeypatch, settings=general(hand_check_gate=False))
    before = len(posts(t))
    for when in (datetime(2026, 10, 27, 12, 0, tzinfo=UTC), datetime(2026, 10, 28, 9, 5, tzinfo=UTC),
                 datetime(2026, 10, 28, 9, 10, tzinfo=UTC), datetime(2026, 10, 29, 9, 0, tzinfo=UTC)):
        ctx.now = when
        tab.apply(ctx)
        if when.day == 27:
            assert len(posts(t)) == before
    assert len(posts(t)) == before + 1
    text = posts(t)[-1].json["text"]
    assert text.startswith("<@U_HARRY> :warning: *Weekly hand-check: 10 companies of Week 44 still need a decision*")
    assert text.endswith("those companies wait for it, for 7 days from Monday at most; sending does not._")


def test_a_week_left_seven_days_closes_by_itself_and_releases_the_rest(monkeypatch):
    ctx, t = drawn_week(monkeypatch, settings=general(hand_check_gate=False))
    d = sample_domains(ctx)
    decide(ctx, d[0], "fine")
    tab.apply(ctx)
    ctx.now = NOW + timedelta(days=7)
    sent = len(posts(t))
    out = tab.apply(ctx)
    item = hand_check.current(ctx, WEEK)
    assert out["closed"] == [WEEK] and item["status"] == "handled" and len(item["payload"]["released"]) == 9
    assert len(posts(t)) == sent  # no confirmation: the Monday readout says it


def test_the_tab_keeps_two_weeks_after_two_draws_and_every_judged_row_has_a_fact(monkeypatch):
    ctx, _ = drawn_week(monkeypatch)
    for x in sample_domains(ctx):
        decide(ctx, x, "fine")
    tab.apply(ctx)
    ctx.now = NOW + timedelta(days=7)  # Monday 2 Nov, W45
    assert hand_check.run(ctx)["status"] == "recorded"
    weeks = Counter(r["week"] for r in sheet_rows(ctx))
    assert weeks == {WEEK: 10, "2026-W45": 10}
    for r in sheet_rows(ctx):
        if r["week"] == "2026-W45":
            r["decision"] = "fine"
    tab.apply(ctx)
    facts = judgements(ctx)
    assert len(facts) == 20 and all(f["value"]["gate_version"] for f in facts)
    assert Counter(f["value"]["week"] for f in facts) == {WEEK: 10, "2026-W45": 10}


# -- the companies held for doubtful facts ----------------------------------------------------------------------


def doubtful_week(monkeypatch, n=3):
    """No company is ready beyond today's (today's number 14 of 14), so the week's item holds only the n companies
    verify_accounts holds for doubtful facts (no HQ state): mar-070, mar-071, ..."""
    monkeypatch.setattr(hand_check, "today_number", lambda ctx: 14)
    ctx, t = world()
    ctx.clients.sheets = Sheet(ctx.guard, {"Overrides": []})
    held = [acct(70 + i, status="queued", employees=None, size_band="", hq_state="") for i in range(n)]
    ctx.store.insert("accounts", held)
    ctx.store.insert("signal_events", [verify.doubt_fact(ctx, a, [verify.NO_STATE]) for a in held])
    assert hand_check.run(ctx)["status"] == "recorded"
    return ctx, t


def test_a_doubtful_week_writes_one_row_each_with_where_to_fill_the_fact(monkeypatch):
    ctx, t = doubtful_week(monkeypatch)
    rows = sheet_rows(ctx)
    assert [r["domain"] for r in rows] == ["mar70.com", "mar71.com", "mar72.com"]
    assert rows[0]["id"] == f"{WEEK}:mar70.com" and rows[0]["check"] == tab.DOUBT
    assert rows[0]["why"] == f"{verify.NO_STATE} (add the missing fact on the Overrides tab: hq_state or employees)"
    assert posts(t)[-1].json["text"].splitlines()[1] == "Week 44: 3 with a missing HQ state, size or industry."


def test_doubtful_rows_are_applied_and_the_week_closes_when_each_is_settled(monkeypatch):
    ctx, _ = doubtful_week(monkeypatch)
    decide(ctx, "mar70.com", "leave out")
    decide(ctx, "mar71.com", "fine")
    out = tab.apply(ctx)
    assert sorted(out["applied"]) == [f"{WEEK}:mar70.com", f"{WEEK}:mar71.com"] and out["closed"] == []
    left = account(ctx, "mar70.com")
    assert (left["status"], left["tier"], left["status_reason"]) == ("disqualified", "Excluded", "hand_check")
    [cleared] = facts_of(ctx, verify.CLEARED_FACT)
    assert cleared["account_id"] == "mar-071" and cleared["event_id"] == verify.cleared_id(WEEK, "mar-071")
    ctx.store.update("accounts", {"account_id": "mar-072"}, {"status": "disqualified"})  # settled without Harry
    out = tab.apply(ctx)
    assert out["closed"] == [WEEK]
    assert hand_check.current(ctx)["payload"]["pulled_account_ids"] == ["mar-070"]
    # Changed back to fine, the doubtful company is new again, for verify_accounts to decide on.
    decide(ctx, "mar70.com", "fine")
    tab.apply(ctx)
    assert account(ctx, "mar70.com")["status"] == "new"


def test_a_week_written_before_the_tab_existed_gets_its_rows_at_the_next_poll(monkeypatch):
    ctx, _ = doubtful_week(monkeypatch)
    ctx.clients.sheets.tabs.pop(tab.TAB)
    out = tab.apply(ctx)
    assert out["written"] == 3 and len(sheet_rows(ctx)) == 3


# -- the rows applied before W1-5, and the header move ----------------------------------------------------------------


W43 = "2026-W43"
EXPORT_AT = datetime(2026, 10, 19, 13, 30, tzinfo=UTC)  # Mon 19 Oct, 14:30 UK: the export the afternoon W0 deploys
HEADER = [*tab.OLD_COLUMNS, "reason"]  # the tab as it stood on 19 Oct, with a column Harry added on the right


def w43_decision(i: int) -> tuple[str, str]:
    """W43 as Harry judged it: 48 random rows, 38 fine (0-37), 8 leave out (38-45), 2 wrong industry (46, 47)."""
    if i < 38:
        return tab.FINE, ""
    return (tab.LEAVE_OUT, "") if i < 46 else (tab.WRONG_INDUSTRY, "Fintech")


def w43_world(*, live: bool = True):
    """The W43 tab of 19 Oct under the old 15 columns: 48 random-sample rows and 1 doubtful-facts row (fine), 49 in
    all, every one decided and applied by the code before W1-5, and the 49 companies in the database; plus the 10
    companies W44's sample draws."""
    ctx = make_context(SETTINGS, live=live, now=EXPORT_AT)
    companies = [acct(i, source="apollo", label_source="rules+model", score=55) for i in range(48)]
    companies.append(acct(60, status="queued", source="site_visit", label_source="model", hq_state="GA", score=55))
    companies += [acct(100 + i) for i in range(10)]
    ctx.store.insert("accounts", companies)
    rows = []
    for i, a in enumerate(companies[:49]):
        decision, right = w43_decision(i) if i < 48 else (tab.FINE, "")
        done = {tab.FINE: "fine", tab.LEAVE_OUT: "left out", tab.WRONG_INDUSTRY: "industry set to Fintech"}[decision]
        rows.append({"id": tab.row_id(W43, a["domain"]), "week": W43, "company": a["clean_name"], "domain": a["domain"],
                     "check": tab.SAMPLE if i < 48 else tab.DOUBT, "why": "", "hq_state": a["hq_state"],
                     "size": "20-49 (40 staff)", "industry": a["industry"], "decision": decision,
                     "right_label": right, "right_hq_state": "", "right_employees": "",
                     "note": "a law firm's marketing arm" if i == 40 else "", "applied": f"2026-10-19: {done}",
                     "reason": ""})
    ctx.clients.sheets = Sheet(ctx.guard, {tab.TAB: rows})
    return ctx, companies


def w44_payload(companies):
    return {"iso_week": "2026-W44", "accounts": companies[49:59], "doubtful": [], "gate_version": "h:legacy"}


def test_rows_applied_before_w1_5_are_adopted_not_applied_again():
    """The 49 W43 rows carry the old applied text and no hash. The first poll writes one judgement fact each and adds
    the hash, changing no company (the 8 left out stay as the old code left them; nothing is disqualified again or
    relabelled twice); the next poll does nothing. Then one old leave out changed to fine is applied as usual."""
    ctx, companies = w43_world()
    ctx.now = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
    for a in companies[38:46]:  # as the old code left them
        ctx.store.update("accounts", {"account_id": a["account_id"]},
                         {"status": "disqualified", "tier": "Excluded", "tier_reason": f"left out at the hand-check ({W43})"})
    before = {a["account_id"]: dict(a) for a in ctx.store.select("accounts")}
    out = tab.apply(ctx)
    assert (out["adopted"], out["applied"], out["hand_check_errors"]) == (49, [], [])
    assert len(judgements(ctx)) == 49 and facts_of(ctx, "label_corrected") == []
    assert {a["account_id"]: a for a in ctx.store.select("accounts")} == before
    assert all(" · " in r["applied"] for r in sheet_rows(ctx))
    assert tab.apply(ctx)["adopted"] == 0
    row = next(r for r in sheet_rows(ctx) if r["domain"] == companies[38]["domain"])
    row["decision"] = tab.FINE
    assert tab.apply(ctx)["applied"] == [row["id"]]
    assert account(ctx, row["domain"])["status"] == "verified"


def test_an_old_not_done_row_is_applied_now():
    """An old "not done" in applied (B1: the wrong industry whose right_label came later) is applied at the next poll."""
    ctx, companies = w43_world()
    row = next(r for r in sheet_rows(ctx) if r["decision"] == tab.WRONG_INDUSTRY)
    row["applied"] = "not done: pick the right industry in right_label"
    out = tab.apply(ctx)
    assert out["applied"] == [row["id"]] and account(ctx, row["domain"])["industry"] == "Fintech"


def test_migrate_tab_moves_every_old_value_under_its_new_column_after_a_backup():
    """49 rows under the old 15 columns plus Harry's reason. Dry-run: the headers before and after, nothing written.
    Live: a backup tab holding the 49 rows exactly as they were; the tab under COLUMNS (company -> name, hq_state ->
    state, every other old column under its own name; right_hq_state and right_employees, empty on every row, are
    dropped); and the dropdowns set on their new places. A second run moves nothing."""
    dry, _ = w43_world(live=False)
    out = tab.migrate(dry)
    assert (out["rows"], out["before"], out["after"], out["written"]) == (49, HEADER, tab.COLUMNS, False)
    assert list(dry.clients.sheets.tabs) == [tab.TAB]

    ctx, companies = w43_world()
    ctx.clients.sheets.tabs["Industries"] = [{"industry_group": AGENCIES_GROUP, "industry": "Advertising agencies"}]
    old = [dict(r) for r in sheet_rows(ctx)]
    out = tab.migrate(ctx)
    assert out["written"] is True and out["backup"] == "Hand-check backup 2026-10-19"
    assert ctx.clients.sheets.tabs["Hand-check backup 2026-10-19"] == [{h: r[h] for h in HEADER} for r in old]
    rows = sheet_rows(ctx)
    assert len(rows) == 49 and all(list(r) == tab.COLUMNS for r in rows)
    for new, was in zip(rows, old, strict=True):
        assert (new["name"], new["state"]) == (was["company"], was["hq_state"])
        for c in ("id", "week", "domain", "check", "why", "size", "industry", "decision", "right_label", "note",
                  "applied", "reason"):
            assert new[c] == was[c], c
    assert rows[40]["note"] == "a law firm's marketing arm"
    dropdowns = ctx.clients.sheets.validation[tab.TAB]
    assert dropdowns["headers"] == tab.COLUMNS
    assert dropdowns["dropdowns"] == {"decision": tab.DECISIONS, "reason": tab.REASONS, "contact_ok": tab.YES_NO,
                                      "opener_ok": tab.YES_NO, "right_label": "'Industries'!B2:B"}
    again = tab.migrate(ctx)
    assert (again["already"], again["written"]) == (True, False) and len(sheet_rows(ctx)) == 49


AGENCIES_GROUP = "Marketing & Creative Agencies"


def test_a_column_with_values_and_no_place_is_kept_at_the_right():
    ctx, _ = w43_world()
    sheet_rows(ctx)[0]["right_hq_state"] = "IL"
    out = tab.migrate(ctx)
    assert out["after"] == [*tab.COLUMNS, "right_hq_state"]
    assert sheet_rows(ctx)[0]["right_hq_state"] == "IL" and sheet_rows(ctx)[1]["right_hq_state"] == ""


def test_handcheck_migrate_tab_from_the_command_line(capsys):
    from us_outbound.ops import cli

    dry, _ = w43_world(live=False)
    assert cli.main(["handcheck", "migrate-tab"], context_factory=lambda job, live, **kw: dry) == 0
    printed = capsys.readouterr().out
    assert f"Header after: {', '.join(tab.COLUMNS)}" in printed
    assert "Would move it, after a backup to the tab 'Hand-check backup 2026-10-19'." in printed


# -- W0, 19 Oct 2026: the tab keeps every week, and the judgements go into the database --------------------------------


def test_the_next_week_is_added_under_the_last_and_no_row_is_lost():
    """Before W0, write() rewrote the tab with the new week's rows alone: Mon 26 Oct's W44 post would have dropped the
    49 W43 rows, the only record of the 38 fines. Now 49 + 10 = 59 rows, the 49 exactly as they were (Harry's reason
    column included), by one append and no rewrite, each new row placed under the old header (name under company)."""
    ctx, companies = w43_world()
    before = [dict(r) for r in sheet_rows(ctx)]
    assert tab.write(ctx, w44_payload(companies)) == {"rows": 10, "added": 10, "written": True}
    rows = sheet_rows(ctx)
    assert len(rows) == 59 and rows[:49] == before
    assert [r["week"] for r in rows[49:]] == ["2026-W44"] * 10
    assert [(r["domain"], r["company"]) for r in rows[49:]] == [(a["domain"], a["clean_name"]) for a in companies[49:59]]
    assert ctx.clients.sheets.writes == [f"append {tab.TAB}"]  # never a replace
    rows[49]["decision"] = tab.LEAVE_OUT
    assert tab.write(ctx, w44_payload(companies)) == {"rows": 10, "added": 0, "written": True}
    assert len(sheet_rows(ctx)) == 59 and sheet_rows(ctx)[49]["decision"] == tab.LEAVE_OUT


def test_the_append_goes_through_the_sheets_api_under_the_header_as_it_stands():
    """The same week through the real client: the tab is read, its header row read, and the 10 rows appended, each
    placed by that header (the new columns it lacks left out, Harry's reason column blank); nothing is cleared."""
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


def test_export_writes_one_judgement_per_decided_row_and_again_changes_nothing():
    """49 decided rows -> 49 facts. Counted from the facts, the 48 random rows are 38 fine, 8 leave out and 2 wrong
    industry (w43_decision), the W43 tally; the doubtful row is told apart by its check. A second export finds the
    same 49 event ids, so it updates them and adds none. The facts are the ones apply writes."""
    dry, _ = w43_world(live=False)
    out = tab.export(dry)
    assert (out["facts"], out["new"], out["written"]) == (49, 49, False) and judgements(dry) == []

    ctx, companies = w43_world()
    out = tab.export(ctx)
    assert (out["rows"], out["decided"], out["facts"], out["new"], out["written"]) == (49, 49, 49, 49, True)
    assert out["weeks"] == {W43: {tab.FINE: 39, tab.LEAVE_OUT: 8, tab.WRONG_INDUSTRY: 2}} and out["skipped"] == []
    facts = judgements(ctx)
    random = Counter(f["value"]["decision"] for f in facts if f["value"]["check"] == tab.SAMPLE)
    assert random == {tab.FINE: 38, tab.LEAVE_OUT: 8, tab.WRONG_INDUSTRY: 2}
    assert hand_check.tally(ctx, W43) == (38, 48)
    first = companies[40]
    [one] = [f for f in facts if f["account_id"] == first["account_id"]]
    assert one["event_id"] == f"hand_check:{W43}:{first['account_id']}"
    assert one["value"] == {"week": W43, "row_id": tab.row_id(W43, first["domain"]), "check": tab.SAMPLE,
                            "decision": tab.LEAVE_OUT, "reason": "", "right_label": "", "contact_ok": "",
                            "opener_ok": "", "note": "a law firm's marketing arm",
                            "gate_version": hand_check.gate_version(ctx),
                            "snapshot": {"tier": "Priority", "score": 55, "industry_group": AGENCIES_GROUP,
                                         "industry": "Advertising agencies", "size_band": "20-49", "hq_state": "IL",
                                         "source": "apollo", "label_source": "rules+model"}}
    ctx.store.update("accounts", {"account_id": first["account_id"]}, {"tier": "Excluded"})
    sheet_rows(ctx)[0]["decision"] = tab.LEAVE_OUT
    out = tab.export(ctx)
    assert (out["facts"], out["new"], out["already"]) == (49, 0, 49)
    facts = {f["account_id"]: f for f in judgements(ctx)}
    assert len(facts) == 49 and facts[companies[0]["account_id"]]["value"]["decision"] == tab.LEAVE_OUT
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


def test_handcheck_export_from_the_command_line(capsys):
    from us_outbound.ops import cli

    dry, _ = w43_world(live=False)
    assert cli.main(["handcheck", "export"], context_factory=lambda job, live, **kw: dry) == 0
    assert "Would write 49 hand-check judgements (49 new) from 49 decided rows of 49." in capsys.readouterr().out
    live, _ = w43_world()
    assert cli.main(["handcheck", "export", "--live"], context_factory=lambda job, live_flag, **kw: live) == 0
    assert "Wrote 49 hand-check judgements (49 new)" in capsys.readouterr().out and len(judgements(live)) == 49


@pytest.mark.parametrize("typed, code", [("media_publisher", "media_publisher"), ("Media publisher", "media_publisher"),
                                         ("IT services / outsourcing", "it_services_outsourcing"), ("", ""),
                                         ("franchise", "other")])
def test_the_reason_is_read_whichever_way_it_is_typed(typed, code):
    assert tab._reason({"reason": typed}) == code
