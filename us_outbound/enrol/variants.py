"""Copy variants: a test that changes one part of one email for every account (Harry, 7 Oct 2026).

Harry: "I want to create A/B functionality to allow the system to try different versions of email copy.
Specifically to start with I would like to have an A/B where in the A the opening line of the first email in the
sequence is a warm intro 'I hope you're really well. Great to be connected.' and the B version doesn't have that."

A Tests-tab row of kind variant names one email (email, 1 to 4; 1 when blank), one change and each arm's text:
  first_line  a line of its own straight after "Hi {{first_name}},", before the opener line;
  last_line   a line of its own just before the sign-off, "Best wishes,";
  replace     the exact text in find, as written on the Copy tab, wherever it is in that email's body;
  subject     that email's subject.
text_a is what version_a's accounts get and text_b version_b's; a blank text leaves that arm's email as the Copy
row has it, so the warm-intro test is text_a the line and text_b blank. version_a and version_b name the arms
("warm intro", "no intro").

text_violations() is the check settings_sync makes of each text (settings/validate.py), and `us-outbound test start`
with it: the copy rules that apply to a line of body (copy_rules.line_violations) or to a subject
(copy_rules.subject_violations), on the text as written and as filled with SAMPLE_VALUES. A text may use the render
variables ({{first_name}}, {{company}} and the rest of render.VARIABLES) but not {{opener}} or {{legal_overlay}},
which are lines of their own that the Copy row places. coverage() is what `test start` reports: the sendable Copy
rows the change can be made in.

Who is in it (choose). Every account enrolled from start_date on, whatever its tier (Control too), industry, role or
Copy row: "a" or "b" by sha256(account_id + test_id) (queue.test_version, as an ab test splits), half and half
unless the row's share_a says otherwise (Harry, 8 Oct 2026: "a warm greeting on most but not all of the email 1s";
70% gives version_a seven accounts in ten). The opener holdout
and the subject split hash the account id with salts of their own, so the three splits are independent and the
designs factorial. A second contact gets its account's arm when its first contact is in the test, and is never
counted as a new account. Each arm takes its cap (accounts_per_version, counted in the smaller arm and scaled to the
larger: Test.cap); once an arm has them, the accounts it would
have had get the Copy row as it is and are not in the test (nor are any once Harry sets the test read or stopped).

Where the change cannot be made, the account is not in the test, whichever arm it hashes to: a replace whose find is
not in its Copy row's email, a subject change to an email 1 that has the personal subject (render.subject_arm), or a
change that would break a copy rule in this contact's email under either arm (a line made too long by a long company
name, a word count pushed over its limit). The contact gets the Copy row's email as it is, no arm is recorded
(contacts.test_id and test_arm stay NULL), and the card and the enrol summary say why. Leaving out only the arm that
cannot be made would bias the test (the long names and long emails would all fall in the other arm); leaving out the
account under both keeps the arms alike, at the cost of a slightly smaller test. With the warm intro, every one of
the 318 Copy rows' email 1 has room for it (the longest is 104 words of the 120 allowed).

enrol.prepare renders the arm (render.render_sequence(written=...)), so the email Instantly sends (HTML and text), the
card, its editable source and the lead's custom variables all carry it. The Copy row's own approval and QA still
decide whether it may be sent at all (the texts were checked at sync, and every rule is checked again on the email as
it will be sent), and its copy_hash is stamped as before. The contact records test_id and test_arm (enrol.
_record_enrolled), and learn/looks.py reads the arms from them.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date
from typing import Any

from us_outbound.enrol import copy_markup, copy_rules, queue, render
from us_outbound.settings.model import (
    FIRST_LINE,
    LAST_LINE,
    REPLACE,
    SUBJECT,
    VARIANT_ARMS,
    VARIANT_TEST,
    CopyRow,
    CopyStep,
    Settings,
    Test,
)

# The variables a variant's text may use: every render variable but the optional lines, which the Copy row places.
TEXT_VARIABLES = tuple(v for v in render.VARIABLES if v not in render.OPTIONAL_VARIABLES)
# A made-up prospect and sender for the check at sync (copy_desk's sample prospect); each email is checked again
# for its own lead when it is rendered.
SAMPLE_VALUES = {
    "first_name": "Dana", "company": "Harbor & Finch", "place": "Boston, MA",
    "role_line": "You want support your team will use.", "price_line": render.PRICE_LINE.format(dollars=195),
    "demo_url": "https://www.spill.chat/us/book-demo", "industry_url": "https://www.spill.chat/us/industry",
    "site_url": "https://www.spill.chat/us", "sender_first_name": "Hannah", "proof": "Teams like yours use Spill.",
}
EXEMPT = ("Dana", "Harbor & Finch", "Boston, MA", "Hannah")  # proper nouns, not our wording (copy_rules._mask)
LINES = (FIRST_LINE, LAST_LINE)  # the changes that add a line of its own

# The copy-rule problems of one email of a contact's Copy row, written as given and rendered for that contact.
Check = Callable[[int, CopyStep], Sequence[str]]


def text_violations(text: str, *, change: str, email: int) -> list[str]:
    """Why one arm's text may not go in its email, or []: the copy rules that apply to it alone (module docstring).

    A subject takes the subject rules, with the email's own ask rule (only email 1 may not name a demo). A line of its
    own (first_line, last_line) is one line with no link: the body has one link, its call to action. A replacement
    may span lines and carry a link (find may hold one); the rules on the whole email decide that, at render time.
    The variables are settings/validate.py's to check first."""
    if change == SUBJECT:
        out = ["is more than one line; a subject is one line"] if "\n" in text else []
        if copy_markup.links(text) or "**" in text:
            out.append("has markup; a subject is plain text")
        filled, _ = copy_markup.fill_text(text, SAMPLE_VALUES)
        return out + copy_rules.subject_violations(filled, exempt=EXEMPT, step=email)
    out = []
    if change in LINES and "\n" in text:
        out.append("is more than one line; it is a line of its own in the email")
    if change in LINES and copy_markup.links(text):
        out.append("has a link; the body's one link is its call to action, so a line of its own carries none")
    rendered = copy_markup.render(text, SAMPLE_VALUES)
    return out + rendered.problems + copy_rules.line_violations(text, rendered.words, step=email, exempt=EXEMPT)


# -- one email, changed ------------------------------------------------------------------------------------------------


def _strip_blank(lines: Sequence[str], *, end: bool) -> list[str]:
    out = list(lines)
    while out and not out[-1 if end else 0].strip():
        out.pop(-1 if end else 0)
    return out


def apply(st: CopyStep, change: str, text: str, find: str = "") -> CopyStep | None:
    """The email as written with one arm's change; the email itself when the text is blank. None when the change
    cannot be made: find is not in the body, or the body has no greeting ("Hi {{first_name}},") or sign-off
    ("Best wishes,") to place the line by. A line of its own is a paragraph: a blank line either side."""
    text = text.strip()
    if not text:
        return st
    if change == SUBJECT:
        return replace(st, subject=text)
    if change == REPLACE:
        return replace(st, body=st.body.replace(find, text)) if find and find in st.body else None
    lines = st.body.replace("\r\n", "\n").split("\n")
    filled = [i for i, line in enumerate(lines) if line.strip()]
    if change == FIRST_LINE:
        if not filled or not copy_rules.GREETING.match(lines[filled[0]].strip()):
            return None
        at = filled[0] + 1
    elif change == LAST_LINE:
        if len(filled) < 2 or lines[filled[-2]].strip() not in copy_rules.SIGN_OFFS:
            return None
        at = filled[-2]
    else:
        return None
    before, after = _strip_blank(lines[:at], end=True), _strip_blank(lines[at:], end=False)
    return replace(st, body="\n".join([*before, "", text, "", *after]))


def arms_written(test: Test, row: CopyRow, check: Check) -> tuple[dict[str, CopyStep], str]:
    """Each arm's email (test.email of the Copy row) as written with its change, and checked: ({arm: email}, "") when
    the change can be made under both arms, else ({}, why not). An arm whose email breaks a rule the Copy row's own
    email does not is why not; a rule the Copy row's email breaks already is the enrol run's to report."""
    original = row.step(test.email)
    written = {arm: apply(original, test.change, test.text(arm), test.find) for arm in VARIANT_ARMS}
    if any(w is None for w in written.values()):
        if test.change == REPLACE:
            return {}, f"its email {test.email} ({row.copy_version}) does not have the text the test replaces"
        return {}, f"its email {test.email} ({row.copy_version}) has no greeting or sign-off line to place it by"
    base = set(check(test.email, original))
    for arm, st in written.items():
        if st is original:
            continue
        extra = [p for p in check(test.email, st) if p not in base]
        if extra:
            return {}, f"{test.arm_name(arm)} would break a copy rule in its email {test.email}: {extra[0]}"
    return written, ""  # type: ignore[return-value]  # no None left


# -- one contact -------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Arm:
    """The running variant test as it applies to one contact."""

    test_id: str
    arm: str = ""  # "a" or "b" when the contact is in the test; "" when not
    written: Mapping[int, CopyStep] = field(default_factory=dict)  # email -> as written with the arm's change
    note: str = ""  # why the contact is not in the test, else ""


def arm_for(test: Test, account_id: Any) -> str:
    """"a" or "b" by sha256(account_id + test_id), in the test's share_a (queue.test_version), as an ab test splits."""
    return queue.test_version(str(account_id), test.test_id, test.share_a)


def choose(test: Test | None, account: Mapping[str, Any], row: CopyRow, *, today: date, subject_arm: str,
           counts: Mapping[str, int], check: Check, first: Mapping[str, Any] | None = None) -> Arm | None:
    """The running variant test's arm for this contact (module docstring): None when no variant test runs, before its
    start_date, or for a second contact whose first contact is not in it; an Arm with a note when the account is not
    in the test (its arm full, or the change cannot be made here); else its arm and the email it changes. counts:
    accounts in each arm so far (enrol.running_test_counts). first: a second contact's first contact
    (second.first_summary), whose arm it shares."""
    if test is None or test.kind != VARIANT_TEST or (test.start_date is not None and today < test.start_date):
        return None
    if first is not None and str(first.get("test_id") or "") != test.test_id:
        return None
    arm = arm_for(test, account["account_id"])
    if first is None and 0 < test.cap(arm) <= counts.get(arm, 0):
        return Arm(test.test_id, note=f"{test.arm_name(arm)} has its {test.cap(arm)} accounts")
    if test.change == SUBJECT and test.email == render.SUBJECT_STEP and subject_arm == render.PERSONAL_SUBJECT:
        return Arm(test.test_id, note="its email 1 has the personal subject (General email1_subject), which the test "
                                      "does not change")
    written, why = arms_written(test, row, check)
    if why:
        return Arm(test.test_id, note=why)
    st = written[arm]
    return Arm(test.test_id, arm, {test.email: st} if st is not row.step(test.email) else {})


def coverage(test: Test, rows: Iterable[CopyRow], settings: Settings) -> tuple[list[str], dict[str, str]]:
    """(the Copy rows the change can be made in, {copy_version: why not} for the rest), each row rendered for
    copy_desk's sample prospect in its first role, sent by the first sample mailbox, with the generic opener line:
    what `test start` reports. A contact's own email is checked again when it is rendered."""
    from us_outbound.enrol import copy_desk  # it builds on render, as this module does

    mailbox = copy_desk.sample_mailboxes(settings)[0]
    ok: list[str] = []
    not_: dict[str, str] = {}
    for row in rows:
        contact = {**copy_desk.SAMPLE_CONTACT, "role": copy_desk.roles_for(row)[0]}
        values = render.variables(copy_desk.sample_account(row, settings), contact, mailbox, settings, copy_row=row,
                                  opener=copy_desk.SAMPLE_OPENER)

        def check(n: int, st: CopyStep, row: CopyRow = row, values: dict[str, str] = values) -> list[str]:
            return list(render.render_step(row, values, step=n, mailbox=mailbox, settings=settings, for_send=False,
                                           written=st).violations)

        _, why = arms_written(test, row, check)
        if why:
            not_[row.copy_version] = why
        else:
            ok.append(row.copy_version)
    return ok, not_
