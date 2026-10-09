"""What the Industries rules read about a company, parsed one way (9 Oct 2026, the refactoring scan's phase 3).

Apollo describes a company three ways: NAICS codes (which Apollo infers), keyword tags (free text) and an industry
(LinkedIn's list, the one HubSpot and Clay also use). They come in a search row or an enrichment record (from_org)
or as the stored apollo_org facts (from_facts). Three private parsers used to read "saas, fintech" three ways and
NAICS codes four ways; RulesInput is the one best_label reads.

entity() is the check before any label is tried (the industry investigation of 9 Oct 2026: 17 of 115 companies the
model was sure of were trade associations or chambers of commerce, and the Marketing & Creative Agencies row's
codes labelled most of them). It knows only what is never an employer we prospect: business, professional, labour
and political organisations (NAICS 8139), public administration (NAICS 92), and a few tags and industries that say
so outright. A code an active Industries label claims is the sheet's, so turning such a label on reaches it.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from us_outbound.settings.model import Settings

ASSOCIATION, PUBLIC_BODY = "association", "public_body"  # as labels.ENTITIES names them

ENTITY_NAICS = {"8139": ASSOCIATION, "92": PUBLIC_BODY}
# A whole tag or Apollo's industry, in any case: "association management software" is a company that sells to them.
ENTITY_TERMS = {
    "trade association": ASSOCIATION, "trade associations": ASSOCIATION, "chamber of commerce": ASSOCIATION,
    "professional association": ASSOCIATION, "industry association": ASSOCIATION,
    "business association": ASSOCIATION, "membership organization": ASSOCIATION,
    "government administration": PUBLIC_BODY, "government agency": PUBLIC_BODY,
}
ENTITY_REASONS = {ASSOCIATION: "an association or chamber, never prospected",
                  PUBLIC_BODY: "a public body, never prospected"}


def naics_codes(value: Any) -> list[str]:
    """The NAICS codes in a value: a list, a number, or text such as "541613, 5418"."""
    if value is None:
        return []
    if isinstance(value, (list, tuple, set, frozenset)):
        return [c for v in value for c in naics_codes(v)]
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return re.findall(r"\d{2,6}", str(value))


def texts(value: Any) -> list[str]:
    """The texts in a list, or in comma-separated text, with spaces folded and blanks left out."""
    items = value if isinstance(value, (list, tuple)) else str(value).split(",") if isinstance(value, str) else ()
    return [t for t in (text(v) for v in items if isinstance(v, str)) if t]


def text(value: Any) -> str:
    return " ".join(str(value or "").split())


@dataclass(frozen=True)
class RulesInput:
    codes: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    industry: str = ""

    @classmethod
    def from_org(cls, org: Mapping[str, Any]) -> RulesInput:
        """From an Apollo search row or enrichment record."""
        codes = naics_codes(org.get("naics_codes")) or naics_codes(org.get("naics_code"))
        return cls(tuple(codes), tuple(texts(org.get("keywords"))), text(org.get("industry")))

    @classmethod
    def from_facts(cls, naics: Any, keywords: Any, industry: Any) -> RulesInput:
        """From the stored apollo_org facts' values."""
        return cls(tuple(naics_codes(naics)), tuple(texts(keywords)), text(industry))

    @property
    def keyword_text(self) -> str:
        """The tags and the industry as one text, which best_label finds the Industries tab's terms in."""
        return " ; ".join([*self.tags, self.industry]).strip(" ;")

    def __bool__(self) -> bool:
        return bool(self.codes or self.keyword_text)


def entity(inp: RulesInput, settings: Settings) -> str | None:
    """ASSOCIATION or PUBLIC_BODY when Apollo's record says so outright (the module docstring), else None."""
    claimed = [p for ind in settings.industries if ind.active for p in ind.naics_prefixes]
    for code in inp.codes:
        for prefix, kind in ENTITY_NAICS.items():
            if code.startswith(prefix) and not any(code.startswith(p) for p in claimed):
                return kind
    for t in (*inp.tags, inp.industry):
        kind = ENTITY_TERMS.get(t.lower())
        if kind:
            return kind
    return None
