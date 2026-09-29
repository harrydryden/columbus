-- v_credits_month: this calendar month's spend against budget, per system (SPEC 1.1, 1.6, 8).
-- SPEC 1.6: "Never exceed the monthly Clay and Apollo budgets in settings. Check the
-- balance before every batch." SPEC 8: "the month's remaining Clay budget:
-- clay_monthly_credits minus the credits recorded this month." SPEC 1.1 caps Claude at
-- claude_monthly_cap_usd. Months are UTC calendar months, as clients/claude.py counts.
--   budget:    the General value in force (clay_monthly_credits, apollo_monthly_credits,
--              claude_monthly_cap_usd); NULL if the key is missing or not a number.
--   used:      credits for Clay and Apollo, dollars for Claude.
CREATE OR REPLACE VIEW `{project}.us_outbound.v_credits_month`
OPTIONS (
  description = "Per system (clay, apollo, claude), this UTC calendar month: credits and dollars recorded in credit_ledger, the monthly budget from the General tab, and what is left (SPEC 1.1, 1.6, 8)."
)
AS
WITH general AS (
  SELECT `key`, JSON_VALUE(`values`, '$.value') AS value
  FROM `{project}.us_outbound.settings`
  WHERE tab = 'General' AND effective_to IS NULL
  QUALIFY ROW_NUMBER() OVER (PARTITION BY `key` ORDER BY effective_from DESC) = 1
),
systems AS (
  SELECT *
  FROM UNNEST([
    STRUCT('clay' AS system, 'clay_monthly_credits' AS budget_key, 'credits' AS unit),
    STRUCT('apollo' AS system, 'apollo_monthly_credits' AS budget_key, 'credits' AS unit),
    STRUCT('claude' AS system, 'claude_monthly_cap_usd' AS budget_key, 'usd' AS unit)
  ])
),
spend AS (
  SELECT
    system,
    SUM(COALESCE(credits, 0)) AS credits,
    SUM(COALESCE(usd, 0)) AS usd,
    COUNT(*) AS entries
  FROM `{project}.us_outbound.credit_ledger`
  WHERE occurred_at >= TIMESTAMP(DATE_TRUNC(CURRENT_DATE(), MONTH))
    AND occurred_at < TIMESTAMP(DATE_ADD(DATE_TRUNC(CURRENT_DATE(), MONTH), INTERVAL 1 MONTH))
  GROUP BY system
),
joined AS (
  SELECT
    s.system,
    s.unit,
    s.budget_key,
    COALESCE(p.credits, 0) AS credits_used,
    COALESCE(p.usd, 0) AS usd_used,
    COALESCE(p.entries, 0) AS entries,
    SAFE_CAST(REGEXP_REPLACE(g.value, r'[\s,$]', '') AS FLOAT64) AS budget
  FROM systems AS s
  LEFT JOIN spend AS p
    ON p.system = s.system
  LEFT JOIN general AS g
    ON g.`key` = s.budget_key
)
SELECT
  system,
  DATE_TRUNC(CURRENT_DATE(), MONTH) AS month_start,
  unit,
  credits_used,
  usd_used,
  IF(unit = 'usd', usd_used, credits_used) AS used,
  budget_key,
  budget,
  budget - IF(unit = 'usd', usd_used, credits_used) AS remaining,
  SAFE_DIVIDE(IF(unit = 'usd', usd_used, credits_used), budget) AS share_used,
  entries
FROM joined
ORDER BY system;
