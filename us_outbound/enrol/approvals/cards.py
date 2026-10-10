"""The card and its thread's messages as Slack Block Kit (9 Oct 2026, split from enrol/approvals.py).

Pure: each function takes a payload and returns text and blocks, so what an approver sees is tested without Slack.
card_copy_level says which copy level the card's emails were written at (relabel.label_unfit reads it).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from us_outbound import labels
from us_outbound.context import Context
from us_outbound.enrol import openers, render
from us_outbound.enrol.approvals.model import (
    FOLLOW_UP_DAYS,
    SECTION_CHARS,
    _day,
    _esc,
    _first,
    _step,
    _text,
    _who,
    is_second,
)
from us_outbound.settings.model import GENERAL_COPY, Settings
from us_outbound.timeparse import iso_date


def opener_label(arm: str, source: str) -> str:
    """What set email 1's opener, for the card's context line."""
    if arm == openers.HOLDOUT:
        return "no opener (held out)"
    if not source or arm == openers.NONE:
        return "no opener"
    if source == openers.FOCUS_SOURCE_NAME:
        return "opener: what they do"
    if source.startswith(openers.GENERIC_OPENER_KEY):
        return "opener: the generic line"
    return f"opener from the signal {source.split(' / ')[0]}"


def test_label(p: Mapping[str, Any]) -> str:
    """The card's test fact (Harry, 7 Oct 2026): "Test: warm-intro · warm intro", the running copy test and the arm
    the contact is in; "Test: warm-intro · not in the test (…)" when the account is left out of a variant test; else
    ""."""
    test_id = _text(p.get("test_id"))
    if test_id:
        return f"Test: {test_id} · {_text(p.get('test_name')) or _text(p.get('copy_version'))}"
    note = _text(p.get("test_note"))
    return f"Test: {note}" if note else ""


def _quoted(text: str) -> list[str]:
    """The text as Slack quote lines, in sections of at most SECTION_CHARS characters."""
    out: list[str] = []
    cur = ""
    for line in str(text or "").split("\n"):
        q = f"> {_esc(line)}"
        if cur and len(cur) + 1 + len(q) > SECTION_CHARS:
            out.append(cur)
            cur = ""
        cur = f"{cur}\n{q}" if cur else q[:SECTION_CHARS]
    if cur:
        out.append(cur)
    return out


def _section(text: str) -> dict:
    return {"type": "section", "text": {"type": "mrkdwn", "text": text[:SECTION_CHARS]}}


def _context(text: str) -> dict:
    return {"type": "context", "elements": [{"type": "mrkdwn", "text": text[:SECTION_CHARS]}]}


def _source_line(p: Mapping[str, Any]) -> str:
    src = p.get("source") or {}
    clay = _text(src.get("email_source")).lower() == "clay"
    if src.get("url"):
        return f"<{src['url']}|Apollo>" + (" · email from Clay" if clay else "")
    if clay:
        return "email from Clay"
    name = _text(src.get("email_source"))
    return {"apollo": "Apollo"}.get(name.lower(), name) or "source not recorded"


def _second_line(p: Mapping[str, Any]) -> str:
    """"*Second contact* at Acme: the first was Jane Doe, Head of People (People leader), email 1 on Tue 27 Oct. ...",
    or "" for a first contact's card."""
    if not is_second(p):
        return ""
    f = p.get("first_contact") or {}
    name = " ".join(x for x in (_text(f.get("first_name")), _text(f.get("last_name"))) if x) or "the first contact"
    title, role = _text(f.get("title")), _text(f.get("role"))
    who = ", ".join(x for x in (_esc(name), _esc(title)) if x) + (f" ({_esc(role)})" if role and role != title else "")
    sent = iso_date(f.get("email_1"))
    when = f", email 1 on {_day(sent)}" if sent else ""
    return (f"*Second contact* at {_esc(p.get('company') or 'this company')}: the first was {who}{when}. Same sender; "
            "both people's emails stop when either replies.")


def _before_line(p: Mapping[str, Any]) -> str:
    b = p.get("before") or {}
    company = _esc(p.get("company") or "this company")
    n = int(b.get("sent") or 0)
    if n:
        last = iso_date(b.get("last_sent"))
        line = f"{n} email{'s' if n != 1 else ''} to {company} from us before" + (f", the last on {_day(last)}" if last else "")
    else:
        line = f"no email to {company} from us before"
    declined = b.get("declined") or []
    if declined:
        who = "; ".join(", ".join(x for x in (_esc(d.get("name")), _esc(d.get("title"))) if x) for d in declined)
        line += f"; the earlier contact ({who}) was declined here, so this is the next one"
    return line


def _copy_words(p: Mapping[str, Any]) -> str:
    """Which copy the card's emails are: "Fintech copy", "the group's copy" or "General copy"."""
    row = _text(p.get("copy_industry"))
    if not row:
        return "copy not recorded"
    if row.casefold() == GENERAL_COPY.casefold():
        return "General copy"
    if row.casefold() == _text(p.get("industry_group")).casefold() and row.casefold() != _text(p.get("industry")).casefold():
        return "the group's copy"
    return f"{row} copy"


def industry_lines(p: Mapping[str, Any]) -> list[str]:
    """The card's Industry line, and its "They do" line (labels.py; Harry, 7 Oct 2026): the label, whether the rules
    and the model agreed, and which copy the emails are; then what the model says the company does, with its quote.
    Every value is escaped; a card posted before the check, or under label_check = skip, says it was not checked."""
    industry, group = _text(p.get("industry")), _text(p.get("industry_group"))
    where = f"{industry} ({group})" if group and group.casefold() != industry.casefold() else (industry or group or "no label")
    lc = p.get("label_check") or {}
    sv = labels.StoredVerdict.from_value(lc)  # the verdict as the card was rendered with it (enrol.Prepared)
    source = _text(p.get("label_source")) or _text(sv.decision.get("source"))
    rules, model, conf = _text(sv.rules) or "no label", _text(sv.verdict.label), _text(sv.verdict.confidence)
    copy = _esc(_copy_words(p))
    if source == labels.OVERRIDE:
        line = f"*Industry:* {_esc(where)} · set on the Overrides tab · {copy}"
    elif source == labels.APPROVER:
        line = f"*Industry:* {_esc(where)} · set by an approver · {copy}"
    elif not lc or source in ("", labels.RULES):
        line = f"*Industry:* ⚠️ {_esc(where)} · not checked by the model · {copy}"
    elif source == labels.AGREED:
        line = f"*Industry:* {_esc(where)} · rules and model agree · {copy}"
    elif source == labels.MODEL:
        line = (f"*Industry:* {_esc(where)} · the rules said {_esc(rules)}; the model says {_esc(model)} ({_esc(conf)}) "
                f"· {copy}")
    elif source == labels.UMBRELLA:
        line = f"*Industry:* {_esc(where)} · the rules said {_esc(rules)}, the model {_esc(model)} ({_esc(conf)}): {copy}"
    else:  # disputed: the rules' label, General copy
        line = (f"*Industry:* ⚠️ {_esc(where)} · the rules say {_esc(rules)}, the model says {_esc(model)} "
                f"({_esc(conf)}) · {copy}")
    out = [line]
    phrase, quote = _text(sv.verdict.what_they_do), _text(sv.verdict.evidence)
    if phrase or quote:
        out.append("*They do:* " + " · ".join(x for x in (_esc(phrase), f"“{_esc(quote)}”" if quote else "") if x))
    return out


def card(p: Mapping[str, Any], status: str = "") -> tuple[str, list[dict]]:
    """The card: (plain-text fallback, Block Kit blocks). status, once decided, replaces the footer."""
    c = p.get("contact") or {}
    name = " ".join(x for x in (_text(c.get("first_name")), _text(c.get("last_name"))) if x) or "the contact"
    company, domain = _text(p.get("company")), _text(p.get("domain"))
    first = _step(p, 1)
    owner = _text(p.get("owner"))
    head = f"*{_esc(company)}* · <https://{domain}|{_esc(domain)}>" if domain else f"*{_esc(company)}*"
    industry = _text(p.get("industry"))
    group = _text(p.get("industry_group"))
    where = f"{industry} ({group})" if group and group.casefold() != industry.casefold() else (industry or group)
    score = p.get("score")
    if isinstance(score, float) and score.is_integer():
        score = int(score)
    facts = [f"{_text(p.get('tier'))} · score {score}" if p.get("tier") else "",
             _text(p.get("angle")), opener_label(_text(p.get("opener_arm")), _text(p.get("opener_source"))), where,
             test_label(p)]
    title = _text(c.get("title"))
    role = _text(p.get("role"))
    to = ", ".join(x for x in (_esc(name), _esc(title)) if x) + (f" ({_esc(role)})" if role and role != title else "")
    boxes = p.get("mailboxes") or ([p["mailbox"]] if p.get("mailbox") else [])
    sender = " or ".join(_esc(b) for b in boxes) + (" (Instantly picks)" if len(boxes) > 1 else "")
    people = "\n".join(x for x in [
        f"*To:* {to} · {_esc(_text((p.get('lead') or {}).get('email')))} · {_source_line(p)}",
        _second_line(p),
        f"*From:* {_esc(owner)}" + (f" · {sender}" if sender else ""),
        f"*Subject:* {_esc(first.get('subject'))}",
    ] if x)
    days = ", ".join(str(d) for d in FOLLOW_UP_DAYS[:-1]) + f" and {FOLLOW_UP_DAYS[-1]}"
    counts = "\n".join([
        f"*Emails:* Email 1 of {len(render.STEPS)} · follow-ups on days {days} (in the thread)",
        f"*Before:* {_before_line(p)}",
        f"*{_esc(_first(owner)) or 'The sender'} today:* card {p.get('slot', '?')} of {p.get('slots', '?')}",
    ])
    blocks = []
    if status:
        blocks.append(_section(status))
    kind = "Send approval · second contact" if is_second(p) else "Send approval"
    blocks += [_section(f"{kind} · {head}"), _context(" · ".join(_esc(f) for f in facts if f)), _section(people)]
    blocks.append(_section("\n".join(industry_lines(p))))  # the label check (labels.py; Harry, 7 Oct 2026)
    blocks += [_section(q) for q in _quoted(first.get("text") or "")]
    blocks.append(_section(counts))
    if status:
        blocks.append(_context(status))
    else:
        blocks.append(_context("✅ send · ❌ don't send. Or reply \"send\" or \"skip\" in the thread. Wrong industry? "
                               "Reply \"industry: <label>\"."))
    text = _esc(f"{'Send approval (second contact)' if is_second(p) else 'Send approval'}: {company} · {name} · "
                f"from {owner}: {first.get('subject') or ''}")
    return (f"{status} {text}" if status else text), blocks  # status is mrkdwn already (it may mention)


def followups(p: Mapping[str, Any]) -> tuple[str, list[dict]]:
    """The card's first thread reply: emails 2 to 4 in full."""
    c = p.get("contact") or {}
    first = _text(c.get("first_name")) or "they"
    days = ", ".join(str(d) for d in FOLLOW_UP_DAYS[:-1]) + f" and {FOLLOW_UP_DAYS[-1]}"
    blocks = [_section(f"Emails 2 to {len(render.STEPS)}, sent on days {days} unless {_esc(first)} replies. "
                       "The ✅ on the card approves all four.")]
    for n, day in zip(render.STEPS[1:], FOLLOW_UP_DAYS):
        st = _step(p, n)
        blocks.append(_section(f"*Email {n} · day {day} · Subject:* {_esc(st.get('subject'))}"))
        blocks += [_section(q) for q in _quoted(st.get("text") or "")]
    return _esc(f"Follow-ups for {p.get('company') or 'this card'}: emails 2 to {len(render.STEPS)}"), blocks


def choices_text(p: Mapping[str, Any], by: str) -> str:
    company = _esc(p.get("company") or "the company")
    c = p.get("contact") or {}
    name = _esc(" ".join(x for x in (_text(c.get("first_name")), _text(c.get("last_name"))) if x) or "this person")
    # A second contact's company is enrolled already: dropping it stops any further contact, not the first's emails.
    drop = (f"drop {company}: no second contact there (the first person's emails carry on)" if is_second(p)
            else f"drop {company}")
    return (f"❌ Not sent ({_who(by)}). What next? React to this message, or reply with the word:\n"
            "✏️ *edit*: change the email (or a follow-up), then approve it again\n"
            f"👤 *contact*: not {name}; you'll get a card for the next person at {company}\n"
            f"🚫 *company*: {drop}\n"
            f"If nothing is chosen, it expires at the end of {_day(iso_date(p.get('expires_on')))} (UK) and "
            f"{company} goes back to the queue.")


def edit_help(ctx: Context, p: Mapping[str, Any], by: str) -> str:
    values = p.get("values") or {}
    first, sender = _esc(values.get("first_name") or "them"), _esc(values.get("sender_first_name") or "the sender")
    st = _step(p, 1)
    copy = f"Subject: {st.get('subject') or ''}\n{st.get('source') or ''}"
    return (f"✏️ Editing ({_who(by)}). Reply in this thread with the new email 1: an optional first line "
            "`Subject: …`, then the body. To change a follow-up, start with `Email 2:` (or `Email 3:`, `Email 4:`). "
            f"Keep the greeting \"Hi {first},\" and the sign-off \"Best wishes,\" then \"{sender}\"; write links as "
            "[anchor text](https://…). The signature is added as before. Each version "
            "is checked against the copy rules and posted back here for a fresh ✅. (Slack can't open an editor "
            "here: this app has no interactive endpoint.)\nEmail 1 as it stands, to copy:\n"
            f"```{_esc(copy)}```")


def version_message(p: Mapping[str, Any], step: int, by: str) -> tuple[str, list[dict]]:
    st = _step(p, step)
    head = (f"✏️ New version of email {step} ({_who(by)}). ✅ this message to send the sequence with it, "
            "❌ if not. The other emails are as before.")
    blocks = [_section(head), _section(f"*Subject:* {_esc(st.get('subject'))}")]
    blocks += [_section(q) for q in _quoted(st.get("text") or "")]
    return _esc(f"New version of email {step} for {p.get('company') or 'this card'}"), blocks


def card_copy_level(p: Mapping[str, Any], settings: Settings) -> str:
    """The copy level the card's emails were written at: General's row, the group's, or the label's own. A card
    whose Copy row is not on the tab any more counts as the label's (the most specific), so it is never kept on a
    guess."""
    row_industry = _text(p.get("copy_industry"))
    if not row_industry:
        row = settings.copy_row(_text(p.get("copy_version")))
        row_industry = row.industry if row else ""
    found = row_industry.casefold()
    if found == GENERAL_COPY.casefold():
        return labels.GENERAL_COPY_LEVEL
    if found and found == _text(p.get("industry_group")).casefold():
        return labels.GROUP_COPY
    return labels.LABEL_COPY
