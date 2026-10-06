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
  * signature() is the sender's full name and ONE linked line (Harry, 5 Oct 2026: "The signature should
    only have one line and link"), chosen from the email's own body so it never repeats the body's link:
    the booking line whenever the body suggests booking a call (it links the demo page or the booking
    link, or says a phrase in BOOKING_PHRASES); otherwise never the website line when the body links a
    page on the site, never a line whose address the body already links, and among the lines left, a
    rotation by the recipient and the email's number. With the current copy (data/copy.csv, 318 rows),
    email 1 links only {{industry_url}}, a page on the site, so it shows the booking or the reviews line
    by rotation; emails 2 to 4 all link {{demo_url}}, so they always show the booking line.
  * render_sequence() renders emails 1 to 4 and checks the sequence links the industry page;
    custom_variables() turns them into the lead's Instantly custom variables.
  * subject_arm() is email 1's subject arm (Harry, 5 Oct 2026: a personal subject, as a measured split). The
    General email1_subject_share of accounts, by sha256 of the salted account id (its own salt, so the split is
    independent of the opener holdout's), are personal: email 1's subject is General email1_subject ("support
    for the {{company}} team") instead of the Copy row's s1_subject. The rest are copy. Emails 2 to 4 keep the
    Copy row's subjects either way. render_sequence(subject_arm=...) renders the arm, so the send card, the
    auto_send path, `copy preview` and `seed send` show the subject that is sent; enrol records the arm on the
    contact (contacts.subject_arm), and `signals review` and the daily post compare the arms' replies.
  * UTM tags (Harry, 6 Oct 2026; enrol/utm.py): with General utm_links = yes, the HTML's links to Spill's site
    and the signature's site or booking link carry utm_source, utm_medium, utm_campaign (the Copy row's
    copy_version) and utm_content (the email's number). The words, the plain text, the copy rules' reading and
    the signature's choice of line all see the bare addresses; the Trustpilot and unsubscribe links are never
    tagged.
  * pick_opener() falls back to "" when the evidence opener breaks a copy rule (for
    example "unlimited PTO" quoted from a benefits page): the opener line then disappears.
  * max_rendered_lengths() is the phase-0 probe for Instantly's custom-variable limit.
"""

from __future__ import annotations

import hashlib
import html
import os
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from functools import lru_cache
from pathlib import Path
from typing import Any

from us_outbound.enrol import copy_markup, copy_rules, utm
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
# A paragraph of its own (the space after the sign-off), the sender's full name, then its one line in
# smaller grey type: the plain look of a personal mail client's signature, no image, no table.
SIGNATURE_STYLE = "margin:16px 0 0;font-size:13px;line-height:1.5;color:#555555"
SIGNATURE_NAME_STYLE = "color:#222222"
# The signature's lines by kind, as signature.txt names them, in the order the rotation takes them
# (Harry, 5 Oct 2026): Spill's US site, Harry's booking link and the Trustpilot reviews.
WEBSITE, BOOKING, REVIEWS = SIGNATURE_KINDS = ("website", "booking", "reviews")
# Harry, 5 Oct 2026: "If the email copy suggests booking a call, then this should be included in the
# signature as well." Whole phrases in any case: "call" or "booked" on their own are not an ask
# ("calling", "fully booked Saturday", "physically").
BOOKING_PHRASES = ("book a call", "book a demo", "book a time", "schedule a call", "set up a call", "grab time")
_BOOKING_PHRASE = re.compile(
    r"\b(?:" + "|".join(r"\s+".join(map(re.escape, p.split())) for p in BOOKING_PHRASES) + r")\b", re.IGNORECASE
)
# A link as written in the copy names its page by its variable. The demo page is for booking a call, not a
# website page (Harry, 5 Oct 2026: "treat 'book a call' as not a website page").
_LINK_VARIABLES = {"demo_url": BOOKING, "industry_url": WEBSITE, "site_url": WEBSITE}
_SIGNATURE_LINE = re.compile(r"^([a-z]+):\s*(.+)$")
LAWFUL_BASIS = "legitimate interests: telling businesses about Spill"

# Email 1's subject arm (contacts.subject_arm; Harry, 5 Oct 2026): General email1_subject, or the Copy row's s1_subject.
PERSONAL_SUBJECT, COPY_SUBJECT = "personal", "copy"
SUBJECT_ARMS = (PERSONAL_SUBJECT, COPY_SUBJECT)
SUBJECT_SALT = "email1-subject:"  # not openers.HOLDOUT_SALT: the two splits never line up
SUBJECT_STEP = 1  # only email 1's subject changes

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
    signature: str = ""  # the signature's one line: "website", "booking" or "reviews" (Harry, 5 Oct 2026)

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


# -- email 1's subject arm (Harry, 5 Oct 2026) ---------------------------------------------------


def subject_arm(account_id: Any, settings: Settings) -> str:
    """personal or copy, for the account: personal when sha256 of the salted account id falls below General
    email1_subject_share (openers.in_holdout's rule, with its own salt), and email1_subject is not blank."""
    g = settings.general
    if not g.email1_subject.strip():
        return COPY_SUBJECT
    h = int(hashlib.sha256((SUBJECT_SALT + str(account_id)).encode()).hexdigest()[:15], 16)
    return PERSONAL_SUBJECT if h / 16**15 < g.email1_subject_share else COPY_SUBJECT


def email1_subject(arm: str, settings: Settings) -> str:
    """Email 1's subject as written for the arm: General email1_subject when personal, else "" (the Copy row's)."""
    return settings.general.email1_subject.strip() if arm == PERSONAL_SUBJECT else ""


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


def signature_lines() -> dict[str, str]:
    """{kind: line} from templates/copy/signature.txt as written: its website, booking and reviews lines."""
    out: dict[str, str] = {}
    for line in filter(None, (x.strip() for x in load_template(SIGNATURE_TEMPLATE).splitlines())):
        m = _SIGNATURE_LINE.match(line)
        if not m or m.group(1) not in SIGNATURE_KINDS or m.group(1) in out:
            raise ValueError(f'{SIGNATURE_TEMPLATE}: "{line}" is not "kind: line" with a new kind of '
                             f'{", ".join(SIGNATURE_KINDS)}')
        out[m.group(1)] = m.group(2).strip()
    if set(out) != set(SIGNATURE_KINDS):
        raise ValueError(f"{SIGNATURE_TEMPLATE} needs one line of each kind: {', '.join(SIGNATURE_KINDS)}")
    return out


def _filled_lines(settings: Settings) -> dict[str, tuple[str, list[str]]]:
    """{kind: (its line with the General tab's values in, the values that are blank)}."""
    g = settings.general
    values = {"site_url": g.site_url, "booking_link": g.booking_link}
    return {kind: fill(line, values) for kind, line in signature_lines().items()}


def _url_key(url: str) -> str:
    """An address compared loosely: any case, no scheme, no "www.", no trailing slash."""
    return re.sub(r"^https?://", "", url.strip().casefold()).removeprefix("www.").rstrip("/")


def _host(url: str) -> str:
    return re.split(r"[/?#]", _url_key(url), maxsplit=1)[0]


def link_kind(url: str, settings: Settings) -> str:
    """What one of the body's links is, for the signature (Harry, 5 Oct 2026), as written or as filled.

    "booking": {{demo_url}} (General booking_page) or Harry's booking_link; booking a call is not a website
    page. "website": any other page on the site: {{industry_url}}, {{site_url}}, or an address on General
    site_url's host. "" for anything else.
    """
    m = copy_markup.VARIABLE.match(url.strip())
    if m:
        return _LINK_VARIABLES.get(m.group(1), "")
    g = settings.general
    if _url_key(url) in {_url_key(u) for u in (g.booking_page, g.booking_link) if u.strip()}:
        return BOOKING
    site = _host(g.site_url) if g.site_url.strip() else ""
    return WEBSITE if site and _host(url) == site else ""


def suggests_booking(words: str) -> bool:
    """True when the copy's words ask for a call in so many words (BOOKING_PHRASES)."""
    return bool(_BOOKING_PHRASE.search(words))


def signature_key(values: Mapping[str, str]) -> str:
    """The recipient, for the signature's rotation: the company and first name (a render has no ids)."""
    return "\n".join(" ".join(str(values.get(k) or "").split()).casefold() for k in ("company", "first_name"))


def rotation(key: str, step: int, n: int) -> int:
    """Which of n lines: a stable hash of the recipient, moved on one per email.

    The hash spreads prospects over the lines, a prospect's next email takes the next line when the choice
    is the same, and the same key and email always give the same line, so a re-render, a Slack edit and a
    preview agree (Python's own hash() changes from run to run; sha256 does not).
    """
    start = int.from_bytes(hashlib.sha256(key.encode("utf-8")).digest()[:4], "big")
    return (start + step) % n


def signature_kind(settings: Settings, body: copy_markup.Rendered, source: str = "", *, key: str = "",
                   step: int = 0) -> tuple[str, list[str]]:
    """(the signature's line for this email, the General values that leave it with none) (Harry, 5 Oct 2026).

    body is the email's body as rendered (its links filled, its words); source is the copy as written,
    whose links name their page by variable. The rules, in order:
      * The body suggests booking a call (a booking link, or a phrase in BOOKING_PHRASES): the booking line,
        always, even though the body links it too.
      * Otherwise the website line is left out when the body links a page on the site, and any line is left
        out when the body already links its address.
      * A line whose General value is blank (site_url, booking_link) is left out too.
      * Among the lines left, rotation(key, step). With none left, the line keeps its placeholder in sight
        and the blank values are returned, which block the send (_FIXED_MISSING).
    """
    filled = _filled_lines(settings)
    urls = [u for _, u in body.links] + [link.url for link in copy_markup.links(source)]
    kinds = {link_kind(u, settings) for u in urls}
    if BOOKING in kinds or suggests_booking(body.words):
        choices = [BOOKING]
    else:
        linked = {_url_key(u) for u in urls}
        choices = [k for k in SIGNATURE_KINDS if not (k == WEBSITE and WEBSITE in kinds)
                   and not any(_url_key(link.url) in linked for link in copy_markup.links(filled[k][0]))]
    usable = [k for k in choices if not filled[k][1]]
    # choices is never empty: the booking line goes only when the body links its address, the first rule.
    pool = usable or choices or [BOOKING]
    kind = pool[rotation(key, step, len(pool))]
    return kind, [] if usable else list(dict.fromkeys(m for k in pool for m in filled[k][1]))


def signature(settings: Settings, sender_name: str = "", *, body: copy_markup.Rendered, source: str = "",
              key: str = "", step: int = 0,
              href: Callable[[str], str] | None = None) -> tuple[copy_markup.Rendered, list[str], str]:
    """The signature every email ends with, after the copy's sign-off (Harry, 1 and 5 Oct 2026): the
    rendered signature, the General values it is missing, and the kind of its line.

    The sender's full name, then one line and one link (Harry, 5 Oct 2026), chosen by signature_kind from
    the email's body and source, the recipient (key, signature_key) and the email's number: Spill's US site,
    Harry's booking link or the Trustpilot reviews. No postal address and no privacy link; the opt-out is
    Instantly's unsubscribe link, which the campaign's step template adds after everything here
    (clients/instantly.py). href: the address its link carries in the HTML (UTM tags, enrol/utm.py; the words
    and the plain text keep it as written).
    """
    kind, missing = signature_kind(settings, body, source, key=key, step=step)
    sig = copy_markup.render(_filled_lines(settings)[kind][0], {}, href=href)
    inner = sig.html[len("<p>"):-len("</p>")] if sig.html.count("<p>") == 1 else sig.html  # the line alone
    name = sender_name.strip()
    if name:
        inner = f'<strong style="{SIGNATURE_NAME_STYLE}">{html.escape(name, quote=False)}</strong><br>{inner}'
    return replace(sig, html=f'<p style="{SIGNATURE_STYLE}">{inner}</p>',
                   text=f"{name}\n{sig.text}" if name else sig.text), missing, kind


def signature_texts(settings: Settings, sender_name: str = "") -> set[str]:
    """Every line a signature can show, as plain text: the sender's full name and each kind's line.
    approvals takes a signature pasted into an edit back off with these (render_step adds its own)."""
    out = {copy_markup.render(line, {}).text.strip() for line, _ in _filled_lines(settings).values()}
    return out | ({sender_name.strip()} if sender_name.strip() else set())


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
    for_send: bool = True, subject: str = "",
) -> Rendered:
    """One email, signature included, with every copy-rule violation.

    for_send=False leaves out the approval and QA checks, for previews and the sheet check. subject, when given,
    is the subject as written in place of the Copy row's (email 1's personal subject, email1_subject), and goes
    through the same rules.
    """
    g = settings.general
    st = copy_row.step(step)
    if subject.strip():
        st = replace(st, subject=subject)
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
    # UTM tags on our own links, in the HTML only (General utm_links; enrol/utm.py): the copy rules and the
    # signature's choice of line read the bare addresses in body.links.
    body = copy_markup.render(st.body, variables, optional=OPTIONAL_VARIABLES,
                              href=utm.tagger(settings, copy_version=copy_row.copy_version, step=step))
    problems += [f"body {x}" for x in body.problems]

    # After the copy's sign-off: the signature, as its own paragraph, its one line chosen from the body.
    sig, missing, kind = signature(settings, mailbox.owner_name, body=body, source=st.body,
                                   key=signature_key(variables), step=step,
                                   href=utm.tagger(settings, copy_version=copy_row.copy_version, step=step,
                                                   signature=True))
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
    return Rendered(subject, sent, tuple(dict.fromkeys(problems)), step, copy_row.copy_version, text, html_body,
                    kind)


def render_sequence(
    copy_row: CopyRow, variables: Mapping[str, str], *, mailbox: Mailbox, settings: Settings, for_send: bool = True,
    subject_arm: str = COPY_SUBJECT,
) -> list[Rendered]:
    """Emails 1 to 4 of one Copy row, and the sequence's own check: it links the industry page.

    subject_arm personal gives email 1 the General email1_subject; emails 2 to 4 keep the Copy row's subjects.
    """
    first = email1_subject(subject_arm, settings)
    out = [render_step(copy_row, variables, step=n, mailbox=mailbox, settings=settings, for_send=for_send,
                       subject=first if n == SUBJECT_STEP else "")
           for n in STEPS]
    page = str(variables.get("industry_url") or "").strip()
    uses = any("industry_url" in copy_markup.VARIABLE.findall(copy_row.step(n).body) for n in STEPS)
    if page and not uses:
        out[1] = replace(out[1], violations=(*out[1].violations,
                                             "the sequence never links the industry page ({{industry_url}})"))
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
    arms = SUBJECT_ARMS if settings.general.email1_subject.strip() else (COPY_SUBJECT,)
    for row in rows:
        values = max_length_variables(row, settings)
        for mb in mailboxes:
            for arm in arms:
                cv = custom_variables(render_sequence(row, values, mailbox=mb, settings=settings, for_send=False,
                                                      subject_arm=arm))
                for k, v in cv.items():
                    out[k] = max(out[k], len(v))
    return out
