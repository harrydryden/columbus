"""The card and its thread's messages as Slack Block Kit (9 Oct 2026, split from enrol/approvals.py).

Pure: each function takes a payload and returns text and blocks, so what an approver sees is tested without Slack.
card_copy_level says which copy level the card's emails were written at (relabel.label_unfit reads it).

The words follow base/say.py (Harry, 12 Oct 2026): a newcomer approving emails sees at a glance who the emails go
to, why this company was picked, that the ✅ approves all four emails, and what each reaction does. The emails
themselves are the prospect's and are shown exactly as they will be sent.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from us_outbound import labels
from us_outbound.base import say
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

# The tiers in plain words, as scoring/score.py's TIER_WORDS: why the company was picked.
FIT_WORDS = {"Priority": "Top fit", "Standard": "Good fit", "Control": "Comparison group"}
CONTROL_WHY = "a lower-scoring company, emailed to check the scoring works"
# What the card's reactions do: the footer of every waiting card.
FOOTER = ("✅ send all four emails · ❌ don't send (you then choose: edit it, another person, or drop the company). "
          "Or reply \"send\" or \"skip\" in the thread. Wrong industry? Reply \"industry: <label>\" in the thread.")


def _days() -> str:
    return ", ".join(str(d) for d in FOLLOW_UP_DAYS[:-1]) + f" and {FOLLOW_UP_DAYS[-1]}"


def _name(p: Mapping[str, Any], default: str = "the contact") -> str:
    c = p.get("contact") or {}
    return " ".join(x for x in (_text(c.get("first_name")), _text(c.get("last_name"))) if x) or default


def fit_label(tier: str, score: Any) -> str:
    """Why the company was picked, in plain words: "Good fit (score 34)"; "" without a tier."""
    if not tier:
        return ""
    if isinstance(score, float) and score.is_integer():
        score = int(score)
    scored = f"score {score}" if score not in (None, "") else ""
    words = FIT_WORDS.get(tier, tier)
    if tier == "Control":
        return f"{words} ({', '.join(x for x in (scored, CONTROL_WHY) if x)})"
    return f"{words} ({scored})" if scored else words


def opener_label(arm: str, source: str) -> str:
    """What set email 1's opening line, for the card's "Why this company" line."""
    if arm == openers.HOLDOUT:
        return "no personal opening line (left out on purpose, to compare)"
    if not source or arm == openers.NONE:
        return "no personal opening line"
    if source == openers.FOCUS_SOURCE_NAME:
        return "opening line: what they do"
    if source.startswith(openers.GENERIC_OPENER_KEY):
        return "opening line: the standard one"
    return f"opening line: from the signal “{source.split(' / ')[0]}”"


def test_label(p: Mapping[str, Any]) -> str:
    """The card's test fact (Harry, 7 Oct 2026): "Copy test: warm-intro · version: warm intro", the running copy test
    and the arm the contact is in; "Copy test: warm-intro · not in the test (…)" when the account is left out of a
    variant test; else ""."""
    test_id = _text(p.get("test_id"))
    if test_id:
        return f"Copy test: {test_id} · version: {_text(p.get('test_name')) or _text(p.get('copy_version'))}"
    note = _text(p.get("test_note"))
    return f"Copy test: {note}" if note else ""


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
        return f"found on <{src['url']}|Apollo>" + (" · email from Clay" if clay else "")
    if clay:
        return "email from Clay"
    name = _text(src.get("email_source"))
    name = {"apollo": "Apollo"}.get(name.lower(), name)
    return f"found on {_esc(name)}" if name else "source not recorded"


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
        line = f"yes, {n} email{'s' if n != 1 else ''} to {company}" + (f", the last on {_day(last)}" if last else "")
    else:
        line = f"no, {company} has had no email from us"
    declined = b.get("declined") or []
    if declined:
        who = "; ".join(", ".join(x for x in (_esc(d.get("name")), _esc(d.get("title"))) if x) for d in declined)
        line += f"; the earlier contact ({who}) was declined here, so this is the next one"
    return line


def _copy_words(p: Mapping[str, Any]) -> str:
    """Which emails the card has: "emails written for Fintech", "for the whole … group" or "the general emails"."""
    row = _text(p.get("copy_industry"))
    if not row:
        return "which emails: not recorded"
    if row.casefold() == GENERAL_COPY.casefold():
        return "the general emails (not industry-specific)"
    if row.casefold() == _text(p.get("industry_group")).casefold() and row.casefold() != _text(p.get("industry")).casefold():
        return f"emails written for the whole {row} group"
    return f"emails written for {row}"


def industry_lines(p: Mapping[str, Any]) -> list[str]:
    """The card's Industry line, and its "What they do" line (labels.py; Harry, 7 Oct 2026): the label, how it was
    decided (our keyword rules, the AI check, or a person) and which emails that earns; then what the AI check says the
    company does, with its quote. Every value is escaped; a card posted before the check, or under label_check = skip,
    says it was not checked."""
    industry, group = _text(p.get("industry")), _text(p.get("industry_group"))
    where = f"{industry} ({group})" if group and group.casefold() != industry.casefold() else (industry or group or "no label")
    lc = p.get("label_check") or {}
    sv = labels.StoredVerdict.from_value(lc)  # the verdict as the card was rendered with it (enrol.Prepared)
    source = _text(p.get("label_source")) or _text(sv.decision.get("source"))
    rules, model, conf = _text(sv.rules) or "no label", _text(sv.verdict.label), _text(sv.verdict.confidence)
    copy = _esc(_copy_words(p))
    sure = f"{_esc(conf)} confidence" if conf else "confidence not given"
    if source == labels.OVERRIDE:
        line = f"*Industry:* {_esc(where)} · set by hand on the Overrides tab · {copy}"
    elif source == labels.APPROVER:
        line = f"*Industry:* {_esc(where)} · set by an approver · {copy}"
    elif not lc or source in ("", labels.RULES):
        line = f"*Industry:* ⚠️ {_esc(where)} · from our keyword rules, not yet checked by the AI · {copy}"
    elif source == labels.AGREED:
        line = f"*Industry:* {_esc(where)} · our keyword rules and the AI check agree · {copy}"
    elif source == labels.MODEL:
        line = f"*Industry:* {_esc(where)} · the AI check says {_esc(model)} ({sure}) · {copy}"
    elif source == labels.UMBRELLA:
        line = f"*Industry:* {_esc(where)} · the AI check says {_esc(model)} ({sure}), so {copy}"
    else:  # disputed: the rules' label, General copy
        line = (f"*Industry:* ⚠️ {_esc(where)} · our keyword rules say {_esc(rules)}, the AI check says {_esc(model)} "
                f"({sure}), so {copy}")
    out = [line]
    phrase, quote = _text(sv.verdict.what_they_do), _text(sv.verdict.evidence)
    if phrase or quote:
        out.append("*What they do:* " + " · ".join(x for x in (_esc(phrase), f"“{_esc(quote)}”" if quote else "") if x))
    return out


def card(p: Mapping[str, Any], status: str = "") -> tuple[str, list[dict]]:
    """The card: (plain-text fallback, Block Kit blocks). status, once decided, replaces the footer.

    The fallback text is what a phone notification shows: who, where, from whom, and email 1's subject."""
    c = p.get("contact") or {}
    name = _name(p)
    company, domain = _text(p.get("company")), _text(p.get("domain"))
    first = _step(p, 1)
    owner = _text(p.get("owner"))
    n = len(render.STEPS)
    whom = (f"{_esc(name)}, a second contact at {_esc(company)}" if is_second(p)
            else f"{_esc(name)} at {_esc(company)}")
    head = f"{say.DECIDE} *Approve {n} emails to {whom}*" + (f" · <https://{domain}|{_esc(domain)}>" if domain else "")
    angle = _text(p.get("angle"))
    facts = [fit_label(_text(p.get("tier")), p.get("score")),
             ("pitch: the general one" if angle.casefold() == "general" else f"pitch: {angle}") if angle else "",
             opener_label(_text(p.get("opener_arm")), _text(p.get("opener_source"))), test_label(p)]
    why = " · ".join(_esc(f) for f in facts if f)
    title = _text(c.get("title"))
    role = _text(p.get("role"))
    to = ", ".join(x for x in (_esc(name), _esc(title)) if x) + (f" ({_esc(role)})" if role and role != title else "")
    boxes = p.get("mailboxes") or ([p["mailbox"]] if p.get("mailbox") else [])
    sender = " or ".join(_esc(b) for b in boxes) + (" (Instantly picks one)" if len(boxes) > 1 else "")
    people = "\n".join(x for x in [
        f"*To:* {to} · {_esc(_text((p.get('lead') or {}).get('email')))} · {_source_line(p)}",
        _second_line(p),
        f"*From:* {_esc(owner)}" + (f" · {sender}" if sender else ""),
        f"*Subject:* {_esc(first.get('subject'))}",
    ] if x)
    reader = _esc(_text(c.get("first_name"))) or "they"
    counts = "\n".join([
        f"*You're approving:* all {n} emails: email 1 above, then {n - 1} follow-ups on days {_days()} unless "
        f"{reader} replies (they're in the thread)",
        f"*Emailed before:* {_before_line(p)}",
        f"*{_esc(_first(owner)) or 'The sender'}'s cards today:* this is {p.get('slot', '?')} of {p.get('slots', '?')}",
    ])
    blocks = []
    if status:
        blocks.append(_section(status))
    blocks += [_section(head), _context(f"*Why this company:* {why}" if why else ""), _section(people)]
    blocks.append(_section("\n".join(industry_lines(p))))  # the label check (labels.py; Harry, 7 Oct 2026)
    blocks += [_section(q) for q in _quoted(first.get("text") or "")]
    blocks.append(_section(counts))
    blocks.append(_context(status or FOOTER))
    whom_text = f"{name}, a second contact at {company}" if is_second(p) else f"{name} at {company}"
    text = _esc(f"Approve {n} emails to {whom_text}, from {owner}: “{first.get('subject') or ''}”")
    return (f"{status} {text}" if status else text), blocks  # status is mrkdwn already (it may mention)


def followups(p: Mapping[str, Any]) -> tuple[str, list[dict]]:
    """The card's first thread reply: emails 2 to 4 in full."""
    c = p.get("contact") or {}
    first = _text(c.get("first_name")) or "they"
    n = len(render.STEPS)
    blocks = [_section(f"*Follow-ups: emails 2 to {n}*, sent on days {_days()} unless {_esc(first)} replies. "
                       f"The ✅ on the card approves all {n}. To change one, ❌ the card, then ✏️ edit.")]
    for step, day in zip(render.STEPS[1:], FOLLOW_UP_DAYS):
        st = _step(p, step)
        blocks.append(_section(f"*Email {step} · day {day} · Subject:* {_esc(st.get('subject'))}"))
        blocks += [_section(q) for q in _quoted(st.get("text") or "")]
    return _esc(f"Follow-ups for {p.get('company') or 'this card'}: emails 2 to {n}"), blocks


def choices_text(p: Mapping[str, Any], by: str) -> str:
    company = _esc(p.get("company") or "the company")
    name = _esc(_name(p, "this person"))
    # A second contact's company is enrolled already: dropping it stops any further contact, not the first's emails.
    drop = (f"drop {company}: no second contact there (the first person's emails carry on)" if is_second(p)
            else f"drop {company}: it is never emailed")
    return (f"❌ *Not sent* ({_who(by)}). What next? React to this message, or reply with the word:\n"
            "✏️ *edit*: change the email (or a follow-up), then approve it again\n"
            f"👤 *contact*: not {name}; you'll get a card for the next person at {company}\n"
            f"🚫 *company*: {drop}\n"
            f"If you choose nothing, the card expires at the end of {_day(iso_date(p.get('expires_on')))} (UK) and "
            f"{company} goes back in the queue for a later card.")


def edit_help(ctx: Context, p: Mapping[str, Any], by: str) -> str:
    values = p.get("values") or {}
    first, sender = _esc(values.get("first_name") or "them"), _esc(values.get("sender_first_name") or "the sender")
    st = _step(p, 1)
    copy = f"Subject: {st.get('subject') or ''}\n{st.get('source') or ''}"
    return (f"✏️ *Editing* ({_who(by)}). Reply in this thread with the new version:\n"
            "• email 1: an optional first line `Subject: …`, then the body\n"
            "• a follow-up: start with `Email 2:` (or `Email 3:`, `Email 4:`)\n"
            f"Keep \"Hi {first},\" at the top and \"Best wishes,\" then \"{sender}\" at the end; write links as "
            "[anchor text](https://…). The signature is added for you.\n"
            "Each version is checked against the copy rules and posted back here for a fresh ✅.\n"
            "Email 1 as it stands, to copy:\n"
            f"```{_esc(copy)}```")


def version_message(p: Mapping[str, Any], step: int, by: str) -> tuple[str, list[dict]]:
    st = _step(p, step)
    n = len(render.STEPS)
    head = (f"✏️ *New version of email {step}* ({_who(by)}). ✅ this message to send all {n} emails with it, "
            "❌ if not. The other emails are unchanged.")
    blocks = [_section(head), _section(f"*Subject:* {_esc(st.get('subject'))}")]
    blocks += [_section(q) for q in _quoted(st.get("text") or "")]
    return _esc(f"New version of email {step} for {p.get('company') or 'this card'}: approve it in the thread"), blocks


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
