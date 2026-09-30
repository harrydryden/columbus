"""Suppression: hashes only, domain entries, expiry, idempotent loads from HubSpot (SPEC 6, 9, 11, 14)."""

from datetime import UTC, datetime, timedelta

from tests.fakes import FakeTransport, make_context
from us_outbound import suppression
from us_outbound.clients.db import MemoryStore
from us_outbound.clients.guard import Guard
from us_outbound.logs import hash_email
from us_outbound.settings.model import General, Settings

NOW = datetime(2026, 10, 27, 12, 0, tzinfo=UTC)


def store():
    return MemoryStore(Guard())


def test_email_entry_stores_only_the_hash():
    s = store()
    assert suppression.add(s, email=" Jane.Doe@Acme.com ", reason="unsubscribe", source="reply", now=NOW)
    [row] = s.tables["suppression"]
    assert row["email_sha256"] == hash_email("jane.doe@acme.com")
    assert row["domain"] is None and row["expires_at"] is None
    assert "jane" not in repr(row).lower()


def test_domain_entry_is_cleaned_to_the_domain():
    s = store()
    suppression.add(s, domain="https://www.Acme.com/careers", reason="kill_rule", source="kill_rules", now=NOW)
    [row] = s.tables["suppression"]
    assert row == {"email_sha256": None, "domain": "acme.com", "reason": "kill_rule", "source": "kill_rules",
                   "added_at": NOW, "expires_at": None}


def test_is_suppressed_by_email_domain_and_subdomain():
    s = store()
    suppression.add(s, email="jane@acme.com", reason="unsubscribe", source="reply", now=NOW)
    suppression.add(s, domain="layoffco.com", reason="signal:Layoffs", source="scoring", now=NOW)
    assert suppression.is_suppressed(s, email="JANE@acme.com", now=NOW)
    assert not suppression.is_suppressed(s, email="john@acme.com", now=NOW)  # an email entry is not the domain
    assert not suppression.is_suppressed(s, domain="acme.com", now=NOW)
    assert suppression.is_suppressed(s, domain="layoffco.com", now=NOW)
    assert suppression.is_suppressed(s, email="ceo@layoffco.com", now=NOW)
    assert suppression.is_suppressed(s, email="ceo@mail.layoffco.com", now=NOW)
    assert not suppression.is_suppressed(s, email="ceo@notlayoffco.com", now=NOW)


def test_expiry_is_honored():
    s = store()
    until = NOW + timedelta(days=90)
    suppression.add(s, domain="layoffco.com", reason="signal:Layoffs", source="scoring", now=NOW, expires_at=until)
    assert suppression.is_suppressed(s, domain="layoffco.com", now=NOW + timedelta(days=89))
    assert not suppression.is_suppressed(s, domain="layoffco.com", now=until)


def test_add_is_idempotent_and_never_shortens():
    s = store()
    later = NOW + timedelta(days=1)
    assert suppression.add(s, domain="x.com", reason="signal:A", source="scoring", now=NOW, expires_at=NOW + timedelta(days=30))
    assert not suppression.add(s, domain="x.com", reason="signal:A", source="scoring", now=later, expires_at=NOW + timedelta(days=10))
    assert suppression.add(s, domain="x.com", reason="kill_rule", source="kill_rules", now=later)  # indefinite wins
    [row] = s.tables["suppression"]
    assert row["expires_at"] is None and row["reason"] == "kill_rule" and row["added_at"] == NOW
    assert not suppression.add(s, domain="x.com", reason="signal:A", source="scoring", now=later, expires_at=later)


def test_add_needs_an_email_or_domain():
    import pytest

    with pytest.raises(ValueError):
        suppression.add(store(), reason="x", source="y", now=NOW)


def test_load_from_hubspot_hashes_and_is_idempotent():
    t = FakeTransport()
    emails = [{"id": str(i), "properties": {"email": f"person{i}@example.com"}} for i in (1, 2, 3)]
    t.route("POST", "/crm/v3/objects/contacts/search",
            fn=lambda req: {"results": emails if req.json["filterGroups"][0]["filters"][1]["value"] == "0" else []})
    ctx = make_context(Settings(general=General()), transport=t)
    first = suppression.load_from_hubspot(ctx)
    assert first == {"seen": 3, "added": 3, "already_suppressed": 0}
    rows = ctx.store.tables["suppression"]
    assert {r["email_sha256"] for r in rows} == {hash_email(f"person{i}@example.com") for i in (1, 2, 3)}
    assert all(r["reason"] == "hubspot_opt_out_or_bounce" and r["source"] == "hubspot" for r in rows)
    assert "person1" not in repr(rows)
    second = suppression.load_from_hubspot(ctx)
    assert second == {"seen": 3, "added": 0, "already_suppressed": 3}
    assert len(ctx.store.tables["suppression"]) == 3
    assert not ctx.guard.writes("hubspot")  # reads only
