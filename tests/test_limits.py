"""What limits today's enrollment (limits.py): the binding term, and what stands behind too few ready accounts."""

from __future__ import annotations

from datetime import date, timedelta

from tests.fakes import make_context
from tests.test_render import make_settings
from us_outbound import limits

TUE = date(2026, 10, 27)


def ctx_with(accounts=(), ledger=(), **general):
    ctx = make_context(make_settings(**general))
    if accounts:
        ctx.store.insert("accounts", list(accounts))
    if ledger:
        ctx.store.insert("credit_ledger", list(ledger))
    return ctx


def acct(i, status):
    return {"account_id": f"a{i}", "domain": f"a{i}.com", "status": status}


def spend(system, credits, ctx_now):
    return {"entry_id": f"{system}-{credits}", "system": system, "credits": float(credits), "occurred_at": ctx_now - timedelta(hours=1)}


def test_the_head_line_names_the_binding_term():
    ctx = ctx_with()
    lim = limits.today(ctx, TUE, ready_accounts=100)
    # 150 a week over 4 send days is 38; 4 mailboxes at 30 a day give 8 + 15 + 8 new today.
    assert lim.number == 31 and lim.terms["binding"] == "sending_capacity"
    assert lim.explanation == ("Today: 31, limited by sending capacity "
                               "(weekly target 38, sending capacity 31, ready accounts 100).")
    assert any("another Active mailbox" in line for line in lim.detail)


def test_ready_accounts_waiting_for_an_email():
    ctx = ctx_with(accounts=[acct(i, "verified") for i in range(5)])
    lim = limits.today(ctx, TUE, ready_accounts=2)
    assert lim.detail[-1] == "Behind it: 3 verified accounts are waiting for an email (pick_contacts)."


def test_apollo_spent_holds_emails_until_monday():
    ctx = ctx_with(accounts=[acct(i, "verified") for i in range(5)])
    ctx.store.insert("credit_ledger", [spend("apollo", 500, ctx.now)])
    lim = limits.today(ctx, TUE, ready_accounts=2)
    assert "Apollo's weekly budget is used, so no more emails are looked up until Monday" in lim.detail[-1]


def test_no_clay_budget_means_nothing_is_verified():
    ctx = ctx_with(accounts=[acct(1, "queued")], clay_weekly_credits=0.0)
    lim = limits.today(ctx, TUE, ready_accounts=0)
    assert lim.detail[-1] == "Behind it: Clay has no weekly budget (clay_weekly_credits is 0), so no account can be verified."


def test_clay_spent_with_accounts_waiting():
    ctx = ctx_with(accounts=[acct(i, "queued") for i in range(4)])
    ctx.store.insert("credit_ledger", [spend("clay", 500, ctx.now)])
    lim = limits.today(ctx, TUE, ready_accounts=0)
    assert lim.detail[-1] == "Behind it: Clay's weekly budget is used and 4 accounts are waiting to be verified until Monday."


def test_nothing_waiting_for_clay_points_at_the_universe():
    ctx = ctx_with()
    lim = limits.today(ctx, TUE, ready_accounts=0)
    assert "no accounts are waiting for Clay, so the universe or the free checks are the limit" in lim.detail[-1]
