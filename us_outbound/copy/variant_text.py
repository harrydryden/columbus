"""A variant test's text, checked on its own (Harry, 7 Oct 2026; moved from enrol/variants.py, 9 Oct 2026).

text_violations() is the check settings_sync makes of each text on the Tests tab (settings/validate.py): the copy
rules that apply to a line of body (copy_rules.line_violations) or to a subject (copy_rules.subject_violations), on
the text as written and as filled with SAMPLE_VALUES. A text may use the render variables ({{first_name}},
{{company}} and the rest of variables.VARIABLES) but not {{opener}} or {{legal_overlay}}, which are lines of their
own that the Copy row places. enrol/variants.py makes the change in each contact's email, where every rule is
checked again on the email as it will be sent.
"""

from __future__ import annotations

from us_outbound.copy import copy_markup, copy_rules
from us_outbound.copy.variables import OPTIONAL_VARIABLES, PRICE_LINE, VARIABLES
from us_outbound.settings.model import FIRST_LINE, LAST_LINE, SUBJECT

# The variables a variant's text may use: every render variable but the optional lines, which the Copy row places.
TEXT_VARIABLES = tuple(v for v in VARIABLES if v not in OPTIONAL_VARIABLES)
# A made-up prospect and sender for the check at sync (copy_desk's sample prospect); each email is checked again
# for its own lead when it is rendered.
SAMPLE_VALUES = {
    "first_name": "Dana", "company": "Harbor & Finch", "place": "Boston, MA",
    "role_line": "You want support your team will use.", "price_line": PRICE_LINE.format(dollars=195),
    "demo_url": "https://www.spill.chat/us/book-demo", "industry_url": "https://www.spill.chat/us/industry",
    "site_url": "https://www.spill.chat/us", "sender_first_name": "Hannah", "proof": "Teams like yours use Spill.",
}
EXEMPT = ("Dana", "Harbor & Finch", "Boston, MA", "Hannah")  # proper nouns, not our wording (copy_rules._mask)
LINES = (FIRST_LINE, LAST_LINE)  # the changes that add a line of its own


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
