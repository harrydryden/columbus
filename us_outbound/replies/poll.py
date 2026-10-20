"""poll_replies (SPEC 9, 11): every 15 minutes, each new reply is recorded, classified, routed, drafted and alerted.

It is the one reader of received mail (W2-4, 20 Oct 2026; sync_outcomes listed it too until then). It reads the
emails the registry mailboxes received (GET /emails, email_type received, one request series per mailbox; SPEC
1.2), keeps those from our contacts or anyone at their accounts' domains (SPEC 2: a reply from anyone stops outreach
to the whole account), and handles each one once.

Recorded before it is classified (RS §2 W2-4): first, in one pass over everything read and in dry-run too, each
new reply's events row (type replied, no class yet) is written. Only then is each reply classified, so a model
outage, the Claude cap or the run's time budget leaves the reply on record (the kill rules, the send forecast, the
account stop and every report see it) and the next run classifies it. The account becomes engaged once the class
is known and is not out_of_office (an away message Instantly did not flag engages nobody: marked at the first pass, it
stayed engaged for good, and its contact's later send slots were freed). An email
Instantly flags as an automatic reply is not recorded in that pass, as sync_outcomes did not record one before W2-4:
an unclassified replied row frees the contact's later send slots and counts as a human reply, while an away message
keeps its sequence going (stop_on_auto_reply is off); it is recorded with its class, which a human-written reply
Instantly misflagged also gets. A reply
is done once its event carries a class and, for a class a person answers (or an out-of-office), once its item
exists: the class is written first and the item after it, so a run that stops between the two leaves the reply to
the next run, which writes the item.

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
    from_address, from_name   who wrote it (the From line), so HubSpot records a colleague who replied
    also_asked_to_stop, opt_out, stop_note   a warm reply that also asked to stop: opted out, answered by hand
    sequence_may_continue, sequence_paused   see "Stopping the sequence"
    instantly_interest   for a positive reply: "interested" once Instantly's interest status is set, or why it
                         was not (see "Interested in Instantly")
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
      wrong opt-out can be caught.
  a warm reply that also asks to stop (W1-2, 19 Oct 2026; code-replies-crm H2)
      In this order: the stop rule (a stop phrasing in the prospect's words; replies/classify.py) opts
      the address out at once, before any model call; then the model is asked, and any failure of it
      is the stop rule's unsubscribe. A positive, referral or wrong_person class is kept: the item is
      opened after the opt-out (so the opt-out's closing of the contact's open items leaves it alone),
      flagged also_asked_to_stop, and its alert says they are suppressed, so it is answered by hand or
      the suppression lifted knowingly (the desk refuses to send to a suppressed address). The model's
      own asks_to_stop opts out the same way. Until then 6 of 7 realistic warm replies were caught by
      the rule and became an unsubscribe with "Nothing to do".
  out_of_office
      the return date is stored on a hitl_items row of kind "out_of_office", status "handled" (there
      is nothing for a person to do). (Re-timing the lead round the return date waited for phase 0 to confirm
      Instantly's lead pause, never confirmed, and went with W2-1, 20 Oct 2026.)
Every email listed as received but not from outside Spill (a campaign send, our own mailbox, a system
sender, a Spill address; outcomes.received_reason) is counted by why in the summary's dropped (W1-2;
code-replies-crm M3: until then a misread field would have looked exactly like "no replies yet"), and one from
nobody of ours under unmatched. Every reply but an out-of-office one engages its account
(replies/outcomes.mark_engaged). The reply's
events row (type replied) carries the class, the stripped text (reply_text, purged after 90 days by
the retention job, SPEC 6; the item's excerpt is reply text too), language_terms and competitor_named.

Stopping the sequence. The campaigns stop on reply and for the whole company (clients/instantly.py
CAMPAIGN_SETTINGS, checked for drift every morning), which covers a reply Instantly ties to the lead.
Two cases are not covered: a human reply Instantly takes for an auto-reply (stop_on_auto_reply is
off, so the jobs can handle out-of-office replies) and a reply Instantly does not tie to the lead.
Both are flagged on the item and in the alert (sequence_may_continue), and the lead is paused once
phase 0 confirms the pause call. An opt-out goes on the workspace blocklist either way.

Interested in Instantly (Harry, 7 Oct 2026): once a reply is classified positive and its item and event are
written, the lead's interest status in its own campaign is set to "Interested" (Instantly.mark_interested, through
the guard, live only), as stop_lead sets "Meeting booked" when a meeting is booked; Instantly's own views and reports
then show it. Once a contact: not again for a later positive reply, and not over a meeting already booked. A
referral is not marked: the person who replied points to someone else, which Instantly would count as the wrong
person rather than an interested one. Nothing else about the lead changes (Instantly stopped it on the reply). A
failure is logged and kept as interest_error (heartbeat_check reports it), and never fails the reply's handling.

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
writes no item or class, so a dry run never uses up a reply that a live run must still alert or opt
out (the copy desk calls Claude only with --live for the same reason). Live runs then also retry pending opt-outs
and post any item not yet in Slack.
"""

from __future__ import annotations

import re
import time
from collections import Counter
from collections.abc import Mapping
from datetime import date, datetime, timedelta
from typing import Any

from us_outbound.base import say
from us_outbound.clients import instantly as instantly_client
from us_outbound.clients.claude import BudgetExceeded, ClaudeError
from us_outbound.clients.guard import US_CAMPAIGN_PREFIX, WARM_REPLY_CLASSES
from us_outbound.clients.http import ApiError
from us_outbound.context import ET, ConfigError, Context
from us_outbound.enrol import render
from us_outbound.logs import log
from us_outbound.replies import classify, desk, draft, optout, outcomes
from us_outbound.replies.outcomes import Directory, Match
from us_outbound.settings.model import Settings
from us_outbound.timeparse import iso_date, utc

JOB = "poll_replies"
KIND = "reply"  # hitl_items.kind for a reply waiting for a person (the reply desk reads these)
OOO_KIND = "out_of_office"  # hitl_items.kind for an out-of-office record (status handled)
OPEN, HANDLED = "open", "handled"
REPLY_SOURCE = "reply"  # suppression.source for an opt-out by reply
LABELS = {cls: label for cls, (label, _) in desk.CLASS_WORDS.items()}  # "Interested", "Not now", "Wrong person"
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
# The alert's line, and the item's, for a reply kept for a person although it asked to stop (W1-2, 19 Oct 2026).
ALSO_STOP = ("They also asked to be taken off the list: suppressed, so the desk will not send to them. Answer by "
             "hand, or lift the suppression knowingly.")
INTERESTED_CLASSES = frozenset({"positive"})  # marked "Interested" in Instantly (not referral: see the docstring)
INTERESTED = "interested"  # the payload's instantly_interest once it is set
# The alert's last section: what each reaction or thread reply does (replies/desk.py reads them; the words themselves
# never change). Already Slack-escaped.
FOOTER = "\n".join([
    "*What to do* (only an approver's reaction or reply counts; nothing goes out until one approves):",
    "• ✅ on this message, or reply `send` in the thread: sends the draft as it is",
    "• reply `edit: &lt;new text&gt;`: replaces the draft; the new one is posted in the thread for your ✅",
    "• reply `send: &lt;your text&gt;`: sends your own text instead, straight away",
    "• ❌, or reply `skip`: sends nothing and closes it (you answer them yourself, or it needs no answer)",
])
NO_DRAFT_FOOTER = "\n".join([
    "*What to do* (only an approver's reaction or reply counts):",
    "• reply `send: &lt;your text&gt;` in the thread: sends your text from the mailbox they wrote to",
    "• ❌, or reply `skip`: sends nothing and closes it (you answer them yourself, or it needs no answer)",
])


def _lower(v: Any) -> str:
    return str(v or "").strip().lower()


def _esc(text: Any) -> str:
    """Slack mrkdwn escaping: a reply can never mention a channel or fake a link."""
    return str(text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def owner_of(settings: Settings, mailbox: str, account: Mapping[str, Any]) -> str:
    """The person who owns the mailbox the prospect wrote to (replies are signed by them; SPEC 5)."""
    found = next((m for m in settings.mailboxes if m.address.lower() == mailbox), None)
    return found.owner_name if found else str(account.get("sender") or "")


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
    """Replies already handled: the event carries a class and, for a class with an item, the item exists (the module
    docstring). No repair write: a reply whose item is missing is simply handled again (W2-4)."""

    NO_ITEM = frozenset({"unsubscribe"})  # the classes that open no item

    def __init__(self, ctx: Context):
        self.classified = {str(e["event_id"]): str(e["reply_class"])
                           for e in ctx.store.select("events", {"type": outcomes.REPLIED}) if e.get("reply_class")}
        self.items = {str(r["item_id"]): r for r in ctx.store.select("hitl_items", {"kind": [KIND, OOO_KIND]})}

    def done(self, email_id: str) -> bool:
        cls = self.classified.get(email_id)
        if cls is None:
            return False
        return cls in self.NO_ITEM or f"{KIND}:{email_id}" in self.items or f"{OOO_KIND}:{email_id}" in self.items


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
        # Who wrote it (W1-2, code-replies-crm M9): a colleague's reply puts them in HubSpot, not the person emailed.
        "from_address": outcomes.from_address(email), "from_name": _sender_name(email),
    }


def _classify(ctx: Context, text: str, received: datetime, info: Mapping[str, Any], out: dict,
              stop: bool = False) -> classify.Verdict | None:
    """The verdict, or None to try again on a later run. With stop (the stop rule matched, and the address is opted
    out already) there is always a verdict: any model failure is the stop rule's unsubscribe (replies/classify.py)."""
    try:
        verdict = classify.classify(ctx, text, received=received, company=info["company"], title=info["title"],
                                    stop=stop)
        if verdict.source == classify.RULE and verdict.error.startswith(BudgetExceeded.__name__):
            out["claude_cap_reached"] += 1
        elif verdict.source == classify.RULE and verdict.error:
            out.setdefault("claude_error", verdict.error[:200])  # the run's first, for heartbeat_check
        return verdict
    except BudgetExceeded:
        out["claude_cap_reached"] += 1
        return classify.Verdict.fallback("Claude's monthly cap is used up")
    except ClaudeError as exc:
        out.setdefault("claude_error", str(exc)[:200])  # the run's first, for heartbeat_check (ops/health.py)
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


def _record_ooo(ctx: Context, verdict: classify.Verdict, info: Mapping[str, Any], text: str) -> None:
    """The return date (verdict.ooo_return_date, in the payload) on an out_of_office row."""
    payload = {**info, **verdict.as_payload(), "reply_excerpt": text[:EXCERPT_CHARS]}
    ctx.store.upsert("hitl_items", [{
        "item_id": f"{OOO_KIND}:{info['instantly_email_id']}", "kind": OOO_KIND, "status": HANDLED,
        "account_id": info["account_id"], "contact_id": info["contact_id"], "event_id": info["instantly_email_id"],
        "payload": payload, "created_at": ctx.now, "handled_at": ctx.now, "handled_by": JOB,
    }])


def _mark_interested(ctx: Context, info: Mapping[str, Any], m: Match, out: dict) -> str:
    """Set the lead's interest status to "Interested" in its own campaign (the module docstring); returns what
    happened, for the item's instantly_interest. Never raises but for a guardrail."""
    cid, campaign = str(info.get("contact_id") or ""), str(info.get("instantly_campaign") or "")
    email = _lower(m.contact.get("email"))
    if not (campaign.startswith(US_CAMPAIGN_PREFIX) and email):
        return "no lead in a US Outbound campaign"
    if ctx.store.select("events", {"contact_id": cid, "type": "meeting_booked"}):
        return "a meeting is booked already"  # stop_lead marked it "Meeting booked": never marked back
    earlier = ctx.store.select("hitl_items", {"kind": KIND, "contact_id": cid})
    if any((r.get("payload") or {}).get("instantly_interest") == INTERESTED for r in earlier):
        return "marked for an earlier reply"
    try:
        ctx.clients.instantly.mark_interested(campaign, email)
    except (ApiError, LookupError, ConfigError, ValueError) as exc:  # logged; the reply is handled all the same
        out.setdefault("interest_error", f"{type(exc).__name__}: {str(exc)[:200]}")
        log("instantly_interest_failed", run_id=ctx.run_id, contact_id=cid, error=str(exc)[:200])
        return f"not set: {type(exc).__name__}"
    out["marked_interested"] += 1
    return INTERESTED


def _opt_out_addresses(email: Mapping[str, Any], m: Match) -> list[str]:
    """Whoever asked, and the contact too when a colleague asked for them: an opt-out errs on the safe side."""
    out = [outcomes.from_address(email)]
    if not m.from_is_contact and m.contact.get("email"):
        out.append(_lower(m.contact["email"]))
    return [a for a in out if a]


def opt_out_note(info: Mapping[str, Any], *, from_is_contact: bool, by_rule: bool, result: str) -> str:
    """The note in the alert channel when a reply asks to stop (information: the opt-out is done, or retried)."""
    company = _esc(info.get("company") or "their company")
    who = _esc(info.get("contact_name") or "The contact") if from_is_contact else f"Someone else at {company}"
    at = "" if not from_is_contact else f" at {company}"
    spotted = "the stop-word check" if by_rule else "Claude"
    if result == optout.PENDING:
        done = ("On our do-not-contact list now. Instantly's blocklist or HubSpot's opt-out did not take it yet; "
                "the next check for replies (every 15 minutes) tries again.")
    else:
        done = "No more emails: on our do-not-contact list, Instantly's blocklist and HubSpot's opt-out (if HubSpot has them)."
    return say.compose(say.DONE if result != optout.PENDING else say.INFO,
                       f"{who}{at} asked us to stop emailing, so they are opted out",
                       [f"{done} Read as a stop request by {spotted}; they wrote to {_esc(info.get('mailbox'))}. "
                        "Nothing to do."])


def _note(ctx: Context, text: str) -> None:
    """A one-line note in the alert channel (live; nothing when Slack is not set up)."""
    slack = _slack(ctx)
    if slack is None:
        return
    try:
        slack.post(ctx.settings.general.alert_channel, text)
    except (ApiError, LookupError) as exc:
        log("slack_note_failed", run_id=ctx.run_id, error=str(exc)[:200])


def _opt_out(ctx: Context, email: Mapping[str, Any], m: Match, info: Mapping[str, Any], received: datetime,
             done: set[str], out: dict) -> str:
    """Opt the reply's sender (and the contact, when a colleague asked) out everywhere (replies/optout.py), leaving
    this email's own item open; the result is counted in the summary's opted_out."""
    eid = str(info["instantly_email_id"])
    result = optout.opt_out(
        ctx, marker=optout.reply_marker(eid), addresses=_opt_out_addresses(email, m), contact=m.contact,
        account_id=info["account_id"], mailbox=info["mailbox"], occurred_at=received, source=REPLY_SOURCE, done=done,
        keep_item=f"{KIND}:{eid}",
    )
    out["opted_out"][result] += 1
    return result


def record(ctx: Context, d: Directory, emails: list[Mapping[str, Any]], out: dict) -> list[tuple[dict, Match]]:
    """The first pass (the module docstring): each email someone of ours sent us, with who it is from, and a replied
    event (no class) for each new one but an automatic reply. The rest are counted: dropped by why, unmatched.
    Database writes only, so dry-run does it too (SPEC 0.3)."""
    registry = ctx.guard.bounds.registry_addresses
    known = outcomes.event_ids(ctx, outcomes.REPLIED)
    kept: list[tuple[dict, Match]] = []
    rows: dict[str, dict] = {}
    for email in emails:
        eid, mailbox = str(email.get("id") or ""), _lower(email.get("eaccount"))
        if not eid:
            continue
        why = outcomes.received_reason(email, mailbox, registry)
        if why is not None:  # counted (W1-2; code-replies-crm M3): a misread field shows here, not as "no replies"
            out["dropped"][why] += 1
            continue
        m = d.match_reply(email)
        if m is None:
            out["unmatched"] += 1
            continue
        kept.append((dict(email), m))
        if eid in known or eid in rows:
            continue
        if outcomes.is_auto_reply(email):  # recorded once classified (the module docstring)
            out["auto_replies_left_to_classify"] += 1
            continue
        rows[eid] = {"event_id": eid, "contact_id": m.contact["contact_id"], "account_id": m.contact.get("account_id"),
                     "type": outcomes.REPLIED, "mailbox": mailbox, "occurred_at": outcomes.email_time(email) or ctx.now}
    if rows:
        sends = outcomes.sent_events(ctx, {r["contact_id"] for r in rows.values()})
        for r in rows.values():
            r["step"] = outcomes.step_before(sends.get(str(r["contact_id"]), []), utc(r["occurred_at"]))
        ctx.store.upsert("events", list(rows.values()))  # only these columns: a class already set stays
    out["recorded"] = len(rows)
    return kept


def _process(ctx: Context, d: Directory, email: Mapping[str, Any], m: Match, seen: _Seen, done: set[str],
             out: dict) -> None:
    eid, mailbox = str(email.get("id") or ""), _lower(email.get("eaccount"))
    if seen.done(eid):
        out["already_handled"] += 1
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

    # The stop rule first (W1-2, 19 Oct 2026): a stop phrasing is opted out at once, before the model is asked, so
    # the opt-out never waits on the model; the model is still asked, as a warm reply often says "remove me" too.
    stop = classify.asks_to_stop(text)
    result = _opt_out(ctx, email, m, info, received, done, out) if stop else None
    verdict = _classify(ctx, text, received, info, out, stop=stop)
    if verdict is None:
        return
    if verdict.asks_to_stop and result is None:  # the model saw a request to stop the rule did not: the same, now
        result = _opt_out(ctx, email, m, info, received, done, out)
    cls = verdict.reply_class
    sends = outcomes.sent_events(ctx, [str(info["contact_id"])]).get(str(info["contact_id"]), [])
    event = {
        "event_id": eid, "contact_id": info["contact_id"], "account_id": info["account_id"], "type": outcomes.REPLIED,
        "step": outcomes.step_before(sends, received), "mailbox": mailbox, "reply_class": cls,
        "reply_text": text[:STORED_TEXT_CHARS], "language_terms": verdict.language_terms,
        "competitor_named": verdict.competitor_named or None, "occurred_at": received,
    }
    ctx.store.upsert("events", [event])  # the class on record first; an item, if any, after it (the module docstring)
    if cls == "unsubscribe":
        if result is None:  # the model's class alone (no asks_to_stop): opted out here
            result = _opt_out(ctx, email, m, info, received, done, out)
        _note(ctx, opt_out_note(info, from_is_contact=m.from_is_contact, by_rule=verdict.by_rule, result=result))
    elif cls == "out_of_office":
        _record_ooo(ctx, verdict, info, text)
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
        if verdict.asks_to_stop:  # opted out above; a person answers by hand (the desk will not send to them)
            payload.update(also_asked_to_stop=True, opt_out=result, stop_note=ALSO_STOP)
            out["also_asked_to_stop"] += 1
        item = {"item_id": f"{KIND}:{eid}", "kind": KIND, "status": OPEN, "account_id": info["account_id"],
                "contact_id": info["contact_id"], "event_id": eid, "payload": payload, "created_at": ctx.now}
        ctx.store.upsert("hitl_items", [item])  # a run that stopped before this line is done again next run
        if cls in INTERESTED_CLASSES:  # Instantly's interest status, once the class is on record (7 Oct 2026)
            payload["instantly_interest"] = _mark_interested(ctx, info, m, out)
            ctx.store.update("hitl_items", {"item_id": item["item_id"]}, {"payload": payload})
        seen.items[item["item_id"]] = item
        out["items"][cls] += 1
    seen.classified[eid] = cls
    out["classified"][cls] += 1
    if cls != "out_of_office":
        outcomes.mark_engaged(ctx, [info["account_id"]])
    log("reply_routed", run_id=ctx.run_id, email_id=eid, reply_class=cls, classified_by=verdict.source,
        account_id=info["account_id"])


# -- the sweeps (live) --------------------------------------------------------------------------------


def _sweep_opt_outs(ctx: Context, d: Directory, done: set[str], out: dict) -> None:
    """Opt-outs by reply still waiting for the blocklist or HubSpot: the reply is read back by id. A warm reply that
    also asked to stop (its item's also_asked_to_stop) is retried the same way, its item left open."""
    registry = ctx.guard.bounds.registry_addresses
    asked = {str(r.get("event_id")) for r in ctx.store.select("hitl_items", {"kind": KIND})
             if (r.get("payload") or {}).get("also_asked_to_stop")}
    replies = ctx.store.select("events", {"type": outcomes.REPLIED})
    for ev in [e for e in replies if e.get("reply_class") == "unsubscribe" or str(e.get("event_id")) in asked]:
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
                                mailbox=mailbox, occurred_at=utc(ev.get("occurred_at")) or ctx.now,
                                source=REPLY_SOURCE, done=done, keep_item=f"{KIND}:{ev['event_id']}")
        out["opted_out_on_retry"][result] += 1


def _slack(ctx: Context):
    """The Slack client, or None (logged) when no token is set: the item stays open for the escalation email."""
    try:
        return ctx.clients.slack
    except ConfigError:
        log("slack_not_configured", run_id=ctx.run_id, job=JOB)
        return None


def _notes(p: Mapping[str, Any]) -> list[str]:
    """The alert's "Worth knowing" lines: what the reply and the account add, and anything to check."""
    notes = []
    if p.get("also_asked_to_stop"):
        notes.append(f"*{ALSO_STOP}*")
    if p.get("why"):
        notes.append(f"Why we emailed them: {_esc(p['why'])}")
    if p.get("demo_requested"):
        notes.append("They asked for a demo.")
    if p.get("reply_class") == "objection" and p.get("objection") not in (None, "", "none"):
        notes.append(f"Their objection: {_esc(str(p['objection']).replace('_', ' '))}")
    if p.get("competitor_named"):
        notes.append(f"They named a competitor: {_esc(p['competitor_named'])}")
    ref = p.get("referral") or {}
    if ref:
        notes.append("They pointed us to: "
                     + _esc(", ".join(v for v in (ref.get("name"), ref.get("title"), ref.get("email")) if v)))
    if p.get("follow_up_date"):
        d = iso_date(p["follow_up_date"])
        when = f"{d:%a %d %b %Y}" if d else str(p["follow_up_date"])
        notes.append(f"Date to follow up: {when}" + (" (the date they gave)." if p.get("follow_up_source") == "reply"
                                                     else " (they gave none; the draft asks them when)."))
    if p.get("classified_by") == "fallback":
        notes.append(f"Not sorted automatically ({_esc(p.get('classification_error'))}): read the reply yourself.")
    elif p.get("reply_class") == "other" and p.get("model_class") not in (None, "", "other"):
        notes.append(f"Claude was not sure: it read this as {_esc(desk.class_label(p['model_class']))} at "
                     f"{float(p.get('confidence') or 0):.0%}, too unsure to count, so it is marked Unclear.")
    elif p.get("also_asked_to_stop") and float(p.get("confidence") or 0) < classify.MIN_CONFIDENCE:
        # A class kept because they asked to stop is kept at any confidence (classify.from_answer): say when the
        # model was unsure, as it then counts as an engaged company on a weak read (W1-2 review).
        notes.append(f"Claude was not sure: it read this as {_esc(desk.class_label(str(p.get('reply_class'))))} at "
                     f"only {float(p.get('confidence') or 0):.0%}, kept only because they also asked to stop: "
                     f"read the reply yourself.")
    if p.get("sequence_may_continue"):
        notes.append("Instantly may still send them the rest of the email sequence (it took this for an automatic "
                     "reply, or could not match it to the lead): "
                     + ("the system has paused them there." if p.get("sequence_paused")
                        else "*pause this person in their Instantly campaign* so the follow-ups stop."))
    return notes


def alert(payload: Mapping[str, Any], settings: Settings) -> tuple[str, list[dict]]:
    """The SPEC 11 Slack alert for one item: (notification text, Block Kit blocks).

    It reads on its own for someone new (Harry, 12 Oct 2026): who replied, what they said, what the system takes it
    for, the draft, and what each reaction or reply does."""
    p = payload
    cls = str(p.get("reply_class") or "other")
    company = str(p.get("company") or "")
    label, meaning = desk.class_label(cls), desk.class_meaning(cls)
    contact = ", ".join(x for x in (p.get("contact_name"), p.get("title")) if x) or "the contact"
    if p.get("from_is_contact"):
        name = str(p.get("contact_name") or "The contact")
        replier = f"{name} at {company}" if company else name
        who = contact
    else:
        replier = f"Someone else at {company or 'the company'}"
        who = f"someone else at {company or 'the company'}, not the person we emailed ({contact})"
    where = f"{company} ({p['place']})" if p.get("place") else company
    # The tier is no longer in the line: the tiers were retired (W2-2, Harry's D7), and Queue says nothing here.
    account = " · ".join(str(x) for x in (where, p.get("industry")) if x)
    mention = " ".join(f"<@{u}>" for u in settings.general.approver_slack_ids) if cls in WARM_REPLY_CLASSES else ""
    excerpt = str(p.get("reply_excerpt") or "")[:SLACK_EXCERPT_CHARS]
    owner = str(p.get("owner") or "")
    icon = say.DECIDE if p.get("draft") else say.ACTION
    first = [
        f"{icon} *{_esc(replier)} replied: {_esc(label)}*" + (f" {mention}" if mention else ""),
        f"*Who:* {_esc(who)}" + (f" · {_esc(account)}" if account else ""),
        f"*They wrote to:* {_esc(p.get('mailbox'))}" + (f" ({_esc(owner)}'s mailbox)" if owner else ""),
        "*What they said:*",
        "\n".join(f"> {_esc(line)}" for line in (excerpt or "(no text)").split("\n")),
        f"*What the system thinks it is:* {_esc(label)}" + (f": {_esc(meaning)}." if meaning else "."),
    ]
    sections = ["\n".join(first)]
    notes = _notes(p)
    if notes:
        sections.append("\n".join(["*Worth knowing:*", *(f"• {n}" for n in notes)]))
    if p.get("draft"):
        handoff = "; it hands the demo to Harry" if cls == "positive" and owner != settings.general.demo_host else ""
        sections.append(f"*The draft answer* (from {_esc(p.get('mailbox'))}, signed {_esc(owner)}{handoff}):\n"
                        + "\n".join(f"> {_esc(line)}" for line in str(p["draft"]).split("\n")))
    else:
        why = "; ".join(re.sub(r"^no draft:\s*", "", str(x)) for x in (p.get("draft_problems") or [])[:3]) or "none was written"
        sections.append(f"*No draft* ({_esc(why)}): the answer has to be written by hand.")
    sections.append(FOOTER if p.get("draft") else NO_DRAFT_FOOTER)
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": s[:SLACK_SECTION_CHARS]}} for s in sections]
    return _esc(f"{replier} replied: {label}. \"{excerpt[:150]}\""), blocks


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
    out: dict[str, Any] = {k: Counter() for k in ("classified", "items", "opted_out", "opted_out_on_retry", "dropped")}
    out.update({k: 0 for k in ("recorded", "auto_replies_left_to_classify", "unmatched", "already_handled",
                               "retry_later", "claude_cap_reached",
                               "would_classify", "would_opt_out", "alerts_posted", "slack_errors",
                               "left_for_next_run", "marked_interested", "also_asked_to_stop")})
    out["would_spend_usd_at_most"] = 0.0
    seen = _Seen(ctx)
    done = optout.done_markers(ctx.store)
    emails = ctx.clients.instantly.list_emails(registry, start, email_type="received")
    ordered = record(ctx, d, sorted(emails, key=lambda e: outcomes.email_time(e) or ctx.now), out)
    for i, (email, m) in enumerate(ordered):
        if _clock() >= stop:  # the rest wait for the next run, which reads from the first of them
            rest = [e for e, _ in ordered[i:]]
            out["left_for_next_run"] = len(rest)
            first = min((utc(e.get("timestamp_created")) or outcomes.email_time(e) or ctx.now) for e in rest)
            out["resume_from"] = first.isoformat()
            log("poll_replies_time_budget", run_id=ctx.run_id, left=len(rest), resume_from=out["resume_from"])
            break
        _process(ctx, d, email, m, seen, done, out)
    if ctx.live:
        _sweep_opt_outs(ctx, d, done, out)
        _post_pending(ctx, out)
    summary = {"job": JOB, "dry_run": ctx.dry_run, "since": start.isoformat(), "received": len(emails),
               **{k: dict(v) if isinstance(v, Counter) else v for k, v in out.items()}}
    summary["would_spend_usd_at_most"] = round(summary["would_spend_usd_at_most"], 4)
    log("poll_replies_done", run_id=ctx.run_id, **summary)
    return summary
