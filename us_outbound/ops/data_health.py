"""`us-outbound data show`: what the sources have actually stored, in aggregate (read-only).

Built after the first live sourcing run (2 Oct 2026), which tiered 425 of 432 accounts Control and
left 141 unverified for an unknown employee count. Before tuning thresholds or sources, read what
the data holds: tiers and scores, missing size by industry group, which facts each source wrote
for how many accounts, which signals matched, and the shape of the fields the openers read
(growth, funding stage, open roles, posting titles). Counts only; no domain or person is printed.
"""

from __future__ import annotations

import json
from typing import Any

from us_outbound.context import Context

QUERIES: dict[str, str] = {
    "tiers": "SELECT tier, count(*) AS n FROM us_outbound.accounts GROUP BY 1 ORDER BY 2 DESC",
    "status": "SELECT status, count(*) AS n FROM us_outbound.accounts GROUP BY 1 ORDER BY 2 DESC",
    "scores": ("SELECT (floor(coalesce(score, 0) / 10) * 10)::int AS from_score, count(*) AS n "
               "FROM us_outbound.accounts GROUP BY 1 ORDER BY 1"),
    "size": ("SELECT industry_group, count(*) FILTER (WHERE employees IS NULL) AS no_employees, "
             "count(*) FILTER (WHERE size_band IS NULL) AS no_band, count(*) AS n "
             "FROM us_outbound.accounts GROUP BY 1 ORDER BY 4 DESC"),
    "no_size_band": ("SELECT status, (apollo_org_id IS NOT NULL) AS has_apollo_id, count(*) AS n "
                     "FROM us_outbound.accounts WHERE size_band IS NULL GROUP BY 1, 2 ORDER BY 1, 2"),
    "credits_today": ("SELECT system, job, sum(coalesce(credits, 0)) AS credits, count(*) AS rows "
                      "FROM us_outbound.credit_ledger WHERE occurred_at >= date_trunc('day', now()) "
                      "GROUP BY 1, 2 ORDER BY 1, 2"),
    "facts": ("SELECT source, fact, count(DISTINCT account_id) AS accounts FROM us_outbound.signal_events "
              "WHERE source <> 'scoring' GROUP BY 1, 2 ORDER BY 1, 2"),
    "matched": ("SELECT value->>'signal' AS signal, count(DISTINCT account_id) AS accounts "
                "FROM us_outbound.signal_events WHERE fact = 'signal_matched' GROUP BY 1 ORDER BY 2 DESC"),
    "growth": ("SELECT min((value #>> '{}')::float) AS min, "
               "percentile_cont(0.5) WITHIN GROUP (ORDER BY (value #>> '{}')::float) AS median, "
               "max((value #>> '{}')::float) AS max, count(*) AS n, "
               "count(*) FILTER (WHERE (value #>> '{}')::float >= 0.10) AS at_least_0_10 "
               "FROM us_outbound.signal_events WHERE fact = 'headcount_growth_12m' AND jsonb_typeof(value) = 'number'"),
    "funding_stage": ("SELECT value #>> '{}' AS stage, count(*) AS n FROM us_outbound.signal_events "
                      "WHERE fact = 'funding_stage' GROUP BY 1 ORDER BY 2 DESC LIMIT 15"),
    "days_since_funding": ("SELECT count(*) FILTER (WHERE (value #>> '{}')::float <= 180) AS within_180, "
                           "count(*) FILTER (WHERE (value #>> '{}')::float <= 365) AS within_365, count(*) AS n "
                           "FROM us_outbound.signal_events WHERE fact = 'days_since_funding' "
                           "AND jsonb_typeof(value) = 'number'"),
    "open_roles": ("SELECT (value #>> '{}')::int AS roles, count(*) AS n FROM us_outbound.signal_events "
                   "WHERE fact = 'open_roles' AND jsonb_typeof(value) = 'number' GROUP BY 1 ORDER BY 1 LIMIT 25"),
    "posting_titles_sample": ("SELECT value FROM us_outbound.signal_events WHERE fact = 'posting_titles' "
                              "ORDER BY observed_at DESC LIMIT 8"),
}


def collect(ctx: Context) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for name, sql in QUERIES.items():
        try:
            out[name] = ctx.store.query(sql)
        except Exception as exc:  # one bad query should not hide the others
            out[name] = [{"error": type(exc).__name__, "detail": str(exc)[:200]}]
    return out


def report(ctx: Context) -> list[str]:
    return [f"{name}: {json.dumps(rows, default=str)}" for name, rows in collect(ctx).items()]
