"""A second contact at companies of 50 or more staff (enrol/second.py, replies/account_stop.py; Harry, 6 Oct 2026).

The second pick (another role, every check), the stagger, capacity ordering (first contacts of new accounts
first), the same sender, the card, the account-level stop on reply, bounce and unsubscribe, and the switch: off
changes nothing.
"""

from __future__ import annotations

import dataclasses
from collections import Counter
from datetime import UTC, datetime, timedelta

import pytest

from tests.fakes_replies import CAMPAIGNS, HANNAH as HANNAH_ADDR, JANE as JANE_ADDR, NOW as REPLY_NOW, make_world
from tests.test_enrol import NOW, instantly_posts, make
from tests.test_render import AGENCIES, account, contact, copy_row, make_settings
from us_outbound import suppression
from us_outbound.clients import instantly as instantly_client
from us_outbound.clients.instantly import LEAD_BOUNCED, LEAD_UNSUBSCRIBED
from us_outbound.contacts import pick
from us_outbound.enrol import approvals, capacity, enrol, focus, openers, second
from us_outbound.logs import hash_email
from us_outbound.replies import account_stop, outcomes
from us_outbound.settings.defaults import default_tabs
from us_outbound.settings.model import General
from us_outbound.settings.validate import validate_all

HANNAH_CAMPAIGN = "US Outbound – Hannah Spalding"
FIRST_SENT = datetime(2026, 10, 23, 14, 0, tzinfo=UTC)  # Fri 23 Oct, 10:00 ET: the first contact's email 1
ROLES = validate_all(default_tabs())[0].roles  # the Roles tab as shipped: at 50-249, People leader, then founder
COPY = (
    copy_row("agencies-people-v1", AGENCIES, role="People leader"),
    copy_row("agencies-founder-v1", AGENCIES, role="Founder or executive"),
    copy_row("general-founder-v1", "General", role="Founder or executive"),
    copy_row("general-ops-v1", "General", role="Operations"),
)


@pytest.fixture(autouse=True)
def default_openers(monkeypatch):
    """Each angle's default opener (enrol/openers.py is tested in test_openers.py); records whom it was asked for."""
    asked: list[str] = []

    def opener(ctx, a, c, check=None):
        asked.append(str(c.get("contact_id")))
        angle = ctx.settings.angle(str(a.get("angle") or ""))
        text = angle.default_opener if angle else ""
        return openers.Opener(text, openers.OPENER if text else openers.NONE, "angle default" if text else ""), ""

    monkeypatch.setattr(enrol, "account_opener", opener)
    return asked


def northwind(**kw) -> dict:
    """An enrolled agency of 64 staff (the 50-99 band, read as 50), Hannah's since its first contact."""
    return account(**{"account_id": "acc-nw", "domain": "northwind.com", "clean_name": "Northwind", "status": "enrolled",
                      "sender": "Hannah Spalding", **kw})


def jane(**kw) -> dict:
    """Northwind's first contact: the People leader, enrolled on Friday in Hannah's campaign."""
    base = {"email": "jane@northwind.com", "email_sha256": hash_email("jane@northwind.com"),
            "enrolled_at": FIRST_SENT - timedelta(hours=2), "enrolment_month": "2026-10",
            "instantly_campaign": HANNAH_CAMPAIGN, "instantly_lead_id": "lead-jane", "mailbox": "hannah@meetspill.org",
            "copy_version": "agencies-people-v1", "contact_slot": 1}
    return contact(contact_id="nw-jane", account_id="acc-nw", **{**base, **kw})


def omar(**kw) -> dict:
    """A founder at Northwind, picked but not enrolled."""
    base = {"contact_id": "nw-omar", "account_id": "acc-nw", "first_name": "Omar", "last_name": "Reyes",
            "role": "Founder or executive", "title": "CEO", "email": "omar@northwind.com",
            "email_sha256": hash_email("omar@northwind.com"), "email_status": "verified", "person_state": "NY",
            "email_source": "apollo"}
    return {**base, **kw}


def sent_event(contact_id: str = "nw-jane", at: datetime = FIRST_SENT, account_id: str = "acc-nw") -> dict:
    return {"event_id": f"sent-{contact_id}-{at.isoformat()}", "contact_id": contact_id, "account_id": account_id,
            "type": "sent", "step": 1, "mailbox": "hannah@meetspill.org", "occurred_at": at}


def settings(*, on: bool = True, live: bool = True, roles=ROLES, **general):
    general.setdefault("approver_slack_ids", ("U_HARRY",))
    general.setdefault("copy", COPY)
    s = make_settings(live_sending=live, second_contact=on, **general)
    return dataclasses.replace(s, roles=tuple(roles))


def world(*, on: bool = True, live: bool = False, auto_send: bool = True, accounts=None, contacts=None,
          sent_at: datetime | None = FIRST_SENT, now: datetime = NOW, hand_check="handled", **general):
    s = settings(on=on, live=live, **general)
    ctx, t = make(live=live, settings=s, now=now, auto_send=auto_send, hand_check=hand_check,
                  accounts=[northwind()] if accounts is None else accounts,
                  contacts=[jane(), omar()] if contacts is None else contacts)
    if sent_at is not None:
        ctx.store.insert("events", [sent_event(at=sent_at)])
    return ctx, t


def candidate_ids(ctx) -> list[str]:
    return [c.contact["contact_id"] for c in second.candidates(ctx)[0]]


# -- the settings ------------------------------------------------------------------------------------------------


def test_the_three_general_keys_default_to_off_at_50_staff_3_days_with_notes():
    g = General()
    assert (g.second_contact, g.second_contact_min_employees, g.second_contact_delay_days) == (False, 50, 3)
    rows = {r["key"]: r for r in default_tabs()["General"]}
    assert (rows["second_contact"]["value"], rows["second_contact_min_employees"]["value"],
            rows["second_contact_delay_days"]["value"]) == ("no", "50", "3")
    assert rows["second_contact"]["note"].startswith("yes: at companies of second_contact_min_employees or more staff")
    assert "both people's emails stop" in rows["second_contact"]["note"]
    assert "50-99 band counts as 50" in rows["second_contact_min_employees"]["note"]
    assert "never arrive the same day" in rows["second_contact_delay_days"]["note"]


@pytest.mark.parametrize("key, value, ok", [
    ("second_contact", "yes", True), ("second_contact_min_employees", "100", True),
    ("second_contact_min_employees", "0", False), ("second_contact_delay_days", "0", False),
    ("second_contact_delay_days", "5", True),
])
def test_the_keys_validate(key, value, ok):
    tabs = default_tabs()
    next(r for r in tabs["General"] if r["key"] == key)["value"] = value
    s, errors = validate_all(tabs)
    assert (not errors["General"]) is ok, errors["General"]
    if ok:
        assert str(getattr(s.general, key)).lower() in (value, "true")


# -- who ------------------------------------------------------------------------------------------------------------


def test_the_second_contact_is_the_best_ranked_of_another_role():
    """At 50-249 the Roles tab ranks the People leader, then the founder, then operations, then HR managers. After
    a People leader, an HR manager (People leader copy) is the same role; the founder comes before the COO."""
    people = [omar(contact_id="nw-coo", role="Operations", title="COO", email="coo@northwind.com",
                   email_sha256=hash_email("coo@northwind.com")),
              omar(contact_id="nw-hr", role="People leader", title="HR Manager", email="hr.manager@northwind.com",
                   email_sha256=hash_email("hr.manager@northwind.com")),
              omar()]
    ctx, _ = world(contacts=[jane(), *people])
    [c] = second.candidates(ctx)[0]
    assert (c.slot, c.contact["contact_id"], c.account["sender"]) == (2, "nw-omar", "Hannah Spalding")
    assert c.first == {"contact_id": "nw-jane", "first_name": "Jane", "last_name": "Doe", "title": "Head of People",
                       "role": "People leader", "email_1": "2026-10-23", "copy_version": "agencies-people-v1",
                       "test_id": ""}
    ctx, _ = world(contacts=[jane(), people[1]])  # only another People leader on file: nobody
    assert candidate_ids(ctx) == []
    assert second.candidates(ctx)[1] == Counter({"no contact of another role on file": 1})


@pytest.mark.parametrize("change, why", [
    ({"person_state": "CA"}, "contact in CA or WA"),
    ({"person_state": "Washington"}, "contact in CA or WA"),
    ({"email": "omar@gmail.com"}, "personal email domain"),
    ({"email": "info@northwind.com"}, "shared mailbox"),
    ({"email_status": "unavailable"}, "email status unavailable"),
    ({"suppressed": True}, "contact suppressed"),
])
def test_the_second_contact_passes_every_check_the_first_does(change, why):
    ctx, _ = world(contacts=[jane(), omar(**change)])
    assert second.candidates(ctx) == ([], Counter({why: 1}))


def test_a_suppressed_address_or_domain_and_kill_rule_holds_count_too():
    ctx, _ = world()
    suppression.add(ctx.store, email="omar@northwind.com", reason="unsubscribe", source="test", now=NOW)
    assert second.candidates(ctx) == ([], Counter({"contact suppressed": 1}))
    ctx, _ = world()
    suppression.add(ctx.store, domain="northwind.com", reason="customer", source="test", now=NOW)
    assert second.candidates(ctx) == ([], Counter({"domain suppressed": 1}))
    ctx, _ = world(contacts=[jane(), omar(email_source="clay")])
    ctx.store.insert("hitl_items", [{"item_id": "k1", "kind": "kill_rule", "status": "open", "created_at": NOW,
                                     "payload": {"action": "pause_source", "target": "clay", "reason": "bounces"}}])
    assert second.candidates(ctx) == ([], Counter({"email source paused by a kill rule": 1}))


@pytest.mark.parametrize("employees, band, minimum, ok", [
    (64, "50-99", 50, True), (49, "20-49", 50, False), (30, "20-49", 20, True), (120, "100-249", 150, False),
])
def test_the_size_threshold_reads_company_size(employees, band, minimum, ok):
    ctx, _ = world(accounts=[northwind(employees=employees, size_band=band)], second_contact_min_employees=minimum)
    assert candidate_ids(ctx) == (["nw-omar"] if ok else [])


# -- when ------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("sent_at, delay, ok", [
    (datetime(2026, 10, 24, 15, tzinfo=UTC), 3, True),  # Sat 24 Oct ET + 3 = Tue 27: today
    (datetime(2026, 10, 25, 15, tzinfo=UTC), 3, False),  # Sun 25 + 3 = Wed 28
    (datetime(2026, 10, 26, 15, tzinfo=UTC), 1, True),
    (FIRST_SENT, 5, False),  # Fri 23 + 5 = Wed 28
    (None, 3, False),  # email 1 not recorded as sent
])
def test_the_stagger_counts_from_the_first_contact_s_email_1(sent_at, delay, ok):
    ctx, _ = world(sent_at=sent_at, second_contact_delay_days=delay)
    assert candidate_ids(ctx) == (["nw-omar"] if ok else [])
    if not ok:
        [why] = second.candidates(ctx)[1]
        assert why == (second.NOT_DUE if sent_at else "its first contact's email 1 is not recorded as sent yet")


def test_never_before_the_first_contact_is_enrolled_nor_after_anyone_stopped():
    not_yet = jane(enrolled_at=None, enrolment_month=None, instantly_lead_id=None)
    ctx, _ = world(contacts=[not_yet, omar()])
    assert second.candidates(ctx) == ([], Counter({"its first contact is not enrolled yet": 1}))
    ctx, _ = world(accounts=[northwind(status="verified")])  # the first contact's card is still waiting
    assert candidate_ids(ctx) == []
    ctx, _ = world(accounts=[northwind(status="engaged")])
    assert candidate_ids(ctx) == []
    for event in ({"type": "replied", "reply_class": "negative"}, {"type": "replied", "reply_class": None},
                  {"type": "bounced"}, {"type": "unsubscribed"}, {"type": "complained"}):
        ctx, _ = world()
        ctx.store.insert("events", [{"event_id": "stop", "contact_id": "nw-jane", "account_id": "acc-nw",
                                     "occurred_at": NOW - timedelta(hours=1), **event}])
        assert candidate_ids(ctx) == [], event
    ctx, _ = world()  # an out-of-office reply stops nothing
    ctx.store.insert("events", [{"event_id": "ooo", "contact_id": "nw-jane", "account_id": "acc-nw", "type": "replied",
                                 "reply_class": "out_of_office", "occurred_at": NOW - timedelta(hours=1)}])
    assert candidate_ids(ctx) == ["nw-omar"]
    ctx, _ = world(contacts=[jane(), omar(enrolled_at=NOW - timedelta(days=1), instantly_lead_id="lead-omar",
                                          enrolment_month="2026-10", contact_slot=2),
                             omar(contact_id="nw-coo", role="Operations", email="coo@northwind.com")])
    assert second.candidates(ctx) == ([], Counter({"it has its second contact": 1}))  # one second contact, never two


# -- enrol: same sender, copy, capacity ------------------------------------------------------------------------------


def test_live_enrol_adds_the_second_contact_to_the_first_s_sender_with_its_own_role_s_copy(default_openers):
    ctx, t = world(live=True)
    out = enrol.run(ctx)
    assert out["status"] == "ok" and out["enrolled"] == 1, out
    [add] = instantly_posts(t)
    # Hannah's campaign, the account's sender, though Harry (two mailboxes) has more room for a new account.
    assert add.json["campaign_id"] == "c-hannah" and [lead["email"] for lead in add.json["leads"]] == ["omar@northwind.com"]
    o = ctx.store.get("contacts", contact_id="nw-omar")
    assert (o["contact_slot"], o["instantly_campaign"], o["copy_version"], o["enrolment_month"]) == (
        2, HANNAH_CAMPAIGN, "agencies-founder-v1", "2026-10")
    assert ctx.store.get("contacts", contact_id="nw-jane")["contact_slot"] == 1
    acct = ctx.store.get("accounts", account_id="acc-nw")
    assert (acct["status"], acct["sender"]) == ("enrolled", "Hannah Spalding")
    assert default_openers == ["nw-omar"]  # the opener is the second contact's own, for their role
    assert out["second_contacts"] == {"switch": "on", "ready": 1, "prepared": 1, "by_owner": {"Hannah Spalding": 1},
                                      "not_ready": {}}
    assert out["number_terms"]["second_contacts_ready"] == 1
    assert any(line.startswith("Second contacts: 1 ready") for line in out["limits"])
    # The second contact counts towards the week, as any contact does.
    from us_outbound import budget

    assert budget.enrolled_this_week(ctx.store.select("contacts"), ctx.now) == 1


def test_a_second_contact_waits_for_its_own_sender():
    """Hannah paused: her account's second contact waits for her, though Harry and Sam have room."""
    paused = tuple(dataclasses.replace(m, status="Paused") if m.owner_name == "Hannah Spalding" else m
                   for m in make_settings().mailboxes)
    ctx, t = world(live=True, mailboxes=paused)
    out = enrol.run(ctx)
    assert out["enrolled"] == 0 and instantly_posts(t) == []
    assert out["skipped"] == {"second contact: sender paused": 1}


def test_copy_falls_back_as_a_first_contact_s_does_and_never_repeats_the_first_s_row():
    agencies_people = COPY[0]
    general_founder = COPY[2]
    ctx, _ = world(copy=(agencies_people, general_founder))  # no agencies row for founders: General's
    [p] = [p for p in _prepared(ctx)]
    assert p.copy_version == "general-founder-v1"
    shared = copy_row("agencies-v1", AGENCIES)  # one row for every role: the first contact's
    ctx, _ = world(copy=(shared,), contacts=[jane(copy_version="agencies-v1"), omar()])
    out = enrol.run(ctx)
    assert out["prepared"] == 0 and out["skipped"] == {"second contact: same copy as the first contact": 1}
    ctx, _ = world(copy=(agencies_people,))
    assert enrol.run(ctx)["skipped"] == {"second contact: no approved copy": 1}


def _prepared(ctx) -> list[enrol.Prepared]:
    cands, _ = second.candidates(ctx)
    rows = enrol.sendable_copy(ctx.settings)
    out = []
    for c in cands:
        p = enrol.prepare(ctx, c, Counter({"Hannah Spalding": 5}), Counter(), rows)
        assert isinstance(p, enrol.Prepared), p
        out.append(p)
    return out


def three_new_accounts() -> list[dict]:
    return [account(account_id=f"acc-{i}", domain=f"new{i}.com", clean_name=f"New {i}", score=60 - i) for i in range(3)]


def three_new_contacts() -> list[dict]:
    return [contact(contact_id=f"con-{i}", account_id=f"acc-{i}", email=f"jane@new{i}.com") for i in range(3)]


def test_first_contacts_of_new_accounts_come_first_when_capacity_is_short():
    # weekly_enrol_cap 8: on Tuesday, with four send days left, two today; three new accounts are ready.
    ctx, t = world(live=True, weekly_enrol_cap=8, accounts=[northwind(), *three_new_accounts()],
                   contacts=[jane(), omar(), *three_new_contacts()], copy=COPY + (copy_row("agencies-v1", AGENCIES),))
    out = enrol.run(ctx)
    assert (out["number"], out["number_terms"]["ready_accounts"], out["number_terms"]["second_contacts_ready"]) == (2, 4, 1)
    leads = [lead["email"] for r in instantly_posts(t) for lead in r.json["leads"]]
    assert sorted(leads) == ["jane@new0.com", "jane@new1.com"]
    assert out["second_contacts"]["prepared"] == 0 and ctx.store.get("contacts", contact_id="nw-omar").get("enrolled_at") is None
    # With room left over, the second contact takes it, after every new account.
    ctx, t = world(live=True, weekly_enrol_cap=40, accounts=[northwind(), *three_new_accounts()],
                   contacts=[jane(), omar(), *three_new_contacts()], copy=COPY + (copy_row("agencies-v1", AGENCIES),))
    out = enrol.run(ctx)
    assert out["number"] == 4 and out["enrolled"] == 4 and out["second_contacts"]["prepared"] == 1


def test_a_second_contact_is_not_a_new_account_for_the_focus_shares():
    ctx, _ = world(live=True)
    enrol.run(ctx)
    assert focus.done_this_week(ctx) == Counter()


# -- the card ---------------------------------------------------------------------------------------------------------


def card_world():
    from tests.test_send_approvals import FakeSlack

    ctx, t = world(live=True, auto_send=False, hand_check=None)
    return ctx, t, FakeSlack(t)


def test_the_card_says_it_is_the_second_contact_and_who_the_first_was():
    from tests.test_send_approvals import HARRY_ID, blocks_text, poll

    ctx, t, sl = card_world()
    out = enrol.run(ctx)
    assert out["send_approvals"]["posted"] == 1 and instantly_posts(t) == []
    [row] = [r for r in ctx.store.select("hitl_items", {"kind": approvals.KIND})]
    p = row["payload"]
    assert (p["contact_slot"], p["first_contact"]["contact_id"], p["owner"]) == (2, "nw-jane", "Hannah Spalding")
    [card] = sl.cards()
    text = blocks_text(card)
    assert text.startswith("Send approval · second contact · *Northwind*")
    assert ("*Second contact* at Northwind: the first was Jane Doe, Head of People (People leader), email 1 on "
            "Fri 23 Oct. Same sender; both people's emails stop when either replies.") in text
    assert card["text"].startswith("Send approval (second contact): Northwind · Omar Reyes · from Hannah Spalding")
    sl.react("white_check_mark", HARRY_ID, ts=row["slack_ts"])
    assert poll(ctx)["outcomes"] == {"approved": 1}
    [add] = instantly_posts(t)
    assert add.json["campaign_id"] == "c-hannah"
    o = ctx.store.get("contacts", contact_id="nw-omar")
    assert (o["contact_slot"], o["instantly_lead_id"]) == (2, "lead-omar@northwind.com")
    assert ctx.store.get("accounts", account_id="acc-nw")["status"] == "enrolled"


def test_a_tick_after_the_first_contact_replied_is_blocked():
    from tests.test_send_approvals import HARRY_ID, poll

    ctx, t, sl = card_world()
    enrol.run(ctx)
    [row] = ctx.store.select("hitl_items", {"kind": approvals.KIND})
    ctx.store.insert("events", [{"event_id": "r1", "contact_id": "nw-jane", "account_id": "acc-nw", "type": "replied",
                                 "reply_class": "not_now", "occurred_at": NOW + timedelta(minutes=1)}])
    sl.react("white_check_mark", HARRY_ID, ts=row["slack_ts"])
    assert poll(ctx)["outcomes"] == {"blocked": 1}
    assert instantly_posts(t) == []
    reason = ctx.store.get("hitl_items", item_id=row["item_id"])["payload"]["reason"]
    assert reason == "Northwind: no second contact now: someone there replied, bounced, unsubscribed or complained"
    assert not ctx.store.get("contacts", contact_id="nw-omar").get("suppressed")  # their address is not suppressed


def test_the_drop_company_choice_says_the_first_s_emails_carry_on():
    p = {"company": "Northwind", "contact": {"first_name": "Omar"}, "expires_on": "2026-10-28", "contact_slot": 2}
    assert "🚫 *company*: drop Northwind: no second contact there (the first person's emails carry on)" in \
        approvals.choices_text(p, "U_HARRY")
    assert "🚫 *company*: drop Northwind\n" in approvals.choices_text({**p, "contact_slot": 1}, "U_HARRY")


# -- pick_contacts ------------------------------------------------------------------------------------------------------


def _pick_world(ctx, transport, team, matches, *, accounts=(), first_sent=FIRST_SENT, **general):
    from tests.test_pick_contacts import ORG, FakeApollo
    from tests.test_pick_contacts import account as pick_account

    general.setdefault("second_contact", True)
    ctx.settings = dataclasses.replace(ctx.settings, general=dataclasses.replace(ctx.settings.general, **general))
    ctx.store.insert("accounts", [pick_account(status="enrolled", sender="Hannah Spalding", employees=120,
                                               size_band="100-249"), *accounts])
    ctx.store.insert("contacts", [{
        "contact_id": "bf-vp", "account_id": "acc-1", "first_name": "Vic", "last_name": "Park", "title": "VP of People",
        "role": "People leader", "email": "vp@brightfin.com", "email_sha256": hash_email("vp@brightfin.com"),
        "email_status": "verified", "person_state": "NY", "enrolled_at": first_sent - timedelta(hours=1),
        "enrolment_month": "2026-10", "instantly_campaign": HANNAH_CAMPAIGN, "instantly_lead_id": "lead-vp",
        "contact_slot": 1}])
    ctx.store.insert("events", [sent_event("bf-vp", first_sent, "acc-1")])
    return FakeApollo(transport, {ORG: team, **{a["apollo_org_id"]: team for a in accounts}}, matches)


def _team():
    from tests.test_pick_contacts import person

    return [person("p-vp", "VP of People"), person("p-hr", "HR Manager"),
            person("p-ceo", "CEO", state="California"), person("p-coo", "COO"), person("p-office", "Office Manager")]


@pytest.mark.parametrize("coo, why", [
    ({"email": "coo@brightfin.com", "_suppress": True}, "contact suppressed"),
    ({"email": "coo@gmail.com"}, "personal email domain"),
    ({"email": "info@brightfin.com"}, "shared mailbox"),
    ({"email": "coo@brightfin.com", "state": "Washington"}, "contact in CA or WA"),
    ({"email": "coo@othercorp.com"}, "email not at the company's domain"),
    ({"email": "coo@brightfin.com", "email_status": "guessed"}, "email status guessed"),
])
def test_the_second_pick_reveals_another_role_through_every_check(ctx, transport, coo, why):
    from tests.test_pick_contacts import match

    coo = dict(coo)
    if coo.pop("_suppress", False):
        suppression.add(ctx.store, email=coo["email"], reason="unsubscribe", source="test", now=ctx.now)
    matches = {"p-coo": match("p-coo", **coo), "p-office": match("p-office", "ola.diaz@brightfin.com"),
               "p-vp": match("p-vp", "vp2@brightfin.com"), "p-hr": match("p-hr", "hr@brightfin.com")}
    apollo = _pick_world(ctx, transport, _team(), matches)
    out = pick.run(ctx)
    # The People leader and the HR manager are the first contact's role; the CEO is in California.
    assert apollo.revealed == ["p-coo", "p-office"]
    [c] = [c for c in ctx.store.select("contacts") if c["contact_id"] != "bf-vp"]
    assert (c["role"], c["email"]) == ("Operations", "ola.diaz@brightfin.com")
    reveals = [e["value"] for e in ctx.store.select("signal_events", {"source": pick.SOURCE, "fact": pick.REVEAL_FACT})]
    assert [(r["apollo_person_id"], r["kept"], r["reason"]) for r in reveals] == [("p-coo", False, why),
                                                                                  ("p-office", True, None)]
    [outcome] = [e["value"] for e in ctx.store.select("signal_events", {"source": pick.SOURCE, "fact": pick.OUTCOME_FACT})]
    assert (outcome["outcome"], outcome["slot"], outcome["left_out"]) == (
        "picked", 2, {"the first contact's role": 2, "contact in CA or WA": 1})
    assert out["second_contacts"] == {"ready_before": 0, "waiting": 1, "tried_recently": 0, "picked": 1}
    # Both reveals came out of the Apollo budget, as a first contact's do.
    assert [(r["system"], r["credits"]) for r in ctx.store.select("credit_ledger")] == [("apollo", 1.0), ("apollo", 1.0)]
    # Then enrol can propose them: another role, the same sender.
    [cand] = second.candidates(ctx)[0]
    assert (cand.contact["email"], cand.account["sender"]) == ("ola.diaz@brightfin.com", "Hannah Spalding")


def test_pick_reveals_a_second_contact_only_with_what_the_first_contacts_leave(ctx, transport):
    from tests.test_pick_contacts import ORG2, ORG3, match, person
    from tests.test_pick_contacts import account as pick_account

    from tests.test_pick_contacts import ORG

    news = [pick_account(account_id=f"acc-{i}", apollo_org_id=org, domain=f"new{i}.com", score=70)
            for i, org in ((2, ORG2), (3, ORG3))]
    apollo = _pick_world(ctx, transport, [], {}, accounts=news, weekly_enrol_cap=5)  # a lookahead of 2 accounts
    apollo.people = {ORG: [person("p-ceo1", "CEO"), person("p-coo", "COO")], ORG2: [person("p-ceo2", "CEO")],
                     ORG3: [person("p-ceo3", "CEO")]}
    apollo.matches = {"p-ceo1": match("p-ceo1", "ceo@brightfin.com"), "p-coo": match("p-coo", "coo@brightfin.com"),
                      "p-ceo2": match("p-ceo2", "ceo@new2.com"), "p-ceo3": match("p-ceo3", "ceo@new3.com")}
    out = pick.run(ctx)
    assert sorted(apollo.revealed) == ["p-ceo2", "p-ceo3"] and out["second_contacts"]["picked"] == 0
    # One new account fewer: the second contact gets the place it leaves, after the new account's.
    ctx.store.update("accounts", {"account_id": "acc-3"}, {"status": "enrolled", "tier": "Excluded"})
    ctx.store.delete("contacts", {"account_id": "acc-2"})
    ctx.store.delete("signal_events", {"source": pick.SOURCE})
    apollo.revealed.clear()
    out = pick.run(ctx)
    assert apollo.revealed == ["p-ceo2", "p-ceo1"] and out["second_contacts"]["picked"] == 1


def test_pick_waits_for_the_stagger(ctx, transport):
    from tests.test_pick_contacts import match

    apollo = _pick_world(ctx, transport, _team(), {"p-coo": match("p-coo", "coo@brightfin.com")},
                         first_sent=ctx.now - timedelta(hours=1))  # email 1 today: due Fri, beyond Wed
    out = pick.run(ctx)
    assert apollo.revealed == [] and out["status"] == "skipped"


# -- the account-level stop --------------------------------------------------------------------------------------------

OMAR = "omar@acmecreative.com"


def stop_world(default_settings, *, live=True, second_contact=True):
    w = make_world(default_settings, live=live)
    w.ctx.job = "sync_outcomes"
    if second_contact:
        w.ctx.store.insert("contacts", [{
            "contact_id": "k-omar", "account_id": "acc-acme", "first_name": "Omar", "last_name": "Reyes",
            "title": "CEO", "role": "Founder or executive", "email": OMAR, "email_sha256": hash_email(OMAR),
            "enrolment_month": "2026-10", "enrolled_at": REPLY_NOW - timedelta(days=6),
            "instantly_campaign": CAMPAIGNS["cmp-hannah"], "instantly_lead_id": "L-omar", "mailbox": HANNAH_ADDR,
            "contact_slot": 2}])
    return w


def deletes(w) -> list[str]:
    return [r.url.rsplit("/", 1)[1] for r in w.transport.requests if r.method == "DELETE"]


@pytest.mark.parametrize("who, status, stopped", [("L-jane", LEAD_BOUNCED, "L-omar"),
                                                  ("L-jane", LEAD_UNSUBSCRIBED, "L-omar"),
                                                  ("L-omar", LEAD_BOUNCED, "L-jane"),
                                                  ("L-omar", LEAD_UNSUBSCRIBED, "L-jane")])
def test_a_bounce_or_unsubscribe_stops_the_colleague_s_lead_and_suppresses_only_the_address(
        default_settings, who, status, stopped):
    w = stop_world(default_settings)
    address = JANE_ADDR if who == "L-jane" else OMAR
    other = OMAR if who == "L-jane" else JANE_ADDR
    w.lead(who, address, status)
    out = outcomes.run(w.ctx)
    assert out["account_stops"]["stopped"] == {"deleted": 1} and deletes(w) == [stopped]
    other_id = "k-omar" if who == "L-jane" else "k-jane"
    ev = w.ctx.store.get("events", event_id=f"lead_stopped:{other_id}")
    assert (ev["type"], ev["account_id"], ev["occurred_at"]) == ("lead_stopped", "acc-acme", w.ctx.now)
    # Per address: the one who bounced or unsubscribed is suppressed (and blocklisted for an unsubscribe); the
    # colleague's emails stop, but they are not suppressed, nor blocklisted.
    assert w.suppressed(address) and not w.suppressed(other)
    assert not w.ctx.store.get("contacts", contact_id=other_id).get("suppressed")
    assert all(other not in b for b in w.blocked)
    assert other_id in capacity.stopped_contacts(w.ctx.store)  # its later steps hold no slot
    w.at(w.ctx.now + timedelta(minutes=15))
    assert outcomes.run(w.ctx)["account_stops"]["to_stop"] == 0 and deletes(w) == [stopped]  # once


@pytest.mark.parametrize("cls, stops", [("positive", True), ("negative", True), ("referral", True),
                                        ("unsubscribe", True), ("other", True), ("out_of_office", False),
                                        (None, False)])
def test_a_reply_stops_the_colleague_s_lead_once_it_is_classified(default_settings, cls, stops):
    w = stop_world(default_settings)
    w.ctx.store.insert("events", [{"event_id": "em-1", "contact_id": "k-jane", "account_id": "acc-acme",
                                   "type": "replied", "reply_class": cls, "occurred_at": REPLY_NOW - timedelta(hours=1)}])
    out = account_stop.sweep(w.ctx)
    assert deletes(w) == (["L-omar"] if stops else []), out
    assert out["stopped"] == ({"deleted": 1} if stops else {})


def test_a_complaint_stops_the_colleague_and_the_lead_is_paused_once_instantly_s_pause_is_confirmed(
        default_settings, monkeypatch):
    monkeypatch.setattr(instantly_client, "LEAD_PAUSE_CONFIRMED", True)
    w = stop_world(default_settings)
    w.ctx.store.insert("events", [{"event_id": "c-1", "contact_id": "k-omar", "account_id": "acc-acme",
                                   "type": "complained", "occurred_at": REPLY_NOW - timedelta(hours=1)}])
    assert account_stop.sweep(w.ctx)["stopped"] == {"paused": 1}
    assert w.lead_patches == [("L-jane", {"status": instantly_client.LEAD_PAUSED})] and deletes(w) == []


def test_the_sweep_in_a_dry_run_calls_nothing(default_settings):
    w = stop_world(default_settings, live=False)
    w.lead("L-jane", JANE_ADDR, LEAD_BOUNCED)
    out = outcomes.run(w.ctx)["account_stops"]
    assert out["to_stop"] == 1 and out["would_stop"] == [{"account_id": "acc-acme", "contact_id": "k-omar",
                                                          "because": "bounced"}]
    assert deletes(w) == [] and w.lead_patches == [] and not w.events("lead_stopped")


def test_with_one_contact_per_account_the_sweep_calls_nothing(default_settings):
    w = stop_world(default_settings, second_contact=False)
    w.lead("L-jane", JANE_ADDR, LEAD_BOUNCED)
    out = outcomes.run(w.ctx)
    assert out["bounced"] == 1 and out["account_stops"] == {"to_stop": 0, "stopped": {}, "errors": []}
    assert deletes(w) == [] and w.lead_patches == []


# -- the switch: off changes nothing -------------------------------------------------------------------------------------


def test_switch_off_reads_nothing_and_changes_nothing_in_enrol(monkeypatch):
    # The same world with the switch off, with and without a second contact on file: the same run, to the lead.
    def run(contacts):
        ctx, t = world(on=False, live=True, accounts=[northwind(), *three_new_accounts()],
                       contacts=[jane(), *contacts, *three_new_contacts()], copy=COPY + (copy_row("agencies-v1", AGENCIES),))
        out = enrol.run(ctx)
        leads = sorted(lead["email"] for r in instantly_posts(t) for lead in r.json["leads"])
        return out, leads, ctx

    with_second, leads_a, ctx = run([omar()])
    without, leads_b, _ = run([])
    assert leads_a == leads_b == ["jane@new0.com", "jane@new1.com", "jane@new2.com"]
    for key in ("number", "number_terms", "prepared", "enrolled", "by_owner", "skipped", "limits"):
        assert with_second[key] == without[key], key
    assert with_second["second_contacts"] == {"switch": "off"} and "second_contacts_ready" not in with_second["number_terms"]
    assert ctx.store.get("contacts", contact_id="nw-omar").get("enrolled_at") is None
    # second.candidates reads nothing while the switch is off.
    reads: list = []
    monkeypatch.setattr(ctx.store, "select", lambda *a, **k: reads.append(a) or [])
    assert second.candidates(ctx) == ([], Counter()) and reads == []


def test_switch_off_pick_contacts_reveals_no_second_contact(ctx, transport):
    from tests.test_pick_contacts import match

    apollo = _pick_world(ctx, transport, _team(), {"p-coo": match("p-coo", "coo@brightfin.com")}, second_contact=False)
    out = pick.run(ctx)
    assert apollo.revealed == [] and apollo.searches == [] and "second_contacts" not in out


def test_status_the_daily_post_and_golive_name_the_switch(capsys):
    from tests.test_daily_post import world as post_world
    from us_outbound.learn import daily_post

    s = make_settings()
    assert second.describe(s) == "Second contact: off (General second_contact = no): one person per company."
    on = settings()
    assert second.describe(on) == ("Second contact: on, at companies of 50 or more staff: a person of another role, "
                                   "3 days after the first person's email 1, from the same sender, with the room the "
                                   "first contacts of new companies leave.")
    ctx, _ = post_world()
    lines, _ = daily_post.build(ctx)
    assert "  Second contact: off (General second_contact = no): one person per company." in lines
    from tests.test_operator_cli import SETTINGS, Harness, status_lines

    assert "Second contact: off (General second_contact = no): one person per company." in status_lines(
        Harness(SETTINGS), capsys)


# -- on Postgres -------------------------------------------------------------------------------------------------------

from tests.test_db import db, dsn, store  # noqa: E402,F401  (the Postgres fixtures; skipped without a database)


def test_on_postgres_the_second_contact_is_found_enrolled_and_stopped(store, monkeypatch):
    """The same path against the real DDL: the list filters, contact_slot, and the lead_stopped row."""
    import tests.test_enrol as enrol_world
    from tests import fakes

    monkeypatch.setattr(enrol_world, "make_context", lambda *a, **k: fakes.make_context(*a, store=store, **k))
    ctx, t = world(live=True)
    assert enrol.run(ctx)["enrolled"] == 1
    o = store.get("contacts", contact_id="nw-omar")
    assert (o["contact_slot"], o["instantly_campaign"]) == (2, HANNAH_CAMPAIGN)
    store.insert("events", [{"event_id": "bounced:lead-jane", "contact_id": "nw-jane", "account_id": "acc-nw",
                             "type": "bounced", "step": 1, "occurred_at": NOW}])
    t.route("GET", "/leads/lead-omar", {"id": "lead-omar@northwind.com", "campaign": "c-hannah"})
    assert account_stop.sweep(ctx)["stopped"] == {"deleted": 1}
    assert store.get("events", event_id="lead_stopped:nw-omar")["type"] == "lead_stopped"
    assert "nw-omar" in capacity.stopped_contacts(store)
    assert account_stop.to_stop(ctx) == []


def test_every_count_of_ready_to_send_includes_the_second_contacts():
    """enrol/today.py (9 Oct 2026): the daily post's headline counted first contacts only, its limits line both, and
    the accounts view first contacts only. One count now: what enrol could propose today."""
    from us_outbound.enrol import today
    from us_outbound.learn import daily_post
    from us_outbound.ops import accounts_view

    ctx, _ = world(weekly_enrol_cap=40, accounts=[northwind(), *three_new_accounts()],
                   contacts=[jane(), omar(), *three_new_contacts()], copy=COPY + (copy_row("agencies-v1", AGENCIES),))
    t = today.read(ctx)
    assert (len(t.ready), len(t.seconds), t.ready_count) == (3, 1, 4)
    assert t.limits.terms["ready_accounts"] == 4
    _, nums = daily_post.build(ctx)
    assert (nums["ready_accounts"], nums["ready_to_send"]) == (4, 4)
    assert accounts_view._ready(ctx) == 4
