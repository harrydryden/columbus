"""Starting a copy test on the Tests tab, and reading it at a look (SPEC 12; 9 Oct 2026, the refactoring scan's
phase 6: these lived in the command line, ops/cli.py). A refusal is a ValueError, which `us-outbound test` reports in
its own words (exit 2); reading before the first look is looks.NotYet.
"""

from __future__ import annotations

from typing import Any

from us_outbound.context import Context


def start(ctx: Context, test_id: str) -> dict:
    """Pre-registration first (SPEC 12; Harry, 6 Oct 2026: kind and looks, learn/looks.py). One copy test (kind ab,
    or variant: Harry, 7 Oct 2026) runs at a time, since it decides each account's copy (SPEC 9); a holdout assigns
    nothing, so it may run beside it, and its versions are the arms enrol records, not Copy rows. A variant's row is
    checked as settings_sync checks it (its texts against the copy rules; enrol/variants.py), and refused when it
    breaks one; the result says which sendable Copy rows the change can be made in (variants.coverage), and a
    change that fits none of them is refused."""
    from us_outbound.settings.model import AB_TEST, COPY_TEST_KINDS, TEST_KINDS, VARIANT_TEST
    from us_outbound.settings.validate import parse_looks, parse_share, validate_tab

    sheet_id = ctx.guard.bounds.settings_sheet_id
    if not sheet_id:
        raise ValueError("no settings sheet id: set US_OUTBOUND_SETTINGS_SHEET_ID")
    rows = ctx.clients.sheets.read_tabs(sheet_id, ["Tests"])["Tests"]
    row = next((r for r in rows if r.get("test_id", "").strip() == test_id), None)
    if row is None:
        raise ValueError(f"no test {test_id!r} on the Tests tab")

    def kind(r: dict) -> str:
        return (r.get("kind") or "").strip().lower() or AB_TEST

    if kind(row) not in TEST_KINDS:
        raise ValueError(f"kind {row.get('kind')!r} on the Tests tab is not one of {', '.join(TEST_KINDS)}")
    others = [r["test_id"] for r in rows if r.get("status", "").strip().lower() == "running"
              and r.get("test_id", "").strip() != test_id and kind(r) in COPY_TEST_KINDS]
    if others and kind(row) in COPY_TEST_KINDS:
        raise ValueError(f"only one copy test (ab or variant) runs at a time (SPEC 9); {', '.join(others)} is running")
    copy_rows: dict[str, Any] = {}
    if kind(row) == VARIANT_TEST:
        from us_outbound.enrol import enrol, variants

        checked, errors = validate_tab("Tests", [row])
        if errors:
            raise ValueError("the Tests tab's row does not pass the checks settings_sync makes: "
                          + "; ".join(f"{e.column}: {e.message}" for e in errors[:6]))
        fits, not_ = variants.coverage(checked[0], enrol.sendable_copy(ctx.settings).values(), ctx.settings)
        if not_ and not fits:
            raise ValueError("the change cannot be made in any sendable Copy row, so the test would have no accounts: "
                          + "; ".join(f"{v}: {why}" for v, why in list(not_.items())[:4]))
        copy_rows = {"change_fits": len(fits), "sendable": len(fits) + len(not_),
                     "not_in_the_test": dict(list(not_.items())[:10])}
    if row.get("status", "").strip().lower() == "running":
        return {"test_id": test_id, "status": "running", "changed": False}
    read_date = row.get("read_date", "").strip()
    if not read_date or not row.get("decision_rule", "").strip():
        raise ValueError("pre-register the read_date and decision_rule on the Tests tab first (SPEC 12)")
    try:
        parse_looks(row.get("looks", "") or "")
    except ValueError as exc:
        raise ValueError(f"looks on the Tests tab: {exc}") from exc
    start = row.get("start_date", "").strip() or ctx.today_uk().isoformat()
    # The row as it will be written, checked as settings_sync will read it, whatever its kind (9 Oct 2026: a read_date
    # of 2026-9-30 passed a comparison of text, the sheet said running, and the next sync refused the tab).
    _, errors = validate_tab("Tests", [{**row, "status": "running", "start_date": start}])
    if errors:
        raise ValueError("the Tests tab's row would not pass the checks settings_sync makes once it is running: "
                      + "; ".join(f"{e.column}: {e.message}" for e in errors[:6]))
    split: dict[str, str] = {}
    if (row.get("share_a") or "").strip():  # version_a's share of the accounts (Harry, 8 Oct 2026)
        try:
            share = parse_share(row["share_a"])
        except ValueError as exc:
            raise ValueError(f"share_a on the Tests tab: {exc}") from exc
        split = {"split": f"{row.get('version_a', '').strip()} {share:.0%}, {row.get('version_b', '').strip()} "
                          f"{1 - share:.0%}"}
    if kind(row) == AB_TEST:
        missing = [
            v for v in (row.get("version_a", "").strip(), row.get("version_b", "").strip())
            if (c := ctx.settings.copy_row(v)) is None or c.status != "approved" or not c.qa_current
        ]
        if missing:
            raise ValueError("copy is not approved, with a current QA pass, in the synced settings for: "
                          + ", ".join(missing))
    sheets = ctx.clients.sheets
    writes = [("start_date", start)] if not row.get("start_date", "").strip() else []
    for column, value in [*writes, ("status", "running")]:
        if not sheets.update_cell(sheet_id, "Tests", {"test_id": test_id}, column, value):
            raise ValueError(f"no row for test {test_id!r} on the Tests tab to update")
    return {"dry_run": ctx.dry_run, "test_id": test_id, "kind": kind(row), "status": "running", "start_date": start,
            "looks": row.get("looks", "").strip(), "read_date": read_date, "changed": ctx.live, **split,
            **({"copy_rows": copy_rows} if copy_rows else {})}


def read(ctx: Context, test_id: str) -> dict:
    """The test at its latest pre-registered look (learn/looks.read); looks.NotYet before the first one.

    Reply rate = accounts with a human reply (any class but out_of_office) within REPLY_WINDOW_DAYS (28) days of
    step 1 ÷ accounts whose step 1 was delivered, as in v_account_outcomes, over the accounts the look covers.
    """
    from us_outbound.learn import looks

    return looks.read(ctx, test_id)
