"""Send approvals (enrol/approvals.py; Harry, 2 Oct 2026): with auto_send = no every email waits for an
approver's ✅ in Slack before its lead is added to Instantly.

The enrol world is tests/test_enrol.py's (accounts, contacts, Instantly's /leads/add and HubSpot's searches on
a FakeTransport); Slack answers on the same transport, with one thread per message and reactions per message,
so every call goes through the real clients and the guard.
"""

from __future__ import annotations

import dataclasses
import re
from datetime import UTC, date, datetime, timedelta

import pytest

from tests.fakes import make_context
from tests.test_enrol import NOW, instantly_posts, make
from tests.test_render import account, contact, make_settings
from us_outbound import suppression
from us_outbound.context import Secrets
from us_outbound.contacts import pick
from us_outbound.enrol import approvals, enrol, openers
from us_outbound.ops import bootstrap, cli
from us_outbound.replies import desk
from us_outbound.scoring import score, tiers
from us_outbound.settings.model import State

HARRY_ID, BOT, SOMEONE = "U_HARRY", "U_BOT", "U_SOMEONE"
HARRY_CAMPAIGN = "US Outbound – Harry Dryden"
CONTRACT_KEYS = {
    "state", "outcome", "owner", "mailbox", "campaign", "lead", "copy_version", "angle", "test_id", "opener_arm",
    "opener_source", "industry", "industry_group", "role", "tier", "score", "send_day", "edited", "original", "reason",
}


@pytest.fixture(autouse=True)
def default_openers(monkeypatch):
    """Each angle's default opener (enrol/openers.py is tested in test_openers.py), as in test_enrol.py."""

    def opener(ctx, a, c, check=None):
        angle = ctx.settings.angle(str(a.get("angle") or ""))
        text = angle.default_opener if angle else ""
        return openers.Opener(text, openers.OPENER if text else openers.NONE, "EAP named / opener_people" if text else ""), ""

    monkeypatch.setattr(enrol, "account_opener", opener)


class FakeSlack:
    """A Slack workspace behind the transport: threads under each message, reactions on each, posts recorded."""

    def __init__(self, t):
        self.threads: dict[str, list[dict]] = {}
        self.reactions: dict[str, list[dict]] = {}
        self.posts: list[dict] = []
        self.updates: list[dict] = []
        self.reacted: list[dict] = []
        self.fail_posts = False
        self._n = 0
        t.route("GET", "conversations.list", {"ok": True, "channels": [{"id": "C_ALERT", "name": "us-outbound"},
                                                                      {"id": "C_DEV", "name": "us-outbound-dev"}]})
        t.route("GET", "conversations.replies", fn=lambda r: {
            "ok": True, "messages": [{"ts": r.params["ts"], "text": "card"}] + self.threads.get(r.params["ts"], [])})
        t.route("GET", "reactions.get", fn=lambda r: {
            "ok": True, "message": {"reactions": self.reactions.get(r.params["timestamp"], [])}})
        t.route("GET", "auth.test", {"ok": True, "user_id": BOT})
        t.route("POST", "chat.postMessage", fn=self._post)
        t.route("POST", "chat.update", fn=self._update)
        t.route("POST", "reactions.add", fn=self._react)

    def _ts(self) -> str:
        self._n += 1
        return f"{2000 + self._n}.000100"

    def _post(self, req) -> dict:
        if self.fail_posts:
            return {"ok": False, "error": "service_unavailable"}
        ts = self._ts()
        self.posts.append({**req.json, "ts": ts})
        if req.json.get("thread_ts"):
            self.threads.setdefault(req.json["thread_ts"], []).append(
                {"ts": ts, "bot_id": "B1", "user": BOT, "text": req.json["text"]})
        return {"ok": True, "channel": req.json["channel"], "ts": ts}

    def _update(self, req) -> dict:
        self.updates.append(dict(req.json))
        return {"ok": True, "channel": req.json["channel"], "ts": req.json["ts"]}

    def _react(self, req) -> dict:
        self.reacted.append(dict(req.json))
        self.react(req.json["name"], BOT, ts=req.json["timestamp"])
        return {"ok": True}

    def say(self, user: str, text: str, parent: str) -> None:
        self.threads.setdefault(parent, []).append({"ts": self._ts(), "user": user, "text": text})

    def react(self, name: str, *users: str, ts: str) -> None:
        for r in self.reactions.setdefault(ts, []):
            if r["name"] == name:
                r["users"] += [u for u in users if u not in r["users"]]
                return
        self.reactions[ts].append({"name": name, "users": list(users), "count": len(users)})

    def cards(self, channel: str = "C_ALERT") -> list[dict]:
        return [p for p in self.posts if not p.get("thread_ts") and p["channel"] == channel]

    def thread(self, parent: str) -> list[dict]:
        return [p for p in self.posts if p.get("thread_ts") == parent]

    def texts(self) -> list[str]:
        return [p["text"] for p in self.posts]


def settings_for(**general):
    return make_settings(live_sending=general.pop("live_sending", True), approver_slack_ids=(HARRY_ID,), **general)


def world(*, live=True, accounts=None, contacts=None, now=NOW, **general):
    """Today's enrol world with auto_send = no (the default) and no hand-check: it is not a gate any more."""
    s = settings_for(live_sending=live, **general)
    ctx, t = make(live=live, settings=s, accounts=accounts, contacts=contacts, now=now, hand_check=None,
                  auto_send=False)
    return ctx, t, FakeSlack(t)


def items(ctx, status=None) -> list[dict]:
    rows = [r for r in ctx.store.select("hitl_items", {"kind": approvals.KIND})]
    return [r for r in rows if status is None or r["status"] == status]


def item_for(ctx, account_id: str) -> dict:
    return next(r for r in items(ctx) if r["account_id"] == account_id)


def at(ctx, when: datetime, *, live: bool | None = None, job: str = "poll_approvals") -> None:
    ctx.now = when
    ctx.job = job
    ctx.guard.configure(job=job, **({} if live is None else {"live": live}))


def poll(ctx, minutes: int = 5) -> dict:
    at(ctx, ctx.now + timedelta(minutes=minutes))
    return desk.poll_approvals(ctx)["send_approvals"]


def blocks_text(post: dict) -> str:
    out = []
    for b in post.get("blocks") or []:
        if b.get("text"):
            out.append(b["text"]["text"])
        for e in b.get("elements") or []:
            out.append(e.get("text", ""))
    return "\n".join(out)


def proposed(**kw):
    """A live enrol run with auto_send = no: three cards, one per account."""
    ctx, t, sl = world(**kw)
    out = enrol.run(ctx)
    return ctx, t, sl, out


# -- the switch ---------------------------------------------------------------------------------------------


def test_auto_send_yes_keeps_todays_path():
    ctx, t = make(live=True, settings=settings_for(), auto_send=True)
    sl = FakeSlack(t)
    out = enrol.run(ctx)
    assert out["auto_send"] is True and out["enrolled"] == 3 and "send_approvals" not in out
    assert instantly_posts(t) and items(ctx) == [] and sl.posts == []
    ctx, t = make(live=True, settings=settings_for(), auto_send=True, hand_check=None)
    assert "hand-check" in enrol.run(ctx)["reason"]  # the weekly hand-check gate as before


def test_the_general_key_defaults_to_no_with_harrys_note():
    from us_outbound.settings.defaults import default_tabs
    from us_outbound.settings.model import General
    from us_outbound.settings.validate import validate_all

    row = next(r for r in default_tabs()["General"] if r["key"] == "auto_send")
    assert row["value"] == "no" and General().auto_send is False
    assert row["note"].startswith("no (the pilot): every email waits for an approver's ✅ on its card in #us-outbound")
    assert "yes: emails go straight to Instantly once the weekly hand-check is approved" in row["note"]
    tabs = default_tabs()
    next(r for r in tabs["General"] if r["key"] == "auto_send")["value"] = "yes"
    assert validate_all(tabs)[0].general.auto_send is True


# -- enrol with auto_send = no --------------------------------------------------------------------------------


def test_live_enrol_writes_items_and_posts_cards_instead_of_adding_leads():
    ctx, t, sl, out = proposed()
    assert out["status"] == "ok" and out["auto_send"] is False and out["enrolled"] == 0
    assert out["send_approvals"]["posted"] == 3 and out["by_owner"] == {"Harry Dryden": 1, "Hannah Spalding": 1,
                                                                        "Sam Jackson": 1}
    assert instantly_posts(t) == [] and ctx.guard.writes("instantly") == []  # nothing reaches Instantly yet
    assert {a["status"] for a in ctx.store.select("accounts")} == {"verified"}
    assert not any(c.get("instantly_lead_id") or c.get("enrolled_at") for c in ctx.store.select("contacts"))
    rows = items(ctx)
    assert len(rows) == 3 and {r["status"] for r in rows} == {"open"}
    jane = item_for(ctx, "acc-1")
    assert jane["contact_id"] == "con-1" and jane["slack_channel"] == "#us-outbound" and jane["created_at"] == NOW
    p = jane["payload"]
    assert CONTRACT_KEYS <= set(p)
    assert (p["state"], p["outcome"], p["reason"], p["edited"], p["original"]) == ("waiting", "", "", False, {})
    assert (p["owner"], p["campaign"], p["mailbox"]) == ("Harry Dryden", HARRY_CAMPAIGN, "")  # two Active mailboxes
    assert (p["send_day"], p["expires_on"]) == ("2026-10-27", "2026-10-28")
    assert (p["copy_version"], p["angle"], p["test_id"], p["opener_arm"]) == ("agencies-v1", "Upgrade the EAP", "", "opener")
    assert (p["industry"], p["industry_group"], p["role"], p["tier"], p["score"]) == (
        "Advertising agencies", "Marketing & Creative Agencies", "People leader", "Priority", 60)
    assert p["lead"]["email"] == "jane@acmecreative.com"
    assert set(p["lead"]["custom_variables"]) == {f"s{i}_{k}" for i in range(1, 5) for k in ("subject", "body")}
    assert item_for(ctx, "acc-2")["payload"]["mailbox"] == "hannah@meetspill.org"
    # Each card in #us-outbound, ✅ and ❌ seeded on it by the bot, and emails 2 to 4 in its thread.
    cards = sl.cards()
    assert len(cards) == 3 and {r["slack_ts"] for r in rows} == {c["ts"] for c in cards}
    for c in cards:
        seeded = sorted(r["name"] for r in sl.reacted if r["timestamp"] == c["ts"])
        assert seeded == ["white_check_mark", "x"]
        [follow] = sl.thread(c["ts"])
        assert "Email 2 · day 7" in blocks_text(follow) and "Email 4 · day 21" in blocks_text(follow)
    assert p["approve_ts"] == jane["slack_ts"]


def test_the_hand_check_is_not_a_gate_but_its_pulls_still_count():
    ctx, t, sl = world()
    ctx.store.insert("hitl_items", [{"item_id": "hc-1", "kind": "hand_check", "status": "handled",
                                     "created_at": NOW - timedelta(hours=3), "payload": {"pulled_account_ids": ["acc-1"]}}])
    out = enrol.run(ctx)
    assert out["status"] == "ok" and out["skipped"]["pulled at this week's hand-check"] == 1
    assert {r["account_id"] for r in items(ctx)} == {"acc-2", "acc-3"}


def test_dry_run_writes_no_item_and_previews_five_cards_in_the_dev_channel():
    accts = [account(account_id=f"acc-{i}", domain=f"a{i}.com", clean_name=f"Agency {i}") for i in range(7)]
    cons = [contact(contact_id=f"con-{i}", account_id=f"acc-{i}", email=f"jane@a{i}.com") for i in range(7)]
    ctx, t, sl = world(live=False, accounts=accts, contacts=cons)
    out = enrol.run(ctx)
    sa = out["send_approvals"]
    assert out["dry_run"] is True and (sa["would_post"], sa["previews"], sa["posted"]) == (7, 5, 0)
    assert sum(out["by_owner"].values()) == 7 and items(ctx) == [] and instantly_posts(t) == []
    assert sl.cards() == [] and len(sl.cards("C_DEV")) == 5
    assert all(c["text"].startswith("[dry-run → #us-outbound] Send approval: Agency") for c in sl.cards("C_DEV"))
    assert all("approvals approve" not in blocks_text(c) for c in sl.cards("C_DEV"))  # a preview has no item
    assert all(r["channel"] == "C_DEV" for r in sl.reacted) and len(sl.reacted) == 10
    assert all(len(sl.thread(c["ts"])) == 1 for c in sl.cards("C_DEV"))  # emails 2 to 4, in the preview's thread
    assert {w.target for w in ctx.guard.writes("slack", sent=True)} == {"#us-outbound-dev"}


def test_a_live_run_without_the_slack_token_refuses():
    ctx, t, sl = world()
    ctx.clients.secrets = Secrets(ctx.guard, fetch=lambda name: "" if "SLACK" in name else f"test-{name}")
    out = enrol.run(ctx)
    assert out["status"] == "skipped" and "US_OUTBOUND_SLACK_BOT_TOKEN is not set" in out["reason"]
    assert items(ctx) == [] and instantly_posts(t) == []


def test_the_card_shows_what_harry_asked_for():
    ctx, t, sl = world()
    ctx.store.insert("signal_events", [{
        "event_id": "pick-1", "account_id": "acc-1", "source": pick.SOURCE, "fact": pick.OUTCOME_FACT,
        "value": {"outcome": "picked", "contact_id": "con-1", "apollo_person_id": "p-123", "email_source": "clay"},
        "quote": "", "source_url": "", "observed_at": NOW - timedelta(hours=6)}])
    enrol.run(ctx)
    card = next(c for c in sl.cards() if c["ts"] == item_for(ctx, "acc-1")["slack_ts"])
    text = blocks_text(card)
    assert "Send approval · *Acme Creative* · <https://acmecreative.com|acmecreative.com>" in text  # name and domain
    assert ("Priority · score 60 · Upgrade the EAP · opener from the signal EAP named · Advertising agencies "
            "(Marketing &amp; Creative Agencies)") in text
    assert ("*To:* Jane Doe, Head of People (People leader) · jane@acmecreative.com · "
            "<https://app.apollo.io/#/people/p-123|Apollo> · email from Clay") in text
    assert "*From:* Harry Dryden · harry@meetspill.org or harry@tryspill.org (Instantly picks)" in text
    assert "*Subject:* Support for the Acme Creative team" in text
    assert "> Hi Jane," in text and "> I saw your team already has an employee assistance program." in text
    assert "> Spill (https://www.spill.chat/us), on-demand counseling for your team" in text  # the signature
    assert "> Where we got your details" in text  # email 1's data notice: the plain-text version, all of it
    assert "*Emails:* Email 1 of 4 · follow-ups on days 7, 14 and 21 (in the thread)" in text
    assert "*Before:* no email to Acme Creative from us before" in text
    assert "*Today:* Harry: 1 of 15 today" in text
    assert "✅ send · ❌ don't send" in text
    assert f"`us-outbound approvals approve {item_for(ctx, 'acc-1')['item_id'][:8]} --live`" in text
    assert all(len(b["text"]["text"]) <= 3000 for b in card["blocks"] if b.get("text"))
    assert card["text"].startswith("Send approval: Acme Creative · Jane Doe · from Harry Dryden:")
    # Without an Apollo id, the source's name and no link.
    hannah = next(c for c in sl.cards() if c["ts"] == item_for(ctx, "acc-2")["slack_ts"])
    assert "omar@brightfin.com · source not recorded" in blocks_text(hannah)


def test_a_card_after_a_contact_swap_says_the_earlier_contact_was_declined():
    ctx, t, sl, _ = proposed()
    card_ts = item_for(ctx, "acc-1")["slack_ts"]
    sl.say(HARRY_ID, "contact", card_ts)
    poll(ctx)
    ctx.store.insert("contacts", [contact(contact_id="con-9", first_name="Ann", last_name="Lee", title="COO",
                                          role="Operations", email="ann@acmecreative.com")])
    at(ctx, datetime(2026, 10, 28, 12, 0, tzinfo=UTC), job="enrol")
    enrol.run(ctx)
    new = next(r for r in items(ctx, "open") if r["contact_id"] == "con-9")
    text = blocks_text(next(c for c in sl.cards() if c["ts"] == new["slack_ts"]))
    assert "the earlier contact (Jane Doe, Head of People) was declined here, so this is the next one" in text


# -- ✅ ------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("how", ["tick", "word"])
def test_a_tick_adds_the_lead_and_records_the_enrolment_once(how):
    ctx, t, sl, _ = proposed()
    row = item_for(ctx, "acc-1")
    if how == "tick":
        sl.react("white_check_mark", HARRY_ID, ts=row["slack_ts"])
    else:
        sl.say(HARRY_ID, "<@U_BOT> Send", row["slack_ts"])
    out = poll(ctx)
    assert out["outcomes"] == {"approved": 1}
    [add] = instantly_posts(t)
    assert add.json["campaign_id"] == "c-harry" and [lead["email"] for lead in add.json["leads"]] == ["jane@acmecreative.com"]
    assert add.json["leads"][0]["custom_variables"] == row["payload"]["lead"]["custom_variables"]
    jane, acme = ctx.store.get("contacts", contact_id="con-1"), ctx.store.get("accounts", account_id="acc-1")
    assert (jane["instantly_lead_id"], jane["instantly_campaign"], jane["enrolment_month"]) == (
        "lead-jane@acmecreative.com", HARRY_CAMPAIGN, "2026-10")
    assert (jane["copy_version"], jane["angle"], jane["opener_arm"], jane["enrolled_at"]) == (
        "agencies-v1", "Upgrade the EAP", "opener", ctx.now)
    assert (acme["status"], acme["sender"]) == ("enrolled", "Harry Dryden")
    row = item_for(ctx, "acc-1")
    assert (row["status"], row["handled_by"], row["handled_at"]) == ("handled", HARRY_ID, ctx.now)
    assert (row["payload"]["state"], row["payload"]["outcome"]) == ("done", "approved")
    ev = ctx.store.get("events", event_id=f"send-approval:{row['item_id']}")
    assert {k: ev[k] for k in ("type", "approval", "approved_by", "account_id", "contact_id", "step", "occurred_at")} == {
        "type": "send_approval", "approval": "approved", "approved_by": HARRY_ID, "account_id": "acc-1",
        "contact_id": "con-1", "step": 1, "occurred_at": ctx.now}
    [update] = [u for u in sl.updates if u["ts"] == row["slack_ts"]]
    approved = f"✅ Approved by <@{HARRY_ID}> at 11:05 UK · added to {HARRY_CAMPAIGN}"
    assert update["text"].startswith(approved) and blocks_text(update).startswith(approved)
    assert blocks_text(update).endswith(approved)  # and in place of the footer
    assert any(x.startswith(f"Added to {HARRY_CAMPAIGN} at 11:05 UK") for x in sl.texts())
    # Never twice: the ✅ is still there, the next run adds nothing.
    poll(ctx)
    assert len(instantly_posts(t)) == 1 and len(ctx.store.select("events", {"type": "send_approval"})) == 1


def test_only_approvers_count_and_the_bots_own_reactions_are_ignored():
    ctx, t, sl, _ = proposed()
    row = item_for(ctx, "acc-1")
    out = poll(ctx)  # only the bot's seeded ✅ and ❌ are on the card
    assert out["outcomes"] == {} and out["ignored_non_approvers"] == 0 and instantly_posts(t) == []
    sl.react("white_check_mark", SOMEONE, ts=row["slack_ts"])
    sl.say(SOMEONE, "send", row["slack_ts"])
    out = poll(ctx)
    assert out["outcomes"] == {} and out["ignored_non_approvers"] == 2 and instantly_posts(t) == []
    assert item_for(ctx, "acc-1")["status"] == "open"
    sl.react("white_check_mark", HARRY_ID, ts=row["slack_ts"])
    assert poll(ctx)["outcomes"] == {"approved": 1}


def test_a_recheck_blocks_an_opt_out_after_the_card_was_posted():
    ctx, t, sl, _ = proposed()
    row = item_for(ctx, "acc-1")
    suppression.add(ctx.store, email="jane@acmecreative.com", domain="acmecreative.com", reason="unsubscribe",
                    source="test", now=NOW)
    sl.react("white_check_mark", HARRY_ID, ts=row["slack_ts"])
    out = poll(ctx)
    assert out["outcomes"] == {"blocked": 1} and instantly_posts(t) == []
    row = item_for(ctx, "acc-1")
    assert (row["status"], row["payload"]["outcome"]) == ("handled", "blocked")
    assert "Jane Doe: contact suppressed" in row["payload"]["reason"]
    assert ctx.store.get("events", event_id=f"send-approval:{row['item_id']}")["approval"] == "blocked"
    assert any("⛔ Not sent: Jane Doe: contact suppressed" in blocks_text(u) for u in sl.updates)


def test_a_recheck_blocks_while_enrolment_is_stopped():
    ctx, t, sl, _ = proposed()
    hb = {"dry_run": False, "status": "ok", "finished_at": NOW}
    ctx.store.insert("heartbeats", [{**hb, "run_id": "s-1", "job": "operator_stop", "started_at": NOW}])
    sl.react("white_check_mark", HARRY_ID, ts=item_for(ctx, "acc-1")["slack_ts"])
    poll(ctx)
    assert instantly_posts(t) == [] and "stopped by an operator" in item_for(ctx, "acc-1")["payload"]["reason"]


def test_a_failed_add_waits_for_a_fresh_approval():
    ctx, t, sl, _ = proposed()
    row = item_for(ctx, "acc-1")
    t.route("POST", "/leads/add", {"error": "busy"}, status=500)
    sl.react("white_check_mark", HARRY_ID, ts=row["slack_ts"])
    out = poll(ctx)
    assert out["outcomes"] == {} and out["not_added"][0]["why"][0].startswith("ApiError")
    row = item_for(ctx, "acc-1")
    assert (row["status"], row["payload"]["state"]) == ("open", "waiting") and "500" in row["payload"]["failed"]["error"]
    note = row["payload"]["approve_ts"]
    assert note != row["slack_ts"] and any(x.startswith("Not added: Instantly answered ApiError") for x in sl.texts())
    from tests.test_enrol import added

    t.route("POST", "/leads/add", fn=added)
    poll(ctx)  # the ✅ on the card no longer counts
    assert len(instantly_posts(t)) == 1  # the failed attempt only
    sl.react("white_check_mark", HARRY_ID, ts=note)
    assert poll(ctx)["outcomes"] == {"approved": 1} and len(instantly_posts(t)) == 2


def test_an_add_left_sending_goes_back_to_a_person():
    ctx, t, sl, _ = proposed()
    row = item_for(ctx, "acc-1")
    payload = {**row["payload"], "state": "sending", "sending": {"at": (NOW - timedelta(minutes=20)).isoformat()}}
    ctx.store.update("hitl_items", {"item_id": row["item_id"]}, {"status": "sending", "payload": payload})
    sl.react("white_check_mark", HARRY_ID, ts=row["slack_ts"])
    out = poll(ctx)
    assert out["unsure"] == [row["item_id"][:8]] and instantly_posts(t) == []
    row = item_for(ctx, "acc-1")
    assert (row["status"], row["payload"]["state"]) == ("open", "waiting")
    assert any("I can't tell whether jane@acmecreative.com was added" in x for x in sl.texts())
    poll(ctx)
    assert instantly_posts(t) == []  # the old ✅ does not add it


# -- ❌ and the three choices ----------------------------------------------------------------------------------


def _rejected(ctx, sl, account_id="acc-1"):
    row = item_for(ctx, account_id)
    sl.react("x", HARRY_ID, ts=row["slack_ts"])
    sl.react("white_check_mark", HARRY_ID, ts=row["slack_ts"])  # ❌ beats ✅
    out = poll(ctx)
    assert out["rejected"] == [row["item_id"][:8]]
    row = item_for(ctx, account_id)
    assert (row["status"], row["payload"]["state"]) == ("open", "rejected")
    return row


def test_a_cross_offers_three_choices_with_their_reactions_seeded():
    ctx, t, sl, _ = proposed()
    row = _rejected(ctx, sl)
    choices = row["payload"]["choices_ts"]
    [post] = [p for p in sl.posts if p["ts"] == choices]
    assert post["text"].startswith(f"❌ Not sent (by <@{HARRY_ID}>). What next?")
    assert "✏️ *edit*" in post["text"] and "👤 *contact*: not Jane Doe" in post["text"] and "🚫 *company*" in post["text"]
    assert "expires at the end of Wed 28 Oct (UK)" in post["text"]
    assert sorted(r["name"] for r in sl.reacted if r["timestamp"] == choices) == ["bust_in_silhouette", "no_entry_sign",
                                                                                 "pencil2"]
    assert instantly_posts(t) == []
    poll(ctx)  # nothing chosen yet: it waits
    assert item_for(ctx, "acc-1")["payload"]["state"] == "rejected"


SLACK_LINK = "(<https://www.spill.chat/us/industry/agencies>)"  # Slack wraps a typed address in <…>


def _editing(ctx, sl):
    row = _rejected(ctx, sl)
    sl.react("pencil2", HARRY_ID, ts=row["payload"]["choices_ts"])
    out = poll(ctx)
    assert out["editing"] == [row["item_id"][:8]]
    row = item_for(ctx, "acc-1")
    assert row["payload"]["state"] == "editing"
    [help_] = [p for p in sl.posts if p["ts"] == row["payload"]["edit_ts"]]
    assert "Reply in this thread with the new email 1" in help_["text"] and "`Email 2:`" in help_["text"]
    assert "no interactive endpoint" in help_["text"]
    assert "Subject: Support for the Acme Creative team\nHi Jane," in help_["text"]
    assert "[how Spill works for agencies](https://www.spill.chat/us/industry/agencies)" in help_["text"]
    return row


def test_edit_rerender_reapprove_and_send_the_edited_version():
    ctx, t, sl, _ = proposed()
    row = _editing(ctx, sl)
    source = row["payload"]["steps"][0]["source"]
    assert source.startswith("Hi Jane,\n\nI saw your team already has an employee assistance program.")
    new = source.replace("the strain stays hidden", "the strain often stays hidden").replace(
        "(https://www.spill.chat/us/industry/agencies)", SLACK_LINK)
    sl.say(HARRY_ID, f"Subject: Support for the people at Acme Creative\n{new}", row["slack_ts"])
    out = poll(ctx)
    assert out["edits"] == {"accepted": 1}
    row = item_for(ctx, "acc-1")
    p = row["payload"]
    assert (p["state"], p["edited"]) == ("waiting", True)
    assert p["original"]["s1_subject"] == "Support for the Acme Creative team"
    cv = p["lead"]["custom_variables"]
    assert cv["s1_subject"] == "Support for the people at Acme Creative"
    assert "the strain often stays hidden" in cv["s1_body"] and cv["s1_body"].startswith("<p>Hi Jane,</p>")
    assert 'Book a call <a href="https://meetings.hubspot.com/harry336/us-demo-link">here</a>' in cv["s1_body"]
    assert "Where we got your details" in cv["s1_body"]  # the signature and the data notice, added as before
    assert cv["s2_body"] == p["original"]["s2_body"]
    version = p["approve_ts"]
    [post] = [x for x in sl.posts if x["ts"] == version]
    assert "> In most agencies, the work runs on deadlines and client moods, and the strain often stays hidden" in \
        blocks_text(post)
    assert sorted(r["name"] for r in sl.reacted if r["timestamp"] == version) == ["white_check_mark", "x"]
    sl.react("white_check_mark", HARRY_ID, ts=row["slack_ts"])  # a ✅ on the card no longer counts
    poll(ctx)
    assert instantly_posts(t) == []
    sl.react("white_check_mark", HARRY_ID, ts=version)
    assert poll(ctx)["outcomes"] == {"approved_edited": 1}
    [add] = instantly_posts(t)
    assert add.json["leads"][0]["custom_variables"]["s1_subject"] == "Support for the people at Acme Creative"
    row = item_for(ctx, "acc-1")
    assert row["payload"]["outcome"] == "approved_edited"
    assert ctx.store.get("events", event_id=f"send-approval:{row['item_id']}")["approval"] == "approved_edited"


def test_an_edit_that_breaks_a_copy_rule_is_refused_and_waits():
    ctx, t, sl, _ = proposed()
    row = _editing(ctx, sl)
    sl.say(HARRY_ID, "Hi Jane,\n\nOur licensed therapists are free for your team.\n\nBest wishes,\nHarry", row["slack_ts"])
    out = poll(ctx)
    assert out["edits"] == {"refused": 1}
    row = item_for(ctx, "acc-1")
    assert row["payload"]["state"] == "editing" and row["payload"]["edited"] is False
    [refusal] = [x for x in sl.texts() if x.startswith("That version of email 1 breaks the copy rules")]
    assert "licensed" in refusal and "therapists" in refusal and "Reply with another version" in refusal
    assert "{{" not in refusal.split("so it is not used:")[1]  # the rules name Jane and Harry, not variables


def test_a_follow_up_can_be_edited_too():
    ctx, t, sl, _ = proposed()
    row = _editing(ctx, sl)
    sl.say(HARRY_ID, "Email 3:\nSubject: Will anyone at Acme Creative know who's using it?", row["slack_ts"])
    poll(ctx)
    p = item_for(ctx, "acc-1")["payload"]
    assert p["lead"]["custom_variables"]["s3_subject"] == "Will anyone at Acme Creative know who's using it?"
    assert p["lead"]["custom_variables"]["s3_body"] == p["original"]["s3_body"]
    assert any(x.startswith("New version of email 3") for x in sl.texts())


def test_a_cross_on_the_new_version_offers_the_choices_again():
    ctx, t, sl, _ = proposed()
    row = _editing(ctx, sl)
    sl.say(HARRY_ID, "edit: Subject: Support for Acme Creative's people", row["slack_ts"])
    poll(ctx)
    version = item_for(ctx, "acc-1")["payload"]["approve_ts"]
    sl.react("x", HARRY_ID, ts=version)
    poll(ctx)
    p = item_for(ctx, "acc-1")["payload"]
    assert p["state"] == "rejected" and p["choices_ts"] != row["payload"]["choices_ts"]


def test_contact_declines_the_person_and_the_account_can_be_picked_again():
    ctx, t, sl, _ = proposed()
    row = _rejected(ctx, sl)
    sl.react("bust_in_silhouette", HARRY_ID, ts=row["payload"]["choices_ts"])
    assert poll(ctx)["outcomes"] == {"contact_rejected": 1}
    jane = ctx.store.get("contacts", contact_id="con-1")
    assert jane["suppressed"] is True and jane["suppressed_reason"] == "declined in Slack by U_HARRY at a send approval (27 Oct 2026)"
    assert ctx.store.select("suppression") == []  # they did not opt out
    acme = ctx.store.get("accounts", account_id="acc-1")
    assert (acme["status"], acme["tier"]) == ("verified", "Priority")
    row = item_for(ctx, "acc-1")
    assert (row["status"], row["handled_by"], row["payload"]["outcome"]) == ("handled", HARRY_ID, "contact_rejected")
    assert ctx.store.get("events", event_id=f"send-approval:{row['item_id']}")["approval"] == "contact_rejected"
    assert any("pick_contacts looks for the next-ranked person at 05:30" in x for x in sl.texts())
    need, _ = pick.waiting(ctx)
    assert "acc-1" in [a["account_id"] for a in need]  # pick_contacts finds the next person at 05:30
    assert instantly_posts(t) == []


def test_company_drops_the_account_and_a_rescore_keeps_it_out():
    ctx, t, sl, _ = proposed()
    row = _rejected(ctx, sl)
    sl.say(HARRY_ID, "company", row["slack_ts"])
    assert poll(ctx)["outcomes"] == {"company_rejected": 1}
    acme = ctx.store.get("accounts", account_id="acc-1")
    assert acme["tier"] == "Excluded" and "dropped in Slack by U_HARRY" in acme["tier_reason"]
    [fact] = [e for e in ctx.store.select("signal_events", {"account_id": "acc-1"}) if e["fact"] == tiers.DECLINED_IN_SLACK]
    assert (fact["source"], fact["value"]) == ("send_approval", True)
    ctx.settings = dataclasses.replace(ctx.settings, states=(State("IL", True), State("NY", True)))
    score.rescore(ctx)
    acme = ctx.store.get("accounts", account_id="acc-1")
    assert (acme["tier"], acme["tier_reason"]) == ("Excluded", "Excluded: dropped by an approver at a send approval in Slack")
    assert ctx.store.get("accounts", account_id="acc-2")["tier"] != "Excluded"
    assert item_for(ctx, "acc-1")["payload"]["outcome"] == "company_rejected"


def test_the_least_drastic_choice_wins():
    ctx, t, sl, _ = proposed()
    row = _rejected(ctx, sl)
    sl.react("no_entry_sign", HARRY_ID, ts=row["payload"]["choices_ts"])
    sl.react("memo", HARRY_ID, ts=row["payload"]["choices_ts"])
    poll(ctx)
    assert item_for(ctx, "acc-1")["payload"]["state"] == "editing"
    assert ctx.store.get("accounts", account_id="acc-1")["tier"] == "Priority"


# -- expiry, capacity, dry-run and escalation ---------------------------------------------------------------------


def test_expires_at_the_end_of_the_next_send_day():
    s = make_settings()
    assert approvals.expires_on(date(2026, 10, 27), s) == date(2026, 10, 28)  # Tue -> Wed
    assert approvals.expires_on(date(2026, 10, 30), s) == date(2026, 11, 2)  # Fri -> Mon
    assert approvals.expires_on(date(2026, 11, 20), s) == date(2026, 11, 30)  # over the Thanksgiving blackout


def test_an_item_still_waiting_expires_and_the_account_goes_back_to_the_queue():
    ctx, t, sl, _ = proposed()
    at(ctx, datetime(2026, 10, 28, 23, 50, tzinfo=UTC))  # Wed 23:50 UK: still its last day
    desk.poll_approvals(ctx)
    assert len(items(ctx, "open")) == 3
    out = poll(ctx, minutes=15)  # Thu 00:05 UK
    assert out["outcomes"] == {"expired": 3}
    row = item_for(ctx, "acc-1")
    assert (row["status"], row["handled_by"], row["payload"]["outcome"]) == ("handled", "system", "expired")
    assert row["payload"]["reason"] == "not approved by the end of Wed 28 Oct (UK)"
    ev = ctx.store.get("events", event_id=f"send-approval:{row['item_id']}")
    assert (ev["approval"], ev["approved_by"]) == ("expired", "system")
    assert any(blocks_text(u).startswith("⌛ Expired: not approved by the end of Wed 28 Oct (UK)") for u in sl.updates)
    assert instantly_posts(t) == [] and ctx.store.get("accounts", account_id="acc-1")["status"] == "verified"
    at(ctx, datetime(2026, 10, 29, 12, 0, tzinfo=UTC), job="enrol")
    assert enrol.run(ctx)["send_approvals"]["posted"] == 3  # proposed again


def test_waiting_items_hold_their_accounts_and_their_senders_slots():
    accts = [account(account_id=f"acc-{i}", domain=f"a{i}.com", clean_name=f"Agency {i}", score=90 - i) for i in range(6)]
    cons = [contact(contact_id=f"con-{i}", account_id=f"acc-{i}", email=f"jane@a{i}.com") for i in range(6)]
    ctx, t, sl = world(accounts=accts, contacts=cons, weekly_enrol_cap=8)  # Tue: 8 ÷ 4 send days = 2 today
    first = enrol.run(ctx)
    assert first["send_approvals"]["posted"] == 2
    at(ctx, datetime(2026, 10, 28, 12, 0, tzinfo=UTC), job="enrol")  # Wed: nothing approved yet
    out = enrol.run(ctx)
    terms = out["number_terms"]
    assert terms["awaiting_approval"] == 2 and terms["weekly_target"] == 2  # (8 − 2 waiting) ÷ 3 send days
    assert out["skipped"]["waiting for approval in Slack"] == 2
    assert sum(v.get("pending", 0) for v in terms["senders"].values()) == 2
    assert any(line.startswith("Waiting for approval in Slack: 2 emails") for line in out["limits"])
    assert out["send_approvals"]["waiting_before"] == 2 and out["send_approvals"]["posted"] == 2
    assert len({r["account_id"] for r in items(ctx, "open")}) == 4  # no account proposed twice
    # Each card counts its sender's day from the slots the waiting ones already hold.
    newest = sorted(items(ctx, "open"), key=lambda r: r["created_at"])[-1]["payload"]
    assert newest["slot"] > 1 and newest["slots"] >= newest["slot"]


def test_when_waiting_cards_fill_every_sender_the_day_says_so():
    accts = [account(account_id=f"acc-{i:02d}", domain=f"a{i}.com", clean_name=f"Agency {i}", score=90) for i in range(40)]
    cons = [contact(contact_id=f"con-{i:02d}", account_id=f"acc-{i:02d}", email=f"jane@a{i}.com") for i in range(40)]
    ctx, t, sl = world(accounts=accts, contacts=cons)
    assert enrol.run(ctx)["send_approvals"]["posted"] == 31  # every sender's pace today: 8 + 15 + 8
    at(ctx, datetime(2026, 10, 28, 12, 0, tzinfo=UTC), job="enrol")
    out = enrol.run(ctx)
    assert out["number"] == 0 and out["number_terms"]["binding"] == "sending_capacity"
    assert "Hannah Spalding's slots today are held by 8 emails waiting for approval in Slack" in "\n".join(out["limits"])
    assert not any(line.startswith("Add a mailbox") for line in out["limits"])


def test_an_item_whose_payload_was_erased_is_closed():
    ctx, t, sl, _ = proposed()
    ctx.store.update("hitl_items", {"contact_id": "con-1"}, {"payload": None})  # what `erase --email` does
    out = poll(ctx)
    assert out["outcomes"] == {"expired": 1}
    row = item_for(ctx, "acc-1")
    assert (row["status"], row["handled_by"]) == ("handled", "system") and instantly_posts(t) == []


def test_a_dry_poll_works_out_decisions_and_changes_nothing():
    ctx, t, sl, _ = proposed()
    row = item_for(ctx, "acc-1")
    sl.react("white_check_mark", HARRY_ID, ts=row["slack_ts"])
    sl.say(HARRY_ID, "skip", item_for(ctx, "acc-2")["slack_ts"])
    before, posts = [dict(r) for r in items(ctx)], len(sl.posts)
    at(ctx, NOW + timedelta(minutes=5), live=False)
    out = desk.poll_approvals(ctx)["send_approvals"]
    assert sorted((w["action"], w["via"]) for w in out["would"]) == [("reject", "thread"), ("send", "✅")]
    assert items(ctx) == before and len(sl.posts) == posts and sl.updates == [] and instantly_posts(t) == []
    at(ctx, datetime(2026, 10, 29, 9, 0, tzinfo=UTC), live=False)
    assert {w["action"] for w in desk.poll_approvals(ctx)["send_approvals"]["would"]} == {"expire"}
    assert items(ctx) == before


def test_send_approvals_are_never_reposted_or_escalated():
    ctx, t, sl, _ = proposed()
    t.route("POST", "/crm/v3/objects/tasks", {"id": "task-1"})
    at(ctx, NOW + timedelta(hours=26))  # Wed 13:00 UK: past escalation_hours, before expiry
    desk.poll_approvals(ctx)
    assert all(r.get("escalated_at") is None and r.get("reposted_at") is None for r in items(ctx))
    assert not [r for r in t.requests if "/crm/v3/objects/tasks" in r.url]
    assert not [p for p in sl.posts if p.get("reply_broadcast")]


def test_a_card_that_could_not_be_posted_is_posted_by_the_next_poll():
    ctx, t, sl = world()
    sl.fail_posts = True
    out = enrol.run(ctx)
    assert out["send_approvals"]["posted"] == 0 and len(out["errors"]) == 3 and len(items(ctx, "open")) == 3
    assert all(not r["slack_ts"] for r in items(ctx))
    sl.fail_posts = False
    assert poll(ctx)["cards_posted"] == 3
    assert all(r["slack_ts"] and r["payload"]["followups_ts"] for r in items(ctx))


# -- the command line ---------------------------------------------------------------------------------------------


class Harness:
    """`us-outbound approvals …` over the proposed world's store and transport, with no Slack token."""

    def __init__(self, *, live_sending=True):
        self.ctx, self.t, self.sl, _ = proposed()
        g = dataclasses.replace(self.ctx.settings.general, live_sending=live_sending)
        self.settings = dataclasses.replace(self.ctx.settings, general=g)
        self.calls = []

    def __call__(self, job, live_flag, operator=False):
        ctx = make_context(self.settings, job=job, transport=self.t, store=self.ctx.store, now=NOW + timedelta(hours=1))
        ctx.guard.configure(live=bootstrap.resolve_live(live_flag, self.settings, operator=operator))
        ctx.live_flag = live_flag
        ctx.clients.secrets = Secrets(ctx.guard, fetch=lambda name: "" if "SLACK" in name else f"test-{name}")
        self.calls.append((job, ctx.live))
        return ctx

    def run(self, *argv):
        return cli.main(list(argv), context_factory=self)

    def short(self, account_id):
        return item_for(self.ctx, account_id)["item_id"][:8]


def test_cli_list_shows_each_waiting_email(capsys):
    h = Harness()
    assert h.run("approvals", "list") == 0
    out = capsys.readouterr().out
    assert "3 send approvals waiting, oldest first:" in out
    assert f"{h.short('acc-1')}  Acme Creative (acmecreative.com) · Jane Doe, Head of People" in out
    assert "from Harry Dryden (harry@meetspill.org or harry@tryspill.org) · waiting" in out
    assert "Subject: Support for the Acme Creative team" in out
    assert f"us-outbound approvals approve {h.short('acc-1')} --live" in out


def test_cli_approve_takes_the_same_path(capsys):
    h = Harness()
    assert h.run("approvals", "approve", h.short("acc-1"), "--live") == 0
    assert h.calls[-1] == ("approvals_approve", True)
    [add] = instantly_posts(h.t)
    assert add.json["leads"][0]["email"] == "jane@acmecreative.com"
    row = item_for(h.ctx, "acc-1")
    assert (row["status"], row["handled_by"], row["payload"]["outcome"]) == ("handled", "cli", "approved")
    assert h.ctx.store.get("events", event_id=f"send-approval:{row['item_id']}")["approved_by"] == "cli"
    assert h.ctx.store.get("contacts", contact_id="con-1")["instantly_lead_id"]
    assert h.run("approvals", "approve", h.short("acc-1"), "--live") == 2  # handled: nothing to act on
    assert "nothing to act on" in capsys.readouterr().err and len(instantly_posts(h.t)) == 1


def test_cli_approve_needs_live_sending(capsys):
    h = Harness(live_sending=False)
    assert h.run("approvals", "approve", h.short("acc-1"), "--live") == 0
    out = capsys.readouterr().out
    assert "Running dry" in out and "the lead was not added to Instantly" in out
    assert instantly_posts(h.t) == [] and item_for(h.ctx, "acc-1")["status"] == "open"


def test_cli_reject_contact_or_company(capsys):
    h = Harness(live_sending=False)  # an operator command: --live alone
    assert h.run("approvals", "reject", h.short("acc-1"), "--contact") == 0  # dry without --live
    assert item_for(h.ctx, "acc-1")["status"] == "open"
    assert h.run("approvals", "reject", h.short("acc-1"), "--contact", "--live") == 0
    jane = h.ctx.store.get("contacts", contact_id="con-1")
    assert jane["suppressed"] is True and jane["suppressed_reason"].startswith("declined at the command line")
    assert item_for(h.ctx, "acc-1")["handled_by"] == "cli"
    assert h.run("approvals", "reject", h.short("acc-2"), "--company", "--live") == 0
    assert h.ctx.store.get("accounts", account_id="acc-2")["tier"] == "Excluded"
    assert item_for(h.ctx, "acc-2")["payload"]["outcome"] == "company_rejected"
    assert h.run("approvals", "reject", h.short("acc-3"), "--live") == 2
    assert "needs one of --contact" in capsys.readouterr().err
    assert h.run("approvals", "approve") == 2


# -- reading the thread -------------------------------------------------------------------------------------------


@pytest.mark.parametrize("text, kind", [
    ("send", "send"), ("<@U_BOT> Send.", "send"), ("skip", "reject"), ("No", "reject"), ("edit", "edit"),
    ("contact", "contact"), ("new contact", "contact"), ("company", "company"), ("drop the company", "company"),
    ("Subject: Hello", "edit_text"), ("Email 2:\nHi Jane,", "edit_text"), ("edit: Hi Jane,", "edit_text"),
    ("looks good to me", None), ("Hi Jane,\n\nA line.", None),
])
def test_parse_command(text, kind):
    cmd = approvals.parse_command(text)
    assert (cmd.kind if cmd else None) == kind


def test_while_editing_a_reply_with_a_greeting_is_an_edit():
    assert approvals.parse_command("Hi Jane,\n\nA line.", approvals.EDITING).kind == "edit_text"
    assert approvals.parse_command("let me think about it", approvals.EDITING) is None


def test_parse_edit():
    assert approvals.parse_edit("Email 3: Subject: Who knows?\nHi Jane,\n\nBody.") == (3, "Who knows?", "Hi Jane,\n\nBody.")
    assert approvals.parse_edit("Subject: Only the subject") == (1, "Only the subject", None)
    assert approvals.parse_edit("\nHi Jane,\nBody") == (1, None, "Hi Jane,\nBody")


# -- through the production wiring ----------------------------------------------------------------------------------


def test_dry_run_enrol_through_the_cli_previews_the_card_in_the_dev_channel():
    from tests.test_e2e_dry_run import ACCOUNT, CONTACT, SLACK, World

    w = World()
    for row in w.sheet["General"]:
        if row["key"] == "auto_send":
            row["value"] = "no"
    w.run("dry-run", "settings_sync")
    w.store.insert("accounts", [dict(ACCOUNT, tier="Priority", score=60, angle="Upgrade the EAP")])
    w.store.insert("contacts", [dict(CONTACT)])
    e = w.run("dry-run", "enrol")
    detail = e["heartbeat"]["detail"]
    assert detail["status"] == "ok" and detail["auto_send"] is False and detail["enrolled"] == 0
    assert (detail["send_approvals"]["would_post"], detail["send_approvals"]["previews"]) == (1, 1)
    assert w.add_leads_calls == [] and w.store.select("hitl_items", {"kind": approvals.KIND}) == []
    posts = [r for r in e["requests"] if r.url.startswith(SLACK) and r.method == "POST"]
    assert posts and {r.json["channel"] for r in posts} == {"C_DEV"}
    card = next(r for r in posts if r.url.endswith("chat.postMessage") and not r.json.get("thread_ts"))
    assert card.json["text"].startswith("[dry-run → #us-outbound] Send approval: Acme Creative · Jane Doe")
    assert re.search(r"\*Today:\* \w+: 1 of \d+ today", blocks_text(card.json))
