-- v_credits_month: this calendar month's spend against budget, per system (SPEC 1.1, 1.6, 8).
-- SPEC 1.6: "Never exceed the monthly Clay and Apollo budgets in settings. Check the
-- balance before every batch." SPEC 8: "the month's remaining Clay budget:
-- clay_monthly_credits minus the credits recorded this month." SPEC 1.1 caps Claude at
-- claude_monthly_cap_usd. Months are UTC calendar months, as clients/claude.py counts.
--   budget:    the General value in force (clay_monthly_credits, apollo_monthly_credits,
--              claude_monthly_cap_usd), with spaces, commas and $ dropped; NULL if the
--              key is missing or not a number.
--   used:      credits for Clay and Apollo, dollars for Claude.
CREATE OR REPLACE VIEW us_outbound.v_credits_month AS
WITH this_month AS (
  -- The first instant of this UTC month and of the next, as timestamptz.
  SELECT
    date_trunc('month', now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC' AS starts,
    (date_trunc('month', now() AT TIME ZONE 'UTC') + INTERVAL '1 month') AT TIME ZONE 'UTC' AS ends
),
general AS (
  SELECT DISTINCT ON ("key")
    "key",
    regexp_replace("values" ->> 'value', '[\s,$]', '', 'g') AS value
  FROM us_outbound.settings
  WHERE tab = 'General' AND effective_to IS NULL
  ORDER BY "key", effective_from DESC
),
systems (system, budget_key, unit) AS (
  VALUES
    ('clay', 'clay_monthly_credits', 'credits'),
    ('apollo', 'apollo_monthly_credits', 'credits'),
    ('claude', 'claude_monthly_cap_usd', 'usd')
),
spend AS (
  SELECT
    l.system,
    sum(COALESCE(l.credits, 0)) AS credits,
    sum(COALESCE(l.usd, 0)) AS usd,
    count(*) AS entries
  FROM us_outbound.credit_ledger AS l
  CROSS JOIN this_month AS m
  WHERE l.occurred_at >= m.starts AND l.occurred_at < m.ends
  GROUP BY l.system
),
joined AS (
  SELECT
    s.system,
    s.unit,
    s.budget_key,
    COALESCE(p.credits, 0) AS credits_used,
    COALESCE(p.usd, 0) AS usd_used,
    COALESCE(p.entries, 0) AS entries,
    CASE
      WHEN g.value ~ '^[+-]?([0-9]+[.]?[0-9]*|[.][0-9]+)([eE][+-]?[0-9]+)?$' THEN g.value::double precision
    END AS budget
  FROM systems AS s
  LEFT JOIN spend AS p
    ON p.system = s.system
  LEFT JOIN general AS g
    ON g."key" = s.budget_key
)
SELECT
  j.system,
  (date_trunc('month', now() AT TIME ZONE 'UTC'))::date AS month_start,
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
COMMENT ON VIEW us_outbound.v_credits_month IS 'Per system (clay, apollo, claude), this UTC calendar month: credits and dollars recorded in credit_ledger, the monthly budget from the General tab, and what is left (SPEC 1.1, 1.6, 8).';
