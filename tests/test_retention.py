"""The retention job (ops/retention.py; SPEC 6 "Retention and erasure", SPEC 13): Instantly leads 31 days after their
last step, never in flight, never with an opt-out or bounce still to record; dry-run counts and changes nothing."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from tests.fakes import FakeTransport, make_context
from tests.test_registry import C_EU, C_HANNAH, FakeInstantly
from tests.test_render import make_settings
from us_outbound.clients.instantly import LEAD_ACTIVE, LEAD_BOUNCED, LEAD_PAUSED, LEAD_UNSUBSCRIBED
from us_outbound.enrol import capacity
from us_outbound.ops import retention
from us_outbound.ops.heartbeat import run_job
from us_outbound.replies import account_stop, optout

NOW = datetime(2026, 12, 15, 17, 0, tzinfo=UTC)  # Tuesday 15 Dec 2026, 12:00 US Eastern
TODAY = date(2026, 12, 15)
SETTINGS = make_settings()  # Thanksgiving week (23 to 27 Nov) and 18 Dec to 4 Jan are blackout dates


def last_step(enrolled: date) -> date:
    return capacity.step_days(enrolled, SETTINGS)[-1]


class World:
    """Contacts with leads in Hannah's campaign, in a FakeInstantly that answers 404 for a lead it no longer holds."""

    def __init__(self, *, live: bool = True, now: datetime = NOW):
        self.t = FakeTransport()
        self.inst = FakeInstantly(self.t)
        self.campaign = self.inst.add_campaign(C_HANNAH, status=1)
        self.ctx = make_context(SETTINGS, live=live, transport=self.t, now=now, job="retention")
        self.n = 0

    def contact(self, enrolled: date, *, sends: int = 4, status: int | None = None, listed: bool = True,
                campaign: str = C_HANNAH, **kw) -> dict:
        """One enrolled contact at its own account, enrolled at 10:00 US Eastern that day, with `sends` steps sent."""
        self.n += 1
        cid, aid = f"k{self.n}", f"a{self.n}"
        if listed:
            lead = self.inst.add_lead(self.campaign["id"], f"p{self.n}@co{self.n}.com")
            if status is not None:
                lead["status"] = status
            lead_id = lead["id"]
        else:  # gone already (the account-level stop deleted it, or a person did): Instantly answers 404
            lead_id = f"gone{self.n}"
            self.t.route("GET", f"/leads/{lead_id}", body={"error": "not found"}, status=404)
        at = datetime(enrolled.year, enrolled.month, enrolled.day, 15, tzinfo=UTC)
        row = {"contact_id": cid, "account_id": aid, "email": f"p{self.n}@co{self.n}.com", "enrolled_at": at,
               "enrolment_month": f"{enrolled:%Y-%m}", "instantly_campaign": campaign, "instantly_lead_id": lead_id,
               **kw}
        st = self.ctx.store
        st.insert("accounts", [{"account_id": aid, "domain": f"co{self.n}.com", "status": "enrolled"}])
        st.insert("contacts", [row])
        st.insert("events", [{"event_id": f"s{self.n}-{i + 1}", "contact_id": cid, "account_id": aid, "type": "sent",
                              "step": i + 1, "occurred_at": at + timedelta(days=7 * i)} for i in range(sends)])
        return row

    def event(self, contact: dict, type_: str, day: date, **kw) -> None:
        self.ctx.store.insert("events", [{
            "event_id": kw.pop("event_id", f"{type_}-{contact['contact_id']}"), "contact_id": contact["contact_id"],
            "account_id": contact["account_id"], "type": type_,
            "occurred_at": datetime(day.year, day.month, day.day, 16, tzinfo=UTC), **kw}])

    def run(self) -> dict:
        return retention.run(self.ctx)

    def deleted(self) -> list[str]:
        return [r.url.rsplit("/", 1)[1] for r in self.t.requests if r.method == "DELETE"]

    def row(self, contact: dict) -> dict:
        return self.ctx.store.get("contacts", contact_id=contact["contact_id"])


# -- 1. Instantly leads, 31 days after their last step (SPEC 13) -------------------------------------------------------


@pytest.mark.parametrize("days_after", [31, 32])
def test_a_lead_leaves_instantly_more_than_31_days_after_its_last_step(days_after):
    enrolled = date(2026, 10, 6)  # Tuesday: steps on 6, 13, 20 and 27 Oct
    end = last_step(enrolled)
    assert end == date(2026, 10, 27)
    now = datetime.combine(end + timedelta(days=days_after), datetime.min.time(), UTC) + timedelta(hours=17)
    w = World(now=now)
    c = w.contact(enrolled)
    out = w.run()["leads"]
    if days_after == 31:
        assert out["due"] == 0 and w.deleted() == [] and w.row(c)["instantly_lead_id"] == c["instantly_lead_id"]
        return
    assert out == {"due": 1, "deleted": 1, "already_gone": 0, "held": {}, "left_for_next_run": 0}
    assert w.deleted() == [c["instantly_lead_id"]] and c["instantly_lead_id"] not in w.inst.leads
    row = w.row(c)
    assert row["instantly_lead_id"] is None and row["lead_deleted_at"] == now
    # Its enrolment stays, so the forecast, the second-contact rules and the cohorts read it as before.
    assert row["enrolled_at"] and row["enrolment_month"] == "2026-10" and row["instantly_campaign"] == C_HANNAH
    assert w.run()["leads"]["due"] == 0 and len(w.deleted()) == 1  # once


def test_the_last_step_follows_blackout_dates_like_the_send_forecast():
    """Enrolled Tue 3 Nov: steps on 3, 10 and 17 Nov, then the 24th falls in Thanksgiving week: Mon 30 Nov."""
    assert last_step(date(2026, 11, 3)) == date(2026, 11, 30)
    w = World(now=datetime(2026, 12, 31, 17, tzinfo=UTC))  # 31 days after 30 Nov
    w.contact(date(2026, 11, 3))
    assert w.run()["leads"]["due"] == 0
    w = World(now=datetime(2027, 1, 1, 17, tzinfo=UTC))
    w.contact(date(2026, 11, 3))
    assert w.run()["leads"]["deleted"] == 1


def test_dry_run_counts_and_changes_nothing():
    w = World(live=False)
    c = w.contact(date(2026, 10, 6))
    out = w.run()
    assert out["dry_run"] is True and out["leads"]["due"] == 1 and out["leads"]["deleted"] == 0
    assert w.deleted() == [] and w.ctx.guard.writes("instantly") == []
    assert w.row(c)["instantly_lead_id"] == c["instantly_lead_id"] and not w.row(c).get("lead_deleted_at")


def test_a_stopped_lead_leaves_31_days_after_its_stop():
    """Enrolled 5 Nov; a reply on the 9th stops it: 32 days on it goes, though its fourth step was due 30 Nov."""
    w = World(now=datetime(2026, 12, 11, 17, tzinfo=UTC))
    replied = w.contact(date(2026, 11, 5), sends=1)
    w.event(replied, "replied", date(2026, 11, 9), reply_class="negative")
    running = w.contact(date(2026, 11, 5), sends=1, status=LEAD_ACTIVE)  # no stop: its last step was 30 Nov
    out = w.run()["leads"]
    assert w.deleted() == [replied["instantly_lead_id"]] and out["deleted"] == 1
    assert w.row(running)["instantly_lead_id"]


@pytest.mark.parametrize("stop", [
    ("bounced", {}), ("unsubscribed", {}), ("complained", {}), ("lead_stopped", {}),
    ("replied", {"reply_class": "unsubscribe"}),
])
def test_each_stop_of_its_own_dates_the_end(stop):
    type_, kw = stop
    w = World(now=datetime(2026, 12, 11, 17, tzinfo=UTC))
    c = w.contact(date(2026, 11, 5), sends=1)
    marker = {"unsubscribed": optout.lead_marker(c["instantly_lead_id"]),
              "bounced": f"bounced:{c['instantly_lead_id']}"}.get(type_)
    w.event(c, type_, date(2026, 11, 9), **({"event_id": marker} if marker else {}), **kw)
    if type_ == "replied":  # the opt-out by reply is recorded
        w.event(c, "unsubscribed", date(2026, 11, 9), event_id=optout.reply_marker(f"{type_}-{c['contact_id']}"))
    assert w.run()["leads"]["deleted"] == 1


def test_a_booking_at_the_company_stops_its_leads():
    w = World(now=datetime(2026, 12, 11, 17, tzinfo=UTC))
    c = w.contact(date(2026, 11, 5), sends=1)
    w.ctx.store.insert("events", [{"event_id": "m1", "account_id": c["account_id"], "type": "meeting_booked",
                                   "occurred_at": datetime(2026, 11, 9, 16, tzinfo=UTC)}])
    assert w.run()["leads"]["deleted"] == 1


@pytest.mark.parametrize("now, due", [(datetime(2027, 1, 1, 17, tzinfo=UTC), 0), (datetime(2027, 1, 2, 17, tzinfo=UTC), 1)])
def test_a_lead_stays_while_its_conversation_goes_on(now, due):
    """A reply on 9 Nov stops it, but the desk answers on the 20th and they write again on 1 Dec: 31 days from then."""
    w = World(now=now)
    c = w.contact(date(2026, 11, 5), sends=1)
    w.event(c, "replied", date(2026, 11, 9), reply_class="positive", event_id="em-1")
    w.event(c, "reply_sent", date(2026, 11, 20), event_id="desk-1", approval="approved")
    w.event(c, "replied", date(2026, 12, 1), reply_class="positive", event_id="em-2")
    assert w.run()["leads"]["deleted"] == due


def test_a_reply_waiting_for_a_person_holds_the_lead():
    """The desk answers in the lead's thread (Instantly.reply), so a card still open keeps it."""
    w = World(now=datetime(2026, 12, 11, 17, tzinfo=UTC))
    c = w.contact(date(2026, 11, 5), sends=1)
    w.event(c, "replied", date(2026, 11, 9), reply_class="positive", event_id="em-1")
    w.ctx.store.insert("hitl_items", [{"item_id": "reply:em-1", "kind": "reply", "status": "escalated",
                                       "contact_id": c["contact_id"], "created_at": NOW - timedelta(days=32)}])
    out = w.run()["leads"]
    assert out["due"] == 0 and out["held"] == {retention.REPLY_WAITING: 1}
    w.ctx.store.update("hitl_items", {"item_id": "reply:em-1"}, {"status": "handled"})
    assert w.run()["leads"]["deleted"] == 1


@pytest.mark.parametrize("cls", ["out_of_office", None])
def test_an_out_of_office_or_unclassified_reply_is_no_stop(cls):
    w = World(now=datetime(2026, 12, 11, 17, tzinfo=UTC))
    c = w.contact(date(2026, 11, 5), sends=1)
    w.event(c, "replied", date(2026, 11, 9), reply_class=cls)
    assert w.run()["leads"]["due"] == 0


def test_a_stop_before_the_contact_was_enrolled_does_not_count():
    w = World(now=datetime(2026, 12, 11, 17, tzinfo=UTC))
    c = w.contact(date(2026, 11, 5), sends=1)
    w.ctx.store.insert("events", [{"event_id": "m1", "account_id": c["account_id"], "type": "meeting_booked",
                                   "occurred_at": datetime(2026, 10, 1, 16, tzinfo=UTC)}])
    assert w.run()["leads"]["due"] == 0


def test_a_send_later_than_the_forecast_moves_the_last_step():
    """Instantly sends late after a stop and a start: the latest send recorded wins."""
    w = World()
    c = w.contact(date(2026, 10, 6), last_step_at=datetime(2026, 11, 20, 15, tzinfo=UTC))
    assert w.run()["leads"]["due"] == 0  # 25 days ago
    w.ctx.store.update("contacts", {"contact_id": c["contact_id"]},
                       {"last_step_at": datetime(2026, 11, 12, 15, tzinfo=UTC)})  # 33 days ago
    assert w.run()["leads"]["deleted"] == 1


@pytest.mark.parametrize("status", [LEAD_ACTIVE, LEAD_PAUSED])
def test_a_lead_instantly_still_sends_waits_until_its_steps_are_recorded(status):
    """A campaign paused past the forecast holds the later steps: active or paused in Instantly, no stop, fewer
    sends recorded than steps."""
    w = World()
    c = w.contact(date(2026, 10, 6), sends=2, status=status)
    out = w.run()["leads"]
    assert out["due"] == 0 and out["held"] == {retention.STEPS_LEFT: 1} and w.deleted() == []
    w.ctx.store.insert("events", [{"event_id": f"s-late-{i}", "contact_id": c["contact_id"], "type": "sent",
                                   "occurred_at": datetime(2026, 11, 3, 15, tzinfo=UTC)} for i in (3, 4)])
    assert w.run()["leads"]["deleted"] == 1


def test_a_lead_instantly_lists_as_finished_goes_with_steps_unrecorded():
    w = World()
    w.contact(date(2026, 10, 6), sends=3, status=3)  # completed
    assert w.run()["leads"]["deleted"] == 1


def test_an_unsubscribe_not_recorded_yet_holds_the_lead():
    """replies/optout.py retries from the lead's status until the opt-out is recorded: deleting it would end that."""
    w = World()
    c = w.contact(date(2026, 10, 6), status=LEAD_UNSUBSCRIBED)
    out = w.run()["leads"]
    assert out["held"] == {retention.OPT_OUT_PENDING: 1} and w.deleted() == []
    w.ctx.store.insert("events", [{"event_id": optout.lead_marker(c["instantly_lead_id"]), "type": "unsubscribed",
                                   "contact_id": c["contact_id"], "occurred_at": NOW}])
    assert w.run()["leads"]["deleted"] == 1


def test_an_opt_out_by_reply_not_recorded_yet_holds_the_lead():
    w = World()
    c = w.contact(date(2026, 10, 6))
    w.event(c, "replied", date(2026, 10, 8), reply_class="unsubscribe", event_id="em-1")
    assert w.run()["leads"]["held"] == {retention.OPT_OUT_PENDING: 1}
    w.ctx.store.insert("events", [{"event_id": optout.reply_marker("em-1"), "type": "unsubscribed",
                                   "contact_id": c["contact_id"], "occurred_at": NOW}])
    assert w.run()["leads"]["deleted"] == 1


def test_a_bounce_not_recorded_yet_holds_the_lead():
    w = World()
    c = w.contact(date(2026, 10, 6), status=LEAD_BOUNCED)
    assert w.run()["leads"]["held"] == {retention.BOUNCE_PENDING: 1}
    w.event(c, "bounced", date(2026, 10, 6), event_id=f"bounced:{c['instantly_lead_id']}")
    assert w.run()["leads"]["deleted"] == 1


def test_a_lead_gone_already_counts_as_deleted():
    """The account-level stop deleted it (until Instantly's lead pause is confirmed): Instantly answers 404."""
    w = World(now=datetime(2026, 12, 11, 17, tzinfo=UTC))
    c = w.contact(date(2026, 11, 5), sends=1, listed=False)
    w.event(c, "lead_stopped", date(2026, 11, 9), event_id=account_stop.marker(c["contact_id"]))
    out = w.run()["leads"]
    assert out["deleted"] == 0 and out["already_gone"] == 1 and w.deleted() == []
    row = w.row(c)
    assert row["instantly_lead_id"] is None and row["lead_deleted_at"]


def test_never_a_lead_in_flight_or_outside_our_campaigns():
    w = World()
    flying = w.contact(date(2026, 12, 1), sends=2, status=LEAD_ACTIVE)
    eu = w.contact(date(2026, 10, 6), campaign=C_EU)
    out = w.run()["leads"]
    assert out["due"] == 0 and w.deleted() == []
    assert w.row(flying)["instantly_lead_id"] and w.row(eu)["instantly_lead_id"]
    assert all(c.target.startswith("US Outbound – ") for c in w.ctx.guard.calls if c.system == "instantly"
               and c.action.startswith("lead."))


def test_a_lead_the_send_forecast_has_in_flight_is_held(monkeypatch):
    """Belt and braces: whatever dates it, a lead enrol/capacity.in_flight lists is never deleted."""
    w = World()
    c = w.contact(date(2026, 10, 6))
    monkeypatch.setattr(capacity, "in_flight", lambda store, settings, today: [c])
    out = w.run()["leads"]
    assert out["due"] == 0 and out["held"] == {retention.IN_FLIGHT: 1} and w.deleted() == []


def test_at_most_leads_per_run_oldest_first(monkeypatch):
    monkeypatch.setattr(retention, "LEADS_PER_RUN", 2)
    w = World()
    late, early, middle = (w.contact(date(2026, 10, d)) for d in (8, 6, 7))
    out = w.run()["leads"]
    assert out["due"] == 3 and out["deleted"] == 2 and out["left_for_next_run"] == 1
    assert w.deleted() == [early["instantly_lead_id"], middle["instantly_lead_id"]]
    assert w.run()["leads"]["deleted"] == 1 and w.deleted()[-1] == late["instantly_lead_id"]


def test_a_failed_delete_is_reported_and_tried_again():
    w = World()
    c = w.contact(date(2026, 10, 6))
    w.t.route("DELETE", f"/leads/{c['instantly_lead_id']}", body={"error": "busy"}, status=500)
    out = w.run()
    assert out["leads"]["deleted"] == 0 and len(out["errors"]) == 1 and out["errors"][0].startswith(c["contact_id"])
    assert w.row(c)["instantly_lead_id"] == c["instantly_lead_id"]
    w.t.routes.pop()
    assert w.run()["leads"]["deleted"] == 1


def test_a_deleted_lead_is_no_lead_to_the_code_that_reads_one():
    """The account-level stop, the in-flight list and enrol all read instantly_lead_id; a deleted lead has none."""
    from us_outbound.enrol import enrol, second

    w = World(now=datetime(2026, 12, 11, 17, tzinfo=UTC))
    first = w.contact(date(2026, 10, 6))
    w.run()
    row = w.row(first)
    assert row["instantly_lead_id"] is None
    # A colleague's reply at the account later: the sweep has no lead of first's to stop.
    w.ctx.store.insert("contacts", [{"contact_id": "k-colleague", "account_id": first["account_id"]}])
    w.event({"contact_id": "k-colleague", "account_id": first["account_id"]}, "replied", date(2026, 12, 10),
            reply_class="positive")
    assert account_stop.to_stop(w.ctx) == []
    assert capacity.in_flight(w.ctx.store, SETTINGS, TODAY) == []
    assert second.is_enrolled(row) and enrol.contact_block(row, set(), set()) == "contact already enrolled"


def test_the_job_runs_with_a_heartbeat():
    w = World()
    w.contact(date(2026, 10, 6))
    summary = run_job(w.ctx, retention.run)
    [beat] = w.ctx.store.select("heartbeats", {"job": "retention"})
    assert beat["status"] == "ok" and beat["detail"]["leads"]["deleted"] == 1 and summary["errors"] == []


# -- 2. Reply text, after 90 days (SPEC 6) -----------------------------------------------------------------------------


def reply_card(item_id: str, created: datetime, kind: str = "reply", **payload) -> dict:
    base = {"reply_class": "objection", "objection": "already have an EAP", "demo_requested": False,
            "language_terms": ["EAP"], "competitor_named": "Lyra", "reply_excerpt": "We already use Lyra, thanks.",
            "summary": "Has an EAP already.", "draft": "Thanks Jane, ...", "draft_problems": [],
            "referral": {"name": "Sam Roe", "title": "COO", "email": "sam@acme.com"}, "received_at": created.isoformat(),
            "instantly_email_id": f"em-{item_id}", "mailbox": "hannah@meetspill.org", "desk": {"edited_by": "U_HARRY"}}
    return {"item_id": item_id, "kind": kind, "status": "handled", "contact_id": "k1", "account_id": "a1",
            "event_id": f"em-{item_id}", "payload": {**base, **payload}, "created_at": created}


def reply_world(live: bool = True) -> World:
    w = World(live=live)
    old, recent = NOW - timedelta(days=91), NOW - timedelta(days=89)
    w.ctx.store.insert("events", [
        {"event_id": "em-old", "contact_id": "k1", "account_id": "a1", "type": "replied", "reply_class": "objection",
         "reply_text": "We already use Lyra, thanks.", "language_terms": ["EAP"], "competitor_named": "Lyra",
         "step": 2, "mailbox": "hannah@meetspill.org", "occurred_at": old},
        {"event_id": "em-recent", "contact_id": "k2", "type": "replied", "reply_class": "positive",
         "reply_text": "Yes, let's talk.", "occurred_at": recent},
        {"event_id": "b-old", "contact_id": "k3", "type": "bounced", "reply_text": "550 5.1.1", "occurred_at": old},
        {"event_id": "s-old", "contact_id": "k1", "type": "sent", "step": 1, "occurred_at": old},
    ])
    w.ctx.store.insert("hitl_items", [
        reply_card("old", old), reply_card("ooo", old, kind="out_of_office", reply_class="out_of_office"),
        reply_card("first-name", old, kind="reply_approval", sent_text="Thanks Jane"), reply_card("recent", recent),
        {"item_id": "card", "kind": "send_approval", "contact_id": "k1", "created_at": NOW - timedelta(days=200),
         "payload": {"lead": {"email": "jane@acme.com"}, "outcome": "approved"}},
    ])
    return w


def test_reply_text_is_purged_after_90_days_and_the_rest_stays():
    w = reply_world()
    out = w.run()["reply_text"]
    assert out == {"events_due": 2, "events_cleared": 2, "items_due": 3, "items_cleared": 3}
    ev = {e["event_id"]: e for e in w.ctx.store.select("events")}
    assert ev["em-old"]["reply_text"] is None and ev["b-old"]["reply_text"] is None
    assert ev["em-recent"]["reply_text"] == "Yes, let's talk."
    old = ev["em-old"]
    assert (old["type"], old["reply_class"], old["language_terms"], old["competitor_named"], old["step"],
            old["mailbox"], old["occurred_at"]) == ("replied", "objection", ["EAP"], "Lyra", 2,
                                                    "hannah@meetspill.org", NOW - timedelta(days=91))
    items = {i["item_id"]: i for i in w.ctx.store.select("hitl_items")}
    for item_id in ("old", "ooo", "first-name"):
        p = items[item_id]["payload"]
        assert all(p.get(k) is None for k in retention.REPLY_TEXT_KEYS), item_id
        assert p[retention.PURGED] == NOW.isoformat()
        assert p["reply_class"] and p["objection"] == "already have an EAP" and p["language_terms"] == ["EAP"]
        assert p["instantly_email_id"] == f"em-{item_id}" and p["received_at"] and p["desk"] == {"edited_by": "U_HARRY"}
        assert "Lyra, thanks" not in repr(p) and "sam@acme.com" not in repr(p)
    assert items["recent"]["payload"]["reply_excerpt"] == "We already use Lyra, thanks."
    assert items["card"]["payload"]["lead"] == {"email": "jane@acme.com"}  # a send approval quotes no reply
    again = w.run()["reply_text"]
    assert again == {"events_due": 0, "events_cleared": 0, "items_due": 0, "items_cleared": 0}


def test_a_purged_card_still_reads_on_the_reply_desk():
    from us_outbound.replies.items import ReplyItem

    w = reply_world()
    w.run()
    item = ReplyItem(w.ctx.store.get("hitl_items", item_id="old"))
    assert (item.excerpt, item.draft, item.referral, item.reply_class) == ("", "", {}, "objection")
    assert item.email_id == "em-old" and item.received_at == NOW - timedelta(days=91)


def test_reply_text_dry_run_counts_and_changes_nothing():
    w = reply_world(live=False)
    before = {t: [dict(r) for r in w.ctx.store.select(t)] for t in ("events", "hitl_items")}
    assert w.run()["reply_text"] == {"events_due": 2, "events_cleared": 0, "items_due": 3, "items_cleared": 0}
    assert {t: w.ctx.store.select(t) for t in before} == before


# -- 3. Contacts who never replied, 12 months after their last step (SPEC 6) ---------------------------------------------

LATER = datetime(2027, 11, 20, 17, tzinfo=UTC)  # 12 months back is 20 Nov 2026


def contact_world(live: bool = True) -> tuple[World, dict]:
    """Contacts enrolled in Oct and Nov 2026, a year on: who goes, who stays."""
    w = World(live=live, now=LATER)
    c = {
        "silent": w.contact(date(2026, 10, 6)),  # last step 27 Oct 2026
        "recent": w.contact(date(2026, 11, 3)),  # last step 30 Nov 2026 (Thanksgiving week): not a year yet
        "positive": w.contact(date(2026, 10, 6), sends=1),
        "away": w.contact(date(2026, 10, 6)),
        "stop": w.contact(date(2026, 10, 6), sends=1),
        "unread": w.contact(date(2026, 10, 6), sends=1),
        "booked": w.contact(date(2026, 10, 6), sends=1),
        "bounced": w.contact(date(2026, 10, 6), sends=1),
    }
    w.event(c["positive"], "replied", date(2026, 10, 8), reply_class="positive")
    w.event(c["away"], "replied", date(2026, 10, 8), reply_class="out_of_office")
    w.event(c["stop"], "replied", date(2026, 10, 8), reply_class="unsubscribe", event_id="em-stop")
    w.event(c["stop"], "unsubscribed", date(2026, 10, 8), event_id=optout.reply_marker("em-stop"))
    w.event(c["unread"], "replied", date(2026, 10, 8), reply_class=None)  # not classified: counts as a reply
    w.ctx.store.insert("events", [{"event_id": "deal-1", "type": "deal_created", "account_id": c["booked"]["account_id"],
                                   "occurred_at": datetime(2026, 10, 20, tzinfo=UTC)}])
    w.event(c["bounced"], "bounced", date(2026, 10, 6), event_id=f"bounced:{c['bounced']['instantly_lead_id']}")
    st = w.ctx.store
    # Never emailed: revealed by pick_contacts, no last step.
    st.insert("contacts", [{"contact_id": "k-revealed", "account_id": "a1", "email": "rev@co1.com",
                            "created_at": datetime(2026, 9, 1, tzinfo=UTC)}])
    st.insert("hitl_items", [
        {"item_id": "card-silent", "kind": "send_approval", "status": "handled", "contact_id": "k1", "account_id": "a1",
         "slack_ts": "1.1", "payload": {"lead": {"email": "p1@co1.com", "first_name": "Pat"}, "outcome": "approved"},
         "created_at": datetime(2026, 10, 5, tzinfo=UTC)},
        {"item_id": "card-positive", "kind": "send_approval", "status": "handled", "contact_id": "k3",
         "payload": {"lead": {"email": "p3@co3.com"}}, "created_at": datetime(2026, 10, 5, tzinfo=UTC)},
    ])
    st.insert("raw_clay_contacts", [{"key": "co1.com|Pat Doe", "payload": {"email": "P1@co1.com"}},
                                    {"key": "co3.com|Lee Roe", "payload": {"email": "p3@co3.com"}}])
    from us_outbound import suppression
    suppression.add(st, email="p8@co8.com", reason="bounce", source="instantly", now=LATER)
    return w, c


def test_contacts_who_never_replied_go_12_months_after_their_last_step():
    w, c = contact_world()
    out = w.run()
    # Rule 1 deletes the leads first, so the contacts go in the same run.
    assert out["leads"]["deleted"] == 8 and out["leads"]["held"] == {}
    gone = {"silent", "away", "stop", "bounced"}
    assert out["contacts"] == {"due": 4, "deleted": 4, "held": {}, "left_for_next_run": 0, "hitl_items_cleared": 1,
                               "raw_clay_contacts_deleted": 1}
    kept = {r["contact_id"] for r in w.ctx.store.select("contacts")}
    assert kept == {c[k]["contact_id"] for k in set(c) - gone} | {"k-revealed"}
    items = {i["item_id"]: i for i in w.ctx.store.select("hitl_items")}
    assert items["card-silent"]["payload"] is None and items["card-silent"]["slack_ts"] == "1.1"  # the row stays
    assert items["card-positive"]["payload"] == {"lead": {"email": "p3@co3.com"}}
    assert [r["key"] for r in w.ctx.store.select("raw_clay_contacts")] == ["co3.com|Lee Roe"]
    # Their events stay (ids, steps, dates), and so does the bounce's suppression hash (SPEC 6).
    assert {e["event_id"] for e in w.ctx.store.select("events", {"contact_id": c["silent"]["contact_id"]})} == {
        "s1-1", "s1-2", "s1-3", "s1-4"}
    assert len(w.ctx.store.select("suppression")) == 1
    assert w.run()["contacts"]["due"] == 0


def test_a_contact_waits_for_its_lead_and_dry_run_deletes_nothing():
    w, c = contact_world(live=False)
    out = w.run()
    assert out["contacts"]["due"] == 0 and out["contacts"]["held"] == {retention.LEAD_LEFT: 4}
    assert len(w.ctx.store.select("contacts")) == 9 and w.deleted() == []
    for k in ("silent", "away", "stop", "bounced"):  # as rule 1 leaves them, live
        w.ctx.store.update("contacts", {"contact_id": c[k]["contact_id"]}, {"instantly_lead_id": None})
    out = w.run()["contacts"]
    assert out == {"due": 4, "deleted": 0, "held": {}, "left_for_next_run": 0}
    assert len(w.ctx.store.select("contacts")) == 9 and w.ctx.store.get("hitl_items", item_id="card-silent")["payload"]


def test_a_contact_with_an_opt_out_still_to_record_waits():
    w = World(now=LATER)
    k = w.contact(date(2026, 10, 6), instantly_lead_id=None)
    w.event(k, "replied", date(2026, 10, 8), reply_class="unsubscribe", event_id="em-1")
    assert w.run()["contacts"]["held"] == {retention.OPT_OUT_PENDING: 1}


def test_a_deleted_contact_s_company_keeps_counting_in_the_readout_but_leaves_its_cohort():
    """docs: the readout views count the company from its events; the cohort report reads contacts."""
    from us_outbound.learn import cohorts

    w = World(now=LATER)
    k = w.contact(date(2026, 10, 6))
    assert [co.account_id for co in cohorts.companies(w.ctx)] == [k["account_id"]]
    w.run()
    assert w.ctx.store.select("contacts") == [] and cohorts.companies(w.ctx) == []
    assert {e["type"] for e in w.ctx.store.select("events", {"account_id": k["account_id"]})} == {"sent"}


# -- 4. Universe rows not refreshed in 12 months (SPEC 6) ----------------------------------------------------------------

UNIVERSE_NOW = datetime(2027, 11, 20, 12, tzinfo=UTC)
OLD, FRESH = datetime(2026, 10, 1, tzinfo=UTC), datetime(2027, 9, 1, tzinfo=UTC)
# Account id -> why it stays; "" for the one that goes.
UNIVERSE = {
    "a-stale": "", "a-stale-too": "", "a-refreshed": "Apollo saw it again in September", "a-new": "first seen recently",
    "a-contact": "a contact", "a-event": "an event", "a-item": "a hitl item", "a-enrolled": "enrolled",
    "a-disqualified": "disqualified", "a-named-source": "from the Named accounts tab",
    "a-named": "named by Harry", "a-declined": "dropped with a 🚫", "a-undated": "no first_seen",
}


def universe_rows(store, first_seen=OLD) -> None:
    """The UNIVERSE accounts, each first seen at OLD with an Apollo fact from then and a fresh derived fact."""
    rows, facts = [], []
    for i, aid in enumerate(UNIVERSE):
        rows.append({"account_id": aid, "domain": f"{aid}.com", "status": "verified", "source": "apollo",
                     "first_seen": None if aid == "a-undated" else (FRESH if aid == "a-new" else first_seen),
                     "last_scored": UNIVERSE_NOW})
        facts += [{"event_id": f"f{i}-org", "account_id": aid, "source": "apollo_org", "fact": "employees", "value": 40,
                   "observed_at": OLD},
                  {"event_id": f"f{i}-match", "account_id": aid, "source": "scoring", "fact": "signal_matched",
                   "value": {"signal": "x"}, "observed_at": UNIVERSE_NOW},
                  {"event_id": f"f{i}-fit", "account_id": aid, "source": "lookalike", "fact": "lookalike_fit",
                   "value": 50, "observed_at": FRESH},
                  {"event_id": f"f{i}-doubt", "account_id": aid, "source": "verify_accounts", "fact": "doubt",
                   "value": "size", "observed_at": FRESH}]
    rows = [{**r, **{"a-enrolled": {"status": "enrolled"}, "a-disqualified": {"status": "disqualified"},
                     "a-named-source": {"source": "named"}}.get(r["account_id"], {})} for r in rows]
    facts += [{"event_id": "f-fresh", "account_id": "a-refreshed", "source": "apollo_org", "fact": "employees",
               "value": 41, "observed_at": FRESH},
              {"event_id": "f-named", "account_id": "a-named", "source": "named", "fact": "named", "value": False,
               "observed_at": OLD},
              {"event_id": "f-declined", "account_id": "a-declined", "source": "send_approval",
               "fact": "declined_in_slack", "value": True, "observed_at": OLD}]
    store.insert("accounts", rows)
    store.insert("signal_events", facts)
    store.insert("contacts", [{"contact_id": "k-a", "account_id": "a-contact"}])
    store.insert("events", [{"event_id": "e-a", "account_id": "a-event", "type": "site_visit", "occurred_at": OLD}])
    store.insert("hitl_items", [{"item_id": "i-a", "kind": "hand_check", "account_id": "a-item", "created_at": OLD}])


def test_universe_rows_not_refreshed_in_12_months_go_with_their_facts():
    w = World(now=UNIVERSE_NOW)
    universe_rows(w.ctx.store)
    assert retention.stale_accounts(w.ctx, retention.months_before(UNIVERSE_NOW, 12)) == ["a-stale", "a-stale-too"]
    out = w.run()["accounts"]
    assert out == {"due": 2, "deleted": 2, "left_for_next_run": 0}
    kept = {a["account_id"] for a in w.ctx.store.select("accounts")}
    assert kept == {aid for aid, why in UNIVERSE.items() if why}
    assert {e["account_id"] for e in w.ctx.store.select("signal_events")} == kept


def test_universe_rows_dry_run_and_the_cap(monkeypatch):
    w = World(live=False, now=UNIVERSE_NOW)
    universe_rows(w.ctx.store)
    assert w.run()["accounts"] == {"due": 2, "deleted": 0, "left_for_next_run": 0}
    assert len(w.ctx.store.select("accounts")) == len(UNIVERSE)
    monkeypatch.setattr(retention, "ACCOUNTS_PER_RUN", 1)
    w.ctx.guard.configure(live=True)
    assert w.run()["accounts"] == {"due": 2, "deleted": 1, "left_for_next_run": 1}
    assert w.ctx.store.get("accounts", account_id="a-stale") is None


# -- what it did: the daily post and status ------------------------------------------------------------------------------


def beat(ctx, run_id: str, started: datetime, dry_run: bool, **detail) -> None:
    sections = {"leads": {"due": 0, "deleted": 0, "already_gone": 0, "held": {}, "left_for_next_run": 0},
                "reply_text": {"events_due": 0, "events_cleared": 0, "items_due": 0, "items_cleared": 0},
                "contacts": {"due": 0, "deleted": 0, "held": {}, "left_for_next_run": 0},
                "accounts": {"due": 0, "deleted": 0, "left_for_next_run": 0}}
    for k, v in detail.items():
        sections[k] = {**sections[k], **v}
    ctx.store.insert("heartbeats", [{"run_id": run_id, "job": "retention", "status": "ok", "dry_run": dry_run,
                                     "started_at": started, "finished_at": started,
                                     "detail": {"job": "retention", "dry_run": dry_run, **sections, "errors": []}}])


def test_the_daily_post_line_has_today_s_counts_only():
    w = World(now=datetime(2026, 12, 15, 9, tzinfo=UTC))  # 09:00 UK
    assert retention.post_line(w.ctx) == ("", {})
    beat(w.ctx, "yesterday", datetime(2026, 12, 14, 0, 40, tzinfo=UTC), False, leads={"deleted": 9})
    beat(w.ctx, "nothing", datetime(2026, 12, 15, 0, 40, tzinfo=UTC), False)
    assert retention.post_line(w.ctx) == ("", {})
    beat(w.ctx, "by-hand", datetime(2026, 12, 15, 7, 0, tzinfo=UTC), False, leads={"deleted": 12, "already_gone": 3},
         reply_text={"events_cleared": 3, "items_cleared": 1}, contacts={"deleted": 1})
    line, counts = retention.post_line(w.ctx)
    assert line == ("Retention deleted 12 Instantly leads (31 days after their last step), the reply text of 3 replies "
                    "and 1 reply card (90 days) and 1 contact who never replied (12 months).")
    assert counts == {"leads": 12, "replies": 3, "reply_cards": 1, "contacts": 1, "companies": 0, "dry_run": False}


def test_a_dry_run_says_what_it_would_delete():
    w = World(live=False, now=datetime(2026, 12, 15, 9, tzinfo=UTC))
    beat(w.ctx, "r", datetime(2026, 12, 15, 0, 40, tzinfo=UTC), True, leads={"due": 4}, accounts={"due": 2})
    line, counts = retention.post_line(w.ctx)
    assert line == ("Retention would delete 4 Instantly leads (31 days after their last step) and 2 companies no source "
                    "has refreshed (12 months) (dry-run: nothing changed).")
    assert counts["dry_run"] is True


def test_the_status_line():
    w = World()
    assert retention.status_line(w.ctx.store) == "Retention (00:40 UK daily): not run yet"
    beat(w.ctx, "r1", datetime(2026, 12, 15, 0, 40, tzinfo=UTC), False)
    assert retention.status_line(w.ctx.store) == "Retention: last run Tue 15 Dec 00:40 UK (live): nothing due"
    beat(w.ctx, "r2", datetime(2026, 12, 16, 0, 40, tzinfo=UTC), False,
         leads={"deleted": 200, "held": {retention.OPT_OUT_PENDING: 1}, "left_for_next_run": 40},
         contacts={"held": {retention.LEAD_LEFT: 2}})
    assert retention.status_line(w.ctx.store) == (
        "Retention: last run Wed 16 Dec 00:40 UK (live): deleted 200 Instantly leads (31 days after their last step); "
        "held: its Instantly lead is not deleted yet 2, an opt-out not recorded yet 1; 40 left for the next run")


def test_the_summary_never_names_a_person():
    w, c = contact_world()
    w.ctx.store.insert("events", [{"event_id": "em-x", "contact_id": c["silent"]["contact_id"], "type": "replied",
                                   "reply_class": "out_of_office", "reply_text": "Away until Monday, p1@co1.com",
                                   "occurred_at": LATER - timedelta(days=100)}])
    out = w.run()
    assert "@" not in repr(out) and "Away until" not in repr(out)
