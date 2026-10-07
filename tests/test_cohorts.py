"""Cohorts (config_version.py, learn/cohorts.py; Harry, 7 Oct 2026): what a contact was enrolled under, the campaign
constants every lead in flight shares, and the report by enrolment week at fixed ages with what changed between.

Harry: "ensure that it's easy to continue developing the system whilst live, i.e. that there is a cohort system in
place for contacts that have started not being interrupted by changes."
"""

from __future__ import annotations

import dataclasses
import inspect
import re
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from tests.fakes import FakeTransport, make_context
from tests.test_registry import SETTINGS, slack_routes
from tests.test_render import COPY, copy_row, make_settings
from us_outbound import config_version
from us_outbound.clients import instantly as instantly_client
from us_outbound.learn import cohorts, readout

NOW = datetime(2026, 11, 9, 12, 0, tzinfo=UTC)  # Monday 9 Nov 2026, 12:00 UK
W40, W41, W42 = date(2026, 9, 28), date(2026, 10, 5), date(2026, 10, 12)  # Mondays


# -- the campaign constants -----------------------------------------------------------------------------------------


def test_campaign_constants_are_pinned():
    """If this fails you changed what every in-flight lead receives or when (STEP_DAYS, the step template, the campaign
    settings). Follow docs/developing-while-live.md: bump this literal, deploy, then `campaigns ensure --fix
    --in-flight --live` or wait for the campaigns to drain (`us-outbound cohorts in-flight`)."""
    assert config_version.campaign_fingerprint() == "9dd3b701a130"


def test_a_lead_is_never_patched_with_variables():
    """A lead's emails are its custom variables, set once by POST /leads/add: the only lead PATCH there is sets its
    status (set_lead_paused), and nothing else in the package sends a lead's variables anywhere."""
    src = inspect.getsource(instantly_client.Instantly)
    patches = re.findall(r'"PATCH", f"/leads/[^\n]*\n[^\n]*\n\s*json=([^,]+),', src)
    assert patches == ['{"status": status}']
    root = Path(instantly_client.__file__).resolve().parents[1]
    for path in root.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert not re.search(r'"PATCH",\s*f?"/leads', text) or path.name == "instantly.py", path
    t = FakeTransport()
    t.route("GET", "/campaigns", {"items": [{"id": "c1", "name": "US Outbound – Hannah Spalding"}]})
    t.route("GET", "/leads/L1", {"id": "L1", "campaign": "c1"})
    ctx = make_context(SETTINGS, live=True, transport=t)
    ctx.clients.instantly.set_lead_paused("US Outbound – Hannah Spalding", "L1", True)
    [patch] = [r for r in t.requests if r.method == "PATCH"]
    assert patch.json == {"status": instantly_client.LEAD_PAUSED}


# -- the config version -----------------------------------------------------------------------------------------------


def versioned(settings, **tabs):
    return dataclasses.replace(settings, versions={**settings.versions, **tabs})


def test_config_version_changes_only_for_content_tabs(monkeypatch):
    monkeypatch.delenv("RAILWAY_GIT_COMMIT_SHA", raising=False)
    t0 = datetime(2026, 10, 7, 2, 0, tzinfo=UTC)
    base = versioned(make_settings(), Signals=t0, General=t0, **{"Named accounts": t0, "Mailboxes": t0})
    ctx = make_context(base)
    first = config_version.current(ctx)
    later = t0 + timedelta(days=1)

    def vid(settings):
        ctx.settings = settings
        return config_version.current(ctx).id

    # Who is enrolled, by whom, and the General keys that shape nothing sent: the same version.
    assert vid(versioned(base, **{"Named accounts": later, "Mailboxes": later, "General": later})) == first.id
    assert vid(dataclasses.replace(base, general=dataclasses.replace(base.general, live_sending=True))) == first.id
    assert vid(dataclasses.replace(base, copy=(*COPY, copy_row("legal-v1", "Legal Teams", status="draft")))) == first.id
    # What a contact gets: a new version.
    assert vid(versioned(base, Signals=later)) != first.id
    assert vid(dataclasses.replace(base, general=dataclasses.replace(base.general, price_from=250))) != first.id
    reworded = copy_row("agencies-v1", COPY[0].industry, bodies={**{n: COPY[0].step(n).body for n in (1, 2, 3, 4)},
                                                                 2: "A new email 2 for {{first_name}}."})
    assert vid(dataclasses.replace(base, copy=(reworded, COPY[1]))) != first.id
    monkeypatch.setenv("RAILWAY_GIT_COMMIT_SHA", "a4c5c27f00ba7e1234567890")
    deployed = config_version.current(make_context(base))
    assert deployed.id != first.id and deployed.code_sha == "a4c5c27f00ba"
    assert first.code_sha == "dev"
    assert set(first.snapshot) == {"settings_versions", "copy_hashes", "general", "signature_hash",
                                   "campaign_fingerprint", "step_days", "code_sha"}
    assert first.snapshot["settings_versions"] == {"Signals": t0.isoformat()}
    assert first.snapshot["general"]["send_window"] == "Mon–Fri 09:00–16:00 America/New_York"


def test_a_version_is_recorded_once_and_dry_run_records_it_too():
    ctx = make_context(make_settings(), now=NOW)
    cv = config_version.current(ctx)
    assert ctx.dry_run and config_version.record(ctx, cv) is True
    ctx.now = NOW + timedelta(days=1)
    assert config_version.record(ctx, cv) is False
    [row] = ctx.store.tables["config_versions"]
    assert row["config_version"] == cv.id and row["first_seen"] == NOW and row["copy_hashes"] == {
        c.copy_version: c.content_hash() for c in COPY}


def test_a_change_is_logged_only_when_live():
    ctx = make_context(SETTINGS, now=NOW, job="campaigns_ensure")
    config_version.log_change(ctx, config_version.CAMPAIGN_CHANGE, "US Outbound – Sam Jackson", ["steps.1"], {}, 3)
    assert ctx.store.tables["config_log"] == []
    ctx.guard.configure(live=True)
    config_version.log_change(ctx, config_version.CAMPAIGN_CHANGE, "US Outbound – Sam Jackson", ["steps.1"],
                              {"steps.1": [{"body": "new"}, {"body": "old"}]}, 3)
    [row] = ctx.store.tables["config_log"]
    assert (row["kind"], row["campaign"], row["changed_keys"], row["leads_in_flight"], row["changed_by"]) == (
        "campaign_change", "US Outbound – Sam Jackson", ["steps.1"], 3, "campaigns_ensure")


# -- the report's world -----------------------------------------------------------------------------------------------


class World:
    """Companies, their contacts and events in a MemoryStore, at NOW."""

    def __init__(self, settings=None, now=NOW):
        self.ctx = make_context(settings or make_settings(), now=now, job="cohorts",
                                transport=slack_routes(FakeTransport()))
        self.n = 0

    def company(self, enrolled: datetime, *, step1: datetime | None = None, reply_day: float | None = None,
                cls: str = "positive", bounced: bool = False, version: str | None = None, tier: str = "Priority",
                code: str | None = None, sends: int = 1) -> str:
        """One company with one contact enrolled then; step 1 sent at step1 (default: when enrolled)."""
        self.n += 1
        aid, cid = f"a{self.n}", f"c{self.n}"
        st = self.ctx.store
        st.insert("accounts", [{"account_id": aid, "domain": f"co{self.n}.com", "tier": tier, "angle": "General",
                                "industry_group": "Legal Teams", "status": "enrolled", "sender": "Hannah Spalding"}])
        st.insert("contacts", [{"contact_id": cid, "account_id": aid, "email": f"p@co{self.n}.com",
                                "enrolled_at": enrolled, "instantly_campaign": "US Outbound – Hannah Spalding",
                                "instantly_lead_id": f"L{self.n}", "copy_version": "general-v1", "tier_at_enrol": tier,
                                "subject_arm": "copy", "opener_arm": "opener", "config_version": version,
                                "code_sha": code}])
        at = step1 or enrolled
        if step1 is not False:
            st.insert("events", [{"event_id": f"s{self.n}-{i}", "contact_id": cid, "account_id": aid, "type": "sent",
                                  "step": i + 1, "occurred_at": at + timedelta(days=7 * i)} for i in range(sends)])
        if reply_day is not None:
            st.insert("events", [{"event_id": f"r{self.n}", "contact_id": cid, "account_id": aid, "type": "replied",
                                  "reply_class": cls, "occurred_at": at + timedelta(days=reply_day)}])
        if bounced:
            st.insert("events", [{"event_id": f"b{self.n}", "contact_id": cid, "account_id": aid, "type": "bounced",
                                  "step": 1, "occurred_at": at}])
        return aid

    def cohort(self, monday: date, n: int, replied: int, *, version: str | None = None, positive: int = 0,
               code: str | None = None) -> None:
        """n companies enrolled that Monday at 15:00 UTC, the first `replied` of them replying on day 2."""
        start = datetime(monday.year, monday.month, monday.day, 15, tzinfo=UTC)
        for i in range(n):
            self.company(start, reply_day=2 if i < replied else None, version=version, code=code,
                         cls="positive" if i < positive else "objection")

    def version(self, vid: str, first_seen: datetime, **snapshot) -> None:
        base = {"code_sha": "4fbfec5aaaaa", "campaign_fingerprint": "f1", "signature_hash": "s1",
                "step_days": [0, 7, 14, 21], "settings_versions": {}, "copy_hashes": {"general-v1": "aaaa1111"},
                "general": {"price_from": "195", "email1_subject_share": "0.5"}}
        self.ctx.store.insert("config_versions", [{"config_version": vid, "first_seen": first_seen, "run_id": "r",
                                                   **{**base, **snapshot}}])

    def only(self, cut: str = "all") -> cohorts.Cohort:
        [c] = cohorts.rows(self.ctx, cut)
        return c


# -- performance at fixed ages ----------------------------------------------------------------------------------------


def test_a_company_counts_at_an_age_only_once_it_has_reached_it():
    w = World()
    w.company(NOW - timedelta(days=10), reply_day=5)
    c = w.only()
    assert (c.ages[7].matured, c.ages[7].replied, c.ages[7].positive) == (1, 1, 1)
    assert [c.ages[a].emailed for a in (14, 21, 28)] == [0, 0, 0]
    assert cohorts.age_line(c.ages[14]) == "14 days: not reached yet"
    w = World()
    w.company(NOW - timedelta(days=30), reply_day=20, cls="out_of_office")
    c = w.only()
    assert all(c.ages[a].matured == 1 for a in cohorts.AGES) and c.ages[28].replied == 0  # an away message is no reply


def test_a_reply_after_the_age_counts_only_at_the_later_ages():
    w = World()
    w.company(NOW - timedelta(days=30), reply_day=16)
    c = w.only()
    assert [c.ages[a].replied for a in cohorts.AGES] == [0, 0, 1, 1]


def test_a_bounced_step_1_is_emailed_not_delivered():
    w = World()
    w.company(NOW - timedelta(days=30), bounced=True)
    c = w.only()
    assert (c.enrolled, c.emailed, c.delivered) == (1, 1, 0)
    assert (c.ages[7].emailed, c.ages[7].matured, c.ages[7].bounced, c.ages[7].sends) == (1, 0, 1, 1)


def test_a_second_contact_counts_to_its_company_s_cohort():
    w = World()
    aid = w.company(datetime(2026, 10, 6, 15, tzinfo=UTC), sends=2)  # W41
    w.ctx.store.insert("contacts", [{"contact_id": "second", "account_id": aid, "enrolled_at": datetime(2026, 10, 13, 15, tzinfo=UTC),
                                     "instantly_campaign": "US Outbound – Hannah Spalding", "instantly_lead_id": "L2b"}])
    w.ctx.store.insert("events", [
        {"event_id": "s2b", "contact_id": "second", "account_id": aid, "type": "sent", "step": 1,
         "occurred_at": datetime(2026, 10, 13, 16, tzinfo=UTC)},
        {"event_id": "b2b", "contact_id": "second", "type": "bounced", "step": 1,  # no account_id: found by contact
         "occurred_at": datetime(2026, 10, 13, 16, tzinfo=UTC)}])
    c = w.only()
    assert (c.week, c.enrolled, c.contacts, c.delivered) == ("2026-W41", 1, 2, 1)  # the first contact's step 1 went
    assert (c.ages[7].bounced, c.ages[14].bounced, c.ages[14].sends) == (0, 1, 3)
    assert c.ages[14].sends_by_step == {1: 2, 2: 1}


def test_the_cohort_is_the_uk_week_of_the_first_contact_s_enrolment():
    w = World()
    w.company(datetime(2026, 10, 11, 23, 30, tzinfo=UTC))  # Sunday 23:30 UTC is Monday 00:30 in BST
    c = w.only()
    assert (c.week, c.week_start, c.label) == ("2026-W42", W42, "W42")
    assert cohorts.week_days(W40) == "28 Sep–4 Oct" and cohorts.week_days(W41) == "5–11 Oct"


def test_under_30_companies_counts_only_and_a_bounce_rate_from_100_sends():
    w = World()
    w.cohort(W40, 29, 5)
    line = cohorts.age_line(w.only().ages[28])
    assert line.startswith("28 days: 5 of 29 replied (too few to read), 0 positive (too few to read)")
    assert "%" not in line
    w = World()
    w.cohort(W40, 30, 6)
    for i in range(1, 31):  # three more steps each: 120 sends
        w.ctx.store.insert("events", [{"event_id": f"x{i}-{k}", "contact_id": f"c{i}", "account_id": f"a{i}",
                                       "type": "sent", "step": k + 1, "occurred_at": datetime(2026, 9, 28, 15, tzinfo=UTC)
                                       + timedelta(days=7 * k)} for k in (1, 2, 3)])
    w.ctx.store.insert("events", [{"event_id": "bx", "contact_id": "c30", "account_id": "a30", "type": "bounced",
                                   "step": 4, "occurred_at": datetime(2026, 10, 19, 15, tzinfo=UTC)}])
    line = cohorts.age_line(w.only().ages[28])
    assert line.startswith("28 days: 6 of 30 replied (20.0%), 0 positive (0.0%), 0 meetings, 0 demos held")
    assert "1 bounce of 120 sends (0.8%) (step 1 30, step 2 30, step 3 30, step 4 30)" in line


def test_the_cuts_split_a_week_and_unstamped_contacts_read_as_unstamped():
    w = World()
    w.company(datetime(2026, 10, 5, 15, tzinfo=UTC))
    w.company(datetime(2026, 10, 8, 15, tzinfo=UTC), version="abc123abc123", tier="Standard")
    by_version = {c.value: c.enrolled for c in cohorts.rows(w.ctx, "config_version")}
    assert by_version == {"unstamped": 1, "abc123abc123": 1}
    assert {c.value for c in cohorts.rows(w.ctx, "tier")} == {"Priority", "Standard"}
    with pytest.raises(ValueError):
        cohorts.rows(w.ctx, "colour")


# -- what changed ----------------------------------------------------------------------------------------------------


def test_changes_say_what_differs_in_plain_words():
    w = World()
    t1, t2 = datetime(2026, 10, 5, 11, tzinfo=UTC), datetime(2026, 10, 12, 11, tzinfo=UTC)
    long_line = "Pressure in work and life seems to keep rising, and it hardly ever waits."
    w.version("v1", t1, general={"price_from": "195", "opener_generic": "Short line.", "utm_links": "no"},
              copy_hashes={"general-v1": "aaaa1111", "legal-v1": "cccc3333"},
              settings_versions={"Signals": (t1 - timedelta(days=3)).isoformat()})
    w.version("v2", t2, code_sha="a4c5c27bbbbb", signature_hash="s2",
              general={"price_from": "250", "opener_generic": long_line, "utm_links": "no"},
              copy_hashes={"general-v1": "bbbb2222", "agencies-v1": "dddd4444"},
              settings_versions={"Signals": t2.isoformat()})
    w.ctx.store.insert("settings", [
        {"tab": "Signals", "key": "New People leader", "values": {"weight": "20", "opener_people": "Old line."},
         "effective_from": t1 - timedelta(days=3), "effective_to": t2},
        {"tab": "Signals", "key": "New People leader", "values": {"weight": "30", "opener_people": long_line},
         "effective_from": t2, "effective_to": None}])
    w.ctx.store.insert("config_log", [
        {"log_id": "l1", "changed_at": datetime(2026, 10, 6, 20, 8, tzinfo=UTC), "kind": "campaign_change",
         "campaign": "US Outbound – Hannah Spalding", "changed_keys": ["steps.1", "steps.2"], "leads_in_flight": 11},
        {"log_id": "l2", "changed_at": datetime(2026, 10, 5, 9, tzinfo=UTC), "kind": "sender_name",
         "campaign": "US Outbound – Hannah Spalding", "changed_keys": ["hannah@meetspill.org"],
         "detail": {"from": ["Hannah", "at Spill"], "to": ["Hannah", "Spalding"]}, "leads_in_flight": 0}])
    assert cohorts.changes(w.ctx, "v1", "v2") == [
        "code 4fbfec5 → a4c5c27",
        "signature template changed",
        "General opener_generic: changed",
        "General price_from: 195 → 250",
        "Copy agencies-v1: now sendable",
        "Copy legal-v1: no longer sent",
        "Copy general-v1: new wording (aaaa1111 → bbbb2222)",
        "Signals New People leader: opener_people changed, weight 20 → 30",
        "Tue 06 Oct 21:08: steps.1, steps.2 applied to US Outbound – Hannah Spalding with 11 leads in flight",
    ]
    assert cohorts.changes(w.ctx, "v1", "v1") == ["No change."]
    assert cohorts.changes(w.ctx, "unstamped", "v1")[0].startswith("Not recorded for unstamped")
    week = cohorts.week_changes(w.ctx, datetime(2026, 10, 5, tzinfo=UTC), datetime(2026, 10, 13, tzinfo=UTC))
    assert week[-2:] == ['Mon 05 Oct 10:00: From name of hannah@meetspill.org set to "Hannah Spalding" (0 leads in '
                         'flight)', "Tue 06 Oct 21:08: steps.1, steps.2 applied to US Outbound – Hannah Spalding with "
                                    "11 leads in flight"]
    assert week[0] == "code 4fbfec5 → a4c5c27"


def test_a_copy_row_reworded_names_the_columns_from_the_settings_history():
    w = World()
    t1, t2 = datetime(2026, 10, 5, 11, tzinfo=UTC), datetime(2026, 10, 12, 11, tzinfo=UTC)
    w.version("v1", t1)
    w.version("v2", t2, copy_hashes={"general-v1": "bbbb2222"})
    w.ctx.store.insert("settings", [
        {"tab": "Copy", "key": "general-v1", "values": {"s2_subject": "Quick one", "qa": "pass aaaa1111"},
         "effective_from": t1 - timedelta(days=1), "effective_to": t1 + timedelta(days=2)},
        {"tab": "Copy", "key": "general-v1", "values": {"s2_subject": "A quick one", "qa": "pass bbbb2222"},
         "effective_from": t1 + timedelta(days=2), "effective_to": None}])
    assert cohorts.changes(w.ctx, "v1", "v2") == [
        "Copy general-v1: qa pass aaaa1111 → pass bbbb2222, s2_subject Quick one → A quick one"]


# -- insights --------------------------------------------------------------------------------------------------------


def test_two_readable_cohorts_are_compared_and_what_changed_is_named():
    w = World()
    w.version("v1", datetime(2026, 9, 28, 11, tzinfo=UTC))
    w.version("v2", datetime(2026, 10, 5, 11, tzinfo=UTC), general={"price_from": "250", "email1_subject_share": "0.5"})
    w.cohort(W40, 30, 9, version="v1")
    w.cohort(W41, 30, 2, version="v2")
    out = cohorts.insights(w.ctx, cohorts.rows(w.ctx))
    assert out == ["W41 replies less than W40 at 28 days (6.7% vs 30.0%, p = 0.02).",
                   "Between them: General price_from: 195 → 250. A coincidence to test, not a cause."]


def test_nothing_changed_between_them_is_said_too():
    w = World()
    w.version("v1", datetime(2026, 9, 28, 11, tzinfo=UTC))
    w.cohort(W40, 30, 9, version="v1", positive=9)
    w.cohort(W41, 30, 2, version="v1")
    out = cohorts.insights(w.ctx, cohorts.rows(w.ctx))
    assert out[1] == "W41 has positive replies less than W40 at 28 days (0.0% vs 30.0%, p = 0.00)."
    assert out[-1] == "Nothing changed between them."


def test_no_clear_difference_and_too_few_are_said_as_such():
    w = World()
    w.cohort(W40, 30, 3)
    w.cohort(W41, 30, 4)
    assert cohorts.insights(w.ctx, cohorts.rows(w.ctx)) == [
        "No clear difference in replies between W41 and W40 at 28 days (13.3% vs 10.0%, p = 0.69)."]
    w = World()
    w.cohort(W40, 30, 9)
    w.cohort(W41, 29, 2)
    assert cohorts.insights(w.ctx, cohorts.rows(w.ctx)) == [
        "Too few to read: W41 has 29 companies at 28 days (30 needed on each side to compare W41 with W40)."]


def test_unstamped_cohorts_never_break_a_comparison():
    w = World()
    w.version("v2", datetime(2026, 10, 5, 11, tzinfo=UTC))
    w.cohort(W40, 30, 9)  # enrolled before the stamp
    w.cohort(W41, 30, 2, version="v2")
    out = cohorts.insights(w.ctx, cohorts.rows(w.ctx))
    assert out[0].startswith("W41 replies less than W40")
    assert out[1] == "What changed between them is not recorded: contacts enrolled before 8 Oct 2026 are unstamped."


def test_two_versions_inside_one_week_are_compared():
    w = World()
    w.version("v1", datetime(2026, 10, 5, 11, tzinfo=UTC))
    w.version("v2", datetime(2026, 10, 7, 11, tzinfo=UTC), code_sha="a4c5c27bbbbb")
    w.cohort(W41, 30, 9, version="v1")
    w.cohort(W41, 30, 1, version="v2")
    out = cohorts.within_week(w.ctx, ["2026-W41"])
    assert out[0].startswith("W41 version v2 replies less than W41 version v1 at 28 days (3.3% vs 30.0%")
    assert out[1] == "Between them: code 4fbfec5 → a4c5c27. A coincidence to test, not a cause."


def test_a_reply_rate_falling_three_cohorts_running_is_said():
    w = World(now=datetime(2026, 11, 2, 12, tzinfo=UTC))
    for monday, replied in ((W40, 9), (W41, 6), (W42, 3)):
        w.cohort(monday, 30, replied)
    assert cohorts.run_of_three(cohorts.rows(w.ctx)) == (
        "The 14-day reply rate has fallen three cohorts running: W40 30.0%, W41 20.0%, W42 10.0%.")


# -- in flight --------------------------------------------------------------------------------------------------------


def test_in_flight_lists_each_campaign_s_leads_with_a_step_to_send():
    w = World(make_settings())
    w.company(NOW - timedelta(days=3))  # Friday: its last step is still to come
    w.company(NOW - timedelta(days=40))  # done
    stopped = w.company(NOW - timedelta(days=2))
    w.ctx.store.insert("events", [{"event_id": "stop", "contact_id": "c3", "account_id": stopped, "type": "replied",
                                   "reply_class": "positive", "occurred_at": NOW}])
    out = cohorts.in_flight_lines(w.ctx)
    # Fri 6 Nov, then the 13th, the 20th, and the 27th, a blackout date (Thanksgiving week): Monday 30 Nov.
    assert "  US Outbound – Hannah Spalding: 1 lead, the last step due Mon 30 Nov (W45 1)" in out
    assert "  US Outbound – Sam Jackson: none: a change to its campaign reaches no lead" in out


# -- the readout and the CLI -----------------------------------------------------------------------------------------


def test_the_readout_has_the_cohort_section_under_20_lines():
    w = World(SETTINGS)
    w.version("v1", datetime(2026, 9, 28, 11, tzinfo=UTC))
    w.version("v2", datetime(2026, 11, 3, 11, tzinfo=UTC), general={"price_from": "250", "email1_subject_share": "0.5"})
    w.cohort(W40, 30, 9, version="v1")
    w.cohort(W41, 30, 2, version="v1")
    w.company(datetime(2026, 11, 3, 15, tzinfo=UTC), version="v2")
    lines, nums = readout.build(w.ctx)
    start, end = lines.index("*Cohorts* (companies by enrolment week, at the latest age reached after email 1; "
                             "`us-outbound cohorts` has the table)"), lines.index(
        "*Signal value* (companies with the signal at enrolment, against those without)")
    section = lines[start:end - 1]
    assert len(section) < 20, section
    assert section[1] == "  W45 (2–8 Nov): 1 enrolled, 1 emailed; no company has reached 7 days yet"
    assert section[3] == ("  W40 (28 Sep–4 Oct): 30 enrolled, 30 emailed; at 28 days 9 of 30 replied (30.0%), "
                          "0 meetings; 0 bounces of 30 sends")
    assert "  W41 replies less than W40 at 28 days (6.7% vs 30.0%, p = 0.02)." in section
    assert section[section.index("*Settings changes* (last week)") + 1] == "  General price_from: 195 → 250"
    assert nums["cohorts"]["2026-W41"]["replied"][28] == [2, 30] and nums["settings_changes"] == 1


def test_the_cli_prints_the_three_forms_and_posts_nothing(capsys):
    from tests.test_cli import Harness

    h = Harness()
    assert h.run("cohorts") == 0
    assert "No contact has been enrolled yet." in capsys.readouterr().out
    h.store.insert("accounts", [{"account_id": "a1", "domain": "co1.com", "status": "enrolled", "tier": "Priority"}])
    h.store.insert("contacts", [{"contact_id": "c1", "account_id": "a1", "enrolled_at": datetime(2026, 10, 19, 15, tzinfo=UTC),
                                 "instantly_campaign": "US Outbound – Sam Jackson", "instantly_lead_id": "L1"}])
    h.store.insert("events", [{"event_id": "s1", "contact_id": "c1", "account_id": "a1", "type": "sent", "step": 1,
                               "occurred_at": datetime(2026, 10, 19, 15, tzinfo=UTC)}])
    assert h.run("cohorts", "--cut", "tier", "--age", "7") == 0
    out = capsys.readouterr().out
    assert "Priority: 2026-W43 (19–25 Oct): 1 company enrolled (1 contact), 1 emailed, 1 delivered" in out
    assert "  7 days: 0 of 1 replied (too few to read)" in out and "14 days" not in out
    assert h.run("cohorts", "changes") == 0
    assert "0 config versions recorded so far" in capsys.readouterr().out
    assert h.run("cohorts", "in-flight") == 0
    assert "  US Outbound – Sam Jackson: 1 lead, the last step due Mon 09 Nov (W43 1)" in capsys.readouterr().out
    assert h.run("cohorts", "changes", "only-one") == 2
    assert not [r for r in h.transport.requests if r.method != "GET"]
    assert h.store.tables["heartbeats"] == [] and h.store.tables["config_versions"] == []


def test_cohorts_parses():
    from us_outbound.ops import cli

    args = cli.build_parser().parse_args(["cohorts", "changes", "v1", "v2"])
    assert (args.action, args.versions, args.cut, args.weeks) == ("changes", ["v1", "v2"], "all", 8)
    assert cli.build_parser().parse_args(["cohorts"]).action is None
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["cohorts", "--cut", "colour"])
    assert tuple(cli.build_parser()._subparsers._group_actions[0].choices["cohorts"]._option_string_actions[
        "--cut"].choices) == cohorts.CUTS
