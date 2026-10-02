"""The copy desk: drafting, checking and approving the Copy tab (Harry, 30 Sep 2026).

Copy reaches a prospect only through every one of these gates, in order:

  1. Draft. Claude's writing model (claude_model, Opus) drafts a row from templates/copy/style.md,
     facts.md and the industry's page (`us-outbound copy draft`), or a person writes one.
  2. Sheet check (check_row, `us-outbound copy check`). Every email of the row is rendered
     for each contacted role, sent by the demo host and by another sender, with sample values
     and with the longest ones, and run through every copy rule. Free, and repeatable.
  3. QA (qa_row, `us-outbound copy qa`). A row with no sheet-check problems goes to Claude's
     task model (claude_task_model, Sonnet), which reads it against facts.md, the style and
     the industry's page: claims nobody has checked, wrong or missing data, tone, US grammar,
     formatting. Its verdict is written to the row's qa cell with the copy's check code
     ("pass 1a2b3c4d"), and its notes to qa_notes. Editing the copy changes the code, so an
     edited row has to pass again.
  4. Approval. Harry sets status approved and approved_by. Only approved rows that passed QA
     in their current wording are sent (enrol.sendable_copy).
  5. Render-time check. The enrol job renders every email for the lead it is going to and
     runs the rules again (render.render_step); any violation skips the lead.

preview() renders a row as one prospect would see it, for Harry to read before approving: the sample
prospect with a real opener line from the Signals tab filled with sample facts (sample_opener), or a
stored account with the opener enrol would give its contact (`copy preview --account DOMAIN`).
check_openers() is the sheet check of the opener lines (enrol/openers.py): the Signals tab's, and the General
tab's generic and focus lines, each filled with sample facts and run through the rules an opener must pass,
no funding or money among them; `copy check` reports it too.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from us_outbound.enrol import copy_rules, render
from us_outbound.settings.model import (
    COPY_STEPS,
    GENERAL_COPY,
    ROLE_LINE_COLUMNS,
    CopyRow,
    Industry,
    Mailbox,
    Settings,
)

# A made-up prospect for previews and the sheet check.
SAMPLE_ACCOUNT = {"account_id": "sample", "domain": "harborfinch.com", "clean_name": "Harbor & Finch",
                  "hq_city": "Boston", "hq_state": "MA", "employees": 40}
SAMPLE_CONTACT = {"contact_id": "sample", "first_name": "Dana", "last_name": "Reyes"}
SAMPLE_OPENER = "Pressure at work and at home seems to keep rising, and most teams feel it somewhere."  # the generic line
ROLE_LINE_MAX = 220  # characters; style.md asks for under 30 words
QA_MAX_TOKENS = 3000
DRAFT_MAX_TOKENS = 16000
DRAFT_TIMEOUT_SECONDS = 600.0


@dataclass
class Check:
    copy_version: str
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems


def sample_mailboxes(settings: Settings) -> list[Mailbox]:
    """One mailbox of the demo host and one of another sender, so both demo rules are exercised."""
    boxes = [m for m in settings.mailboxes if m.status != "Retired"]
    host = next((m for m in boxes if render.is_demo_host(m, settings)), None)
    other = next((m for m in boxes if not render.is_demo_host(m, settings)), None)
    out = [m for m in (host, other) if m is not None]
    if not out:
        out = [Mailbox("harry@example.org", "example.org", settings.general.demo_host or "Harry Dryden", "Active", 30)]
    return out


def sample_account(row: CopyRow, settings: Settings) -> dict[str, Any]:
    """The sample prospect, placed in the row's industry (or, for General, an industry with a page)."""
    account = dict(SAMPLE_ACCOUNT)
    industry: Industry | None = settings.industry(row.industry)
    if industry is None and row.industry != GENERAL_COPY:
        industry = next((i for i in settings.industries if i.industry_group == row.industry), None)
    if industry is None:
        industry = next((i for i in settings.industries if i.landing_page_url), None)
    if industry is not None:
        account.update(industry=industry.industry, industry_group=industry.industry_group)
    return account


def roles_for(row: CopyRow) -> list[str]:
    return [row.role] if row.role else list(ROLE_LINE_COLUMNS)


def _values(row: CopyRow, settings: Settings, mailbox: Mailbox, role: str, *, opener: str) -> dict[str, str]:
    contact = {**SAMPLE_CONTACT, "role": role}
    return render.variables(sample_account(row, settings), contact, mailbox, settings, copy_row=row, opener=opener,
                            legal_overlay="")


def check_row(row: CopyRow, settings: Settings) -> Check:
    """Every copy rule on every email of the row, for each role and both kinds of sender, with and without an opener."""
    out = Check(row.copy_version)
    problems: dict[str, None] = {}
    for n in COPY_STEPS:
        st = row.step(n)
        subject, _ = render.copy_markup.fill_text(st.subject, {"company": SAMPLE_ACCOUNT["clean_name"],
                                                               "first_name": SAMPLE_CONTACT["first_name"],
                                                               "place": "Boston, MA"})
        if len(subject) > copy_rules.SUBJECT_MAX:
            problems[f"email {n}: subject is {len(subject)} characters with a typical company name "
                     f"(the limit is {copy_rules.SUBJECT_MAX})"] = None
    for role in roles_for(row):
        line = render.role_line_for(row, role)
        if len(line) > ROLE_LINE_MAX:
            problems[f"the {role} line is {len(line)} characters (the limit is {ROLE_LINE_MAX})"] = None
        for v in copy_rules.content_violations(line) + copy_rules.structure_violations(line):
            problems[f"the {role} line {v}"] = None
    for mb in sample_mailboxes(settings):
        for role in roles_for(row):
            for opener in ("", SAMPLE_OPENER):
                values = _values(row, settings, mb, role, opener=opener)
                for v in render.violations(render.render_sequence(row, values, mailbox=mb, settings=settings,
                                                                  for_send=False)):
                    who = "the demo host" if render.is_demo_host(mb, settings) else "another sender"
                    problems[f"{v} ({role}, sent by {who}{', with an opener' if opener else ''})"] = None
    # The same problem for every role and sender reads once.
    out.problems = _collapse(problems)
    return out


def _collapse(problems: Mapping[str, None]) -> list[str]:
    seen: dict[str, list[str]] = {}
    for p in problems:
        m = re.fullmatch(r"(.*) \((.*)\)", p)
        base, where = (m.group(1), m.group(2)) if m and ("sent by" in m.group(2)) else (p, "")
        seen.setdefault(base, []).append(where)
    out = []
    for base, wheres in seen.items():
        wheres = [w for w in wheres if w]
        out.append(base if not wheres or len(wheres) > 2 else f"{base} ({'; '.join(wheres)})")
    return out


def check_all(settings: Settings, versions: Iterable[str] | None = None) -> list[Check]:
    wanted = {v.casefold() for v in versions} if versions else None
    return [check_row(c, settings) for c in settings.copy
            if c.status != "retired" and (wanted is None or c.copy_version.casefold() in wanted)]


# -- opener lines (the Signals tab's tokenized openers, General's generic and focus lines; enrol/openers.py) ----

OPENER_MAX_WORDS = 25  # style.md: one sentence, under about 20 words


def _opener_check(settings: Settings) -> Callable[[str], str]:
    exempt = (SAMPLE_ACCOUNT["clean_name"], SAMPLE_CONTACT["first_name"])
    return lambda text: render.pick_opener(text, sender_is_harry=False, demo_host=settings.general.demo_host,
                                           exempt=exempt)[1]


def check_openers(settings: Settings) -> Check:
    """Every opener line (the Signals tab's, and General's generic and focus lines; openers.sample_lines), filled
    for the sample prospect with sample facts and run through the copy rules an opener must pass
    (render.pick_opener, which never lets one mention funding or money), and the style's length.

    A line that does not fill (it needs a fact the sample has not got, like a named provider, or has a
    retired token) is still read as written for funding or money (copy_rules.money_violations; Harry,
    2 Oct 2026: funding is a signal, never a line).
    """
    from us_outbound.enrol import openers

    out = Check("Openers")
    check = _opener_check(settings)
    for signal, col, line, filled in openers.sample_lines(settings):
        if filled is None:  # validation checked its tokens; the money rule reads it as written
            out.problems += [f"{signal}, {col}: {line!r} {v}" for v in copy_rules.money_violations(line)]
            continue
        where = f"{signal}, {col}: {filled!r}"
        if problem := check(filled):
            out.problems.append(f"{where} {problem}")
        if len(filled.split()) > OPENER_MAX_WORDS:
            out.problems.append(f"{where} is {len(filled.split())} words; style.md asks for under about 20")
    return out


def sample_opener(settings: Settings, role: str, signal: str = "", *, leader: bool = False,
                  generic: bool = False) -> tuple[str, str]:
    """(a real opener line, filled for the sample prospect with sample facts; where from).

    signal names the Signals row (default: the first active one with a line for the role); leader shows
    the line for a contact who is the new People leader themself (opener_self); generic shows the line an
    account with no signal line gets, Control accounts among them (General opener_generic_*).
    """
    from datetime import UTC, datetime

    from us_outbound.enrol import openers

    if generic:
        op = openers.generic_opener(SAMPLE_ACCOUNT, {"role": role}, settings, _opener_check(settings))
        if op.text:
            return op.text, op.source
        return "", "no generic line fills for the sample prospect" + (f" ({'; '.join(op.notes)})" if op.notes else "")
    rows = [s for s in settings.signals if s.active and (s.role_openers or s.opener_self or s.opener)]
    if signal:
        rows = [s for s in settings.signals if s.signal.casefold() == signal.casefold()]
        if not rows:
            raise ValueError(f"no Signals row is called {signal!r}")
    else:
        rows.sort(key=lambda s: role not in s.role_openers)
    for s in rows:
        op = openers.sample_opener(s, role, SAMPLE_ACCOUNT, settings, datetime.now(UTC), subject=leader,
                                   check=_opener_check(settings))
        if op.text:
            return op.text, op.source
    return "", "no opener line fills for the sample prospect" + (f" ({'; '.join(op.notes)})" if rows and op.notes else "")


@dataclass
class Preview:
    copy_version: str
    role: str
    sender: str
    emails: list[render.Rendered]
    opener_note: str = ""  # where the preview's opener came from

    def text(self) -> str:
        parts = [f"{self.copy_version}, {self.role}, sent by {self.sender}"
                 + (f"\nOpener: {self.opener_note}" if self.opener_note else "")]
        for r in self.emails:
            parts.append(f"--- Email {r.step} (day {render_day(r.step)}) ---\nSubject: {r.subject}\n\n{r.text}")
            if r.violations:
                parts.append("Problems:\n" + "\n".join(f"  - {v}" for v in r.violations))
        return "\n\n".join(parts)

    def html(self) -> str:
        import html as h

        cards = []
        for r in self.emails:
            probs = "".join(f"<li>{h.escape(v)}</li>" for v in r.violations)
            cards.append(
                f'<section class="email"><h2>Email {r.step} <small>day {render_day(r.step)}</small></h2>'
                f'<p class="subject"><b>Subject:</b> {h.escape(r.subject)}</p><div class="body">{r.html}</div>'
                + (f'<ul class="problems">{probs}</ul>' if probs else "") + "</section>"
            )
        return (
            "<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width'>"
            f"<title>{h.escape(self.copy_version)}</title><style>"
            "body{font-family:Arial,Helvetica,sans-serif;max-width:680px;margin:24px auto;padding:0 16px;color:#222;background:#fff}"
            ".email{border:1px solid #ddd;border-radius:8px;padding:16px 20px;margin:20px 0}"
            ".subject{border-bottom:1px solid #eee;padding-bottom:8px}.problems{color:#b00020}small{color:#888}"
            "</style></head><body>"
            f"<h1>{h.escape(self.copy_version)}</h1><p>{h.escape(self.role)}, sent by {h.escape(self.sender)}. "
            "Sample prospect: Dana at Harbor &amp; Finch, 40 staff, Boston.</p>"
            + "".join(cards) + "</body></html>"
        )


def render_day(step: int) -> int:
    from us_outbound.clients.instantly import STEP_DAYS

    return STEP_DAYS[step - 1]


def preview(row: CopyRow, settings: Settings, *, role: str = "", sender: str = "", opener: str = "",
            opener_note: str = "", account: Mapping[str, Any] | None = None,
            contact: Mapping[str, Any] | None = None) -> Preview:
    """The row as one prospect reads it: the sample prospect, or a stored account and contact (account, contact)."""
    role = role or (row.role or next(iter(ROLE_LINE_COLUMNS)))
    boxes = sample_mailboxes(settings)
    mb = next((m for m in settings.mailboxes if sender and m.owner_name.casefold() == sender.casefold()), boxes[0])
    if account is not None:
        person = {**SAMPLE_CONTACT, **(contact or {}), "role": role}
        values = render.variables(account, person, mb, settings, copy_row=row, opener=opener,
                                  legal_overlay=render_overlay(account, settings))
    else:
        values = _values(row, settings, mb, role, opener=opener)
    emails = render.render_sequence(row, values, mailbox=mb, settings=settings, for_send=False)
    return Preview(row.copy_version, role, mb.owner_name, emails, opener_note)


def render_overlay(account: Mapping[str, Any], settings: Settings) -> str:
    from us_outbound.scoring.angle import legal_overlay

    return legal_overlay(settings.industry_group_of(account))


# -- the page, the facts and the style, for the models -----------------------------------------


def industry_material(row_industry: str, settings: Settings) -> dict[str, str]:
    """The industry's page material from the Industries tab (the group's hub row for a group; none for General)."""
    ind = settings.industry(row_industry) or next(
        (i for i in settings.industries if i.industry_group == row_industry and i.industry == row_industry), None)
    if ind is None:
        return {}
    out = {"industry": ind.industry, "industry_group": ind.industry_group, "page": ind.landing_page_url}
    out.update({k: v for k, v in ind.page.as_dict().items() if v})
    return out


def _template(name: str) -> str:
    return (render.TEMPLATES_DIR / name).read_text(encoding="utf-8")


def row_as_json(row: CopyRow) -> dict[str, str]:
    out = {"industry": row.industry, "role": row.role or "every role"}
    for n in COPY_STEPS:
        out[f"s{n}_subject"], out[f"s{n}_body"] = row.step(n).subject, row.step(n).body
    for role, col in ROLE_LINE_COLUMNS.items():
        out[col] = row.role_lines.get(role, "")
    return out


# -- QA (the task model) ------------------------------------------------------------------------

QA_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["pass", "fail"]},
        "problems": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "email": {"type": "integer"},
                    "severity": {"type": "string", "enum": ["blocker", "minor"]},
                    "issue": {"type": "string"},
                    "fix": {"type": "string"},
                },
                "required": ["email", "severity", "issue", "fix"],
                "additionalProperties": False,
            },
        },
        "summary": {"type": "string"},
    },
    "required": ["verdict", "problems", "summary"],
    "additionalProperties": False,
}

QA_SYSTEM = """You check cold outbound emails for Spill before a person approves them. Be strict and specific.

A sequence FAILS (verdict "fail", severity "blocker") when any email:
- makes a claim about Spill that is not in the facts list or on the industry's page, or states one
  more strongly than they do;
- quotes a figure other than "30% of employees use Spill" or "trusted by over 50,000 employees", names
  a customer, or gives a price other than through the price line;
- gets the industry wrong, or says something about the reader's own company the email cannot know;
- uses therapy, therapist, licensed or unlimited, criticizes the reader's EAP or benefits, or
  offers a demo "with me";
- has British spelling, a grammar error, or a sentence that does not read naturally in US English;
- reads as salesy, pushy, guilt-inducing or generic, when the style asks for warm, plain and specific;
- breaks the structure (Harry, 1 Oct 2026): every email is written for this row's industry AND its
  role (people leader, founder or executive, operations), so a reader in that role and industry
  feels it was written for them; email 1 informs and plants a seed with a personal, relevant hook,
  and asks only for a visit to the industry page (no demo, call or meeting); email 2 is the
  long-form explainer; emails 2 to 4 each have one call to action to book a demo; email 4 mentions
  the free trial once, before signing off, with no terms; role lines (rows with no role) each read
  naturally after email 1's hook.
A claim that is on the industry's own page is allowed, if it is not a statistic and is not stated
more strongly than the page does. Plain, hedged description of the industry's own work and pressures
("often", "in most ...") is not a claim about Spill and is allowed when it is accurate. The code has
already checked banned words, spelling lists, links, word counts and markup, so judge meaning: claims,
accuracy, tone, US grammar and idiom, and whether each role line reads well after email 1's hook.
Minor problems (severity "minor") are wording you would improve but that could be sent.
A free trial mentioned in email 4, as facts.md allows, is not a problem.
The signature (Spill, "Book a call here", the Trustpilot line) and the data-source notice are fixed
text outside these rules (Harry, 1 Oct 2026): do not judge them, and the signature's booking line is
not an ask in email 1.
Report every problem with the email number (0 for the role lines or the whole sequence) and a fix.
Do not invent problems. verdict is "pass" only when there are no blockers."""


def qa_prompt(row: CopyRow, settings: Settings) -> str:
    sample = preview(row, settings, role=roles_for(row)[0]).text()
    return "\n\n".join([
        "## Facts: the only claims the emails may make\n" + _template("facts.md"),
        "## Style\n" + _template("style.md"),
        "## The industry's page (material; the emails may use its claims but not its statistics)\n"
        + json.dumps(industry_material(row.industry, settings), indent=1),
        "## The copy as written in the sheet\n" + json.dumps(row_as_json(row), indent=1),
        "## The emails as a prospect would get them (a sample prospect, the first role's line; "
        "the signature and the data-source notice are fixed text)\n" + sample,
    ])


@dataclass
class QAResult:
    copy_version: str
    verdict: str
    code: str
    notes: str

    @property
    def cell(self) -> str:
        return f"{self.verdict} {self.code}"


def _notes(problems: Sequence[Mapping[str, Any]], summary: str) -> str:
    lines = [f"{p.get('severity', '')}: email {p.get('email', 0)}: {p.get('issue', '')} Fix: {p.get('fix', '')}".strip()
             for p in problems]
    return "\n".join([summary.strip(), *lines]).strip()[:4000]


def qa_row(ctx, row: CopyRow, settings: Settings | None = None) -> QAResult:
    """The sheet check, then (only when it is clean) the task model's review."""
    settings = settings or ctx.settings
    check = check_row(row, settings)
    code = row.content_hash()
    if not check.ok:
        return QAResult(row.copy_version, "fail", code, "Sheet check:\n" + "\n".join(f"- {p}" for p in check.problems)[:4000])
    answer = ctx.clients.claude_task.json(
        QA_SYSTEM, qa_prompt(row, settings), QA_SCHEMA, max_tokens=QA_MAX_TOKENS, purpose="copy_qa", now=ctx.now,
    )
    problems = [p for p in answer.get("problems") or [] if isinstance(p, dict)]
    blockers = [p for p in problems if p.get("severity") == "blocker"]
    verdict = "pass" if answer.get("verdict") == "pass" and not blockers else "fail"
    model = ctx.settings.general.claude_task_model
    return QAResult(row.copy_version, verdict, code, _notes(problems, f"QA by {model}: {answer.get('summary', '')}"))


# -- drafting (the writing model) -------------------------------------------------------------

DRAFT_FIELDS = ("s1_subject", "s1_body", "s2_subject", "s2_body", "s3_subject", "s3_body", "s4_subject", "s4_body",
                *ROLE_LINE_COLUMNS.values(), "sources")
DRAFT_SCHEMA = {
    "type": "object",
    "properties": {f: {"type": "string"} for f in DRAFT_FIELDS},
    "required": list(DRAFT_FIELDS),
    "additionalProperties": False,
}
DRAFT_SYSTEM = """You write cold outbound email sequences for Spill, employee mental health support, to US
companies with 10 to 249 staff. Follow the style guide and the facts list exactly: they are the rules
the system checks every email against, and copy that breaks one is never sent. Write in US English."""


def draft_prompt(industry: str, role: str, settings: Settings, *, feedback: str = "") -> str:
    material = industry_material(industry, settings)
    parts = [
        "## Style guide\n" + _template("style.md"),
        "## Facts\n" + _template("facts.md"),
        f"## Write the four-email sequence for: {industry}" + (f", for the {role} role only" if role else ""),
        ("## The industry's page\n" + json.dumps(material, indent=1)) if material else
        "## No page: write an industry-neutral sequence; in email 2 link {{industry_url}} as the reader's industry page.",
        "Return the four subjects and bodies, the three role lines (people_leader_line, founder_line, "
        "operations_line), and in sources a short note of the page sections you used. Bodies use \\n for line "
        "breaks and \\n\\n between paragraphs.",
    ]
    if not material.get("page") and material:
        parts.append("This industry's page is not live yet: do not link {{industry_url}}.")
    if feedback:
        parts.append("## Problems to fix from the last draft\n" + feedback)
    return "\n\n".join(parts)


def next_version(settings: Settings, industry: str, role: str = "") -> str:
    base = re.sub(r"[^a-z0-9]+", "-", f"{industry} {role}".lower()).strip("-")
    taken = {c.copy_version.casefold() for c in settings.copy}
    n = 1
    while f"{base}-v{n}" in taken:
        n += 1
    return f"{base}-v{n}"


def draft_row(ctx, industry: str, role: str = "", *, settings: Settings | None = None, feedback: str = "") -> dict[str, str]:
    """A new Copy-tab row (status draft) written by the writing model."""
    settings = settings or ctx.settings
    answer = ctx.clients.claude.json(
        DRAFT_SYSTEM, draft_prompt(industry, role, settings, feedback=feedback), DRAFT_SCHEMA,
        max_tokens=DRAFT_MAX_TOKENS, purpose="copy_draft", now=ctx.now, timeout=DRAFT_TIMEOUT_SECONDS,
    )
    row = {"copy_version": next_version(settings, industry, role), "industry": industry, "role": role,
           "status": "draft", "approved_by": "", "qa": "", "qa_notes": "",
           "note": f"Drafted by {settings.general.claude_model}; not yet checked."}
    row.update({f: str(answer.get(f) or "") for f in DRAFT_FIELDS})
    return row


def row_from_dict(d: Mapping[str, str]) -> CopyRow:
    """A CopyRow from sheet text, for checking a draft before it is on the sheet."""
    from us_outbound.settings.model import CopyStep

    return CopyRow(
        copy_version=d.get("copy_version", ""), industry=d.get("industry", ""), status=d.get("status", "draft"),
        steps=tuple(CopyStep(d.get(f"s{n}_subject", ""), d.get(f"s{n}_body", "")) for n in COPY_STEPS),
        role=d.get("role", ""), role_lines={r: d.get(c, "") for r, c in ROLE_LINE_COLUMNS.items()},
        approved_by=d.get("approved_by", ""), qa=d.get("qa", ""), qa_notes=d.get("qa_notes", ""),
        sources=d.get("sources", ""),
    )


def with_settings_copy(settings: Settings, rows: Iterable[CopyRow]) -> Settings:
    return replace(settings, copy=tuple(rows))
