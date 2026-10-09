"""The send-approval pass of poll_approvals (9 Oct 2026, split from enrol/approvals.py; replies/desk.py calls poll).

Each run posts any card enrol could not, withdraws the cards a label decision has overtaken, then reads each open
item: its thread replies in order, then the reactions on the message whose ✅ counts now. An item left sending is
looked up (_stuck), and one past its last day expires. Only approvers count; the bot's own reactions never do.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from us_outbound.clients.guard import CLI_APPROVER
from us_outbound.clients.http import ApiError
from us_outbound.context import ConfigError, Context
from us_outbound.enrol import enrol, plan
from us_outbound.enrol.approvals.add import _added, _plan_full, _refused, send
from us_outbound.enrol.approvals.edit import apply_edit, start_edit
from us_outbound.enrol.approvals.model import (
    BLOCKED,
    COMPANY_REACTIONS,
    CONTACT_REACTIONS,
    EDIT_REACTIONS,
    EDITING,
    EXPIRED,
    HOLD_PLAN,
    LIST_LIMIT,
    OPEN,
    REJECT_REACTIONS,
    REJECTED,
    SEED_APPROVE,
    SENDING,
    STUCK_AFTER,
    SYSTEM,
    WAITING,
    Item,
    _esc,
    _key,
    _save,
    _text,
    is_expired,
    items,
)
from us_outbound.enrol.approvals.relabel import set_industry, unfit_cards, withdraw_unfit
from us_outbound.enrol.approvals.thread import Command, _seed, _thread, parse_command, post_card, post_followups
from us_outbound.enrol.approvals.transitions import _hold, drop_company, drop_contact, expire, reject, transition
from us_outbound.logs import log
from us_outbound.replies.desk import APPROVE_REACTIONS
from us_outbound.timeparse import utc


@dataclass
class _Run:
    outcomes: Counter[str] = field(default_factory=Counter)
    rejected: list[str] = field(default_factory=list)
    editing: list[str] = field(default_factory=list)
    edits: Counter[str] = field(default_factory=Counter)  # accepted, refused
    not_added: list[dict] = field(default_factory=list)
    held: list[dict] = field(default_factory=list)  # approvals a hold stopped this run; they stay valid
    cards_posted: int = 0
    relabelled: list[dict] = field(default_factory=list)  # approvers' industry corrections (labels.py)
    unsure: list[str] = field(default_factory=list)
    would: list[dict] = field(default_factory=list)
    ignored: int = 0
    errors: list[str] = field(default_factory=list)
    plan_full: bool = False  # Instantly's plan had no room at an add this run: the rest are held without one

    def add(self, name: str, entry: Any) -> None:
        bucket = getattr(self, name)
        if len(bucket) < LIST_LIMIT:
            bucket.append(entry)


def _act(ctx: Context, item: Item, cmd: Command, by: str, via: str, slack: Any, run: _Run) -> bool:
    """Carry out one decision (live). True when nothing more is read for the item this run."""
    state = item.state
    if cmd.kind == "send":
        if run.plan_full:  # Instantly's plan was full earlier this run: held without another add (enrol/plan.py)
            _hold(ctx, item, {HOLD_PLAN: plan.HOLD}, by=by, via=via, slack=slack)
            run.add("held", {"item": item.short_id, "why": [plan.HOLD]})
            return True
        res = send(ctx, item, by=by, via=via, slack=slack)
        run.plan_full = run.plan_full or bool(res.get("plan_full"))
        if res.get("added"):
            run.outcomes[res["outcome"]] += 1
        elif res.get("outcome") == BLOCKED:
            run.outcomes[BLOCKED] += 1
        elif res.get("held"):
            run.add("held", {"item": item.short_id, "why": res["held"]})
        else:
            run.add("not_added", res)
        return True
    if cmd.kind == "reject":
        if state == WAITING:
            reject(ctx, item, by=by, via=via, slack=slack)
            run.add("rejected", item.short_id)
        return False
    if cmd.kind == "edit":
        if state != EDITING:
            start_edit(ctx, item, by=by, slack=slack)
            run.add("editing", item.short_id)
        return False
    if cmd.kind == "edit_text":
        run.edits["accepted" if apply_edit(ctx, item, cmd.text, by=by, slack=slack) else "refused"] += 1
        return False
    if cmd.kind == "industry":  # in any state: the card closes once the label is set (Harry, 7 Oct 2026)
        res = set_industry(ctx, item, cmd.text, by=by, via=via, slack=slack)
        if res.get("done"):
            run.add("relabelled", {"item": item.short_id, "from": res["from"], "to": res["to"], "sheet": res["sheet"],
                                   "new_card": res.get("new_card", "")})
        return bool(res.get("done"))
    fn = drop_contact if cmd.kind == "contact" else drop_company
    res = fn(ctx, item, by=by, via=via, slack=slack)
    if res.get("done"):
        run.outcomes[res["outcome"]] += 1
    return True


def _first_by(reactions: Sequence[Mapping[str, Any]], names: frozenset[str], approvers: frozenset[str],
              bot: str, run: _Run) -> str:
    """The first approver with one of these reactions, else ""; others (not the bot) are counted as ignored."""
    for r in reactions:
        if r.get("name") not in names:
            continue
        for u in r.get("users") or ():
            if bot and u == bot:
                continue  # its own seeded ✅ and ❌, even if its id were on approver_slack_ids
            if u in approvers:
                return str(u)
            run.ignored += 1
    return ""


def _reaction_decision(item: Item, slack: Any, approvers: frozenset[str], bot: str, run: _Run) -> Command | None:
    """What the reactions say now: ❌ / ✅ on the message that counts, or a choice on the choices message."""
    p = item.payload
    if item.state == WAITING and p.get("approve_ts"):
        rs = slack.reactions(item.channel, p["approve_ts"])
        no, yes = _first_by(rs, REJECT_REACTIONS, approvers, bot, run), _first_by(rs, APPROVE_REACTIONS, approvers, bot, run)
        if no:  # ❌ beats ✅
            return Command("reject", no)
        if yes:
            return Command("send", yes)
    elif item.state == REJECTED and p.get("choices_ts"):
        rs = slack.reactions(item.channel, p["choices_ts"])
        for names, kind in ((EDIT_REACTIONS, "edit"), (CONTACT_REACTIONS, "contact"), (COMPANY_REACTIONS, "company")):
            by = _first_by(rs, names, approvers, bot, run)
            if by:  # the least drastic choice wins
                return Command(kind, by)
    return None


def _held_approval(item: Item) -> tuple[str, str] | None:
    """(who approved, how) of an approval a hold stopped, while the card still waits; else None."""
    held = item.payload.get("held") or {}
    if item.status != OPEN or item.state != WAITING or not held.get("by"):
        return None
    return _text(held.get("by")), _text(held.get("via")) or "held"


def _retry(item: Item, approvers: frozenset[str]) -> tuple[Command | None, str]:
    """(send, how it was approved) for an approval a hold stopped, by someone who may still approve; else (None, "")."""
    held = _held_approval(item)
    if held is None or (held[0] not in approvers and held[0] != CLI_APPROVER):
        return None, ""
    return Command("send", held[0]), held[1]


def _work(ctx: Context, item: Item, slack: Any, bot: str, run: _Run) -> None:
    approvers = frozenset(x.strip() for x in ctx.settings.general.approver_slack_ids if x.strip())
    p = item.payload
    seen = before = str(p.get("seen_ts") or "")
    replies = sorted(slack.replies(item.channel, item.ts), key=lambda m: _key(m.get("ts")))
    for m in replies:
        if _key(m.get("ts")) <= _key(seen):
            continue
        seen = str(m.get("ts"))
        user = str(m.get("user") or "")
        if m.get("bot_id") or m.get("subtype") == "bot_message" or (bot and user == bot):
            continue  # the bot's own notes
        cmd = parse_command(m.get("text"), item.state)
        if cmd is None:
            continue
        if user not in approvers:
            run.ignored += 1
            continue
        if ctx.dry_run:
            run.add("would", {"item": item.short_id, "action": cmd.kind, "by": user, "via": "thread"})
            return
        item.payload["seen_ts"] = seen
        if _act(ctx, item, cmd, user, "thread", slack, run):
            return
        if item.status != OPEN:
            return
    if ctx.live and seen != before:  # the newest reply read, so it is not read again
        item.payload["seen_ts"] = seen
        _save(ctx, item)
    if item.status != OPEN:
        return
    decision = _reaction_decision(item, slack, approvers, bot, run)
    if decision is not None:
        via = {"send": "✅", "reject": "❌", "edit": "✏️", "contact": "👤", "company": "🚫"}[decision.kind]
    else:  # an approval a hold stopped (a "send" reply, or the command line) is tried again
        decision, via = _retry(item, approvers)
        if decision is None:
            return
    if ctx.dry_run:
        run.add("would", {"item": item.short_id, "action": decision.kind, "by": decision.text, "via": via})
        return
    _act(ctx, item, Command(decision.kind), decision.text, via, slack, run)


def _stuck(ctx: Context, item: Item, slack: Any, run: _Run) -> None:
    """An item left "sending" by a run that stopped, or whose lead Instantly did not confirm: the campaign is
    looked up (enrol.campaign_lead_ids). A lead found there is recorded and the item closed approved, by whoever
    approved it. One not there, after Instantly answered the add (sending.answered), was refused (_refused);
    otherwise it goes back to waiting for a person's fresh ✅: it is never added again alone, as the add may
    never have reached Instantly. If the campaign cannot be read, a person is asked to check."""
    p = item.payload
    sending = dict(p.get("sending") or {})
    started = utc(sending.get("at"))
    if started is not None and ctx.now - started < STUCK_AFTER:
        return  # an add may still be going
    if ctx.dry_run:
        run.add("unsure", item.short_id)
        return
    campaign, email = _text(p.get("campaign")), item.email.lower()
    try:
        found: dict[str, str] | None = enrol.campaign_lead_ids(ctx, campaign, [email]) if campaign and email else {}
    except (ApiError, LookupError, ConfigError) as exc:
        found = None
        log("send_approval_lookup_failed", item_id=item.id, error=str(exc)[:200])
    by, via = _text(sending.get("by")) or SYSTEM, _text(sending.get("via"))
    if found and found.get(email):
        res = _added(ctx, item, found[email], by=by, via=via, slack=slack)
        run.outcomes[res["outcome"]] += 1
        log("send_approval_found", item_id=item.id)
        return
    if found is not None and sending.get("answered") and sending.get("plan_full"):  # no room then: held, kept
        _plan_full(ctx, item, by=by, via=via, slack=slack, alert=None)
        run.add("held", {"item": item.short_id, "why": [plan.HOLD]})
        return
    if found is not None and sending.get("answered"):  # Instantly had the add and did not take it: refused
        run.outcomes[_refused(ctx, item, by=by, via=via, slack=slack)["outcome"]] += 1
        return
    if found is None:
        run.add("unsure", item.short_id)
        text = (f"I can't tell whether {_esc(item.email)} was added to {_esc(campaign)} (the run adding it stopped, "
                "and Instantly could not be read). Check the campaign in Instantly; if the lead is not there, ✅ this "
                "message or reply \"send\" to add it.")
    else:
        run.add("not_added", {"item": item.short_id, "why": ["the run adding it stopped before Instantly had it"]})
        text = (f"{_esc(item.email)} is not in {_esc(campaign)}: the run adding it stopped before Instantly had it, so "
                "nothing was sent. ✅ this message or reply \"send\" to add it.")
    note = _thread(slack, item, text)
    _seed(slack, item.channel, note, SEED_APPROVE[:1])
    p.update(approve_ts=note, unsure=p.pop("sending", None))
    transition(ctx, item, WAITING)
    log("send_approval_unsure", item_id=item.id, looked_up=found is not None)


def _post_missing(ctx: Context, slack: Any, run: _Run) -> None:
    """Live: a card that could not be posted when enrol ran, or its follow-ups thread, is posted now."""
    for item in items(ctx.store, (OPEN,)):
        if is_expired(item.payload, ctx.today_uk()):
            continue
        try:
            if not item.ts:
                if post_card(ctx, slack, item):
                    run.cards_posted += 1
            elif not item.payload.get("followups_ts"):
                post_followups(ctx, slack, item)
        except (ApiError, LookupError) as exc:
            run.errors.append(f"{item.short_id}: the card was not posted ({str(exc)[:160]})")


def poll(ctx: Context, slack: Any | None) -> dict:
    """The send-approval pass of poll_approvals (replies/desk.py calls it): decisions, expiry, stuck adds."""
    run = _Run()
    if ctx.live and slack is not None:
        _post_missing(ctx, slack, run)
    # Cards a label decision has overtaken go first, so no ✅ sends one (labels.py; Harry, 7 Oct 2026).
    overtaken = unfit_cards(ctx)
    if ctx.dry_run:
        for item, why, _ in overtaken:
            run.add("would", {"item": item.short_id, "action": "withdraw", "why": why})
        withdrawn: list[str] = []
    else:
        withdrawn = withdraw_unfit(ctx, slack, overtaken)
        if withdrawn:
            run.outcomes["withdrawn"] += len(withdrawn)
    today = ctx.today_uk()
    todo = items(ctx.store)
    bot: str | None = None  # the bot's own user id, asked once when an item is read
    for item in todo:
        try:
            if item.status == SENDING:
                _stuck(ctx, item, slack, run)
            elif is_expired(item.payload, today):
                if ctx.dry_run:
                    run.add("would", {"item": item.short_id, "action": "expire"})
                elif expire(ctx, item, slack):
                    run.outcomes[EXPIRED] += 1
            elif slack is not None and item.ts:
                if bot is None:
                    bot = slack.bot_user_id()
                _work(ctx, item, slack, bot, run)
            else:  # no Slack: an approval a hold stopped still goes through once it clears
                approvers = frozenset(x.strip() for x in ctx.settings.general.approver_slack_ids if x.strip())
                cmd, via = _retry(item, approvers)
                if cmd is not None and ctx.dry_run:
                    run.add("would", {"item": item.short_id, "action": cmd.kind, "by": cmd.text, "via": via})
                elif cmd is not None:
                    _act(ctx, item, Command(cmd.kind), cmd.text, via, slack, run)
        except (ApiError, LookupError) as exc:  # one item's trouble never stops the others
            run.errors.append(f"{item.short_id}: {type(exc).__name__}: {str(exc)[:160]}")
    out: dict[str, Any] = {
        "items": len(todo), "outcomes": dict(run.outcomes), "rejected": run.rejected, "editing": run.editing,
        "edits": dict(run.edits), "not_added": run.not_added, "held": run.held, "cards_posted": run.cards_posted,
        "unsure": run.unsure, "instantly_plan_full": run.plan_full,
        "ignored_non_approvers": run.ignored, "errors": run.errors, "withdrawn": withdrawn[:LIST_LIMIT],
        "relabelled": run.relabelled,
    }
    if ctx.dry_run:
        out["would"] = run.would
    return out
