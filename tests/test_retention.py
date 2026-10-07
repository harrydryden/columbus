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
