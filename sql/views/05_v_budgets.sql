-- v_budgets: what each budget has spent this period and what is left, per system.
-- Harry (30 Sep 2026): the Apollo and Clay budgets are weekly, Monday 00:00 to Sunday
-- 24:00 UK time (budget.py). SPEC 1.6: "Never exceed the ... Clay and Apollo budgets in
-- settings. Check the balance before every batch." The Claude cap stays monthly, a UTC
-- calendar month as clients/claude.py counts it: SPEC 1.1 sets it at $10 a month, the
-- same period as the Anthropic Console's spend limit.
--   budget:    the General value in force (apollo_weekly_credits, clay_weekly_credits,
--              claude_monthly_cap_usd), with spaces, commas and $ dropped; NULL if the
--              key is missing or not a number.
--   used:      credits for Clay and Apollo, dollars for Claude.
-- It replaces v_credits_month.
DROP VIEW IF EXISTS us_outbound.v_credits_month;
CREATE OR REPLACE VIEW us_outbound.v_budgets AS
WITH periods (system, period, budget_key, unit, starts, ends) AS (
  VALUES
    ('apollo', 'week', 'apollo_weekly_credits', 'credits',
     date_trunc('week', now() AT TIME ZONE 'Europe/London') AT TIME ZONE 'Europe/London',
     (date_trunc('week', now() AT TIME ZONE 'Europe/London') + INTERVAL '7 days') AT TIME ZONE 'Europe/London'),
    ('clay', 'week', 'clay_weekly_credits', 'credits',
     date_trunc('week', now() AT TIME ZONE 'Europe/London') AT TIME ZONE 'Europe/London',
     (date_trunc('week', now() AT TIME ZONE 'Europe/London') + INTERVAL '7 days') AT TIME ZONE 'Europe/London'),
    ('claude', 'month', 'claude_monthly_cap_usd', 'usd',
     date_trunc('month', now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC',
     (date_trunc('month', now() AT TIME ZONE 'UTC') + INTERVAL '1 month') AT TIME ZONE 'UTC')
),
general AS (
  SELECT DISTINCT ON ("key")
    "key",
    regexp_replace("values" ->> 'value', '[\s,$]', '', 'g') AS value
  FROM us_outbound.settings
  WHERE tab = 'General' AND effective_to IS NULL
  ORDER BY "key", effective_from DESC
),
spend AS (
  SELECT
    p.system,
    sum(COALESCE(l.credits, 0)) AS credits,
    sum(COALESCE(l.usd, 0)) AS usd,
    count(l.entry_id) AS entries
  FROM periods AS p
  LEFT JOIN us_outbound.credit_ledger AS l
    ON l.system = p.system AND l.occurred_at >= p.starts AND l.occurred_at < p.ends
  GROUP BY p.system
),
joined AS (
  SELECT
    p.system,
    p.period,
    p.starts,
    p.unit,
    p.budget_key,
    COALESCE(s.credits, 0) AS credits_used,
    COALESCE(s.usd, 0) AS usd_used,
    COALESCE(s.entries, 0) AS entries,
    CASE
      WHEN g.value ~ '^[+-]?([0-9]+[.]?[0-9]*|[.][0-9]+)([eE][+-]?[0-9]+)?$' THEN g.value::double precision
    END AS budget
  FROM periods AS p
  LEFT JOIN spend AS s
    ON s.system = p.system
  LEFT JOIN general AS g
    ON g."key" = p.budget_key
)
SELECT
  j.system,
  j.period,
  j.starts AS period_start,
  j.unit,
  j.credits_used,
  j.usd_used,
  CASE WHEN j.unit = 'usd' THEN j.usd_used ELSE j.credits_used END AS used,
  j.budget_key,
  j.budget,
  j.budget - CASE WHEN j.unit = 'usd' THEN j.usd_used ELSE j.credits_used END AS remaining,
  CASE WHEN j.unit = 'usd' THEN j.usd_used ELSE j.credits_used END / NULLIF(j.budget, 0) AS share_used,
  j.entries
FROM joined AS j
ORDER BY j.system;
COMMENT ON VIEW us_outbound.v_budgets IS 'Per system: Apollo and Clay credits this week (Monday to Sunday, UK time) and Claude dollars this UTC month, recorded in credit_ledger, against the General budgets, with what is left (SPEC 1.1, 1.6, 8; weekly budgets from 30 Sep 2026).';
