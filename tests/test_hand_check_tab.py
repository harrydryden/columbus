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
    monkeypatch.setattr(hand_check, "today_number", lambda ctx, **kw: 2)
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
    # The 5th was done but not marked: unmarked, not applied (the review's round 2: applied listed it, one too many).
    assert (first["applied"], first["unmarked"]) == (ids[:4], ids[4:5]) and len(first["hand_check_errors"]) == 1
    assert "could not be written (ApiError" in first["hand_check_errors"][0] and first["closed"] == []
    assert [r["applied"] != "" for r in sheet_rows(ctx)] == [True] * 4 + [False] * 6
    second = tab.apply(ctx)
    assert second["applied"] == ids[4:] and second["hand_check_errors"] == [] and second["closed"] == [WEEK]
    assert len(judgements(ctx)) == 10 and len(facts_of(ctx, "label_corrected")) == 1
    assert tab.apply(ctx)["applied"] == []


def test_a_correction_applied_twice_is_one_fact_that_keeps_the_label_it_went_out_as(monkeypatch):
    """A wrong industry whose applied cell cannot be written (a 503 on the first write) is applied again at the next
    poll: still one label_corrected fact (B3: before, each pass added one, padding the labels' answer key), and its
    "from" is the label the company went out as, Advertising agencies, not Fintech, which the account holds by then."""
    ctx, _ = drawn_week(monkeypatch, sheet=FlakySheet, fail=[1])
    x = sample_domains(ctx)[0]
    decide(ctx, x, "wrong industry", right_label="Fintech")
    assert len(tab.apply(ctx)["hand_check_errors"]) == 1
    assert tab.apply(ctx)["applied"] == [tab.row_id(WEEK, x)]
    [corrected] = facts_of(ctx, "label_corrected")
    assert (corrected["value"]["from"], corrected["value"]["to"]) == ("Advertising agencies", "Fintech")
    assert corrected["event_id"] == f"label_corrected:{WEEK}:{account(ctx, x)['account_id']}"


def test_a_changed_reason_alone_is_applied_again(monkeypatch):
    """The reason is part of the decision's hash: media_publisher changed to subsidiary on a row already applied is
    applied again, and the company's tier_reason and the fact say subsidiary."""
    ctx, _ = drawn_week(monkeypatch)
    x = sample_domains(ctx)[0]
    decide(ctx, x, "leave out", reason="media_publisher")
    tab.apply(ctx)
    decide(ctx, x, "leave out", reason="subsidiary")
    assert tab.apply(ctx)["applied"] == [tab.row_id(WEEK, x)]
    assert account(ctx, x)["tier_reason"] == f"hand_check: subsidiary ({WEEK})"
    [fact] = judgements(ctx)
    assert fact["value"]["reason"] == "subsidiary"


@pytest.mark.parametrize("status", ["enrolled", "engaged", "demo_requested", "demo_booked"])
def test_a_left_out_company_already_emailed_is_not_changed(monkeypatch, status):
    """Every status past verify was emailed (the DDL's account statuses): a leave out records the judgement and never
    disqualifies it (before, demo_requested and demo_booked were disqualified)."""
    ctx, _ = drawn_week(monkeypatch)
    x = sample_domains(ctx)[0]
    ctx.store.update("accounts", {"domain": x}, {"status": status})
    decide(ctx, x, "leave out", reason="subsidiary")
    tab.apply(ctx)
    assert account(ctx, x)["status"] == status
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
    ctx.now = NOW + timedelta(days=7)  # Monday 2 Nov 07:00 UK: W45's draw ran at 05:45 and posted its own notice
    assert hand_check.run(ctx)["status"] == "recorded"
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


def test_a_week_with_fewer_rows_on_the_tab_than_it_drew_stays_open(monkeypatch):
    """The item drew 10 but only 7 of its rows are on the tab (3 not written yet, or deleted by hand), all 7 decided:
    the 3 have no decision, so the week stays open (7 rows < 10 drawn) though no row on the tab waits. The review's
    round 2 found no test failed with that count removed from _close_and_remind."""
    ctx, _ = drawn_week(monkeypatch)
    ctx.clients.sheets.tabs[tab.TAB] = sheet_rows(ctx)[:7]
    for x in sample_domains(ctx):
        decide(ctx, x, "fine")
    out = tab.apply(ctx)
    assert len(out["applied"]) == 7 and out["closed"] == [] and out["hand_check_errors"] == []
    assert hand_check.current(ctx)["status"] == "open"


# -- the catch-up draw: a Monday that did not happen (the review's round 2) -----------------------------------------


def calls_to_number(monkeypatch, n=2):
    """today_number patched to n, counting its calls: one per draw (hand_check.build)."""
    calls = []
    monkeypatch.setattr(hand_check, "today_number", lambda ctx, **kw: calls.append(1) or n)
    return calls


def test_a_failed_monday_draw_is_drawn_by_the_poll_once_its_grace_has_passed(monkeypatch):
    """The gate on (SETTINGS), no W44 item: the 05:45 draw failed. 06:10 UK is inside DRAW_GRACE (05:45 + 30 = 06:15):
    the poll draws nothing, as the job may still be running. 06:20: the poll draws (14 ready, today's number 2:
    positions 3 to 152, 12 accounts, 10 drawn), writes the 10 rows and posts the one notice, so enrol waits for the
    check ("not approved yet") rather than all week for a draw ("has not been posted"). 06:25: nothing more. A dry-run
    poll at 06:20 draws nothing."""
    calls = calls_to_number(monkeypatch)
    ctx, t = world()
    ctx.clients.sheets = Sheet(ctx.guard, {"Overrides": []})
    assert hand_check.draw_due(WEEK) == datetime(2026, 10, 26, 6, 15, tzinfo=UTC)  # GMT since 25 Oct
    ctx.now = datetime(2026, 10, 26, 6, 10, tzinfo=UTC)
    assert "drawn" not in tab.apply(ctx) and hand_check.current(ctx) is None and calls == []
    dry = make_context(SETTINGS, live=False, store=ctx.store, now=datetime(2026, 10, 26, 6, 20, tzinfo=UTC))
    dry.clients.sheets = ctx.clients.sheets
    assert "drawn" not in tab.apply(dry) and hand_check.current(ctx) is None
    ctx.now = datetime(2026, 10, 26, 6, 20, tzinfo=UTC)
    out = tab.apply(ctx)
    item = hand_check.current(ctx)
    assert out["drawn"] == "recorded" and item["status"] == "open" and len(hand_check.sample_ids(item)) == 10
    assert len(sample_domains(ctx)) == 10 and len(posts(t)) == 1 and calls == [1]
    assert enrol.hand_check(ctx, ctx.now_et().date())[0] == f"this week's hand-check ({WEEK}) is not approved yet"
    ctx.now = datetime(2026, 10, 26, 6, 25, tzinfo=UTC)
    assert "drawn" not in tab.apply(ctx) and len(posts(t)) == 1 and calls == [1]


def test_a_gated_week_recorded_with_nothing_to_check_is_drawn_once_an_account_is_ready(monkeypatch):
    """The gate on. 05:45: none of the 14 has a contact yet (pick_contacts still running), so the week is recorded
    nothing to check, handled, and enrol would go ahead all week unchecked. The poll looks for a ready account before
    drawing, so while none is ready it never draws again (one draw so far). Once the 14 have their contacts the
    next poll draws: 10 rows, the item open, and enrol waits for the check."""
    from tests.test_hand_check import person

    calls = calls_to_number(monkeypatch)
    ctx, t = world(contacts=0)
    ctx.clients.sheets = Sheet(ctx.guard, {"Overrides": []})
    assert hand_check.run(ctx)["status"] == hand_check.NOTHING and calls == [1]
    out = tab.apply(ctx)
    assert "drawn" not in out and calls == [1] and hand_check.current(ctx)["payload"]["nothing_to_check"] is True
    fourteen = {f"mar-{i:03d}" for i in range(14)}
    ctx.store.insert("contacts", [person(a) for a in ctx.store.select("accounts") if a["account_id"] in fourteen])
    out = tab.apply(ctx)
    item = hand_check.current(ctx)
    assert out["drawn"] == "recorded" and calls == [1, 1]
    assert item["status"] == "open" and len(hand_check.sample_ids(item)) == 10 and len(sample_domains(ctx)) == 10
    assert enrol.hand_check(ctx, ctx.now_et().date())[0] == f"this week's hand-check ({WEEK}) is not approved yet"


def test_with_the_gate_off_a_week_with_nothing_to_check_is_not_drawn_again_by_the_poll(monkeypatch):
    """The gate off, today's number 14 of 14 ready: nothing beyond today's, recorded nothing to check. It holds nobody,
    so the poll does not draw it again (the known gap: someone runs the draw): still one draw."""
    calls = calls_to_number(monkeypatch, n=14)
    ctx, _ = world(settings=general(hand_check_gate=False))
    ctx.clients.sheets = Sheet(ctx.guard, {"Overrides": []})
    assert hand_check.run(ctx)["status"] == hand_check.NOTHING
    assert "drawn" not in tab.apply(ctx) and calls == [1]


# -- one pass, many rows: each write starts from the item as stored (the W1-5 review) --------------------------------


def ids_of(ctx, domains):
    return [account(ctx, x)["account_id"] for x in domains]


def pulled_now(ctx):
    return hand_check.current(ctx)["payload"]["pulled_account_ids"]


def test_two_leave_outs_in_one_pass_are_both_pulled(monkeypatch):
    """Two leave outs and 8 rows undecided, so the week stays open and close() does not union the facts back in: both
    companies are in pulled_account_ids. Before, each pull rebuilt the payload from the copy read at the start of the
    pass, so only the last one was left."""
    ctx, _ = drawn_week(monkeypatch)
    d = sample_domains(ctx)
    decide(ctx, d[0], "leave out", reason="subsidiary")
    decide(ctx, d[1], "leave out", reason="media_publisher")
    out = tab.apply(ctx)
    assert sorted(out["applied"]) == sorted(tab.row_id(WEEK, x) for x in d[:2]) and out["closed"] == []
    assert pulled_now(ctx) == ids_of(ctx, d[:2])  # in the tab's order, as the rows were applied


def test_a_leave_out_changed_to_fine_in_the_pass_that_closes_the_week_is_emailed(monkeypatch):
    """test_hand_check.enrol_world's 14 ready (h-00 to h-13), the gate on, drawn Tuesday with today's number 2: 10 of
    h-02 to h-13 on the tab. Pass 1: one leave out, 8 fine, 1 undecided: pulled is that one, the week open. Pass 2: the
    leave out changed to fine and the last row fine: the week closes, and the company is verified and in no pull
    (before, close() wrote back the pulls it read before the pass, so enrol skipped it, held or pulled, all week).
    Enrol then takes all 14: today's number is 25 (100 a week over 4 send days), above the 14 ready."""
    from tests.test_hand_check import enrol_world, enrolled

    monkeypatch.setattr(hand_check, "today_number", lambda ctx, **kw: 2)
    ctx, _, _ = enrol_world(gate=True, n=14)
    ctx.clients.sheets = Sheet(ctx.guard, {"Overrides": []})
    assert hand_check.run(ctx)["status"] == "recorded"
    d = sample_domains(ctx)
    [x] = ids_of(ctx, d[:1])
    decide(ctx, d[0], "leave out", reason="subsidiary")
    for y in d[1:9]:
        decide(ctx, y, "fine")
    first = tab.apply(ctx)
    assert first["closed"] == [] and pulled_now(ctx) == [x] and account(ctx, d[0])["status"] == "disqualified"
    decide(ctx, d[0], "fine", reason="")
    decide(ctx, d[9], "fine")
    second = tab.apply(ctx)
    assert second["closed"] == [WEEK] and sorted(second["applied"]) == sorted(tab.row_id(WEEK, y) for y in (d[0], d[9]))
    item = hand_check.current(ctx)
    assert item["status"] == "handled" and item["payload"]["pulled_account_ids"] == []
    assert account(ctx, d[0])["status"] == "verified"
    assert enrol.hand_check(ctx, ctx.now_et().date()) == (None, frozenset())
    went = enrol.run(ctx)
    assert (went["status"], went["enrolled"]) == ("ok", 14) and x in enrolled(ctx)


def test_the_wednesday_reminder_keeps_a_pull_made_in_the_same_pass(monkeypatch):
    """Wednesday 28 Oct 09:05 UK: one leave out applied, 9 rows waiting, so the pass also posts the reminder. The
    reminder's write keeps the pull (before, it wrote back the payload read before the row was applied)."""
    ctx, _ = drawn_week(monkeypatch, settings=general(hand_check_gate=False))
    d = sample_domains(ctx)
    ctx.now = datetime(2026, 10, 28, 9, 5, tzinfo=UTC)
    decide(ctx, d[0], "leave out")
    out = tab.apply(ctx)
    assert out["reminded"] == 9
    p = hand_check.current(ctx)["payload"]
    assert p["pulled_account_ids"] == ids_of(ctx, d[:1]) and p["reminded_at"] == ctx.now.isoformat()


def test_a_pull_made_with_handcheck_approve_stays_whatever_the_tab_says(monkeypatch):
    """`handcheck approve --pull` pulls d[0] (operator_pulled): a fine on its row later leaves it pulled, as that
    pull only ever narrows. A leave out made on the tab (d[1]) is still taken off by a later fine."""
    ctx, _ = drawn_week(monkeypatch)
    d = sample_domains(ctx)
    a0, a1 = ids_of(ctx, d[:2])
    hand_check.approve(ctx, [a0], "harry")
    assert hand_check.current(ctx)["payload"]["operator_pulled"] == [a0]
    decide(ctx, d[0], "fine")
    decide(ctx, d[1], "leave out")
    tab.apply(ctx)
    assert pulled_now(ctx) == [a0, a1]
    decide(ctx, d[1], "fine")
    tab.apply(ctx)
    assert pulled_now(ctx) == [a0]
    assert enrol.hand_check(ctx, ctx.now_et().date()) == (None, frozenset({a0}))


# -- the companies held for doubtful facts ----------------------------------------------------------------------


def doubtful_week(monkeypatch, n=3):
    """No company is ready at all (with the gate on, the pool tops up from position 1, so none must be ready for a
    week of doubts alone), so the week's item holds only the n companies verify_accounts holds for doubtful facts (no
    HQ state): mar-070, mar-071, ..."""
    monkeypatch.setattr(hand_check, "today_number", lambda ctx, **kw: 0)
    ctx, t = world(0)
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
    lines = posts(t)[-1].json["text"].splitlines()
    assert lines[1] == "Week 44: 3 with a missing HQ state, size or industry."
    # The gate on: the open item holds the whole day (enrol.hand_check), so the notice says so, of every row, as none
    # was picked at random (the review's round 2: it said only these companies wait).
    assert lines[0] == ("<@U_HARRY> :warning: *Weekly hand-check: 3 companies to look at before this week's new "
                        "emails go out*")
    assert lines[-1] == "_Why it matters: until every row has a decision, no new leads go to Instantly this week._"
    assert enrol.hand_check(ctx, ctx.now_et().date())[0] == f"this week's hand-check ({WEEK}) is not approved yet"


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


# -- migrate-tab never loses the rows: the review's round 2 ----------------------------------------------------------

BACKUP = "Hand-check backup 2026-10-19"  # EXPORT_AT is Monday 19 Oct 14:30 UK (13:30 UTC, BST)


class ClearThenFailSheet(Sheet):
    """replace_tab on the Hand-check tab the first time: the clear goes through and the write answers 503, as the real
    client's two calls can (clients/sheets.replace_tab: values.clear, then values.update)."""

    def __init__(self, guard, tabs):
        super().__init__(guard, tabs)
        self.failed = False

    def replace_tab(self, sheet_id, name, headers, rows):
        if name == tab.TAB and not self.failed:
            self.failed = True
            self.tabs[name] = []
            raise ApiError("sheets", 503, "The service is currently unavailable.", "https://sheets.googleapis.com/")
        return super().replace_tab(sheet_id, name, headers, rows)


class ShortBackupSheet(Sheet):
    """Writes only the first 10 rows of any tab but the Hand-check tab: a backup that is not whole."""

    def replace_tab(self, sheet_id, name, headers, rows):
        return super().replace_tab(sheet_id, name, headers, rows if name == tab.TAB else rows[:10])


def as_backed_up(rows):
    return [{h: r[h] for h in HEADER} for r in rows]


def test_a_migrate_that_clears_the_tab_and_then_fails_keeps_the_backup_and_the_next_run_writes_nothing():
    """The blocking probe of the review's round 2 (w15probe_migrate.py): run 1 backs up the 49 rows, then the rewrite
    clears the tab and fails. Before, run 2 read the empty tab, wrote the empty table over the backup (49 rows -> 0)
    and a bare header to the tab: every decision gone. Now run 1 raises with a note naming the backup, which holds the
    49 rows as they were; run 2 sees an empty tab and writes nothing at all (no backup, no header, no new tab). Harry
    copies the rows back, and run 3 moves them after a backup of its own: today's name holds rows, so it carries the
    time, 1430, and the first backup is left exactly as it was."""
    ctx, _ = w43_world()
    old = [dict(r) for r in sheet_rows(ctx)]
    s = ctx.clients.sheets = ClearThenFailSheet(ctx.guard, ctx.clients.sheets.tabs)
    with pytest.raises(ApiError) as failed:
        tab.migrate(ctx)
    assert f"its 49 rows are on the tab {BACKUP!r}, as they were" in " ".join(failed.value.__notes__)
    assert sheet_rows(ctx) == [] and s.tabs[BACKUP] == as_backed_up(old)
    again = tab.migrate(ctx)
    assert (again["rows"], again["written"], again["backup"], again["note"]) == (0, False, None, tab.EMPTY)
    assert sorted(s.tabs) == sorted([tab.TAB, BACKUP]) and sheet_rows(ctx) == []
    assert s.tabs[BACKUP] == as_backed_up(old) and s.validation == {}
    s.tabs[tab.TAB] = [dict(r) for r in s.tabs[BACKUP]]
    third = tab.migrate(ctx)
    assert (third["written"], third["backup"]) == (True, f"{BACKUP} 1430")
    assert s.tabs[f"{BACKUP} 1430"] == s.tabs[BACKUP] == as_backed_up(old)
    rows = sheet_rows(ctx)
    assert len(rows) == 49 and all(list(r) == tab.COLUMNS for r in rows)
    assert [r["name"] for r in rows] == [r["company"] for r in old]


def test_a_backup_that_does_not_hold_every_row_stops_the_move_before_the_tab_is_touched():
    """The backup is read back before the tab is cleared: 10 rows of 49 there, so it raises, and the tab keeps its 49
    rows under the old header."""
    ctx, _ = w43_world()
    old = [dict(r) for r in sheet_rows(ctx)]
    ctx.clients.sheets = ShortBackupSheet(ctx.guard, ctx.clients.sheets.tabs)
    with pytest.raises(RuntimeError, match=f"the backup tab {BACKUP!r} holds 10 rows, not 49"):
        tab.migrate(ctx)
    assert sheet_rows(ctx) == old


def test_migrate_refuses_when_both_backup_names_hold_rows_and_writes_nothing():
    ctx, _ = w43_world()
    s = ctx.clients.sheets
    s.tabs[BACKUP] = [{"id": "x"}]
    s.tabs[f"{BACKUP} 1430"] = [{"id": "y"}]
    before = {k: [dict(r) for r in v] for k, v in s.tabs.items()}
    with pytest.raises(ValueError, match="both hold rows already"):
        tab.migrate(ctx)
    assert s.tabs == before


def test_migrate_refuses_while_this_week_s_hand_check_is_open():
    """W43's item open (its rows not all decided, or not closed yet): a live run refuses (ValueError, the command's exit
    2) and writes nothing, as a decision typed while the tab moves could be lost, and with the tab cleared
    poll_approvals would put this week's rows alone on it. A dry run says a live run would refuse. Once the item is
    handled the move goes ahead."""
    from us_outbound.ops import cli

    ctx, _ = w43_world()
    ctx.store.upsert("hitl_items", [{"item_id": f"hand_check-{W43}", "kind": "hand_check", "status": "open",
                                     "created_at": EXPORT_AT,
                                     "payload": {"iso_week": W43, "drawn_at": EXPORT_AT.isoformat(), "accounts": []}}])
    old = [dict(r) for r in sheet_rows(ctx)]
    with pytest.raises(ValueError, match=r"this week's hand-check \(2026-W43\) is still open"):
        tab.migrate(ctx)
    assert list(ctx.clients.sheets.tabs) == [tab.TAB] and sheet_rows(ctx) == old
    assert cli.main(["handcheck", "migrate-tab", "--live"], context_factory=lambda job, live, **kw: ctx) == 2
    dry = make_context(SETTINGS, live=False, store=ctx.store, now=EXPORT_AT)
    dry.clients.sheets = ctx.clients.sheets
    assert tab.migrate(dry)["refused"].startswith("this week's hand-check (2026-W43) is still open")
    ctx.store.update("hitl_items", {"item_id": f"hand_check-{W43}"}, {"status": "handled"})
    assert tab.migrate(ctx)["written"] is True and len(sheet_rows(ctx)) == 49


def test_a_value_in_a_column_apply_never_reads_is_named_on_the_row_it_applies():
    """Row 0 decided fine and not applied yet, with GA typed in right_hq_state, which migrate-tab keeps at the right as
    it holds a value. apply applies the fine, leaves the company's state as it was (IL: the column is not read; the
    fact belongs on the Overrides tab) and says so in the applied cell."""
    ctx, companies = w43_world()
    sheet_rows(ctx)[0].update(right_hq_state="GA", applied="")
    assert tab.migrate(ctx)["after"] == [*tab.COLUMNS, "right_hq_state"]
    out = tab.apply(ctx)
    row = sheet_rows(ctx)[0]
    assert out["applied"] == [row["id"]] and out["adopted"] == 48
    assert row["applied"].startswith("2026-10-19: fine; right_hq_state is not read: put the fact on the Overrides "
                                     "tab · ")
    assert account(ctx, row["domain"])["hq_state"] == "IL"


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
    same 49 event ids, so it updates them and adds none. The facts are the ones apply writes. W43's rows have no
    gate_version cell and W43 has no item recording one, so the version is unknown, not the one in force at export
    (which would date the judgement to a gate the company never passed)."""
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
                            "gate_version": tab.UNKNOWN_VERSION,
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
    rows[46]["right_label"] = ""  # a wrong industry apply would not apply: its judgement waits for the label
    out = tab.export(ctx)
    assert (out["decided"], out["facts"]) == (48, 45)  # 49 - 1 undecided; 48 - 3 skipped
    assert [(s["domain"], s["why"]) for s in out["skipped"]] == [
        (companies[1]["domain"], "the decision must be one of fine, leave out, wrong industry"),
        ("gone.example", "no company with this domain"),
        (companies[46]["domain"], "pick the right industry in right_label")]
    assert companies[46]["account_id"] not in {f["account_id"] for f in judgements(ctx)}


def test_the_gate_version_is_the_row_s_then_the_draw_s_never_today_s():
    """The row's own cell first; else what the week's item recorded at the draw; else unknown. A fact written before
    with a version keeps it when a later pass knows none."""
    ctx, companies = w43_world()
    item = {"item_id": "hand_check-2026-W43", "payload": {"gate_version": "drawn:legacy"}}
    assert tab._version({"gate_version": "cell:legacy"}, item) == "cell:legacy"
    assert tab._version({"gate_version": ""}, item) == "drawn:legacy"
    assert tab._version({}, None) == tab.UNKNOWN_VERSION != hand_check.gate_version(ctx)
    r, a = sheet_rows(ctx)[0], companies[0]
    ctx.store.upsert("signal_events", [tab.judgement(ctx, r, a, W43, item)])
    [kept] = tab._keep_first(ctx, [tab.judgement(ctx, r, a, W43)])
    assert kept["value"]["gate_version"] == "drawn:legacy"


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
