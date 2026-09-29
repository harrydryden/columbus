"""Angle and opener for an account (SPEC 9 "Scoring" steps 4 and 5; SPEC 5 Angles).

  * Angle: the first ACTIVE angle, in Angles-tab order, that any matched signal suggests.
    Control accounts always get General; with no suggestion the angle is General too.
  * Opener: the opener template of the signal that set the angle, filled with its
    evidence. If that signal has no opener, the angle's default_opener.
  * Legal Teams also gets an overlay line, returned separately (copy variable {{legal_overlay}}).

When several matched signals suggest the chosen angle, the one with the largest
weight_applied set it (ties: Signals-tab order).
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, NamedTuple

from us_outbound.settings.model import Settings

if TYPE_CHECKING:
    from us_outbound.scoring.score import Evidence, Match

GENERAL = "General"  # SPEC 5: Control-tier accounts always get this angle
CONTROL = "Control"
LEGAL_GROUP = "Legal Teams"
LEGAL_OVERLAY = "The bar's Lawyer Assistance Program covers attorneys. Who covers paralegals and staff?"

# Placeholders an opener may use. validate.py allows only {evidence} on the sheet today.
_PLACEHOLDER = re.compile(r"(?<!\{)\{\s*(evidence|quote|url)\s*\}(?!\})")


class AngleChoice(NamedTuple):
    angle: str
    opener: str
    legal_overlay: str  # "" unless the account is in Legal Teams


def legal_overlay(industry_group: str | None) -> str:
    return LEGAL_OVERLAY if (industry_group or "").strip().casefold() == LEGAL_GROUP.casefold() else ""


def _is_acronym(word: str) -> bool:
    """EAP, PTO, HRIS; or a capital after the first letter, as in ComPsych or BetterUp.

    Each hyphen or slash part is judged on its own, so "Employer-Paid" is not a brand.
    """
    for part in re.split(r"[-/]", word):
        letters = [c for c in part if c.isalpha()]
        if len(letters) >= 2 and (all(c.isupper() for c in letters) or any(c.isupper() for c in part[1:])):
            return True
    return False


def evidence_display(ev: Evidence) -> str:
    """Evidence text as it reads mid-sentence.

    A term match is lower-cased word by word, except where the sheet term has capitals
    (a brand or acronym: its sheet spelling is used) or the page word is an acronym.
    Other evidence (a quote, or "field = value") is used as it is.
    """
    if not ev.term:
        return ev.text
    got, want = ev.text.split(), ev.term.split()
    if len(want) != len(got):
        want = [""] * len(got)
    out = []
    for g, w in zip(got, want):
        if any(c.isupper() for c in w):
            out.append(w)
        elif _is_acronym(g):
            out.append(g)
        else:
            out.append(g.lower())
    return " ".join(out)


def fill_opener(template: str, ev: Evidence) -> str:
    values = {"evidence": evidence_display(ev), "quote": ev.quote, "url": ev.url}
    return _PLACEHOLDER.sub(lambda m: values[m.group(1)], template).strip()


def _default_opener(settings: Settings, name: str) -> str:
    a = settings.angle(name)
    return a.default_opener if a else ""


def angle_setter(angle: str, matches: list[Match]) -> Match | None:
    """The matched signal that set this angle: the largest weight_applied, then sheet order."""
    suggesting = [m for m in matches if m.signal.suggests_angle.strip().casefold() == angle.casefold()]
    return max(suggesting, key=lambda m: m.weight_applied) if suggesting else None


def choose_angle(tier: str, matches: list[Match], settings: Settings, industry_group: str | None = "") -> AngleChoice:
    """(angle, opener, legal_overlay) for one account."""
    overlay = legal_overlay(industry_group)
    if tier == CONTROL:
        return AngleChoice(GENERAL, _default_opener(settings, GENERAL), overlay)
    suggested = {m.signal.suggests_angle.strip().casefold() for m in matches if m.signal.suggests_angle.strip()}
    for a in settings.active_angles():
        if a.angle.casefold() not in suggested:
            continue
        setter = angle_setter(a.angle, matches)
        if setter and setter.signal.opener.strip() and setter.evidence:
            return AngleChoice(a.angle, fill_opener(setter.signal.opener, setter.evidence[0]), overlay)
        return AngleChoice(a.angle, a.default_opener, overlay)
    return AngleChoice(GENERAL, _default_opener(settings, GENERAL), overlay)
