"""Copy rendering: variables, the four steps, the footer and the Article 14 notice.

SPEC 10 ("Sequence", "Variables", "Rules"), SPEC 9 ("Instantly campaigns": subjects and
bodies go to Instantly as custom variables {{s1_subject}}, {{s1_body}} ...; "Sender
continuity": every demo is with Harry) and SPEC 13 (CAN-SPAM footer).

  * variables() builds the SPEC 10 variables for one account, contact and sender mailbox.
  * render_step() fills a Copy-tab row, appends the Article 14 notice on step 1 and the
    footer on every step (templates/copy/*.txt; "#" lines are comments and are stripped),
    then runs copy_rules.check(). An unknown or empty variable is a violation and stays
    visible in the text. Any violation blocks the send.
  * render_sequence() renders steps 1 to 4 of one copy version; custom_variables() turns
    them into the lead's Instantly custom variables.
  * pick_opener() falls back to the angle's default_opener when the evidence opener breaks
    a copy rule (for example "unlimited PTO" quoted from a benefits page).
  * max_rendered_lengths() is the phase-0 probe for Instantly's custom-variable limit.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from us_outbound.enrol import copy_rules
from us_outbound.settings.model import CopyRow, Mailbox, Settings

STEPS = (1, 2, 3, 4)
VARIABLES = (
    "first_name", "company", "opener", "proof", "place", "ask", "price_line", "signature", "demo_line", "legal_overlay",
)
OPTIONAL_VARIABLES = frozenset({"legal_overlay"})  # empty unless the account is in Legal Teams
LEGAL_GROUP = "Legal Teams"

TEMPLATES_DIR = Path(os.environ.get("US_OUTBOUND_TEMPLATES") or Path(__file__).resolve().parents[2] / "templates") / "copy"
FOOTER_TEMPLATE = "footer.txt"
ARTICLE14_TEMPLATE = "article14.txt"

# SPEC 10 "ask", by role (Roles tab names). PHASE0-CONFIRM: the full sentences are ours.
PEOPLE_LEADER, FOUNDER, OPERATIONS = "People leader", "Founder or executive", "Operations"
ASKS = {
    PEOPLE_LEADER: "Would a 20-minute walkthrough be useful?",
    FOUNDER: "Worth a look for the team?",
    OPERATIONS: "Happy to send the one-pager if that's useful.",
}
# "When the sender is not Harry, the demo ask names Harry as the host" (SPEC 10).
HOSTED_ASKS = {
    PEOPLE_LEADER: "Would a 20-minute walkthrough with my colleague {host}, who runs our US demos, be useful?",
}

# SPEC 4 US prices, Core plan, flat monthly by team size: (up to this many employees, dollars).
CORE_PRICES = ((10, 195), (25, 250), (50, 350), (100, 495), (200, 995))
PER_EMPLOYEE_PRICE = 5  # 201+: $5 per employee
PRICE_LINE = "For a team your size it's ${dollars} a month, flat."
PER_EMPLOYEE_LINE = "For a team your size it's ${dollars} per employee a month."

DEFAULT_SIGNATURE = "{owner}\nSpill\nspill.chat/us"  # SPEC 5 Mailboxes; SPEC 15 open item
DEMO_LINE_HOST = "grab a time with me: {booking_link}"  # SPEC 9, 10
DEMO_LINE_OTHER = "my colleague {host} runs our US demos; you can grab a time with him here: {booking_link}"

_VARIABLE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")
_PLACEHOLDER = re.compile(r"(?<!\{)\{([a-z_]+)\}(?!\})")
_FOOTER_MISSING = {
    "sender": "the mailbox has no owner_name for the footer",
    "postal_address": "postal_address is blank on the General tab, so the footer has no postal address",
    "privacy_url": "privacy_url is blank on the General tab, so the footer has no opt-out link",
    "company": "the Article 14 notice has no company name",
}


@dataclass(frozen=True)
class Rendered:
    subject: str
    body: str
    violations: tuple[str, ...] = ()
    step: int = 0
    copy_version: str = ""

    @property
    def ok(self) -> bool:
        return not self.violations


# -- variables (SPEC 10) ---------------------------------------------------------------


def is_demo_host(mailbox: Mailbox, settings: Settings) -> bool:
    """True when the sender is the demo host (Harry): "grab a time with me"."""
    host = settings.general.demo_host.strip().casefold()
    return bool(host) and mailbox.owner_name.strip().casefold() == host


def _int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(float(str(value).replace(",", "").strip()))
    except ValueError:
        return None


def price_line(employees: Any) -> str:
    """The Core plan price for this team size (SPEC 4), e.g. "For a team your size it's $350 a month, flat."."""
    n = _int(employees)
    if n is None or n < 1:
        return ""
    for most, dollars in CORE_PRICES:
        if n <= most:
            return PRICE_LINE.format(dollars=dollars)
    return PER_EMPLOYEE_LINE.format(dollars=PER_EMPLOYEE_PRICE)


def ask_for(role: str | None, *, sender_is_host: bool, host: str) -> str:
    role = (role or "").strip()
    if not sender_is_host and role in HOSTED_ASKS:
        return HOSTED_ASKS[role].format(host=host)
    return ASKS.get(role, "")


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


def signature_for(mailbox: Mailbox) -> str:
    return mailbox.signature.strip() or DEFAULT_SIGNATURE.format(owner=mailbox.owner_name.strip())


def demo_line_for(mailbox: Mailbox, settings: Settings) -> str:
    g = settings.general
    booking = g.booking_link.strip()
    if not booking:
        return ""
    if is_demo_host(mailbox, settings):
        return DEMO_LINE_HOST.format(booking_link=booking)
    return DEMO_LINE_OTHER.format(host=g.demo_host.strip(), booking_link=booking)


def variables(
    account: Mapping[str, Any],
    contact: Mapping[str, Any],
    mailbox: Mailbox,
    settings: Settings,
    *,
    opener: str,
    legal_overlay: str,
) -> dict[str, str]:
    """The SPEC 10 variables for one lead. Overrides for the account's domain win (SPEC 5, 13)."""
    domain = str(account.get("domain") or "").strip().lower()
    acct = {**account, **(settings.overrides_for(domain) if domain else {})}
    host = is_demo_host(mailbox, settings)
    legal = str(acct.get("industry_group") or "").strip().casefold() == LEGAL_GROUP.casefold()
    employees = acct.get("employees")
    if _int(employees) is None:
        employees = acct.get("us_employees")
    return {
        "first_name": str(contact.get("first_name") or "").strip(),
        "company": str(acct.get("clean_name") or "").strip(),
        "opener": (opener or "").strip(),
        "proof": proof_for(acct, settings),
        "place": place_for(acct),
        "ask": ask_for(contact.get("role"), sender_is_host=host, host=settings.general.demo_host.strip()),
        "price_line": price_line(employees),
        "signature": signature_for(mailbox),
        "demo_line": demo_line_for(mailbox, settings),
        "legal_overlay": (legal_overlay or "").strip() if legal else "",
    }


def pick_opener(
    opener: str,
    default_opener: str,
    *,
    sender_is_harry: bool = True,
    demo_host: str = "Harry Dryden",
    exempt: Iterable[str] = (),
) -> tuple[str, str]:
    """(opener to use, why the default was used or ""). An opener that breaks a copy rule is replaced.

    SPEC 9 step 5 fills the opener with evidence from the prospect's own pages, which can
    carry words the copy may not use ("unlimited PTO", "100% employer-paid", "therapy").
    """
    opener, default_opener = (opener or "").strip(), (default_opener or "").strip()
    if not opener:
        return default_opener, ""
    problems = copy_rules.content_violations(
        opener, sender_is_harry=sender_is_harry, demo_host=demo_host, exempt=exempt
    ) + copy_rules.structure_violations(opener)
    if problems and default_opener:
        return default_opener, "; ".join(problems)
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


def substitute(text: str, values: Mapping[str, str]) -> tuple[str, list[str]]:
    """Fill {{variables}}. Unknown or empty ones (other than OPTIONAL_VARIABLES) stay visible and are reported."""
    problems: list[str] = []

    def one(m: re.Match[str]) -> str:
        name = m.group(1)
        if name not in values:
            problems.append(f"has the unknown variable {{{{{name}}}}}")
            return m.group(0)
        value = str(values[name] or "").strip()
        if not value:
            if name in OPTIONAL_VARIABLES:
                return ""
            problems.append(f"has the empty variable {{{{{name}}}}}")
            return m.group(0)
        return value

    return _VARIABLE.sub(one, text), problems


def _tidy(body: str) -> str:
    """Trailing spaces off every line; at most one blank line in a row (an empty overlay leaves a gap)."""
    lines = [line.rstrip() for line in body.replace("\r\n", "\n").split("\n")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def footer(mailbox: Mailbox, settings: Settings) -> tuple[str, list[str]]:
    """The footer every step ends with (SPEC 10, 13): sender and role, postal address, ad line, opt-out, privacy link."""
    g = settings.general
    name, role = mailbox.owner_name.strip(), mailbox.owner_role.strip()
    return fill(
        load_template(FOOTER_TEMPLATE),
        {"sender": f"{name}, {role}" if name and role else name, "postal_address": g.postal_address,
         "privacy_url": g.privacy_url},
    )


def article14(values: Mapping[str, str]) -> tuple[str, list[str]]:
    """Step 1's UK GDPR Article 14 notice: where the data came from and the lawful basis (SPEC 10)."""
    return fill(load_template(ARTICLE14_TEMPLATE), {"company": values.get("company", "")})


# -- rendering ------------------------------------------------------------------------------


def render_step(
    copy_row: CopyRow, variables: Mapping[str, str], *, step: int, mailbox: Mailbox, settings: Settings
) -> Rendered:
    """One step, footer and (step 1) Article 14 notice included, with every copy-rule violation."""
    g = settings.general
    problems: list[str] = []
    if copy_row.step != step:
        problems.append(f"copy {copy_row.copy_version} row is step {copy_row.step}, rendered as step {step}")
    subject, p = substitute(copy_row.subject, variables)
    problems += [f"subject {x}" for x in p]
    body, p = substitute(copy_row.body, variables)
    problems += [f"body {x}" for x in p]
    subject = " ".join(subject.split())

    parts = [_tidy(body)]
    if step == 1:
        notice, missing = article14(variables)
        problems += [_FOOTER_MISSING.get(m, f"the Article 14 notice has no {m}") for m in missing]
        parts.append(notice)
    foot, missing = footer(mailbox, settings)
    problems += [_FOOTER_MISSING.get(m, f"the footer has no {m}") for m in missing]
    parts.append(foot)
    full = "\n\n".join(x for x in parts if x)

    exempt = (variables.get("first_name", ""), variables.get("company", ""), variables.get("place", ""),
              g.postal_address, mailbox.owner_name)
    problems += copy_rules.check(
        subject, full, copy_row=copy_row, step=step, sender_is_harry=is_demo_host(mailbox, settings),
        privacy_url=g.privacy_url, demo_host=g.demo_host, exempt=exempt,
    )
    return Rendered(subject, full, tuple(dict.fromkeys(problems)), step, copy_row.copy_version)


def copy_rows(settings: Settings, copy_version: str) -> dict[int, CopyRow]:
    """The Copy-tab row for each step of a version; an approved row wins over a draft of the same step."""
    rows: dict[int, CopyRow] = {}
    for c in settings.copy:
        if c.copy_version != copy_version:
            continue
        if c.step not in rows or (c.status == "approved" and rows[c.step].status != "approved"):
            rows[c.step] = c
    return rows


def render_sequence(
    copy_version: str, variables: Mapping[str, str], *, mailbox: Mailbox, settings: Settings
) -> list[Rendered]:
    """Steps 1 to 4 of one copy version; a missing step is a violation."""
    rows = copy_rows(settings, copy_version)
    out: list[Rendered] = []
    for step in STEPS:
        row = rows.get(step)
        if row is None:
            out.append(Rendered("", "", (f"copy {copy_version} has no step {step} row",), step, copy_version))
        else:
            out.append(render_step(row, variables, step=step, mailbox=mailbox, settings=settings))
    return out


def violations(rendered: Sequence[Rendered]) -> list[str]:
    return [f"step {r.step}: {v}" for r in rendered for v in r.violations]


def custom_variables(rendered: Sequence[Rendered]) -> dict[str, str]:
    """{"s1_subject", "s1_body", ..., "s4_body"}: the lead's Instantly custom variables (SPEC 9)."""
    out: dict[str, str] = {}
    for r in sorted(rendered, key=lambda r: r.step):
        out[f"s{r.step}_subject"] = r.subject
        out[f"s{r.step}_body"] = r.body
    return out


# -- empty and maximum-length values (SPEC 10 tests; SPEC 9 custom-variable limit) ----------

# The longest value each free-text variable can plausibly take. An opener is its template
# plus a quote of up to 300 characters (SPEC 6), and a proof point is free sheet text, so both
# can pass the 300-character line limit.
MAX_LENGTHS = {"first_name": 40, "company": 100, "opener": 340, "proof": 400, "place": 60, "legal_overlay": 120}


def _filler(n: int) -> str:
    """Exactly n characters of plain letters and spaces."""
    s = ("abcdefghi " * (n // 10 + 1))[:n]
    return s[:-1] + "x" if s.endswith(" ") else s


def empty_variables() -> dict[str, str]:
    return {v: "" for v in VARIABLES}


def max_length_variables(mailbox: Mailbox, settings: Settings) -> dict[str, str]:
    """Every variable at its longest: free text at MAX_LENGTHS, fixed choices at their longest option."""
    host = settings.general.demo_host.strip()
    asks = list(ASKS.values()) + [a.format(host=host) for a in HOSTED_ASKS.values()]
    prices = [price_line(most) for most, _ in CORE_PRICES] + [price_line(CORE_PRICES[-1][0] + 1)]
    values = {k: _filler(n) for k, n in MAX_LENGTHS.items()}
    values.update(
        ask=max(asks, key=len),
        price_line=max(prices, key=len),
        signature=signature_for(mailbox),
        demo_line=demo_line_for(mailbox, settings),
    )
    return values


def max_rendered_lengths(settings: Settings, copy_versions: Iterable[str] | None = None) -> dict[str, int]:
    """The longest each custom variable gets, over every copy version and registry mailbox.

    Phase 0 sends a lead with values this long to find Instantly's custom-variable limit
    (SPEC 9: "Test the custom-variable length limit in phase 0").
    """
    versions = list(dict.fromkeys(copy_versions if copy_versions is not None else (c.copy_version for c in settings.copy)))
    mailboxes = [m for m in settings.mailboxes if m.status != "Retired"]
    out = {f"s{step}_{part}": 0 for step in STEPS for part in ("subject", "body")}
    for version in versions:
        for mb in mailboxes:
            cv = custom_variables(render_sequence(version, max_length_variables(mb, settings), mailbox=mb, settings=settings))
            for k, v in cv.items():
                out[k] = max(out[k], len(v))
    return out
