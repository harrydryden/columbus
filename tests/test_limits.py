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


def test_today_s_share_used_waits_for_tomorrow():
    ctx = ctx_with(accounts=[acct(i, "queued") for i in range(4)], clay_verification="required")
    ctx.store.insert("credit_ledger", [spend("clay", 500, ctx.now)])  # 2,000 over the 4 weekdays left is 500 a day
    lim = limits.today(ctx, TUE, ready_accounts=0)
    assert lim.detail[-1] == ("Behind it: Clay's budget is used until tomorrow (today's share of the month is used), "
                              "and 4 accounts are waiting to be verified.")


def test_no_clay_budget_means_nothing_is_verified():
    ctx = ctx_with(accounts=[acct(1, "queued")], clay_monthly_credits=0.0, clay_verification="required")
    lim = limits.today(ctx, TUE, ready_accounts=0)
    assert lim.detail[-1] == "Behind it: Clay has no monthly budget (clay_monthly_credits is 0), so no account can be verified."


def test_clay_spent_with_accounts_waiting():
    ctx = ctx_with(accounts=[acct(i, "queued") for i in range(4)], clay_verification="required")
    ctx.store.insert("credit_ledger", [spend("clay", 2000, ctx.now)])
    lim = limits.today(ctx, TUE, ready_accounts=0)
    assert lim.detail[-1] == "Behind it: Clay's budget is used until 1 Nov, and 4 accounts are waiting to be verified."


def test_nothing_waiting_for_clay_points_at_the_universe():
    ctx = ctx_with(clay_verification="required")
    lim = limits.today(ctx, TUE, ready_accounts=0)
    assert "no accounts are waiting for Clay, so the universe or the free checks are the limit" in lim.detail[-1]
    ctx = ctx_with(accounts=[acct(i, "queued") for i in range(4)], clay_verification="required")
    assert limits.today(ctx, TUE, ready_accounts=0).detail[-1] == (
        "Behind it: 4 accounts are waiting for Clay (verify_in_clay).")


def test_with_clay_verification_skip_accounts_wait_for_verify_accounts_not_clay():
    """A14: while clay_verification = skip (the default), verify_accounts verifies new and queued accounts,
    so neither Clay nor its budget is named, even when the Clay budget is spent or zero."""
    rows = [acct(i, "queued") for i in range(3)] + [acct(10 + i, "new") for i in range(2)]
    for general in ({}, {"clay_monthly_credits": 0.0}):
        ctx = ctx_with(accounts=rows, **general)
        assert ctx.settings.general.clay_verification == "skip"
        ctx.store.insert("credit_ledger", [spend("clay", 2000, ctx.now)])
        lim = limits.today(ctx, TUE, ready_accounts=0)
        assert lim.detail[-1] == "Behind it: 5 accounts are waiting for verify_accounts (weekdays 04:30 UK)."
    lim = limits.today(ctx_with(), TUE, ready_accounts=0)
    assert lim.detail[-1] == ("Behind it: no accounts are waiting to be verified, so the universe or the free checks "
                              "are the limit (source_universe, apollo_people).")


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
