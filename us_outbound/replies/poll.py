"""poll_replies (SPEC 9, 11): every 15 minutes, each new reply is classified, routed, drafted and alerted.

It reads the emails the registry mailboxes received (GET /emails, email_type received, one request
series per mailbox; SPEC 1.2), keeps those from our contacts or anyone at their accounts' domains
(SPEC 2: a reply from anyone stops outreach to the whole account), and handles each one once.

THE CONTRACT WITH THE REPLY DESK
(replies/desk.py and crm/: approvals, sending the approved reply, HubSpot writes, escalation and
hubspot_readback). This job writes one hitl_items row for each reply a human must answer:

  item_id        "reply:{Instantly email id}", so one reply never gets two items
  kind           "reply"
  status         "open"; the desk moves it to handled or escalated
  account_id, contact_id, event_id (the reply's events row: the Instantly email id), created_at
  slack_channel, slack_ts    the alert, once posted (the same two are in the payload); empty until
                 then, and while no Slack token is set (the desk's escalation email covers that)
  payload (JSON):
    contact_id, account_id
    mailbox              the registry address the prospect wrote to; the reply goes from it (SPEC 9)
    owner                that mailbox's owner, who signs the draft
    instantly_email_id   the reply's Instantly email id. reply_to_uuid is the same id, so
                         Instantly.reply(mailbox, reply_to_uuid, reply_subject, text) answers in the
                         thread; with instantly_thread_id, message_id, reply_subject ("Re: ..."),
                         instantly_campaign and instantly_lead_id
    reply_class          SPEC 11's class after the rules in replies/classify.py, with model_class,
                         confidence, classified_by (model, rule or fallback) and classification_error
    reply_excerpt        at most 500 characters of the prospect's own words (quotes and our footer cut)
    summary, demo_requested, objection, competitor_named, language_terms, asks_to_stop
    draft                the proposed reply text, signed by the owner. "" when there is none:
                         draft_problems says why, and draft_rejected holds a draft that broke a rule
    referral             {name, title, email} as the prospect gave them, or null
    not_now_date, follow_up_date, follow_up_source   for not_now: the date they gave ("reply"), or
                         NOT_NOW_DEFAULT after the reply when they gave none ("default"; the draft asks)
    received_at          ISO 8601 (UTC)
    company, place, industry, tier, why (tier_reason), contact_name, first_name, title, role,
    from_is_contact      for the alert and the desk; from_is_contact is false when a colleague replied
    sequence_may_continue, sequence_paused   see "Stopping the sequence"
    slack_channel, slack_ts   once posted

Routing (SPEC 11, and docs/gtm-review/README.md D12: a draft for every class a human answers):
  positive, referral, objection, not_now, wrong_person, negative, other
      an item with a draft from the writing model (replies/draft.py), and a Slack alert in the
      General tab's alert_channel. Positive and referral alerts mention the approvers (SPEC 11:
      Harry; the mailbox owner has no Slack id in the sheet, so is named). SPEC 11 sends objection,
      negative and other to the daily post only; the daily post is phase 3, so until it exists they
      are alerted too, without the mention, so that every reply reaches a person from day one.
      The bot puts ✅ and ❌ on each alert (only ❌ when there is no draft), so deciding is one click.
  unsubscribe, or a reply asking to stop
      opted out everywhere (replies/optout.py): suppression (hashed), the Instantly blocklist, and
      HubSpot's opt-out where HubSpot already has the contact. No item; a one-line note in Slack so a
      wrong opt-out can be caught. The stop rule catches the plain ones without a model call.
  out_of_office
      the return date is stored on a hitl_items row of kind "out_of_office", status "handled" (there
      is nothing for a person to do), and the lead is re-timed: paused now and set going again two
      send days after the return date. Re-timing waits for phase 0 to confirm Instantly can pause a
      lead (clients/instantly.py LEAD_PAUSE_CONFIRMED); until then the row only notes the date.
Every reply but an out-of-office one engages its account (replies/outcomes.mark_engaged). The reply's
events row (type replied) carries the class, the stripped text (reply_text, purged after 90 days by
the retention job, SPEC 6; the item's excerpt is reply text too), language_terms and competitor_named.

Stopping the sequence. The campaigns stop on reply and for the whole company (clients/instantly.py
CAMPAIGN_SETTINGS, checked for drift every morning), which covers a reply Instantly ties to the lead.
Two cases are not covered: a human reply Instantly takes for an auto-reply (stop_on_auto_reply is
off, so the jobs can handle out-of-office replies) and a reply Instantly does not tie to the lead.
Both are flagged on the item and in the alert (sequence_may_continue), and the lead is paused once
phase 0 confirms the pause call. An opt-out goes on the workspace blocklist either way.

Claude: classification is claude_task_model (Sonnet) at effort low, at most 1,024 tokens a call;
the draft is claude_model (Opus) at effort medium, at most 3,000 tokens a call and two calls a reply.
Both count against claude_monthly_cap_usd. When the cap is used, a reply still reaches a person: it
becomes "other" with no draft, and the stop rule still opts people out. Any other model error is
retried on the next runs until the reply is RETRY old, then it goes to a person the same way.

Time: a reply can take a minute of model calls, so the run starts no new reply after RUN_SECONDS
(6 minutes; the scheduler stops it at 10). The rest wait: the summary's left_for_next_run counts
them and its resume_from (the first one's time) makes the next run read from there.

Dry-run reads Instantly, applies the stop rule (writing its suppression, which only protects), and
reports how many replies it would classify and the most that would cost. It calls no model and
writes no item or class, so a dry run never uses up a reply that a live run must still alert, opt
out or re-time (the copy desk calls Claude only with --live for the same reason). Live runs then
also retry pending opt-outs, set out-of-office leads going again, and post any item not yet in Slack.
"""

from __future__ import annotations

import re
import time
from collections import Counter
from collections.abc import Mapping
from datetime import date, datetime, timedelta
from typing import Any

from us_outbound.budget import is_send_day
from us_outbound.clients import instantly as instantly_client
from us_outbound.clients.claude import BudgetExceeded, ClaudeError
from us_outbound.clients.guard import WARM_REPLY_CLASSES
from us_outbound.clients.http import ApiError
from us_outbound.context import ET, ConfigError, Context
from us_outbound.enrol import render
from us_outbound.logs import log
from us_outbound.replies import classify, desk, draft, optout, outcomes
from us_outbound.replies.outcomes import Directory, Match
from us_outbound.settings.model import Settings

JOB = "poll_replies"
KIND = "reply"  # hitl_items.kind for a reply waiting for a person (the reply desk reads these)
OOO_KIND = "out_of_office"  # hitl_items.kind for an out-of-office record (status handled)
OPEN, HANDLED = "open", "handled"
REPLY_SOURCE = "reply"  # suppression.source for an opt-out by reply
LABELS = {
    "positive": "Positive", "referral": "Referral", "objection": "Objection", "not_now": "Not now",
    "negative": "Negative", "out_of_office": "Out of office", "wrong_person": "Wrong person",
    "unsubscribe": "Unsubscribe", "other": "Other",
}
EXCERPT_CHARS = 500  # the payload's reply_excerpt (the contract)
SLACK_EXCERPT_CHARS = 300
STORED_TEXT_CHARS = 5000  # events.reply_text
SLACK_SECTION_CHARS = 2900  # Slack's section text limit is 3,000
RETRY = timedelta(minutes=30)  # a model error is retried on later runs until the reply is this old
# The run starts no new reply after this (a draft takes 15 to 40 seconds a call, up to two calls), so
# it ends well inside the scheduler's 10-minute timeout; the rest are taken by the next run.
RUN_SECONDS = 6 * 60
_clock = time.monotonic  # tests replace it
NOT_NOW_DEFAULT = timedelta(days=90)  # the follow-up date when a not-now reply gives none
OOO_RESUME_SEND_DAYS = 2  # set going again two send days after the return date (gtm-review 03 §6)
FOOTER = ('✅ sends this draft · ❌ skips it (you\'ll answer yourself) · reply "edit: <new text>" to change it, '
          'then ✅ the new version · "send: <text>" sends your text now')
NO_DRAFT_FOOTER = '❌ skips it (you\'ll answer yourself) · "send: <text>" sends your text now'


def _lower(v: Any) -> str:
    return str(v or "").strip().lower()


def _esc(text: Any) -> str:
    """Slack mrkdwn escaping: a reply can never mention a channel or fake a link."""
    return str(text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _date(v: Any) -> date | None:
    try:
        return date.fromisoformat(str(v)[:10]) if v else None
    except ValueError:
        return None


def owner_of(settings: Settings, mailbox: str, account: Mapping[str, Any]) -> str:
    """The person who owns the mailbox the prospect wrote to (replies are signed by them; SPEC 5)."""
    found = next((m for m in settings.mailboxes if m.address.lower() == mailbox), None)
    return found.owner_name if found else str(account.get("sender") or "")


def resume_on(back: date, settings: Settings) -> date:
    """OOO_RESUME_SEND_DAYS send days after the return date."""
    d, n = back, 0
    for _ in range(60):
        d += timedelta(days=1)
        n += is_send_day(d, settings)
        if n >= OOO_RESUME_SEND_DAYS:
            break
    return d


def _subject(email: Mapping[str, Any]) -> str:
    s = re.sub(r"\s+", " ", str(email.get("subject") or "")).strip()
    return s if re.match(r"(?i)^re\s*:", s) else f"Re: {s}".strip()


def _sender_name(email: Mapping[str, Any]) -> str:
    """The display name on the From line, if Instantly gives one. PHASE0-CONFIRM: from_address_json."""
    raw = email.get("from_address_json")
    items = raw if isinstance(raw, list) else [raw] if isinstance(raw, Mapping) else []
    for item in items:
        if isinstance(item, Mapping) and str(item.get("name") or "").strip():
            return str(item["name"]).strip()
    return ""


def may_continue(email: Mapping[str, Any]) -> bool:
    """Whether Instantly may not have stopped the lead: it took the reply for an auto-reply, or tied it to no lead."""
    untied = ("lead" in email or "lead_id" in email) and not (email.get("lead") or email.get("lead_id"))
    return outcomes.is_auto_reply(email) or untied


# -- what is already done -----------------------------------------------------------------------


class _Seen:
    """Replies already handled: their replied event has a class, or they have an item."""

    def __init__(self, ctx: Context):
        self.ctx = ctx
        self.classified = {str(e["event_id"]) for e in ctx.store.select("events", {"type": outcomes.REPLIED})
                           if e.get("reply_class")}
        self.items = {str(r["item_id"]): r for r in ctx.store.select("hitl_items", {"kind": [KIND, OOO_KIND]})}

    def done(self, email_id: str) -> bool:
        if email_id in self.classified:
            return True
        item = self.items.get(f"{KIND}:{email_id}") or self.items.get(f"{OOO_KIND}:{email_id}")
        if item is None:
            return False
        # The item was written but the run stopped before the event: give the event its class.
        payload = item.get("payload") if isinstance(item.get("payload"), Mapping) else {}
        self.ctx.store.upsert("events", [{
            "event_id": email_id, "contact_id": item.get("contact_id"), "account_id": item.get("account_id"),
            "type": outcomes.REPLIED, "mailbox": payload.get("mailbox"),
            "reply_class": payload.get("reply_class") or "other",
            "occurred_at": outcomes.to_time(payload.get("received_at")) or self.ctx.now,
        }])
        self.classified.add(email_id)
        return True


# -- one reply ------------------------------------------------------------------------------------


def _with_body(ctx: Context, email: Mapping[str, Any], mailbox: str) -> Mapping[str, Any]:
    """The email with its body: the list may carry only a preview, so it is read by id then."""
    body = email.get("body")
    if (isinstance(body, Mapping) and (body.get("text") or body.get("html"))) or (isinstance(body, str) and body.strip()):
        return email
    try:
        return ctx.clients.instantly.get_email(mailbox, str(email["id"]))
    except ApiError as exc:
        log("reply_body_unavailable", run_id=ctx.run_id, email_id=email.get("id"), status=exc.status)
        return email


def _info(ctx: Context, email: Mapping[str, Any], m: Match, mailbox: str, received: datetime) -> dict[str, Any]:
    a, c = m.account, m.contact
    first = str(c.get("first_name") or "").strip() if m.from_is_contact else (_sender_name(email).split() or [""])[0]
    return {
        "contact_id": c.get("contact_id"), "account_id": c.get("account_id") or a.get("account_id"),
        "mailbox": mailbox, "owner": owner_of(ctx.settings, mailbox, a),
        "instantly_email_id": str(email.get("id")), "reply_to_uuid": str(email.get("id")),
        "reply_subject": _subject(email), "instantly_thread_id": email.get("thread_id"),
        "message_id": email.get("message_id"), "instantly_campaign": c.get("instantly_campaign"),
        "instantly_lead_id": c.get("instantly_lead_id"), "received_at": received.isoformat(),
        "company": str(a.get("clean_name") or a.get("domain") or ""), "place": render.place_for(a),
        "industry": str(a.get("industry") or ""), "tier": str(a.get("tier") or ""), "why": str(a.get("tier_reason") or ""),
        "contact_name": " ".join(str(c.get(k) or "").strip() for k in ("first_name", "last_name")).strip(),
        "first_name": first, "title": str(c.get("title") or ""), "role": str(c.get("role") or ""),
        "from_is_contact": m.from_is_contact,
    }


def _classify(ctx: Context, text: str, received: datetime, info: Mapping[str, Any], out: dict) -> classify.Verdict | None:
    """The verdict, or None to try again on a later run."""
    try:
        return classify.classify(ctx, text, received=received, company=info["company"], title=info["title"])
    except BudgetExceeded:
        out["claude_cap_reached"] += 1
        return classify.Verdict.fallback("Claude's monthly cap is used up")
    except ClaudeError as exc:
        out.setdefault("claude_error", str(exc)[:200])  # the run's first, for heartbeat_check (ops/job_errors.py)
        if ctx.now - received < RETRY:
            out["retry_later"] += 1
            return None
        return classify.Verdict.fallback(f"Claude could not classify it: {exc}")


def _draft(ctx: Context, verdict: classify.Verdict, info: Mapping[str, Any], text: str,
           follow_up: date | None, given: bool, out: dict | None = None) -> dict[str, Any]:
    owner = info["owner"]
    req = draft.Request(
        reply_class=verdict.reply_class, reply_text=text, first_name=info["first_name"], sender_name=owner,
        sender_is_harry=owner.strip().casefold() == ctx.settings.general.demo_host.strip().casefold(),
        company=info["company"], title=info["title"], subject=info["reply_subject"], objection=verdict.objection,
        demo_requested=verdict.demo_requested, follow_up_date=follow_up, follow_up_given=given,
        referral=verdict.referral, competitor_named=verdict.competitor_named,
    )
    if verdict.source == "fallback":  # nothing reliable to answer: a person reads it
        return {"draft": "", "draft_problems": [f"no draft: {verdict.error}"], "draft_rejected": ""}
    exempt = [info["first_name"], info["company"], info["contact_name"], *((verdict.referral or {}).values())]
    try:
        d = draft.write(ctx, req, exempt=exempt)
    except BudgetExceeded:
        return {"draft": "", "draft_problems": ["Claude's monthly cap is used up, so there is no draft"], "draft_rejected": ""}
    except ClaudeError as exc:
        if out is not None:
            out.setdefault("claude_error", str(exc)[:200])
        return {"draft": "", "draft_problems": [f"Claude could not draft it: {exc}"], "draft_rejected": ""}
    return {"draft": d.text, "draft_problems": d.problems, "draft_rejected": d.rejected}


def _pause_if_needed(ctx: Context, email: Mapping[str, Any], info: Mapping[str, Any]) -> tuple[bool, bool]:
    """(sequence_may_continue, sequence_paused) for a reply a person answers."""
    if not may_continue(email):
        return False, False
    campaign, lead_id = info.get("instantly_campaign"), info.get("instantly_lead_id")
    if not (instantly_client.LEAD_PAUSE_CONFIRMED and campaign and lead_id):
        return True, False
    try:
        ctx.clients.instantly.set_lead_paused(str(campaign), str(lead_id), True)
    except (ApiError, LookupError) as exc:
        log("lead_pause_failed", run_id=ctx.run_id, lead_id=lead_id, error=str(exc)[:200])
        return True, False
    return True, True


def _record_ooo(ctx: Context, verdict: classify.Verdict, info: Mapping[str, Any], text: str, out: dict) -> None:
    """The return date, and the re-timing (when Instantly's pause is confirmed), on an out_of_office row."""
    back = verdict.ooo_return_date
    today = ctx.now_et().date()
    campaign, lead_id = info.get("instantly_campaign"), info.get("instantly_lead_id")
    resume = resume_on(back, ctx.settings) if back else None
    retime: dict[str, Any] = {"status": "noted", "resume_on": resume.isoformat() if resume else None,
                              "campaign": campaign, "lead_id": lead_id}
    if back is None:
        retime["note"] = "the reply gives no return date"
    elif not instantly_client.LEAD_PAUSE_CONFIRMED:
        retime["note"] = "re-timing waits for phase 0 to confirm Instantly can pause a lead (LEAD_PAUSE_CONFIRMED)"
    elif not (campaign and lead_id):
        retime["note"] = "no Instantly lead to pause"
    elif resume is not None and resume <= today:
        retime["note"] = "they are already back"
    else:
        try:
            ctx.clients.instantly.set_lead_paused(str(campaign), str(lead_id), True)
            retime.update(status="paused", paused_at=ctx.now.isoformat())
            out["ooo_paused"] += 1
        except (ApiError, LookupError) as exc:
            retime["note"] = f"pausing the lead failed: {str(exc)[:200]}"
    payload = {**info, **verdict.as_payload(), "reply_excerpt": text[:EXCERPT_CHARS], "retime": retime}
    ctx.store.upsert("hitl_items", [{
        "item_id": f"{OOO_KIND}:{info['instantly_email_id']}", "kind": OOO_KIND, "status": HANDLED,
        "account_id": info["account_id"], "contact_id": info["contact_id"], "event_id": info["instantly_email_id"],
        "payload": payload, "created_at": ctx.now, "handled_at": ctx.now, "handled_by": JOB,
    }])


def _opt_out_addresses(email: Mapping[str, Any], m: Match) -> list[str]:
    """Whoever asked, and the contact too when a colleague asked for them: an opt-out errs on the safe side."""
    out = [outcomes.from_address(email)]
    if not m.from_is_contact and m.contact.get("email"):
        out.append(_lower(m.contact["email"]))
    return [a for a in out if a]


def _note(ctx: Context, text: str) -> None:
    """A one-line note in the alert channel (live; nothing when Slack is not set up)."""
    slack = _slack(ctx)
    if slack is None:
        return
    try:
        slack.post(ctx.settings.general.alert_channel, text)
    except (ApiError, LookupError) as exc:
        log("slack_note_failed", run_id=ctx.run_id, error=str(exc)[:200])


def _process(ctx: Context, d: Directory, email: Mapping[str, Any], seen: _Seen, done: set[str], out: dict) -> None:
    eid, mailbox = str(email.get("id") or ""), _lower(email.get("eaccount"))
    if not eid or not outcomes.is_received(email, mailbox, ctx.guard.bounds.registry_addresses):
        return
    if seen.done(eid):
        out["already_handled"] += 1
        return
    m = d.match_reply(email)
    if m is None:
        out["unmatched"] += 1
        return
    email = _with_body(ctx, email, mailbox)
    text = classify.reply_text(email)
    received = outcomes.email_time(email) or ctx.now
    info = _info(ctx, email, m, mailbox, received)

    if ctx.dry_run:
        if classify.asks_to_stop(text):
            optout.suppress_now(ctx, _opt_out_addresses(email, m), contact=m.contact, source=REPLY_SOURCE)
            out["would_opt_out"] += 1
        else:
            out["would_classify"] += 1
            req = draft.Request("positive", text, info["first_name"], info["owner"], False)
            try:
                out["would_spend_usd_at_most"] += classify.estimate_usd(ctx, text) + draft.estimate_usd(req, ctx.settings)
            except ClaudeError as exc:  # a model the cap cannot price: live runs would hand the reply to a person
                out["estimate_error"] = str(exc)
        return

    verdict = _classify(ctx, text, received, info, out)
    if verdict is None:
        return
    cls = verdict.reply_class
    sends = outcomes.sent_events(ctx, [str(info["contact_id"])]).get(str(info["contact_id"]), [])
    event = {
        "event_id": eid, "contact_id": info["contact_id"], "account_id": info["account_id"], "type": outcomes.REPLIED,
        "step": outcomes.step_before(sends, received), "mailbox": mailbox, "reply_class": cls,
        "reply_text": text[:STORED_TEXT_CHARS], "language_terms": verdict.language_terms,
        "competitor_named": verdict.competitor_named or None, "occurred_at": received,
    }
    if cls == "unsubscribe":
        ctx.store.upsert("events", [event])
        result = optout.opt_out(
            ctx, marker=optout.reply_marker(eid), addresses=_opt_out_addresses(email, m), contact=m.contact,
            account_id=info["account_id"], mailbox=mailbox, occurred_at=received, source=REPLY_SOURCE, done=done,
        )
        out["opted_out"][result] += 1
        who = info["contact_name"] if m.from_is_contact else f"someone else at {info['company']}"
        _note(ctx, f"Opted out · {_esc(info['company'])} · {_esc(who or 'the contact')} asked to stop "
                   f"({'stop rule' if verdict.source == 'rule' else 'Claude'}); suppressed, blocklisted, HubSpot opt-out "
                   f"where they exist ({result}).")
    elif cls == "out_of_office":
        _record_ooo(ctx, verdict, info, text, out)
        ctx.store.upsert("events", [event])
    else:
        follow_up, given = None, False
        if cls == "not_now":
            given = verdict.not_now_date is not None
            follow_up = verdict.not_now_date or (received.astimezone(ET).date() + NOT_NOW_DEFAULT)
        cont, paused = _pause_if_needed(ctx, email, info)
        payload = {
            **info, **verdict.as_payload(), "reply_excerpt": text[:EXCERPT_CHARS],
            **_draft(ctx, verdict, info, text, follow_up, given, out),
            "follow_up_date": follow_up.isoformat() if follow_up else None,
            "follow_up_source": ("reply" if given else "default") if follow_up else None,
            "sequence_may_continue": cont, "sequence_paused": paused, "slack_channel": None, "slack_ts": None,
        }
        item = {"item_id": f"{KIND}:{eid}", "kind": KIND, "status": OPEN, "account_id": info["account_id"],
                "contact_id": info["contact_id"], "event_id": eid, "payload": payload, "created_at": ctx.now}
        ctx.store.upsert("hitl_items", [item])  # the item first: a run that stops here leaves no reply unseen
        ctx.store.upsert("events", [event])
        seen.items[item["item_id"]] = item
        out["items"][cls] += 1
    seen.classified.add(eid)
    out["classified"][cls] += 1
    if cls != "out_of_office":
        outcomes.mark_engaged(ctx, [info["account_id"]])
    log("reply_routed", run_id=ctx.run_id, email_id=eid, reply_class=cls, classified_by=verdict.source,
        account_id=info["account_id"])


# -- the sweeps (live) --------------------------------------------------------------------------------


def _sweep_opt_outs(ctx: Context, d: Directory, done: set[str], out: dict) -> None:
    """Opt-outs by reply still waiting for the blocklist or HubSpot: the reply is read back by id."""
    registry = ctx.guard.bounds.registry_addresses
    for ev in ctx.store.select("events", {"type": outcomes.REPLIED, "reply_class": "unsubscribe"}):
        marker = optout.reply_marker(str(ev["event_id"]))
        mailbox = _lower(ev.get("mailbox"))
        if marker in done or mailbox not in registry:
            continue
        try:
            email = ctx.clients.instantly.get_email(mailbox, str(ev["event_id"]))
        except ApiError as exc:
            log("opt_out_reread_failed", run_id=ctx.run_id, email_id=ev["event_id"], status=exc.status)
            continue
        contact = d.by_id.get(str(ev.get("contact_id"))) or {}
        sender = outcomes.from_address(email)
        m = Match(contact, d.account_of(contact), sender == _lower(contact.get("email")))
        addresses = _opt_out_addresses(email, m)
        if not addresses:
            continue
        result = optout.opt_out(ctx, marker=marker, addresses=addresses, contact=contact or None,
                                account_id=ev.get("account_id"),
                                mailbox=mailbox, occurred_at=outcomes.to_time(ev.get("occurred_at")) or ctx.now,
                                source=REPLY_SOURCE, done=done)
        out["opted_out_on_retry"][result] += 1


def _resume_ooo(ctx: Context, out: dict) -> None:
    """Out-of-office leads paused until after their return date are set going again."""
    today = ctx.now_et().date()
    for row in ctx.store.select("hitl_items", {"kind": OOO_KIND}):
        payload = dict(row.get("payload") or {})
        retime = dict(payload.get("retime") or {})
        resume = _date(retime.get("resume_on"))
        if retime.get("status") != "paused" or resume is None or resume > today:
            continue
        try:
            ctx.clients.instantly.set_lead_paused(str(retime["campaign"]), str(retime["lead_id"]), False)
        except (ApiError, LookupError) as exc:
            retime["note"] = f"setting the lead going again failed: {str(exc)[:200]}"
        else:
            retime.update(status="resumed", resumed_at=ctx.now.isoformat())
            out["ooo_resumed"] += 1
        payload["retime"] = retime
        ctx.store.update("hitl_items", {"item_id": row["item_id"]}, {"payload": payload})


def _slack(ctx: Context):
    """The Slack client, or None (logged) when no token is set: the item stays open for the escalation email."""
    try:
        return ctx.clients.slack
    except ConfigError:
        log("slack_not_configured", run_id=ctx.run_id, job=JOB)
        return None


def _notes(p: Mapping[str, Any]) -> list[str]:
    notes = []
    if p.get("why"):
        notes.append(f"Why this account: {_esc(p['why'])}")
    if p.get("demo_requested"):
        notes.append("Asked for a demo.")
    if p.get("reply_class") == "objection" and p.get("objection") not in (None, "", "none"):
        notes.append(f"Objection: {_esc(str(p['objection']).replace('_', ' '))}")
    if p.get("competitor_named"):
        notes.append(f"Competitor named: {_esc(p['competitor_named'])}")
    ref = p.get("referral") or {}
    if ref:
        notes.append("Referral: " + _esc(", ".join(v for v in (ref.get("name"), ref.get("title"), ref.get("email")) if v)))
    if p.get("follow_up_date"):
        d = _date(p["follow_up_date"])
        when = f"{d:%a %d %b %Y}" if d else str(p["follow_up_date"])
        notes.append(f"Follow up on {when}" + (" (the date they gave)." if p.get("follow_up_source") == "reply"
                                               else " (no date given; the draft asks when)."))
    if p.get("classified_by") == "fallback":
        notes.append(f"Not classified ({_esc(p.get('classification_error'))}): read the reply yourself.")
    elif p.get("reply_class") == "other" and p.get("model_class") not in (None, "", "other"):
        notes.append(f"Claude was not sure: it read this as {_esc(p['model_class'])} at {float(p.get('confidence') or 0):.0%}.")
    if p.get("sequence_may_continue"):
        notes.append("Instantly may not have stopped this lead's sequence (it took the reply for an auto-reply, or tied "
                     "it to no lead): " + ("the job paused the lead." if p.get("sequence_paused")
                                           else "pause the lead in Instantly."))
    return notes


def alert(payload: Mapping[str, Any], settings: Settings) -> tuple[str, list[dict]]:
    """The SPEC 11 Slack alert for one item: (notification text, Block Kit blocks)."""
    p = payload
    cls = str(p.get("reply_class") or "other")
    where = f"{p.get('company')} ({p['place']})" if p.get("place") else str(p.get("company") or "")
    head = " · ".join(x for x in (f"{LABELS.get(cls, cls)} reply", where, p.get("industry"), p.get("tier")) if x)
    mention = " ".join(f"<@{u}>" for u in settings.general.approver_slack_ids) if cls in WARM_REPLY_CLASSES else ""
    who = ", ".join(x for x in (p.get("contact_name"), p.get("title")) if x) or "the contact"
    if not p.get("from_is_contact"):
        who = f"someone else at {p.get('company') or 'the account'} (the contact is {who})"
    excerpt = str(p.get("reply_excerpt") or "")[:SLACK_EXCERPT_CHARS]
    sections = [
        "\n".join([f"*{_esc(head)}*" + (f" {mention}" if mention else ""),
                   f"From: {_esc(who)} → {_esc(p.get('mailbox'))}",
                   f"\"{_esc(excerpt)}\""]),
    ]
    notes = _notes(p)
    if notes:
        sections.append("\n".join(notes))
    owner = str(p.get("owner") or "")
    if p.get("draft"):
        handoff = "; hands the demo to Harry" if cls == "positive" and owner != settings.general.demo_host else ""
        sections.append(f"Draft reply (signed {_esc(owner)}{handoff}):\n"
                        + "\n".join(f"> {_esc(line)}" for line in str(p["draft"]).split("\n")))
    else:
        why = "; ".join(str(x) for x in (p.get("draft_problems") or [])[:3]) or "none written"
        sections.append(f"No draft ({_esc(why)}). Reply \"send: &lt;your text&gt;\" to answer, or \"skip\".")
    sections.append(_esc(FOOTER if p.get("draft") else NO_DRAFT_FOOTER))
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": s[:SLACK_SECTION_CHARS]}} for s in sections]
    return _esc(f"{head}: \"{excerpt[:150]}\""), blocks


def _post_pending(ctx: Context, out: dict) -> None:
    """Post every open reply item not yet in Slack, and store where (the contract's slack_channel and slack_ts)."""
    pending = [r for r in ctx.store.select("hitl_items", {"kind": KIND, "status": OPEN}) if not r.get("slack_ts")]
    if not pending:
        return
    slack = _slack(ctx)
    if slack is None:
        out["waiting_for_slack"] = len(pending)
        return
    channel = ctx.settings.general.alert_channel
    for row in sorted(pending, key=lambda r: str(r.get("created_at") or "")):
        payload = dict(row.get("payload") or {})
        text, blocks = alert(payload, ctx.settings)
        try:
            posted = slack.post(channel, text, blocks=blocks)
        except (ApiError, LookupError) as exc:
            log("slack_alert_failed", run_id=ctx.run_id, item_id=row["item_id"], error=str(exc)[:200])
            out["slack_errors"] += 1
            continue
        if not posted or not posted.get("ts"):
            continue
        payload.update(slack_channel=posted["channel"], slack_ts=posted["ts"])
        ctx.store.update("hitl_items", {"item_id": row["item_id"]},
                         {"slack_channel": posted["channel"], "slack_ts": posted["ts"], "payload": payload})
        out["alerts_posted"] += 1
        # The bot's ✅ and ❌ (only ❌ with no draft to send), so deciding is one click; never fatal.
        desk.seed(slack, posted["channel"], posted["ts"], desk.SEED_REACTIONS if payload.get("draft") else ("x",))


# -- the job --------------------------------------------------------------------------------------------


def run(ctx: Context) -> dict:
    """The poll_replies job (JOB CONTRACT: run(ctx) -> summary). The summary carries counts, never reply text."""
    registry = sorted(ctx.guard.bounds.registry_addresses)
    if not registry:
        return {"skipped": True, "reason": "no registry mailboxes"}
    stop = _clock() + RUN_SECONDS
    d = Directory(ctx)
    start = outcomes.since(ctx, JOB, live_only=True, earliest=d.earliest_enrolled())
    out: dict[str, Any] = {k: Counter() for k in ("classified", "items", "opted_out", "opted_out_on_retry")}
    out.update({k: 0 for k in ("unmatched", "already_handled", "retry_later", "claude_cap_reached", "would_classify",
                               "would_opt_out", "ooo_paused", "ooo_resumed", "alerts_posted", "slack_errors",
                               "left_for_next_run")})
    out["would_spend_usd_at_most"] = 0.0
    seen = _Seen(ctx)
    done = optout.done_markers(ctx.store)
    emails = ctx.clients.instantly.list_emails(registry, start, email_type="received")
    ordered = sorted(emails, key=lambda e: outcomes.email_time(e) or ctx.now)
    for i, email in enumerate(ordered):
        if _clock() >= stop:  # the rest wait for the next run, which reads from the first of them
            rest = ordered[i:]
            out["left_for_next_run"] = len(rest)
            first = min((outcomes.to_time(e.get("timestamp_created")) or outcomes.email_time(e) or ctx.now) for e in rest)
            out["resume_from"] = first.isoformat()
            log("poll_replies_time_budget", run_id=ctx.run_id, left=len(rest), resume_from=out["resume_from"])
            break
        _process(ctx, d, email, seen, done, out)
    if ctx.live:
        _sweep_opt_outs(ctx, d, done, out)
        _resume_ooo(ctx, out)
        _post_pending(ctx, out)
    summary = {"job": JOB, "dry_run": ctx.dry_run, "since": start.isoformat(), "received": len(emails),
               **{k: dict(v) if isinstance(v, Counter) else v for k, v in out.items()}}
    summary["would_spend_usd_at_most"] = round(summary["would_spend_usd_at_most"], 4)
    log("poll_replies_done", run_id=ctx.run_id, **summary)
    return summary
