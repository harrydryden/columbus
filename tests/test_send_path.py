"""The send path and its gates (enrol/enrol.py, limits.py): one eligibility check for enrol's candidates and
pick_contacts' lists, suppression read by key, and the campaign's status in Instantly.

The world is tests/test_enrol.py's: the real clients and the guard over a FakeTransport. (The send approvals' half
of this file, the ✅'s re-check, holds and blocks, went with enrol/approvals/ in W2-1, 20 Oct 2026.)
"""

from __future__ import annotations

from collections import Counter
from datetime import timedelta

from tests.test_enrol import NOW, make
from tests.test_render import account, contact
from us_outbound.base import holds
from us_outbound.enrol import enrol
from us_outbound.logs import hash_email

# -- A4: one eligibility check -----------------------------------------------------------------------------


def _parity_world():
    """One account per gate candidates applies, each with one contact; and one that passes every gate."""
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
    ctx, t = make(live=True, accounts=accts, contacts=cons, hand_check=None)
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
    return ctx, t, accts, cons


def test_candidates_and_pick_agree_on_every_gate():
    ctx, t, accts, cons = _parity_world()
    _, pulled = enrol.hand_check(ctx, ctx.now_et().date())
    cands, skipped = enrol.candidates(ctx, pulled)
    assert sorted(c.account["account_id"] for c in cands) == ["acc-expired", "acc-ok"]  # expired: holds nothing
    assert skipped == {
        "domain suppressed": 2, "partner domain": 1, "industry switched off": 1, enrol.HAND_CHECK_SKIP: 1,
        "industry group stopped by a kill rule": 1, "email source paused by a kill rule": 1, "contact suppressed": 2,
        "email domain suppressed": 1, "contact in CA or WA": 1, "email status invalid": 1,
    }
    # The one check, account by account: what each account's own contact list gets from enrol.eligible.
    g = enrol.gates(ctx, pulled)
    got = {a["account_id"]: enrol.eligible(a, [c], g)[1] for a, c in zip(accts, cons)}
    assert got["acc-ok"] == "" and got["acc-expired"] == ""
    assert Counter(r for r in got.values() if r) == skipped
    # pick_contacts reads the same gates (W1-6; L1): an account enrol skips for an account reason is in neither of
    # pick's lists (no credit is spent on it, and it is not ready supply); one skipped for its contact needs one; the
    # rest are ready. The hand-check's pulls are the one difference: the weekly draw reads the ready list, so a held
    # account stays in it (enrol leaves it out by its pulled ids).
    from us_outbound.contacts import pick

    contact_reasons = {"email source paused by a kill rule", "contact suppressed", "email domain suppressed",
                       "contact in CA or WA", "email status invalid"}
    need, ready = pick._split(ctx)
    want_ready = {a for a, why in got.items() if why == ""} | {"acc-pulled"}
    want_need = {a for a, why in got.items() if why in contact_reasons}
    assert {a["account_id"] for a in ready} == want_ready == {"acc-ok", "acc-expired", "acc-pulled"}
    assert {a["account_id"] for a in need} == want_need == {
        "acc-source", "acc-hash", "acc-flag", "acc-emaildom", "acc-ca", "acc-status"}
    assert pick.waiting(ctx) == (need, len(ready))
    # Neither: the account reasons.
    neither = set(got) - want_ready - want_need
    assert {got[a] for a in neither} == {"domain suppressed", "partner domain", "industry switched off",
                                         "industry group stopped by a kill rule"}


def test_suppression_for_a_few_is_read_by_key_not_the_whole_table():
    """A7: account_stop and the accounts view look up their own domains, aliases and emails; never the whole
    suppression table; and get the answer the whole-table read gives."""
    ctx, t, accts, cons = _parity_world()
    seen = []
    select = ctx.store.select

    def spy(table, where=None):
        if table in ("suppression", "domain_aliases"):
            seen.append((table, where))
        return select(table, where)

    ctx.store.select = spy
    assert enrol.suppressed_for(ctx, ["NewName.com", "other.com"], []) == ({"newname.com"}, set())
    assert seen and all(where for _, where in seen)  # every read is by key
    assert ("domain_aliases", {"root_domain": ["newname.com", "other.com"]}) in seen
    assert enrol.suppressed_for(ctx, [], [hash_email("jane@hash-co.com"), "nothing"]) == (
        set(), {hash_email("jane@hash-co.com")})
    ctx.store.select = select
    domains, hashes = enrol.suppressed(ctx)
    for a in accts:
        assert (a["domain"] in domains) == bool(enrol.suppressed_for(ctx, [a["domain"]], [])[0])


# -- A1: a campaign that is not active in Instantly takes no leads ------------------------------------------------


def _hannah_draft(t, status=0):
    from tests.test_enrol import CAMPAIGNS

    items = [dict(c, status=status) if c["id"] == "c-hannah" else c for c in CAMPAIGNS["items"]]
    t.route("GET", "/campaigns", {"items": items})


DRAFT = "Hannah Spalding's campaign is not active in Instantly (draft): run `us-outbound start --live`"


def test_live_enrol_adds_no_lead_to_a_campaign_that_is_not_active():
    from tests.fakes import FakeTransport
    from tests.test_enrol import instantly_posts

    t = FakeTransport()
    ctx, _ = make(live=True, transport=t)
    _hannah_draft(t)
    out = enrol.run(ctx)
    assert {r.json["campaign_id"] for r in instantly_posts(t)} == {"c-harry", "c-sam"}
    assert "Hannah Spalding" not in out["by_owner"] and out["enrolled"] == 2
    assert out["campaigns_not_sending"] == {"Hannah Spalding": DRAFT}
    assert out["limited_by"].endswith(f" {DRAFT}.")
    assert f"Sending capacity, Hannah Spalding: no new leads today: {DRAFT}." in out["limits"]
    assert not any("Add a mailbox for Hannah" in line for line in out["limits"])
    [skip] = [x for x in out["skipped_accounts"] if x["account_id"] == "acc-2"]  # Omar's account keeps Hannah
    assert skip["reason"] == "sender's campaign not sending" and skip["detail"] == [
        f"{DRAFT}; the account waits for Hannah Spalding"]
    assert ctx.store.get("accounts", account_id="acc-2")["status"] == "verified"


def test_a_dry_run_still_previews_and_says_a_live_run_would_not():
    from tests.fakes import FakeTransport

    t = FakeTransport()
    ctx, _ = make(transport=t)
    _hannah_draft(t)
    out = enrol.run(ctx)
    assert out["dry_run"] is True and out["prepared"] == 3 and out["by_owner"]["Hannah Spalding"] == 1
    assert out["campaigns_not_sending"] == {"Hannah Spalding": DRAFT}
    assert out["limited_by"].endswith(f" Counted anyway in this dry run, but a live run would not: {DRAFT}.")
    line = next(x for x in out["limits"] if x.startswith("Sending capacity, Hannah Spalding:"))
    assert line.endswith(f"; a live run would give none: {DRAFT}.") and "8 new leads today" in line


def test_when_instantly_cannot_be_read_nothing_is_added():
    from tests.fakes import FakeTransport
    from tests.test_enrol import instantly_posts

    t = FakeTransport()
    ctx, _ = make(live=True, transport=t)
    t.route("GET", "/campaigns", {"error": "down"}, status=503)
    out = enrol.run(ctx)
    assert out["status"] == "ok" and out["number"] == 0 and out["enrolled"] == 0 and instantly_posts(t) == []
    assert "Instantly could not be read (ApiError" in out["limited_by"]
    assert set(out["campaigns_not_sending"]) == {"Harry Dryden", "Hannah Spalding", "Sam Jackson"}


def test_limits_reads_instantly_only_when_asked():
    from tests.fakes import FakeTransport
    from us_outbound import limits

    t = FakeTransport()
    ctx, _ = make(live=True, transport=t)
    _hannah_draft(t)
    lim = limits.today(ctx, ctx.now_et().date(), ready_accounts=10)  # status and the daily post
    assert lim.not_sending == {} and not [r for r in t.requests if "/campaigns" in r.url]
    lim = limits.today(ctx, ctx.now_et().date(), ready_accounts=10, campaigns=True)
    assert lim.not_sending == {"Hannah Spalding": DRAFT} and lim.senders["Hannah Spalding"].free == 0
    assert lim.terms["senders"]["Hannah Spalding"]["not_sending"] == DRAFT
