"""What a reply class counts as, in one place (9 Oct 2026, the refactoring scan's phase 6).

Five reports each had their own copy: an out-of-office reply was a reply in the daily post and not in the Monday
readout, and "positive or referral" was written out six times. A human reply is any class but out_of_office (an
away message is not a person writing back); a warm one is positive or referral. replies/classify.py sets the class.
"""

from __future__ import annotations

from typing import Any

OUT_OF_OFFICE = "out_of_office"
WARM = frozenset({"positive", "referral"})


def is_human(reply_class: Any) -> bool:
    """A person wrote back: any class but out_of_office (a blank class counts, as the reply happened)."""
    return str(reply_class or "").strip().lower() != OUT_OF_OFFICE


def is_warm(reply_class: Any) -> bool:
    return str(reply_class or "").strip().lower() in WARM
