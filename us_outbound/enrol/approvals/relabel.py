"""Cards a label decision has overtaken, and an approver's industry correction (labels.py; Harry, 7 Oct 2026;
9 Oct 2026, split from enrol/approvals.py).

"industry: Fintech" sets the company's label and withdraws the card; reprepare and repropose post a new card with
the new label's emails in its slot. label_unfit says when a waiting card no longer fits its company's label, and
withdraw_unfit withdraws those cards before any ✅ on them is read.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from typing import Any

from us_outbound import config_version, labels
from us_outbound.clients.db import new_id
from us_outbound.clients.guard import CLI_APPROVER
from us_outbound.clients.http import ApiError
from us_outbound.context import ConfigError, Context
from us_outbound.enrol import enrol
from us_outbound.enrol.approvals.cards import card_copy_level
from us_outbound.enrol.approvals.model import KIND, OPEN, TABLE, Item, _esc, _text, _who, is_second, items
from us_outbound.enrol.approvals.payload import build_payload
from us_outbound.enrol.approvals.checks import eligibility
from us_outbound.enrol.approvals.thread import _thread, post_card
from us_outbound.enrol.approvals.transitions import withdraw
from us_outbound.logs import log
from us_outbound.settings.model import Settings
from us_outbound.timeparse import utc


def reprepare(ctx: Context, item: Item) -> enrol.Prepared | None:
    """The card's company and contact made ready again under the company's label as it is now (Harry, 7 Oct 2026;
    design §7 v2): the enrol run's own eligibility check (eligibility), then enrol.prepare with the card's sender and
    slot, the running test's counts and the sendable Copy rows, stamped with the config version in force. None when it
    cannot be (a second contact's card, a skip of any kind, another sender, HubSpot or Instantly not answering): the
    next enrol proposes the company instead."""
    if is_second(item.payload):
        return None
    account = ctx.store.get("accounts", account_id=item.account_id) if item.account_id else None
    contact = ctx.store.get("contacts", contact_id=item.contact_id) if item.contact_id else None
    if account is None or contact is None or eligibility(ctx, item, account, contact):
        return None
    owner = _text(item.payload.get("owner"))
    try:
        p = enrol.prepare(ctx, enrol.Candidate(dict(account), dict(contact)), Counter({owner: 1}),
                          enrol.running_test_counts(ctx), enrol.sendable_copy(ctx.settings))
    except (ApiError, ConfigError, LookupError) as exc:
        log("send_approval_reprepare_failed", item_id=item.id, error=f"{type(exc).__name__}: {str(exc)[:160]}")
        return None
    if isinstance(p, enrol.Skip) or p.owner != owner:
        log("send_approval_reprepare_skipped", item_id=item.id, reason=getattr(p, "reason", "another sender"))
        return None
    cv = config_version.current(ctx)
    config_version.record(ctx, cv)
    p.config_version, p.code_sha = cv.id, cv.code_sha
    return p


def repropose(ctx: Context, old: Item, p: enrol.Prepared, slack: Any, why: str) -> Item:
    """A new card for the prepared company, in the old card's slot (its sender's place today), its thread saying
    which card it replaces (why: mrkdwn, escaped by the caller). Posted now when Slack answers; else the next
    poll_approvals posts it."""
    payload = build_payload(ctx, p, slot=int(old.payload.get("slot") or 1), slots=int(old.payload.get("slots") or 1))
    payload["replaces"] = old.id
    row = {"item_id": new_id(), "kind": KIND, "account_id": p.account["account_id"],
           "contact_id": p.contact["contact_id"], "slack_channel": "", "slack_ts": "", "status": OPEN,
           "created_at": ctx.now, "payload": payload}
    ctx.store.upsert(TABLE, [row])
    item = Item(row)
    if slack is not None:
        try:
            if post_card(ctx, slack, item):
                _thread(slack, item, f"🏷️ This card replaces the earlier one for {_esc(item.company)}: {why}.")
        except (ApiError, LookupError) as exc:
            log("send_approval_slack_failed", item_id=item.id, error=str(exc)[:200])
    log("send_approval_reproposed", item_id=item.id, replaces=old.id, account_id=item.account_id)
    return item


def set_industry(ctx: Context, item: Item, text: str, *, by: str, via: str, slack: Any) -> dict:
    """"industry: Fintech" from an approver (or `approvals industry`), live: the company takes the label
    (labels.correct: the account, a label_corrected fact, the Overrides tab) and the card, written for the old one,
    is withdrawn; a new card with the new label's emails is posted in its place at once (reprepare, repropose), or,
    when that cannot be, the next enrol proposes the company again. An unknown label, or the card's own, changes
    nothing and says so in the thread."""
    s = ctx.settings
    ind = labels.resolve(text, s)
    wanted = _esc(_text(text)[:60])
    if ind is None:
        _thread(slack, item, f"No Industries label called “{wanted}”. The labels are:\n{_esc(labels.active_list(s))}\n"
                             "Reply `industry: <label>` with one of them (any label on the Industries tab will do).")
        return {"done": False, "why": f"no Industries label called {text!r}"}
    old = _text(item.payload.get("industry"))
    if ind.industry.casefold() == old.casefold():
        _thread(slack, item, f"The card already has {_esc(ind.industry)}.")
        return {"done": False, "why": f"the card already has {ind.industry}"}
    account = ctx.store.get("accounts", account_id=item.account_id) if item.account_id else None
    if account is None:
        _thread(slack, item, "The company is no longer on file, so its industry cannot be set.")
        return {"done": False, "why": "the account is no longer on file"}
    who = _who(by)
    got = labels.correct(ctx, account, ind, by=by, via=via, item_id=item.id, card_label=old,
                         who="at the command line" if by == CLI_APPROVER else f"by {by} in Slack")
    sheet = {"updated": "recorded on the Overrides tab", "added": "added to the Overrides tab"}.get(got["sheet"], got["sheet"])
    company, new = _esc(item.company), _esc(ind.industry)
    p = reprepare(ctx, item) if ind.active else None
    if p is not None:
        after = (f"This card was written for {_esc(old) or 'no label'}, so it is withdrawn: nothing was sent, and a new "
                 f"card for {company} with the {new} emails is posted in the channel. Approve that one.")
    elif ind.active:
        after = (f"This card was written for {_esc(old) or 'no label'}, so it is withdrawn: nothing was sent, and the "
                 f"next enrol (12:00 UK on a send day) proposes {company} again with the {new} emails.")
    else:
        after = (f"This card is withdrawn: nothing was sent. {new} is switched off on the Industries tab, so {company} "
                 "will not be emailed unless it is switched on.")
    note = f"🏷️ Industry set to {new} ({who}); {_esc(sheet)}. {after}"
    reason = f"its industry is {ind.industry} now, set {who}"
    if not withdraw(ctx, item, slack, reason, back_to_queue=ind.active, via="industry", note=note):
        return {"done": False, "why": "another run closed the card first"}
    again = repropose(ctx, item, p, slack, f"written for {_esc(old) or 'no label'}, its industry is {new} now, set "
                                           f"{who}") if p is not None else None
    return {"done": True, "from": old, "to": ind.industry, "sheet": got["sheet"],
            "new_card": again.short_id if again else ""}


# -- cards a label decision has overtaken (labels.py; Harry, 7 Oct 2026) --------------------------------------------


def label_unfit(item: Item, account: Mapping[str, Any] | None, settings: Settings) -> tuple[str, bool] | None:
    """(why the card no longer fits its company's label, whether the company goes back to the queue), or None.

    Only a label decided after the card was rendered counts (accounts.label_checked_at), so a card waiting in Slack
    keeps what it shows until the label check, an approver or `relabel` decides the label again. It no longer fits
    when its company is disqualified, held for the hand-check, under another label, or when its emails are more
    specific than the label now allows (a label's pitch where the group's copy is allowed). A second contact's card
    is left alone: its company is enrolled already."""
    if account is None or is_second(item.payload):
        return None
    decided, created = utc(account.get("label_checked_at")), utc(item.row.get("created_at"))
    if decided is None or created is None or decided < created:
        return None
    p, company = item.payload, item.company
    status = _text(account.get("status"))
    if status == labels.DISQUALIFIED:
        return f"{company} is left out: {_text(account.get('tier_reason')) or 'disqualified'}", False
    if status in ("new", "queued"):
        return "the label check doubts its industry, so it waits for the weekly hand-check", True
    if status != "verified":
        return None
    old, new = _text(p.get("industry")), _text(account.get("industry"))
    if old.casefold() != new.casefold():
        return f"its industry was {old or 'not set'} and is now {new or 'not set'}", True
    used, allowed = card_copy_level(p, settings), labels.copy_level(account)
    if labels.more_specific(used, allowed):
        group = _text(account.get("industry_group")) or _text(p.get("industry_group"))
        now = f"{group}'s copy" if allowed == labels.GROUP_COPY else "General copy"
        return f"its emails were written for {new}, and the label check allows {now} now", True
    return None


def unfit_cards(ctx: Context, accounts: Mapping[str, Mapping[str, Any]] | None = None) -> list[tuple[Item, str, bool]]:
    """The open cards that no longer fit their company's label (label_unfit), with why and whether it goes back to
    the queue. accounts: the companies as they will be (a dry run's decisions); else as the database has them."""
    found = [i for i in items(ctx.store, (OPEN,)) if accounts is None or i.account_id in accounts]
    rows: Mapping[str, Mapping[str, Any]] = accounts if accounts is not None else {
        a["account_id"]: a for a in ctx.store.select("accounts", {"account_id": sorted({i.account_id for i in found})})
    } if found else {}
    out = []
    for item in found:
        unfit = label_unfit(item, rows.get(item.account_id), ctx.settings)
        if unfit is not None:
            out.append((item, *unfit))
    return out


def withdraw_unfit(ctx: Context, slack: Any, found: Sequence[tuple[Item, str, bool]] | None = None) -> list[str]:
    """Withdraw each card a label decision has overtaken (unfit_cards), live: the companies withdrawn. The next
    enrol proposes them again with the right copy (verify_accounts decides at 04:30 and runs dry, so the next
    poll_approvals does this; `relabel` and `labels audit --live` do it themselves)."""
    out = []
    for item, why, back in (found if found is not None else unfit_cards(ctx)):
        if withdraw(ctx, item, slack, why, back_to_queue=back, via=labels.JOB):
            out.append(item.company)
            log("send_approval_withdrawn", item_id=item.id, account_id=item.account_id, reason=why[:200])
    return out
