"""Copy rendering: variables, the four emails and the signature.

SPEC 10 ("Sequence", "Variables", "Rules"), SPEC 9 ("Instantly campaigns": subjects and
bodies go to Instantly as custom variables {{s1_subject}}, {{s1_body}} ...; "Sender
continuity": every demo is with Harry), and Harry, 30 Sep and 1 Oct 2026:
copy by industry and role, links embedded in the copy, the demo page as every email's call
to action.

  * variables() builds the variables for one account, contact, sender mailbox and Copy row.
  * render_step() fills one email of a Copy row (copy_markup: HTML and plain text), appends
    the signature on every email (templates/copy/signature.txt; "#" lines are comments and are
    stripped), then runs the copy rules. No email carries the data notice (Harry, 5 Oct 2026):
    data_record() is what enrol keeps on each contact instead (contacts.data_record). An unknown or empty
    variable is a violation and stays visible in the text. Any violation blocks the send,
    and so does copy that is not approved or has not passed QA in its current wording.
  * render_sequence() renders emails 1 to 4 and checks the sequence links the industry page;
    custom_variables() turns them into the lead's Instantly custom variables.
  * pick_opener() falls back to "" when the evidence opener breaks a copy rule (for
    example "unlimited PTO" quoted from a benefits page): the opener line then disappears.
  * max_rendered_lengths() is the phase-0 probe for Instantly's custom-variable limit.
"""

from __future__ import annotations

import html
import os
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from functools import lru_cache
from pathlib import Path
from typing import Any

from us_outbound.enrol import copy_markup, copy_rules
from us_outbound.settings.model import CLAY_SKIP, COPY_STEPS, GENERAL_COPY, CopyRow, Mailbox, Settings

STEPS = COPY_STEPS
VARIABLES = (
    "first_name", "company", "place", "opener", "legal_overlay", "role_line", "price_line", "demo_url",
    "industry_url", "site_url", "sender_first_name", "proof",
)
OPTIONAL_VARIABLES = frozenset({"opener", "legal_overlay"})  # alone on their line; the line goes when empty
LEGAL_GROUP = "Legal Teams"

TEMPLATES_DIR = Path(os.environ.get("US_OUTBOUND_TEMPLATES") or Path(__file__).resolve().parents[2] / "templates") / "copy"
SIGNATURE_TEMPLATE = "signature.txt"
# Harry, 5 Oct 2026: "Ideally we would add some formatting to signature to make it look more professional."
# A paragraph of its own (the space after the sign-off), the sender's full name, then the three lines in
# smaller grey type: the plain look of a personal mail client's signature, no image, no table.
SIGNATURE_STYLE = "margin:16px 0 0;font-size:13px;line-height:1.5;color:#555555"
SIGNATURE_NAME_STYLE = "color:#222222"
LAWFUL_BASIS = "legitimate interests: telling businesses about Spill"

# Harry, 1 Oct 2026: one starting price in every email, as on the website, whatever the team's size
# (General price_from). SPEC 4's price-by-size table is not quoted.
PRICE_LINE = "Plans start from ${dollars} a month for the whole team, on a rolling 30-day contract."

_PLACEHOLDER = re.compile(r"(?<!\{)\{([a-z_]+)\}(?!\})")
_FIXED_MISSING = {
    "site_url": "site_url is blank on the General tab, so the signature has no Spill link",
    "booking_link": "booking_link is blank on the General tab, so the signature has no booking link",
}


@dataclass(frozen=True)
class Rendered:
    subject: str
    body: str  # as sent, in the General tab's email_format, signature included
    violations: tuple[str, ...] = ()
    step: int = 0
    copy_version: str = ""
    text: str = ""  # the plain-text version, signature included (previews and QA)
    html: str = ""

    @property
    def ok(self) -> bool:
        return not self.violations


# -- variables ---------------------------------------------------------------------------


def is_demo_host(mailbox: Mailbox, settings: Settings) -> bool:
    """True when the sender is the demo host (Harry)."""
    host = settings.general.demo_host.strip().casefold()
    return bool(host) and mailbox.owner_name.strip().casefold() == host


def price_line(settings: Settings) -> str:
    """The starting price every email quotes (General price_from), like "Plans start from $195 a month for the whole team, ..."."""
    return PRICE_LINE.format(dollars=settings.general.price_from)


def proof_for(account: Mapping[str, Any], settings: Settings) -> str:
    """The Industries-tab proof_point: the account's own label, else its group's row, else any row in the group."""
    label = str(account.get("industry") or "").strip()
    row = settings.industry(label) if label else None
    if row and row.proof_point.strip():
        return row.proof_point.strip()
    group = str(account.get("industry_group") or (row.industry_group if row else "")).strip()
    if not group:
        return ""
    rows = [i for i in settings.industries if i.industry_group == group and i.proof_point.strip()]
    rows.sort(key=lambda i: i.industry != group)  # the group's own row first, then sheet order
    return rows[0].proof_point.strip() if rows else ""


def place_for(account: Mapping[str, Any]) -> str:
    city = str(account.get("hq_city") or "").strip()
    state = str(account.get("hq_state") or "").strip()
    return f"{city}, {state}" if city and state else (state or city)


def industry_url_for(account: Mapping[str, Any], copy_row: CopyRow, settings: Settings) -> str:
    """The page {{industry_url}} links: the Copy row's industry's page; for General, the account's own.

    With no page (one still in draft on the website), Spill's US site (General site_url; Harry, 1 Oct 2026).
    """
    if copy_row.industry != GENERAL_COPY:
        page = settings.industry_page_url(copy_row.industry)
    else:
        label = str(account.get("industry") or "").strip()
        page = settings.industry_page_url(label) or settings.industry_page_url(settings.industry_group_of(account))
    return page or settings.general.site_url.strip()


def role_line_for(copy_row: CopyRow, role: str | None) -> str:
    role = (role or "").strip()
    return next((v.strip() for k, v in copy_row.role_lines.items() if k.casefold() == role.casefold()), "")


def sender_first_name(mailbox: Mailbox) -> str:
    return (mailbox.owner_name.strip().split() or [""])[0]


def variables(
    account: Mapping[str, Any],
    contact: Mapping[str, Any],
    mailbox: Mailbox,
    settings: Settings,
    *,
    copy_row: CopyRow,
    opener: str = "",
    legal_overlay: str = "",
) -> dict[str, str]:
    """The variables for one lead and one Copy row. Overrides for the account's domain win (SPEC 5, 13)."""
    domain = str(account.get("domain") or "").strip().lower()
    acct = {**account, **(settings.overrides_for(domain) if domain else {})}
    legal = settings.industry_group_of(acct).casefold() == LEGAL_GROUP.casefold()
    return {
        "first_name": str(contact.get("first_name") or "").strip(),
        "company": str(acct.get("clean_name") or "").strip(),
        "place": place_for(acct),
        "opener": (opener or "").strip(),
        "legal_overlay": (legal_overlay or "").strip() if legal else "",
        "role_line": role_line_for(copy_row, contact.get("role")),
        "price_line": price_line(settings),
        "demo_url": settings.general.booking_page.strip(),
        "industry_url": industry_url_for(acct, copy_row, settings),
        "site_url": settings.general.site_url.strip(),
        "sender_first_name": sender_first_name(mailbox),
        "proof": proof_for(acct, settings),
    }


def pick_opener(
    opener: str,
    *,
    sender_is_harry: bool = True,
    demo_host: str = "Harry Dryden",
    exempt: Iterable[str] = (),
) -> tuple[str, str]:
    """(opener to use, why it was dropped or ""). An opener that breaks a copy rule is dropped.

    SPEC 9 step 5 fills the opener with evidence from the prospect's own pages, which can
    carry words the copy may not use ("unlimited PTO", "100% employer-paid", "therapy"), and
    the tokenized openers (enrol/openers.py) with posting titles ("Call Center Agent" would read
    as an ask for a call). An opener never mentions funding or money either (copy_rules.money_violations;
    Harry, 2 Oct 2026: funding is a signal, never a line). Email 1 is written to read well without it
    (style.md), so its line simply goes. enrol/openers.py tries each filled line with this check and
    takes the first that passes; the enrol job checks the chosen one again as it renders.
    """
    opener = (opener or "").strip()
    if not opener:
        return "", ""
    problems = copy_rules.content_violations(
        opener, sender_is_harry=sender_is_harry, demo_host=demo_host, exempt=exempt
    ) + copy_rules.structure_violations(opener) + copy_rules.opener_violations(opener, exempt=exempt)
    if problems:
        return "", "; ".join(problems)
    return opener, ""


# -- templates ---------------------------------------------------------------------------


@lru_cache(maxsize=None)
def load_template(name: str) -> str:
    """A copy template with its "#" comment lines stripped."""
    text = (TEMPLATES_DIR / name).read_text(encoding="utf-8")
    lines = [line.rstrip() for line in text.splitlines() if not line.lstrip().startswith("#")]
    return "\n".join(lines).strip()


def fill(template: str, values: Mapping[str, str]) -> tuple[str, list[str]]:
    """Fill {placeholders}; a blank or unknown one stays visible and is returned as missing."""
    missing: list[str] = []

    def one(m: re.Match[str]) -> str:
        value = str(values.get(m.group(1)) or "").strip()
        if not value:
            missing.append(m.group(1))
            return m.group(0)
        return value

    return _PLACEHOLDER.sub(one, template), missing


def signature(settings: Settings, sender_name: str = "") -> tuple[copy_markup.Rendered, list[str]]:
    """The signature every email ends with, after the copy's sign-off (Harry, 1 Oct 2026), and what is missing.

    The sender's full name, then three lines with three links: Spill's US site, Harry's booking link and the
    Trustpilot reviews. No postal address and no privacy link; the opt-out is Instantly's unsubscribe link,
    which the campaign's step template adds after everything here (clients/instantly.py).
    """
    g = settings.general
    source, missing = fill(load_template(SIGNATURE_TEMPLATE), {"site_url": g.site_url, "booking_link": g.booking_link})
    sig = copy_markup.render(source, {})
    inner = sig.html[len("<p>"):-len("</p>")] if sig.html.count("<p>") == 1 else sig.html  # one paragraph of lines
    name = sender_name.strip()
    if name:
        inner = f'<strong style="{SIGNATURE_NAME_STYLE}">{html.escape(name, quote=False)}</strong><br>{inner}'
    return replace(sig, html=f'<p style="{SIGNATURE_STYLE}">{inner}</p>',
                   text=f"{name}\n{sig.text}" if name else sig.text), missing


def data_sources(settings: Settings) -> str:
    """The contact-data providers in use: Clay only once it runs (clay_verification)."""
    if settings.general.clay_verification == CLAY_SKIP:
        return "Apollo"
    return "Apollo and Clay"


def data_record(settings: Settings) -> dict[str, Any]:
    """Where a contact's details came from and the lawful basis, kept on the contact at enrolment
    (contacts.data_record) and never shown in an email (Harry, 5 Oct 2026: "We should keep an internal
    record but it should not be shown to customers")."""
    return {"contact_data": data_sources(settings), "company_information": "the company's public website",
            "lawful_basis": LAWFUL_BASIS, "shown_in_email": False}


# -- rendering ------------------------------------------------------------------------------


def render_step(
    copy_row: CopyRow, variables: Mapping[str, str], *, step: int, mailbox: Mailbox, settings: Settings,
    for_send: bool = True,
) -> Rendered:
    """One email, signature included, with every copy-rule violation.

    for_send=False leaves out the approval and QA checks, for previews and the sheet check.
    """
    g = settings.general
    st = copy_row.step(step)
    problems: list[str] = []
    if for_send and copy_row.status != "approved":
        problems.append(f"copy {copy_row.copy_version} is {copy_row.status or 'blank'}, not approved")
    if for_send and not copy_row.qa_current:
        problems.append(f"copy {copy_row.copy_version} has not passed QA in its current wording "
                        f"(qa is {copy_row.qa or 'blank'}; run `us-outbound copy qa`)")
    problems += copy_rules.source_violations(st.subject, st.body, step=step)
    subject, p = copy_markup.fill_text(st.subject, variables)
    problems += [f"subject {x}" for x in p]
    if copy_markup.links(st.subject) or "**" in st.subject:
        problems.append("subject has markup; a subject is plain text")
    body = copy_markup.render(st.body, variables, optional=OPTIONAL_VARIABLES)
    problems += [f"body {x}" for x in body.problems]

    # After the copy's sign-off: the signature, as its own paragraph.
    sig, missing = signature(settings, mailbox.owner_name)
    problems += [_FIXED_MISSING.get(m, f"the signature has no {m}") for m in missing]
    problems += [f"signature {x}" for x in sig.problems]
    text = f"{body.text}\n\n{sig.text}"
    html_body = body.html + sig.html
    exempt = (variables.get("first_name", ""), variables.get("company", ""), variables.get("place", ""),
              mailbox.owner_name)
    problems += copy_rules.email_violations(
        subject, body.words, body.links,
        step=step, demo_url=variables.get("demo_url", ""), industry_url=variables.get("industry_url", ""),
        sender_is_harry=is_demo_host(mailbox, settings), demo_host=g.demo_host, exempt=exempt,
        uncounted=[str(variables.get(v) or "").strip() for v in OPTIONAL_VARIABLES],
        site_url=variables.get("site_url", ""),
    )
    sent = html_body if g.email_format == "html" else text
    return Rendered(subject, sent, tuple(dict.fromkeys(problems)), step, copy_row.copy_version, text, html_body)


def render_sequence(
    copy_row: CopyRow, variables: Mapping[str, str], *, mailbox: Mailbox, settings: Settings, for_send: bool = True,
) -> list[Rendered]:
    """Emails 1 to 4 of one Copy row, and the sequence's own check: it links the industry page."""
    out = [render_step(copy_row, variables, step=n, mailbox=mailbox, settings=settings, for_send=for_send)
           for n in STEPS]
    page = str(variables.get("industry_url") or "").strip()
    uses = any("industry_url" in copy_markup.VARIABLE.findall(copy_row.step(n).body) for n in STEPS)
    if page and not uses:
        long_form = out[1]
        out[1] = Rendered(long_form.subject, long_form.body,
                          (*long_form.violations, "the sequence never links the industry page ({{industry_url}})"),
                          long_form.step, long_form.copy_version, long_form.text, long_form.html)
    return out


def violations(rendered: Sequence[Rendered]) -> list[str]:
    return [f"email {r.step}: {v}" for r in rendered for v in r.violations]


def custom_variables(rendered: Sequence[Rendered]) -> dict[str, str]:
    """{"s1_subject", "s1_body", ..., "s4_body"}: the lead's Instantly custom variables (SPEC 9)."""
    out: dict[str, str] = {}
    for r in sorted(rendered, key=lambda r: r.step):
        out[f"s{r.step}_subject"] = r.subject
        out[f"s{r.step}_body"] = r.body
    return out


# -- empty and maximum-length values (SPEC 10 tests; SPEC 9 custom-variable limit) ----------

# The longest value each free-text variable can plausibly take. An opener is its template
# plus a quote of up to 300 characters (SPEC 6), and a proof point is free sheet text.
MAX_LENGTHS = {"first_name": 40, "company": 100, "opener": 340, "place": 60, "legal_overlay": 120,
               "proof": 400, "sender_first_name": 20}


def _filler(n: int) -> str:
    """Exactly n characters of plain letters and spaces."""
    s = ("abcdefghi " * (n // 10 + 1))[:n]
    return s[:-1] + "x" if s.endswith(" ") else s


def empty_variables() -> dict[str, str]:
    return {v: "" for v in VARIABLES}


def max_length_variables(copy_row: CopyRow, settings: Settings) -> dict[str, str]:
    """Every variable at its longest: free text at MAX_LENGTHS, fixed choices at their longest option."""
    pages = [i.landing_page_url for i in settings.industries if i.landing_page_url] or [settings.general.site_url]
    values = {k: _filler(n) for k, n in MAX_LENGTHS.items()}
    values.update(
        role_line=max(copy_row.role_lines.values(), key=len, default=""),
        price_line=price_line(settings),
        demo_url=settings.general.booking_page.strip(),
        industry_url=max(pages, key=len),
        site_url=settings.general.site_url.strip(),
    )
    return values


def max_rendered_lengths(settings: Settings, copy_versions: Iterable[str] | None = None) -> dict[str, int]:
    """The longest each custom variable gets, over every copy version and registry mailbox.

    Phase 0 sends a lead with values this long to find Instantly's custom-variable limit
    (SPEC 9: "Test the custom-variable length limit in phase 0").
    """
    wanted = set(copy_versions) if copy_versions is not None else None
    rows = [c for c in settings.copy if wanted is None or c.copy_version in wanted]
    mailboxes = [m for m in settings.mailboxes if m.status != "Retired"]
    out = {f"s{step}_{part}": 0 for step in STEPS for part in ("subject", "body")}
    for row in rows:
        values = max_length_variables(row, settings)
        for mb in mailboxes:
            cv = custom_variables(render_sequence(row, values, mailbox=mb, settings=settings, for_send=False))
            for k, v in cv.items():
                out[k] = max(out[k], len(v))
    return out
