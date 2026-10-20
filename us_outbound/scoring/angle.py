"""The angle for an account (SPEC 9 "Scoring" step 4; SPEC 5 Angles).

The first ACTIVE angle, in Angles-tab order, that any matched signal suggests; with no suggestion, General. When
several matched signals suggest the chosen angle, the one with the largest weight_applied set it (ties: Signals-tab
order: angle_setter), and enrol/openers.py takes the email's opener from that signal's lines.

W2-2 (20 Oct 2026): every account gets its angle this way. The Control tier's "always General" rule went with the
tiers (Harry's D7: the random explore arm uses its normal copy angle), and so did the scoring-time opener and the
Legal Teams overlay, which nothing read: the opener is chosen at enrol time (enrol/openers.py), with the overlay.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from us_outbound.settings.model import Settings

if TYPE_CHECKING:
    from us_outbound.scoring.score import Match

GENERAL = "General"  # SPEC 5: the angle of an account no active angle suits


def angle_setter(angle: str, matches: list[Match]) -> Match | None:
    """The matched signal that set this angle: the largest weight_applied, then sheet order."""
    suggesting = [m for m in matches if m.signal.suggests_angle.strip().casefold() == angle.casefold()]
    return max(suggesting, key=lambda m: m.weight_applied) if suggesting else None


def choose_angle(matches: list[Match], settings: Settings) -> str:
    """The account's angle: the first active angle a matched signal suggests, else General."""
    suggested = {m.signal.suggests_angle.strip().casefold() for m in matches if m.signal.suggests_angle.strip()}
    return next((a.angle for a in settings.active_angles() if a.angle.casefold() in suggested), GENERAL)
