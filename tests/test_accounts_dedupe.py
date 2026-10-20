"""One account per root domain (W3-3, 20 Oct 2026; code-sources B7): `accounts dedupe`, the front door's insert that
cannot make a second row, and the unique index applied only once no duplicate is left."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta


from tests.test_cli import Harness
from tests.fakes import make_context
from us_outbound import accounts
from us_outbound.clients.db import MemoryStore
from us_outbound.clients.guard import Guard
from us_outbound.ops import cli, ddl
from us_outbound.settings.defaults import default_tabs
from us_outbound.settings.validate import validate_all

SETTINGS, _ = validate_all(default_tabs())
T0 = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)


def world(live=False):
    ctx = make_context(SETTINGS, job="accounts_dedupe", live=live)
    return ctx


def account(ctx, aid, domain, *, days=0, enrolled=False, **cols):
    ctx.store.insert("accounts", [{"account_id": aid, "domain": domain, "status": "enrolled" if enrolled else "verified",
                                   "first_seen": T0 + timedelta(days=days), **cols}])
    ctx.store.insert("contacts", [{"contact_id": f"c-{aid}", "account_id": aid,
                                   "enrolled_at": T0 + timedelta(days=days + 1) if enrolled else None}])
    ctx.store.insert("signal_events", [{"event_id": f"e-{aid}", "account_id": aid, "source": "apollo_org",
                                        "fact": "employees", "value": 50, "observed_at": T0}])


def test_two_rows_for_one_domain_one_with_an_enrolled_contact_merge_into_the_older_leaving_one_row():
    """acme.com twice: a1 (1 Oct, its contact enrolled) and a2 (3 Oct, not). Merged into a1, the older and the one in
    flight: a2's contact and fact move to a1, a2's row goes; 1 row left, 0 duplicates."""
    ctx = world(live=True)
    account(ctx, "a1", "acme.com", enrolled=True)
    account(ctx, "a2", "acme.com", days=2)
    account(ctx, "b1", "other.com")
    out = accounts.dedupe(ctx)
    assert (out["duplicates"], out["remaining"], out["refused"]) == (1, 0, [])
    assert [a["account_id"] for a in ctx.store.select("accounts", {"domain": "acme.com"})] == ["a1"]
    assert {c["contact_id"] for c in ctx.store.select("contacts", {"account_id": "a1"})} == {"c-a1", "c-a2"}
    assert {e["event_id"] for e in ctx.store.select("signal_events", {"account_id": "a1"})} == {"e-a1", "e-a2"}
    assert out["merged"][0]["moved"]["contacts"] == 1


def test_when_only_the_newer_has_an_enrolled_contact_it_is_the_one_kept():
    """The account in flight keeps its id (HubSpot's us_outbound_account_id, the contact in flight): a2 is kept."""
    ctx = world(live=True)
    account(ctx, "a1", "acme.com")
    account(ctx, "a2", "acme.com", days=2, enrolled=True)
    accounts.dedupe(ctx)
    assert [a["account_id"] for a in ctx.store.select("accounts", {"domain": "acme.com"})] == ["a2"]


def test_both_with_enrolled_contacts_are_refused_and_listed_and_nothing_moves():
    ctx = world(live=True)
    account(ctx, "a1", "acme.com", enrolled=True)
    account(ctx, "a2", "acme.com", days=2, enrolled=True)
    out = accounts.dedupe(ctx)
    assert out["refused"] == [{"domain": "acme.com", "keep": "", "merge": ["a1", "a2"],
                               "refused": "2 of its accounts have enrolled contacts"}]
    assert out["remaining"] == 1 and len(ctx.store.select("accounts", {"domain": "acme.com"})) == 2


def test_a_dry_run_lists_and_changes_nothing(capsys):
    h = Harness()
    ctx = make_context(SETTINGS, store=h.store)
    account(ctx, "a1", "acme.com")
    account(ctx, "a2", "acme.com", days=2)
    assert h.run("accounts", "dedupe") == 0
    out = capsys.readouterr().out
    assert "1 domains are held by more than one account." in out and "acme.com: would merge a2 into a1" in out
    assert len(h.store.select("accounts")) == 2


def test_a_live_run_that_leaves_a_duplicate_does_not_make_the_index(capsys, monkeypatch):
    h = Harness()
    ctx = make_context(SETTINGS, store=h.store)
    account(ctx, "a1", "acme.com", enrolled=True)
    account(ctx, "a2", "acme.com", days=2, enrolled=True)
    made = []
    monkeypatch.setattr(ddl, "apply_deferred", lambda *a, **k: made.append(a))
    monkeypatch.setenv("DATABASE_URL", "postgresql://example.invalid/db")
    assert h.run("accounts", "dedupe", "--live") == 0
    assert "1 duplicate domains remain, so the unique index on accounts(domain) was not made" in capsys.readouterr().out
    assert made == []


def test_a_live_run_with_no_duplicate_left_makes_the_index(capsys, monkeypatch):
    h = Harness()
    ctx = make_context(SETTINGS, store=h.store)
    account(ctx, "a1", "acme.com", enrolled=True)
    account(ctx, "a2", "acme.com", days=2)
    made = []
    monkeypatch.setattr(ddl, "apply_deferred", lambda guard, dsn, name, **k: made.append((dsn, name, k)))
    monkeypatch.setenv("DATABASE_URL", "postgresql://example.invalid/db")
    assert h.run("accounts", "dedupe", "--live") == 0
    assert made == [("postgresql://example.invalid/db", cli.UNIQUE_INDEX_FILE, {"dry_run": False})]
    assert "the unique index on accounts(domain) is made" in capsys.readouterr().out


def test_the_unique_index_is_not_in_db_apply_and_checks_as_an_allowed_statement():
    """The pre-deploy's one transaction never holds it (RS §4.8); the deferred file passes the same statement check."""
    assert not any("accounts_domain_unique_idx" in s for _, s in ddl.statements())
    [(file, sql, target)] = ddl.deferred_statements(cli.UNIQUE_INDEX_FILE)
    assert target == "us_outbound.accounts" and "CREATE UNIQUE INDEX IF NOT EXISTS" in sql
    assert ddl.apply_deferred(Guard(), "", cli.UNIQUE_INDEX_FILE) == [sql]  # dry-run: printed, nothing run


def test_admit_of_a_known_domain_is_a_no_op_in_memory():
    """MemoryStore holds the domain unique as the index does: a second insert of acme.com makes no row."""
    store = MemoryStore(Guard())
    first = accounts.admit(store, "acme.com", source="apollo", now=T0)
    assert first.outcome == "created"
    assert not store.insert_new("accounts", {"account_id": "x", "domain": "acme.com"})
    again = accounts.admit(store, "acme.com", source="apollo", now=T0)
    assert (again.outcome, again.account_id) == ("exists", first.account_id)
    assert len(store.select("accounts")) == 1
