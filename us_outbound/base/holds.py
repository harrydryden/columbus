"""What the kill rules hold back right now (SPEC 12 "Kill rules"), read by every job that sends.

kill_rules (learn/kill_rules.py) records each rule that fires as a hitl_items row, kind
"kill_rule", status "open". While it is open (or escalated) it holds, in any mode, as the
operator stop does (the safe direction):
  * pause_mailbox  the mailboxes in payload.mailboxes count as Paused, from the moment the
                   rule fires, whatever the settings in the database still say. kill_rules
                   also pauses them on the Mailboxes tab and their campaign's sending list
                   (registry pause path), but the jobs read the sheet only after the next
                   settings_sync; without the hold, a campaign fix or an enrol run before it
                   could send from the mailbox again. bootstrap.build_context applies it;
  * pause_source   contacts whose email_source is payload.target are not enrolled;
  * stop_group     accounts in the industry group payload.target are not enrolled;
  * pause_enrolment  no new enrolment at all (the stop rule).
Harry lifts a hold with `us-outbound killrules clear ITEM_ID --live`, after checking; a
14-day mailbox pause ends by itself (kill_rules marks it handled). A mailbox stays Paused
on the Mailboxes tab until Harry sets it Active there.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from typing import Any

from us_outbound.clients.db import Store
from us_outbound.settings.model import Settings

KIND = "kill_rule"
WAITING = ("open", "escalated")
PAUSE_MAILBOX, PAUSE_SOURCE, STOP_GROUP, PAUSE_ENROLMENT = "pause_mailbox", "pause_source", "stop_group", "pause_enrolment"
PAUSED = "Paused"


def _payload(item: Mapping[str, Any]) -> Mapping[str, Any]:
    p = item.get("payload")
    return p if isinstance(p, Mapping) else {}


def in_force(store: Store) -> list[dict]:
    """The kill-rule items holding something now: open or escalated."""
    return [r for r in store.select("hitl_items", {"kind": KIND, "status": list(WAITING)})]


def _with_action(store: Store, action: str) -> list[dict]:
    return [r for r in in_force(store) if _payload(r).get("action") == action]


def held_mailboxes(store: Store) -> dict[str, str]:
    """lower-case address -> why a kill rule holds it."""
    out: dict[str, str] = {}
    for r in _with_action(store, PAUSE_MAILBOX):
        p = _payload(r)
        for a in p.get("mailboxes") or ():
            out.setdefault(str(a).strip().lower(), str(p.get("reason") or p.get("rule") or "a kill rule"))
    return out


def paused_sources(store: Store) -> dict[str, str]:
    """email_source (apollo, clay) -> why a kill rule pauses it. pick_contacts should not use these either."""
    return {str(_payload(r).get("target") or "").strip().lower(): str(_payload(r).get("reason") or "")
            for r in _with_action(store, PAUSE_SOURCE)}


def stopped_groups(store: Store) -> dict[str, str]:
    """industry group (casefolded) -> why a kill rule stopped enrolling it."""
    return {str(_payload(r).get("target") or "").strip().casefold(): str(_payload(r).get("reason") or "")
            for r in _with_action(store, STOP_GROUP)}


def enrolment_stop(store: Store) -> str | None:
    """Why the stop rule pauses new enrolment, or None."""
    items = _with_action(store, PAUSE_ENROLMENT)
    if not items:
        return None
    reasons = "; ".join(str(_payload(r).get("reason") or _payload(r).get("rule")) for r in items)
    return (f"enrollment is paused by the stop rule ({reasons}); after the profile and copy review, "
            f"`us-outbound killrules clear <item> --live` resumes it")


def with_holds(store: Store, settings: Settings) -> Settings:
    """settings with every mailbox a kill rule holds shown as Paused."""
    held = held_mailboxes(store)
    if not held or not any(m.address.lower() in held and m.status != PAUSED for m in settings.mailboxes):
        return settings
    boxes = tuple(
        dataclasses.replace(m, status=PAUSED) if m.address.lower() in held and m.status not in (PAUSED, "Retired") else m
        for m in settings.mailboxes
    )
    return dataclasses.replace(settings, mailboxes=boxes)
