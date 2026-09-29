"""Domains, personal and generic mailboxes, redirects and the alias table (SPEC 13, 1.5, 5)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from us_outbound.clean.domains import (
    canonical_domain,
    follow_redirect,
    is_generic_mailbox,
    is_personal_domain,
    record_alias,
    root_domain,
)
from us_outbound.clients.bq import MemoryStore
from us_outbound.clients.guard import Guard

NOW = datetime(2026, 10, 27, 12, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    "given, root",
    [
        ("https://www.Acme.co.uk/about", "acme.co.uk"),
        ("acme.com.", "acme.com"),
        ("sub.acme.io", "acme.io"),
        ("http://1.2.3.4", None),
        ("mail.google.com", "google.com"),
        ("ACME.COM", "acme.com"),
        ("http://acme.com:8080/path?x=1", "acme.com"),
        ("www.acme.com/careers#jobs", "acme.com"),
        ("https://user:pw@acme.com/", "acme.com"),
        ("jane.doe@Acme.com", "acme.com"),
        ("careers.acme.com.au", "acme.com.au"),
        ("acme.github.io", "acme.github.io"),  # hosted sites stay separate accounts
        ("acme.myshopify.com", "acme.myshopify.com"),
        ("bücher.de", "xn--bcher-kva.de"),
        ("", None),
        (None, None),
        ("   ", None),
        ("localhost", None),
        ("acme", None),
        ("co.uk", None),
        ("not a domain", None),
        ("-acme.com", None),
        ("acme..com", None),
        ("https://[2001:db8::1]/", None),
        ("10.0.0.1:8080", None),
    ],
)
def test_root_domain(given, root):
    assert root_domain(given) == root


@pytest.mark.parametrize(
    "domain, personal",
    [
        ("gmail.com", True),
        ("GMAIL.COM", True),
        ("googlemail.com", True),
        ("outlook.com", True),
        ("hotmail.com", True),
        ("yahoo.com", True),
        ("yahoo.co.uk", True),
        ("icloud.com", True),
        ("proton.me", True),
        ("comcast.net", True),
        ("sbcglobal.net", True),
        ("nyc.rr.com", True),  # ISP subdomains count by their root
        ("jane@verizon.net", True),
        ("https://outlook.com/owa", True),
        ("acme.com", False),
        ("mail.google.com", False),  # google.com is a company
        ("meetspill.org", False),
        ("", False),
        (None, False),
    ],
)
def test_is_personal_domain(domain, personal):
    assert is_personal_domain(domain) is personal


@pytest.mark.parametrize(
    "local, generic",
    [
        ("info", True),
        ("Info", True),
        ("hr", True),
        ("hello", True),
        ("contact", True),
        ("admin", True),
        ("office", True),
        ("team", True),
        ("jobs", True),
        ("careers", True),
        ("sales", True),
        ("support", True),
        ("people", True),
        ("benefits", True),
        ("hr-team", True),
        ("us.sales", True),
        ("people+newsletter", True),
        ("jobs@acme.com", True),
        ("no-reply", True),
        ("jane", False),
        ("jane.doe", False),
        ("j.doe", False),
        ("jdoe@acme.com", False),
        ("ap", False),  # initials, not accounts payable
        ("", False),
        (None, False),
    ],
)
def test_is_generic_mailbox(local, generic):
    assert is_generic_mailbox(local) is generic


class Resolver:
    def __init__(self, result):
        self.result, self.calls = result, []

    def __call__(self, url):
        self.calls.append(url)
        return self.result(url) if callable(self.result) else self.result


def test_follow_redirect_to_a_new_root_returns_the_old_one_as_alias():
    r = Resolver("https://www.newname.com/")
    assert follow_redirect("oldname.com", r) == ("newname.com", "oldname.com")
    assert r.calls == ["https://oldname.com"]


@pytest.mark.parametrize(
    "target",
    [
        "https://www.acme.com/home",  # same root
        "https://acme.com",  # no redirect: the resolver returns the URL itself
        "",
        None,
        "https://www.godaddy.com/forsale/acme.com",  # parked
        "https://www.facebook.com/acmeco",  # social profile
        "https://acme.wixsite.com/acme",  # a hosted site: the company's own domain stays the root
        "https://acme.github.io/",
        "https://mail.google.com",  # never adopt a big platform
        "https://1.2.3.4/",
    ],
)
def test_follow_redirect_without_a_new_root(target):
    r = Resolver(target)
    assert follow_redirect("acme.com", r) == ("acme.com", None)
    assert len(r.calls) == 1


def test_follow_redirect_ignores_personal_domain_targets():
    assert follow_redirect("acme.com", Resolver("https://gmail.com")) == ("acme.com", None)


def test_follow_redirect_keeps_the_given_host_and_skips_invalid_domains():
    r = Resolver(lambda url: url)
    assert follow_redirect("https://www.Acme.com/about", r) == ("acme.com", None)
    assert r.calls == ["https://www.acme.com"]
    bad = Resolver("https://acme.com")
    assert follow_redirect("1.2.3.4", bad) == (None, None)
    assert follow_redirect("", bad) == (None, None)
    assert bad.calls == []


@pytest.fixture
def store():
    return MemoryStore(Guard())


def test_record_alias_writes_domain_aliases(store):
    assert record_alias(store, "www.OldName.com", "https://newname.com/x", "redirect", NOW) is True
    assert store.tables["domain_aliases"] == [
        {"alias": "oldname.com", "root_domain": "newname.com", "source": "redirect", "added_at": NOW}
    ]
    writes = store.guard.writes("bq")
    assert writes and writes[-1].target == "us_outbound.domain_aliases"
    # The same alias again is a no-op.
    assert record_alias(store, "oldname.com", "newname.com", "redirect", NOW) is False
    assert len(store.tables["domain_aliases"]) == 1


def test_record_alias_refuses_self_and_cycles(store):
    assert record_alias(store, "acme.com", "www.acme.com", "redirect", NOW) is False
    assert record_alias(store, "a.com", "b.com", "redirect", NOW) is True
    assert record_alias(store, "b.com", "a.com", "redirect", NOW) is False  # would loop b -> a -> b
    assert record_alias(store, "bad", "b.com", "redirect", NOW) is False
    assert [r["alias"] for r in store.tables["domain_aliases"]] == ["a.com"]


def test_record_alias_points_at_the_end_of_an_existing_chain(store):
    record_alias(store, "b.com", "c.com", "redirect", NOW)
    record_alias(store, "a.com", "b.com", "clay", NOW)
    assert store.get("domain_aliases", alias="a.com")["root_domain"] == "c.com"


def test_canonical_domain(store):
    store.upsert(
        "domain_aliases",
        [
            {"alias": "old.com", "root_domain": "mid.com", "source": "redirect", "added_at": NOW},
            {"alias": "mid.com", "root_domain": "new.com", "source": "redirect", "added_at": NOW},
            {"alias": "x.com", "root_domain": "y.com", "source": "redirect", "added_at": NOW},
            {"alias": "y.com", "root_domain": "x.com", "source": "redirect", "added_at": NOW},  # a bad loop
        ],
    )
    assert canonical_domain(store, "https://www.OLD.com/about") == "new.com"
    assert canonical_domain(store, "mid.com") == "new.com"
    assert canonical_domain(store, "unrelated.org") == "unrelated.org"
    assert canonical_domain(store, "x.com") == "y.com"  # stops, does not spin
    assert canonical_domain(store, "1.2.3.4") is None
