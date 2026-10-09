"""The settings sheet's columns, declared once per tab (9 Oct 2026, the refactoring scan's phase 5).

Adding a column used to take edits in four places that had to agree: the tab's column list (defaults.COLUMNS),
the optional columns (validate.TAB_OPTIONAL_COLUMNS), the key columns (validate.KEY_COLUMNS, and a second copy in
settings/load.py) and which tabs depend on which (settings/sync.DEPENDS_ON). Each tab is now a tuple of Col, in the
sheet's order, and those four are derived from it.

  * optional: a sheet without the column reads it as blank. A column added after the sheet was first made must stay
    optional for good, since rows stored under older code are validated again by newer code (since says when).
  * key: the row's key, part of the natural key settings_sync versions rows by, unique across the tab whatever
    its case (validate._prepare).
  * ref: the tab its values name rows on. If that tab has to keep its previous version, so does this one
    (settings_sync), and the other way round.
"""

from __future__ import annotations

from dataclasses import dataclass

from us_outbound.settings.model import OPENER_COLUMNS, OPENER_SELF_COLUMN, PAGE_FIELDS, TABS


@dataclass(frozen=True)
class Col:
    name: str
    optional: bool = False
    key: bool = False
    since: str = ""  # when an optional column was added (Harry's decision date)
    ref: str = ""  # the tab whose rows its values name


def _cols(*names: str, **kw) -> tuple[Col, ...]:
    return tuple(Col(n, **kw) for n in names)


NOTE = Col("note", optional=True)

SPEC: dict[str, tuple[Col, ...]] = {
    "General": (Col("key", key=True), Col("value"), NOTE),
    "Signals": (
        Col("signal", key=True), *_cols("source", "looks_for", "context_rule", "weight", "max_weight", "action"),
        Col("suggests_angle", ref="Angles"), Col("opener"),
        *_cols(*OPENER_COLUMNS.values(), OPENER_SELF_COLUMN, optional=True, since="2026-10-02"),  # tokenized openers
        Col("counts_for_days"), Col("active"), NOTE,
    ),
    "Angles": (Col("angle", key=True), *_cols("order", "argument", "default_opener", "landing_page_override", "active"),
               NOTE),
    "Industries": (
        Col("industry", key=True), *_cols("industry_group", "active", "naics_prefixes", "exclude_naics",
                                          "apollo_keywords", "landing_page_url", "proof_point", "priority"),
        # The industry's spill.chat page, as material for its emails.
        *_cols(*(f"page_{f}" for f in PAGE_FIELDS), optional=True, since="2026-09-30"),
        # What a company under the label is and is not, for the label check; a blank cell takes the build's line.
        Col("definition", optional=True, since="2026-10-07"), NOTE,
    ),
    "States": (Col("state", key=True), Col("active"), NOTE),
    # One row per group of titles, with its order at each size (Harry, 1 Oct 2026).
    "Roles": (Col("role", key=True), Col("copy_role", optional=True, since="2026-10-01"), Col("titles"),
              Col("order_10_49"), Col("order_50_249"),
              Col("industry_groups", optional=True, since="2026-10-01", ref="Industries"), NOTE),
    # One row per industry (and optionally role), the four emails across (Harry, 30 Sep 2026).
    "Copy": (
        Col("copy_version", key=True), Col("industry", ref="Industries"), Col("role", ref="Roles"),
        *_cols("status", "approved_by", "qa"), Col("qa_notes", optional=True, since="2026-09-30"),
        *_cols("s1_subject", "s1_body", "s2_subject", "s2_body", "s3_subject", "s3_body", "s4_subject", "s4_body",
               "people_leader_line", "founder_line", "operations_line"),
        Col("sources", optional=True, since="2026-09-30"), NOTE,
    ),
    "Mailboxes": (
        Col("address", key=True), *_cols("instantly_account_id", "domain", "provider", "owner_name", "owner_role",
                                         "signature", "status", "daily_cap", "added_on", "retire_after"),
        Col("slack_id", optional=True, since="2026-10-01"),  # decision D11: owners approve their own replies
    ),
    # An industry override names an Industries label (validate_all).
    "Overrides": (Col("domain", key=True), Col("field", key=True), Col("value", ref="Industries"), NOTE),
    "Tests": (
        Col("test_id", key=True), Col("kind", optional=True, since="2026-10-06"), Col("hypothesis"),
        Col("version_a", ref="Copy"), Col("version_b", ref="Copy"), Col("accounts_per_version"), Col("start_date"),
        Col("looks", optional=True, since="2026-10-06"), *_cols("read_date", "decision_rule", "status", "result"),
        # A variant test's (enrol/variants.py): blank on any other kind.
        *_cols("email", "change", "text_a", "text_b", "find", optional=True, since="2026-10-07"),
        Col("share_a", optional=True, since="2026-10-08"),  # an uneven split: blank is 50%
    ),
    "Focus": (Col("industry_group", key=True, ref="Industries"), Col("share"), NOTE),
    "Named accounts": (Col("domain", key=True), Col("name"), NOTE),
}
assert tuple(SPEC) == TABS

# General's apollo_enrich_groups names Industries groups from a value, not a column (9 Oct 2026).
EXTRA_DEPENDS: dict[str, tuple[str, ...]] = {"General": ("Industries",)}


def columns(tab: str) -> list[str]:
    return [c.name for c in SPEC[tab]]


def optional(tab: str) -> frozenset[str]:
    return frozenset(c.name for c in SPEC[tab] if c.optional)


def keys(tab: str) -> tuple[str, ...]:
    return tuple(c.name for c in SPEC[tab] if c.key)


def depends_on() -> dict[str, tuple[str, ...]]:
    """tab -> the tabs its rows name, from the refs (and EXTRA_DEPENDS)."""
    out: dict[str, tuple[str, ...]] = {}
    for tab, cols in SPEC.items():
        refs = tuple(dict.fromkeys([*(c.ref for c in cols if c.ref and c.ref != tab), *EXTRA_DEPENDS.get(tab, ())]))
        if refs:
            out[tab] = refs
    return out
