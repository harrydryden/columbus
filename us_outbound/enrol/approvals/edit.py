"""An approver's edit (9 Oct 2026, split from enrol/approvals.py): ✏️ asks for the new email in the thread, and
each version is rendered by the renderer that renders for sending and checked against every copy rule, then posted
for a fresh ✅, or refused with the rules it breaks.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from us_outbound.context import Context
from us_outbound.enrol import render
from us_outbound.enrol.approvals.cards import edit_help, version_message
from us_outbound.enrol.approvals.model import EDITING, SEED_APPROVE, WAITING, Item, _esc, _save, _step, _text, _who
from us_outbound.enrol.approvals.thread import _seed, _thread, _update_card, parse_edit
from us_outbound.enrol.approvals.transitions import transition
from us_outbound.settings.model import GENERAL_COPY, CopyRow, CopyStep, Mailbox, Settings


def _mailbox(settings: Settings, p: Mapping[str, Any]) -> Mailbox:
    address = _text(p.get("render_mailbox")).lower()
    found = next((m for m in settings.mailboxes if m.address.lower() == address), None)
    if found is not None:
        return found
    owner = _text(p.get("owner"))
    return Mailbox(address=address, domain=address.split("@")[-1], owner_name=owner, status="Active", daily_cap=0)


def _templated(body: str, values: Mapping[str, str], settings: Settings, sender_name: str = "") -> str:
    """The approver's body as copy: the greeting and sign-off back to their variables, a pasted signature
    taken off (render_step adds it), so the copy rules read it as they read the sheet. The signature shows
    one of its lines (Harry, 5 Oct 2026), and any of them, or the sender's name, is taken off."""
    fixed = render.signature_texts(settings, sender_name)
    lines = body.replace("\r\n", "\n").split("\n")
    while lines and (not lines[-1].strip() or lines[-1].strip() in fixed):
        lines.pop()
    nonblank = [i for i, line in enumerate(lines) if line.strip()]
    first, sender = str(values.get("first_name") or ""), str(values.get("sender_first_name") or "")
    if nonblank:
        i = nonblank[0]
        if first and re.fullmatch(rf"Hi\s+{re.escape(first)}\s*,", lines[i].strip(), re.I):
            lines[i] = "Hi {{first_name}},"
        j = nonblank[-1]
        if sender and lines[j].strip().casefold() == sender.casefold():
            lines[j] = "{{sender_first_name}}"
    return "\n".join(lines).strip()


def render_edit(ctx: Context, p: Mapping[str, Any], step: int, subject: str | None,
                body: str | None) -> tuple[render.Rendered, list[str], str]:
    """(the edited email rendered for sending, the copy rules it breaks, its copy source)."""
    s = ctx.settings
    values = dict(p.get("values") or {})
    cur = _step(p, step)
    subject = (cur.get("subject") or "") if subject is None else subject
    mailbox = _mailbox(s, p)
    source = _templated(body if body is not None else str(cur.get("source") or ""), values, s, mailbox.owner_name)
    steps = tuple(CopyStep(subject if n == step else "", source if n == step else "") for n in render.STEPS)
    row = CopyRow(str(p.get("copy_version") or "edited"), _text(p.get("industry")) or GENERAL_COPY, "approved", steps)
    r = render.render_step(row, values, step=step, mailbox=mailbox, settings=s, for_send=False)
    names = {"{{first_name}}": str(values.get("first_name") or "the first name"),
             "{{sender_first_name}}": str(values.get("sender_first_name") or "the sender's first name")}
    problems = []
    for v in render.violations([r]):
        for var, value in names.items():
            v = v.replace(var, value)
        problems.append(v)
    return r, problems, source


def start_edit(ctx: Context, item: Item, *, by: str, slack: Any) -> None:
    """✏️: how to edit, with email 1 as it stands, ready to copy."""
    item.payload.pop("held", None)  # an edited version needs its own ✅
    item.payload["edit_ts"] = _thread(slack, item, edit_help(ctx, item.payload, by))
    transition(ctx, item, EDITING)


def apply_edit(ctx: Context, item: Item, text: str, *, by: str, slack: Any) -> bool:
    """An edit reply: rendered and checked; refused in the thread, or posted as the new version for a fresh ✅."""
    p = item.payload
    step, subject, body = parse_edit(text)
    edits = list(p.get("edits") or [])
    if subject is None and body is None:
        _thread(slack, item, f"I couldn't find a new email {step} in that reply: give `Subject: …` and/or the body.")
        return False
    r, problems, source = render_edit(ctx, p, step, subject, body)
    if problems:
        edits.append({"step": step, "by": by, "at": ctx.now.isoformat(), "accepted": False, "problems": problems[:5]})
        p["edits"] = edits
        _save(ctx, item)
        _thread(slack, item, f"That version of email {step} breaks the copy rules, so it is not used: "
                             + _esc("; ".join(problems[:8])) + ". Reply with another version.")
        return False
    lead = dict(p.get("lead") or {})
    variables = dict(lead.get("custom_variables") or {})
    if not p.get("edited"):
        p["original"] = dict(variables)
    variables[f"s{step}_subject"], variables[f"s{step}_body"] = r.subject, r.body
    lead["custom_variables"] = variables
    steps = [dict(s) for s in p.get("steps") or []]
    steps = [s for s in steps if s.get("step") != step] + [{"step": step, "subject": r.subject, "text": r.text,
                                                           "source": source}]
    edits.append({"step": step, "by": by, "at": ctx.now.isoformat(), "accepted": True})
    p.pop("held", None)  # an approval of the earlier version does not carry over
    p.update(lead=lead, steps=sorted(steps, key=lambda s: s.get("step") or 0), edited=True, edits=edits)
    text_, blocks = version_message(p, step, by)
    note = _thread(slack, item, text_, blocks)
    _seed(slack, item.channel, note, SEED_APPROVE)
    p["approve_ts"] = note
    transition(ctx, item, WAITING)
    _update_card(ctx, slack, item, f"✏️ Edited ({_who(by)}): approve the new version in the thread")
    return True
