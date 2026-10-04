"""The send path and its gates (enrol/enrol.py, enrol/approvals.py, limits.py): one eligibility check for
enrol's candidates and a send approval's re-check, the campaign's status in Instantly, holds that keep a ✅
valid against blocks that close the card, HubSpot at the ✅, and leads Instantly leaves out of its summary.

The worlds are tests/test_enrol.py's and tests/test_send_approvals.py's: the real clients and the guard over a
FakeTransport, with Slack's threads and reactions on the same transport.
"""

from __future__ import annotations

from datetime import timedelta

from tests.test_enrol import NOW
from tests.test_render import account, contact
from us_outbound import suppression
from us_outbound.enrol import approvals, enrol
from us_outbound.learn import holds
from us_outbound.logs import hash_email

# -- A4: one eligibility check -----------------------------------------------------------------------------


def _parity_world():
    """One account per gate candidates applies, each with one contact; and one that passes every gate."""
    from tests.test_send_approvals import world

    cases = {
        "ok": ({}, {}),
        "dom": ({"domain": "suppressed-co.com"}, {}),
        "alias": ({"domain": "newname.com"}, {}),
        "expired": ({"domain": "expired-co.com"}, {}),
        "partner": ({"domain": "broker.com"}, {}),
        "off": ({"industry": "Staffing agencies", "industry_group": "Professional Services"}, {}),
        "pulled": ({}, {}),
        "stopped": ({"industry": "Fintech", "industry_group": "Technology & Startups"}, {}),
        "source": ({}, {"email_source": "clay"}),
        "hash": ({}, {}),
        "emaildom": ({}, {"email": "jane@suppressed-mail.com"}),
        "ca": ({}, {"person_state": "CA"}),
        "flag": ({}, {"suppressed": True}),
        "status": ({}, {"email_status": "invalid"}),
    }
    accts, cons = [], []
    for name, (a, c) in cases.items():
        domain = a.pop("domain", f"{name}-co.com")
        accts.append(account(account_id=f"acc-{name}", domain=domain, clean_name=f"Co {name}", **a))
        cons.append(contact(contact_id=f"con-{name}", account_id=f"acc-{name}",
                            **{"email": f"jane@{domain}", "email_source": "apollo", **c}))
    ctx, t, sl = world(accounts=accts, contacts=cons)
    ctx.store.insert("suppression", [
        {"email_sha256": None, "domain": "suppressed-co.com", "reason": "unsubscribe", "expires_at": None},
        {"email_sha256": None, "domain": "oldname.com", "reason": "unsubscribe", "expires_at": None},
        {"email_sha256": None, "domain": "expired-co.com", "reason": "Layoffs", "expires_at": NOW - timedelta(days=1)},
        {"email_sha256": None, "domain": "suppressed-mail.com", "reason": "unsubscribe", "expires_at": None},
        {"email_sha256": hash_email("jane@hash-co.com"), "domain": None, "reason": "unsubscribe", "expires_at": None},
    ])
    ctx.store.insert("domain_aliases", [{"alias": "oldname.com", "root_domain": "newname.com", "source": "redirect"}])
    ctx.store.insert("partners", [{"domain": "broker.com", "name": "Broker", "reason": "broker"}])
    ctx.store.insert("hitl_items", [
        {"item_id": "hc-w44", "kind": "hand_check", "status": "handled", "created_at": NOW - timedelta(hours=3),
         "payload": {"iso_week": "2026-W44", "pulled_account_ids": ["acc-pulled"]}},
        {"item_id": "kr-1", "kind": holds.KIND, "status": "open", "created_at": NOW,
         "payload": {"action": holds.STOP_GROUP, "target": "Technology & Startups", "reason": "low replies"}},
        {"item_id": "kr-2", "kind": holds.KIND, "status": "open", "created_at": NOW,
         "payload": {"action": holds.PAUSE_SOURCE, "target": "clay", "reason": "bounces"}},
    ])
    return ctx, t, sl, accts, cons


def _card_for(a, c) -> approvals.Item:
    return approvals.Item({"item_id": f"it-{a['account_id']}", "account_id": a["account_id"],
                           "contact_id": c["contact_id"],
                           "payload": {"company": a["clean_name"], "contact": {"first_name": c["first_name"],
                                                                                "last_name": c["last_name"]},
                                       "lead": {"email": c["email"]}}})


def test_candidates_and_the_recheck_agree_on_every_gate():
    ctx, t, sl, accts, cons = _parity_world()
    _, pulled = enrol.hand_check(ctx, ctx.now_et().date())
    cands, skipped = enrol.candidates(ctx, pulled)
    assert sorted(c.account["account_id"] for c in cands) == ["acc-expired", "acc-ok"]  # expired: holds nothing
    assert skipped == {
        "domain suppressed": 2, "partner domain": 1, "industry switched off": 1, "pulled at this week's hand-check": 1,
        "industry group stopped by a kill rule": 1, "email source paused by a kill rule": 1, "contact suppressed": 2,
        "email domain suppressed": 1, "contact in CA or WA": 1, "email status invalid": 1,
    }
    # The re-check of a card for each account says the same, account by account.
    got = {}
    for a, c in zip(accts, cons):
        why = approvals.eligibility(ctx, _card_for(a, c), a, c)
        got[a["account_id"]] = why.split(": ", 1)[1] if why else ""
    assert got["acc-ok"] == "" and got["acc-expired"] == ""
    reasons = [r for r in got.values() if r]
    from collections import Counter

    assert Counter(reasons) == skipped
    assert approvals.eligibility(ctx, _card_for(accts[1], cons[1]), accts[1], cons[1]) == "Co dom: domain suppressed"
    assert approvals.eligibility(ctx, _card_for(accts[-1], cons[-1]), accts[-1], cons[-1]) == (
        "Jane Doe: email status invalid")


def test_the_recheck_reads_suppression_by_key_not_the_whole_table():
    """A7: a ✅ looks up its own domain, its aliases and its email; never the whole suppression table."""
    ctx, t, sl, accts, cons = _parity_world()
    seen = []
    select = ctx.store.select

    def spy(table, where=None):
        if table in ("suppression", "domain_aliases"):
            seen.append((table, where))
        return select(table, where)

    ctx.store.select = spy
    a, c = accts[2], cons[2]  # newname.com, suppressed through its alias oldname.com
    assert approvals.eligibility(ctx, _card_for(a, c), a, c) == "Co alias: domain suppressed"
    assert seen and all(where for _, where in seen)  # every read is by key
    assert ("domain_aliases", {"root_domain": ["newname.com"]}) in seen
    assert enrol.suppressed_for(ctx, ["NewName.com", "other.com"], []) == ({"newname.com"}, set())
    assert enrol.suppressed_for(ctx, [], [hash_email("jane@hash-co.com"), "nothing"]) == (
        set(), {hash_email("jane@hash-co.com")})
    # The same answer as the whole-table read candidates makes.
    domains, hashes = enrol.suppressed(ctx)
    for a in accts:
        assert (a["domain"] in domains) == bool(enrol.suppressed_for(ctx, [a["domain"]], [])[0])


def test_a_card_for_an_account_pulled_or_stopped_since_is_blocked():
    """A4: the re-check now applies the kill rules and the hand-check's pulls, as candidates do."""
    from tests.test_send_approvals import HARRY_ID, item_for, poll, proposed

    ctx, t, sl, _ = proposed()
    ctx.store.insert("hitl_items", [
        {"item_id": "kr-1", "kind": holds.KIND, "status": "open", "created_at": NOW,
         "payload": {"action": holds.STOP_GROUP, "target": "Marketing & Creative Agencies", "reason": "low replies"}}])
    sl.react("white_check_mark", HARRY_ID, ts=item_for(ctx, "acc-1")["slack_ts"])
    out = poll(ctx)
    assert out["outcomes"] == {"blocked": 1}
    assert item_for(ctx, "acc-1")["payload"]["reason"] == "Acme Creative: industry group stopped by a kill rule"
    suppression.add(ctx.store, domain="loopstudio.com", reason="unsubscribe", source="test", now=NOW)
    sl.react("white_check_mark", HARRY_ID, ts=item_for(ctx, "acc-3")["slack_ts"])
    poll(ctx)
    assert item_for(ctx, "acc-3")["payload"]["reason"] == "Loop Studio: domain suppressed"
