"""The {{variables}} a Copy row may use, and the price line's wording (moved from enrol/render.py, 9 Oct 2026).

render.variables fills them for each lead; settings_sync checks that the sheet's copy names only these.
"""

from __future__ import annotations

VARIABLES = (
    "first_name", "company", "place", "opener", "legal_overlay", "role_line", "price_line", "demo_url",
    "industry_url", "site_url", "sender_first_name", "proof",
)
OPTIONAL_VARIABLES = frozenset({"opener", "legal_overlay"})  # alone on their line; the line goes when empty

# Harry, 1 Oct 2026: one starting price in every email, as on the website, whatever the team's size
# (General price_from). SPEC 4's price-by-size table is not quoted.
PRICE_LINE = "Plans start from ${dollars} a month for the whole team, on a rolling 30-day contract."
