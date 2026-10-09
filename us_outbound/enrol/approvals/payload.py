"""What a send approval holds (9 Oct 2026, split from enrol/approvals.py): the payload enrol's prepared account
becomes, the editable source of each email, where the contact came from and the company's history with us.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from us_outbound import facts, labels
from us_outbound.context import UK, Context
from us_outbound.copy import copy_markup
from us_outbound.enrol import enrol, queue, render
from us_outbound.enrol.approvals.model import (
    APOLLO_PERSON_URL,
    CONTACT_REJECTED,
    HANDLED,
    KIND,
    PICK_FACT,
    PICK_SOURCE,
    TABLE,
    WAITING,
    Item,
    _text,
    expires_on,
)
from us_outbound.timeparse import utc


def editable(body: str, values: Mapping[str, str]) -> str:
    """A Copy-tab body with the lead's values filled in and the markup kept: what an approver edits."""
    source = copy_markup.drop_empty_optional(body, values, render.OPTIONAL_VARIABLES)

    def fill(m: re.Match[str]) -> str:
        name = m.group(1)
        return " ".join(str(values.get(name) or "").split()) if name in values else m.group(0)

    return re.sub(r"\n\s*\n(?:\s*\n)+", "\n\n", copy_markup.VARIABLE.sub(fill, source)).strip()


def recipient_source(ctx: Context, account_id: str, contact: Mapping[str, Any]) -> dict:
    """Where the contact came from: the account's latest contact_pick fact for them (contacts/pick.py)."""
    cid = _text(contact.get("contact_id"))
    e = facts.newest(ctx.store.select("signal_events", {"account_id": account_id, "source": PICK_SOURCE,
                                                        "fact": PICK_FACT}),
                     where=lambda e: isinstance(e.get("value"), Mapping) and _text(e["value"].get("contact_id")) == cid)
    value = dict(e["value"]) if e else {}
    person = _text(value.get("apollo_person_id"))
    return {"email_source": _text(value.get("email_source")) or _text(contact.get("email_source")),
            "apollo_person_id": person, "url": APOLLO_PERSON_URL.format(person) if person else ""}


def history(ctx: Context, account_id: str) -> dict:
    """What the company has had from us before: emails sent, and earlier contacts declined here."""
    sent = [e for e in ctx.store.select("events", {"account_id": account_id, "type": "sent"})]
    last = max((t for t in (utc(e.get("occurred_at")) for e in sent) if t), default=None)
    declined = []
    for row in ctx.store.select(TABLE, {"kind": KIND, "account_id": account_id, "status": HANDLED}):
        item = Item(row)
        if item.payload.get("outcome") == CONTACT_REJECTED:
            c = item.payload.get("contact") or {}
            declined.append({"name": item.person, "title": _text(c.get("title")),
                             "at": _text((item.payload.get("decided") or {}).get("at"))[:10]})
    return {"sent": len(sent), "last_sent": last.astimezone(UK).date().isoformat() if last else "", "declined": declined}


def build_payload(ctx: Context, p: enrol.Prepared, *, slot: int, slots: int) -> dict:
    """The item's payload: the data contract's keys, and what the card and an edit need."""
    s = ctx.settings
    a, c = p.account, p.contact
    row = s.copy_row(p.copy_version)
    steps = []
    for r in sorted(p.rendered, key=lambda r: r.step):
        # As written: the Copy row's, or a variant test's arm in its place (enrol/variants.py), so an edit starts there.
        written = p.written.get(r.step) or (row.step(r.step) if row else None)
        source = editable(written.body, p.values) if written else r.text
        steps.append({"step": r.step, "subject": r.subject, "text": r.text, "source": source})
    send_day = ctx.today_uk()
    group = s.industry_group_of(a)
    test = s.running_test()
    return {
        # The contract (the daily report reads these).
        "state": WAITING, "outcome": "", "owner": p.owner, "mailbox": p.mailbox,
        "campaign": queue.campaign_name(p.owner), "lead": dict(p.lead), "copy_version": p.copy_version,
        "angle": p.angle, "test_id": p.test_id, "opener_arm": p.opener_arm, "opener_source": p.opener_source,
        "subject_arm": p.subject_arm,
        # The running copy test (Harry, 7 Oct 2026; enrol/variants.py): the contact's arm and its name, or why the
        # account is not in the test. ✅ records test_id and test_arm on the contact, an edited card's included.
        "test_arm": p.test_arm, "test_name": test.arm_name(p.test_arm) if test and p.test_arm else "",
        "test_note": f"{test.test_id} · not in the test ({p.test_note})" if test and p.test_note else "",
        # What the card was rendered under (config_version.py; Harry, 7 Oct 2026): ✅ stamps these, not today's.
        "config_version": p.config_version, "code_sha": p.code_sha, "copy_hash": p.copy_hash,
        "industry": _text(a.get("industry")), "industry_group": group, "role": _text(c.get("role")),
        "tier": _text(a.get("tier")), "score": a.get("score"), "send_day": send_day.isoformat(),
        "edited": False, "original": {}, "reason": "",
        # This module's own.
        "expires_on": expires_on(send_day, s).isoformat(),
        "company": _text(a.get("clean_name")) or _text(a.get("domain")), "domain": _text(a.get("domain")).lower(),
        "contact": {"first_name": _text(c.get("first_name")), "last_name": _text(c.get("last_name")),
                    "title": _text(c.get("title"))},
        "source": recipient_source(ctx, str(a["account_id"]), c),
        "mailboxes": [m.address for m in s.mailboxes_for(p.owner, "Active")],
        "render_mailbox": p.render_mailbox, "values": dict(p.values), "steps": steps,
        "slot": slot, "slots": slots, "before": history(ctx, str(a["account_id"])),
        "approve_ts": "", "seen_ts": "", "choices_ts": "", "edit_ts": "", "followups_ts": "",
        "decided": {}, "added": {}, "edits": [],
        # A second contact at the account (enrol/second.py; Harry, 6 Oct 2026): 2, and who the first was.
        "contact_slot": p.slot, "first_contact": dict(p.first or {}),
        # The label check (labels.py; Harry, 7 Oct 2026): its latest verdict, where the label came from, the copy level
        # it earns and the Copy row's own industry, for the card's Industry line and for unfit_cards.
        "label_check": dict(p.label_check), "label_source": _text(a.get("label_source")),
        "copy_level": p.copy_level or labels.copy_level(a), "copy_industry": row.industry if row else "",
    }
