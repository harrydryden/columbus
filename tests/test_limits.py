"""What limits today's enrollment (limits.py): the binding term, and what stands behind too few ready accounts."""

from __future__ import annotations

from datetime import date, timedelta

from tests.fakes import make_context
from tests.test_ramp import past_ramp
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
    past_ramp(ctx.store, ctx.settings.mailboxes)
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
    ctx.store.insert("credit_ledger", [spend("apollo", 2000, ctx.now)])
    lim = limits.today(ctx, TUE, ready_accounts=2)
    assert "Apollo's budget is used until 1 Nov, so no more emails are looked up" in lim.detail[-1]


def test_accounts_waiting_to_be_verified_wait_for_verify_accounts():
    """verify_accounts verifies new and queued accounts on Apollo data and HubSpot, so with no verified account short
    of an email the line names it: 3 queued + 2 new = 5 waiting. (Clay's verify path and budget went with W2-1.)"""
    rows = [acct(i, "queued") for i in range(3)] + [acct(10 + i, "new") for i in range(2)]
    ctx = ctx_with(accounts=rows)
    ctx.store.insert("credit_ledger", [spend("clay", 2000, ctx.now)])  # an old Clay row names nothing now
    lim = limits.today(ctx, TUE, ready_accounts=0)
    assert lim.detail[-1] == "Behind it: 5 accounts are waiting for verify_accounts (weekdays 04:30 UK)."
    assert set(lim.budgets) == {"apollo"} and "clay" not in lim.terms["budgets"]
    lim = limits.today(ctx_with(), TUE, ready_accounts=0)
    assert lim.detail[-1] == ("Behind it: no accounts are waiting to be verified, so the universe or the free checks "
                              "are the limit (source_universe, verify_accounts).")


def test_a_full_sender_with_accounts_waiting_says_add_a_mailbox():
    from datetime import UTC, datetime

    from tests.test_render import HANNAH

    ctx = make_context(make_settings(mailboxes=(HANNAH,)))
    past_ramp(ctx.store, ctx.settings.mailboxes)
    ctx.store.insert("heartbeats", [{"run_id": "mh", "job": "mailbox_health", "status": "ok",
                                     "started_at": datetime(2026, 10, 27, 7, tzinfo=UTC),
                                     "detail": {"campaign_status": {"Hannah Spalding": {
                                         "code": 3, "meaning": "the campaign reached its daily limit", "at_limit": True}}}}])
    lim = limits.today(ctx, TUE, ready_accounts=100)
    assert lim.terms["binding"] == "sending_capacity" and lim.number == 8
    assert ("Add a mailbox for Hannah Spalding: Instantly says the campaign reached its daily limit. "
            '`us-outbound mailbox add <address> --owner "Hannah Spalding" --live`, then it warms for 21 days.') in lim.detail
    assert "92 ready accounts are waiting for inbox space." in lim.detail
