"""`us-outbound accounts` (ops/accounts_view.py): the summary, the list, one company, the CSV; read-only throughout."""

from __future__ import annotations

import copy
import csv
import io
from datetime import UTC, datetime, timedelta

import pytest

from tests.test_cli import SETTINGS, Harness
from tests.test_registry import C_HARRY, HARRY
from us_outbound.logs import EMAIL_RE, hash_email
from us_outbound.ops import accounts_view as view
from us_outbound.ops import cli
from us_outbound.settings.model import TIERS

T0 = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)


def account(domain: str, name: str, **kw) -> dict:
    return {"account_id": f"a-{domain}", "domain": domain, "clean_name": name, "first_seen": T0, **kw}


def contact(cid: str, account_id: str, first: str, last: str, email: str, **kw) -> dict:
    return {"contact_id": cid, "account_id": account_id, "first_name": first, "last_name": last, "email": email,
            "email_sha256": hash_email(email), "email_status": "verified", "email_source": "apollo",
            "person_state": "NY", "created_at": T0, **kw}


ACCOUNTS = [
    account("fresh.com", "Fresh", status="new"),  # not scored yet, no contact
    account("bright.org", "Bright Legal", legal_name="Bright Legal LLP", industry="Law firm",
            industry_group="Legal & Professional", size_band="20-49", hq_city="Austin", hq_state="TX", tier="Standard",
            score=40, status="enrolled", sender="Harry Dryden", angle="General", source="apollo", employees=31),
    account("acme.com", "Acme", legal_name="Acme Holdings Inc", industry="Software", industry_group="Technology",
            size_band="50-99", hq_city="New York", hq_state="NY", tier="Priority", tier_reason="New People leader",
            score=72, status="verified", source="apollo", employees=64, us_employees=60, founded_year=2015,
            apollo_org_id="org-acme", last_scored=T0 + timedelta(days=3)),
    account("calm.io", "Calm Agency", industry="Marketing agency", industry_group="Agencies", size_band="10-19",
            hq_state="NJ", tier="Control", score=5, status="queued"),  # no contact yet
    account("dull.net", "Dull Co", industry="Retail", tier="Excluded", score=0, status="disqualified"),
]
CONTACTS = [
    contact("c-jane", "a-acme.com", "Jane", "Doe", "jane@acme.com", role="People leader", title="Head of People"),
    contact("c-bob", "a-acme.com", "Bob", "Smith", "bob@acme.com", role="Founder or executive",
            title='=HYPERLINK("http://x.example","CEO")', email_source="clay", email_status="valid",
            created_at=T0 + timedelta(hours=1)),
    contact("c-sam", "a-bright.org", "Sam", "Lee", "sam@bright.org", role="People leader", title="HR Director",
            enrolled_at=T0 + timedelta(days=2), enrolment_month="2026-10", instantly_lead_id="lead-1", mailbox=HARRY,
            copy_version="legal-pl-v1", instantly_campaign=C_HARRY),
    contact("c-ann", "a-dull.net", "Ann", "Ray", "ann@dull.net", role="Operations", title="COO", suppressed=True,
            suppressed_reason="unsubscribe"),
]
SIGNALS = [
    {"event_id": f"s{i}", "account_id": "a-bright.org", "source": "apollo_jobs", "fact": "open_roles", "value": i,
     "quote": f"{i} open roles", "source_url": "https://bright.org/careers", "observed_at": T0 + timedelta(hours=i)}
    for i in range(11)
] + [{"event_id": "s-match", "account_id": "a-bright.org", "source": "scoring", "fact": "signal_matched",
      "value": {"signal": "Hiring HR", "weight": 15, "evidence": "x"}, "observed_at": T0 + timedelta(days=1)}]
EVENTS = [
    {"event_id": "e1", "account_id": "a-bright.org", "contact_id": "c-sam", "type": "sent", "step": 1, "mailbox": HARRY,
     "occurred_at": T0 + timedelta(days=2)},
    {"event_id": "e2", "account_id": "a-bright.org", "contact_id": "c-sam", "type": "replied", "mailbox": HARRY,
     "reply_class": "positive", "reply_text": "PRIVATE REPLY TEXT", "occurred_at": T0 + timedelta(days=3)},
]
CARDS = [
    {"item_id": "i1", "kind": "send_approval", "account_id": "a-bright.org", "status": "handled", "created_at": T0,
     "handled_at": T0 + timedelta(days=2), "handled_by": "cli",
     "payload": {"state": "approved", "outcome": "approved", "steps": [{"body": "PRIVATE DRAFT"}]}},
]


def harness() -> Harness:
    h = Harness(SETTINGS)
    for table, rows in (("accounts", ACCOUNTS), ("contacts", CONTACTS), ("signal_events", SIGNALS),
                        ("events", EVENTS), ("hitl_items", CARDS)):
        h.store.tables[table].extend(copy.deepcopy(rows))
    h.store.tables["domain_aliases"].append({"alias": "acme.co", "root_domain": "acme.com"})
    return h


def printed(out: str) -> list[str]:
    """The printed lines, without the JSON log lines."""
    return [line for line in out.splitlines() if not line.startswith('{"event"')]


# -- the summary and the list -----------------------------------------------------------------------------


def test_summary_counts_then_the_companies_in_queue_order(capsys):
    h = harness()
    assert h.run("accounts") == 0
    lines = printed(capsys.readouterr().out)
    assert lines[:9] == [
        view.WHERE,
        "Companies: 5",
        "  By status: new 1, queued 1, verified 1, enrolled 1, disqualified 1",
        "  By tier: Priority 1, Standard 1, Control 1, Excluded 1, not scored yet 1",
        "Contacts: 4",
        "  With an email: 4 (by source: apollo 3, clay 1)",
        "  Suppressed: 1",
        "  Enrolled: 1",
        "Ready to email: 1 (verified, with a sendable contact, as the enrol job counts them)",
    ]
    assert lines[10] == "Companies, in queue order (tier, then score): all 5:"
    rows = lines[11:16]
    assert [r.split()[0] for r in rows] == ["acme.com", "bright.org", "calm.io", "dull.net", "fresh.com"]
    assert rows[0] == ("  acme.com    Acme · Software · 50-99 staff · NY · Priority 72 · verified · "
                       "People leader: Head of People")
    assert rows[1].endswith("Standard 40 · enrolled · People leader: HR Director")
    assert rows[2].endswith("Control 5 · queued · no contact yet")
    assert rows[4] == ("  fresh.com   Fresh · industry unknown · size unknown · state unknown · no tier - · new · "
                       "no contact yet")
    assert lines[16].startswith("One company in full: `us-outbound accounts DOMAIN`.")


def test_the_list_never_prints_an_email_address(capsys):
    h = harness()
    for argv in (["accounts"], ["accounts", "--limit", "0"], ["accounts", "--status", "verified"]):
        assert h.run(*argv) == 0
        out = capsys.readouterr().out
        assert EMAIL_RE.search(out) is None, out
        assert "jane" not in out.lower()


def test_ready_to_email_says_unavailable_rather_than_fail(monkeypatch, capsys):
    from us_outbound.enrol import enrol

    def boom(*a, **kw):
        raise RuntimeError("boom")

    monkeypatch.setattr(enrol, "candidates", boom)
    assert harness().run("accounts") == 0
    assert "Ready to email: unavailable (RuntimeError: boom)" in printed(capsys.readouterr().out)


def test_an_empty_database_says_so(capsys):
    assert Harness(SETTINGS).run("accounts") == 0
    lines = printed(capsys.readouterr().out)
    assert "Companies: 0" in lines and lines[-1] == "No company is stored yet."


# -- filters --------------------------------------------------------------------------------------------


def listed(h: Harness, capsys, *argv: str) -> list[str]:
    assert h.run("accounts", *argv) == 0
    lines = printed(capsys.readouterr().out)
    head = next(i for i, line in enumerate(lines) if line.startswith(("Companies,", "Companies with", "No company")))
    return [line.split()[0] for line in lines[head + 1:] if line.startswith("  ")]


def test_filters(capsys):
    h = harness()
    assert listed(h, capsys, "--status", "verified,ENROLLED") == ["acme.com", "bright.org"]
    assert listed(h, capsys, "--status", "queued", "--status", "new") == ["calm.io", "fresh.com"]
    assert listed(h, capsys, "--tier", "priority") == ["acme.com"]
    assert listed(h, capsys, "--industry", "legal") == ["bright.org"]  # its group, Legal & Professional
    assert listed(h, capsys, "--industry", "AGENCY") == ["calm.io"]  # its industry, Marketing agency
    assert listed(h, capsys, "--tier", "Standard", "--industry", "soft") == []
    assert listed(h, capsys, "--limit", "2") == ["acme.com", "bright.org"]
    assert listed(h, capsys, "--limit", "0") == ["acme.com", "bright.org", "calm.io", "dull.net", "fresh.com"]


def test_the_list_head_says_what_it_shows(capsys):
    h = harness()
    assert h.run("accounts", "--limit", "2") == 0
    assert "Companies, in queue order (tier, then score): the first 2 of 5 (`--limit 0` lists them all):" in printed(
        capsys.readouterr().out)
    assert h.run("accounts", "--status", "verified,enrolled", "--tier", "Standard") == 0
    assert "Companies with status verified or enrolled, tier Standard, in queue order (tier, then score): all 1:" in (
        printed(capsys.readouterr().out))
    assert h.run("accounts", "--industry", "mining") == 0
    assert printed(capsys.readouterr().out)[-1] == 'No company with industry or group containing "mining".'


@pytest.mark.parametrize("argv, error", [
    (["--status", "enroled"], "no status 'enroled'; the statuses are: new, queued, verified, enrolled"),
    (["--tier", "Gold"], "no tier 'Gold'; the tiers are: Priority, Standard, Control, Held, Excluded"),
    (["--limit", "-1"], "--limit takes 0 (all) or more"),
    (["acme.com", "--tier", "Priority"], "a domain shows one company; --status, --tier, --industry and --limit"),
])
def test_bad_filters_are_refused(argv, error, capsys):
    assert harness().run("accounts", *argv) == 2
    assert error in capsys.readouterr().err


# -- one company ----------------------------------------------------------------------------------------


def test_one_company_in_full(capsys):
    h = harness()
    assert h.run("accounts", "https://www.Acme.com/about") == 0  # a web address is cut to its domain
    lines = printed(capsys.readouterr().out)
    assert lines[:13] == [
        "Company: Acme (acme.com)",
        "  Legal name: Acme Holdings Inc",
        "  HQ: New York, NY",
        "  Industry: Software (group Technology)",
        "  Employees: 64 (60 in the US) · size band 50-99 · founded 2015",
        "  Source: apollo · first seen 01 Oct 2026 10:00 UK · last scored 04 Oct 2026 10:00 UK",
        "  Tier: Priority (New People leader) · score 72",
        "  Angle: - · sender not assigned yet",
        "  Status: verified",
        "  Ids: account a-acme.com · Apollo org-acme · HubSpot -",
        "  Domain suppressed: no",
        "  Ready to email: yes, through Jane Doe (People leader: Head of People)",
        "Contacts: 2 (the enrolled one first, then in the order enrol picks from)",
    ]
    assert lines[13:17] == [
        "  Jane Doe · Head of People · People leader · state NY",
        "    Email: jane@acme.com (source apollo, status verified)",  # the one-company view shows emails
        "    Suppressed: no",
        "    Enrolled: no",
    ]
    assert "    Email: bob@acme.com (source clay, status valid)" in lines
    assert lines[-3:] == ["Signals: 0", "Events (sends, replies, bounces, opt-outs): 0", "Slack cards: 0"]


def test_one_enrolled_company_with_its_signals_events_and_cards(capsys):
    h = harness()
    assert h.run("accounts", "bright.org") == 0
    out = capsys.readouterr().out
    lines = printed(out)
    assert "  Ready to email: no: enrol takes verified companies, and this one is enrolled" in lines
    assert ("    Enrolled: 03 Oct 2026 10:00 UK · mailbox harry@meetspill.org · copy legal-pl-v1 · "
            f"campaign {C_HARRY}") in lines
    i = lines.index("Signals: 12, the latest 10:")
    assert lines[i + 1] == "  02 Oct 2026 10:00 UK · scoring · matched Hiring HR (weight 15)"
    assert lines[i + 2] == ('  01 Oct 2026 20:00 UK · apollo_jobs · open_roles = 10 · "10 open roles" · '
                            "https://bright.org/careers")
    assert lines[i + 11].startswith("Events (sends, replies, bounces, opt-outs): 2")
    assert lines[i + 12:i + 14] == [
        "  04 Oct 2026 10:00 UK · replied · harry@meetspill.org · positive · Sam Lee",
        "  03 Oct 2026 10:00 UK · sent · step 1 · harry@meetspill.org · Sam Lee",
    ]
    assert lines[-2:] == ["Slack cards: 1", "  01 Oct 2026 10:00 UK · send_approval · handled · state approved · "
                                             "outcome approved · handled 03 Oct 2026 10:00 UK by cli"]
    assert "PRIVATE REPLY TEXT" not in out and "PRIVATE DRAFT" not in out  # never reply text or a card's draft


def test_a_suppressed_contact_and_domain(capsys):
    h = harness()
    h.store.tables["suppression"].append({"email_sha256": None, "domain": "dull.net", "reason": "kill_rule",
                                          "source": "kill_rules", "added_at": T0})
    assert h.run("accounts", "dull.net") == 0
    lines = printed(capsys.readouterr().out)
    assert "  Domain suppressed: yes (kill_rule, from kill_rules, since 01 Oct 2026 10:00 UK)" in lines
    assert "    Suppressed: yes (unsubscribe)" in lines
    assert "  Ready to email: no: enrol takes verified companies, and this one is disqualified" in lines


def test_an_alias_shows_its_company(capsys):
    assert harness().run("accounts", "www.acme.co") == 0
    lines = printed(capsys.readouterr().out)
    assert lines[:2] == ["Company: Acme (acme.com)", "  (acme.co is recorded as an alias of acme.com)"]


def test_an_unknown_domain_exits_1(capsys):
    h = harness()
    assert h.run("accounts", "https://nope.com/x") == 1
    assert printed(capsys.readouterr().out) == ["No company with domain nope.com."]
    assert h.run("accounts", "not a domain") == 1
    assert printed(capsys.readouterr().out) == ["No company with domain not a domain."]


# -- the CSV --------------------------------------------------------------------------------------------


def read_csv(out: str) -> list[dict]:
    return list(csv.DictReader(io.StringIO(out)))


def test_csv_is_one_row_per_contact_and_nothing_else_on_stdout(capsys):
    h = harness()
    assert h.run("accounts", "--csv") == 0
    captured = capsys.readouterr()
    assert '{"event"' not in captured.out  # the log lines went to stderr
    assert '"event": "call"' in captured.err
    assert captured.err.rstrip().endswith("Wrote 6 rows: 5 companies, 4 contacts. The file holds personal data "
                                          "(names and emails): keep it private and delete it when you are done.")
    assert captured.out.splitlines()[0] == ",".join(view.CSV_COLUMNS)
    rows = read_csv(captured.out)
    assert [(r["domain"], r["email"]) for r in rows] == [
        ("acme.com", "jane@acme.com"), ("acme.com", "bob@acme.com"), ("bright.org", "sam@bright.org"),
        ("calm.io", ""), ("dull.net", "ann@dull.net"), ("fresh.com", ""),
    ]
    calm = rows[3]
    assert calm["clean_name"] == "Calm Agency" and calm["tier"] == "Control" and calm["score"] == "5"
    assert all(calm[k] == "" for k in view.CONTACT_COLUMNS) and calm["contact_id"] == ""
    sam = rows[2]
    assert sam["enrolled_at"] == "2026-10-03 10:00" and sam["first_seen"] == "2026-10-01 10:00"  # UK time
    assert sam["instantly_campaign"] == C_HARRY and sam["suppressed"] == ""
    assert rows[4]["suppressed"] == "yes" and rows[4]["suppressed_reason"] == "unsubscribe"
    assert rows[1]["title"] == "'=HYPERLINK(\"http://x.example\",\"CEO\")"  # stays text in a spreadsheet


def test_csv_takes_the_filters_and_a_domain(capsys):
    h = harness()
    assert h.run("accounts", "--csv", "--status", "verified,queued") == 0
    assert [r["contact_id"] for r in read_csv(capsys.readouterr().out)] == ["c-jane", "c-bob", ""]
    assert h.run("accounts", "--csv", "--limit", "1") == 0
    assert [r["domain"] for r in read_csv(capsys.readouterr().out)] == ["acme.com", "acme.com"]
    assert h.run("accounts", "bright.org", "--csv") == 0
    assert [r["email"] for r in read_csv(capsys.readouterr().out)] == ["sam@bright.org"]
    assert h.run("accounts", "nope.com", "--csv") == 1
    captured = capsys.readouterr()
    assert captured.out == "" and "No company with domain nope.com." in captured.err


# -- read-only ------------------------------------------------------------------------------------------


def test_every_mode_only_reads_the_database(capsys):
    h = harness()
    before = copy.deepcopy(h.store.tables)
    for argv in (["accounts"], ["accounts", "--tier", "Priority", "--limit", "0"], ["accounts", "acme.com"],
                 ["accounts", "bright.org"], ["accounts", "nope.com"], ["accounts", "--csv"]):
        h.run(*argv)
    assert h.store.tables == before  # no row written, no heartbeat
    assert h.transport.requests == []  # no Apollo, HubSpot, Instantly or Slack call
    assert {(c.job, c.live) for c in h.contexts} == {("accounts", False)}
    assert [w for c in h.contexts for w in c.guard.writes()] == []
    assert {r.system for c in h.contexts for r in c.guard.calls} == {"db"}


def test_accounts_takes_no_live_flag_and_is_in_help():
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["accounts", "--live"])
    text = " ".join(cli.build_parser().format_help().split())
    assert "accounts the companies and contacts we hold: a summary, a list, one company, or a CSV" in text
    sub = " ".join(cli.build_parser()._subparsers._group_actions[0].choices["accounts"].format_help().split())
    for name in (*view.STATUSES, *TIERS):  # --help writes the choices out, so it need not import the view
        assert name in sub, name
