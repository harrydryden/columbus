-- v_budgets: each credit budget this month, and how the month is going against it.
-- Harry (30 Sep 2026): Apollo and Clay budgets are monthly, a calendar month in UK time, as
-- the vendors count credits (budget.py). SPEC 1.6: "Never exceed the ... Clay and Apollo
-- budgets in settings. Check the balance before every batch." The Claude cap is a UTC
-- calendar month, as clients/claude.py and the Anthropic Console count it (SPEC 1.1, $10).
--   budget:            the General value in force (apollo_monthly_credits,
--                      clay_monthly_credits, claude_monthly_cap_usd), with spaces, commas and $
--                      dropped; NULL if the key is missing or not a number.
--   used:              credits for Clay and Apollo, dollars for Claude.
--   expected_by_now:   the budget spread evenly over the month's weekdays, through today.
--   projected:         used ÷ weekdays through today × weekdays in the month (budget.py).
--   pace:              ahead (more than 10% over expected_by_now), behind (more than 10%
--                      under) or on.
-- It replaces v_credits_month.
DROP VIEW IF EXISTS us_outbound.v_credits_month;
CREATE OR REPLACE VIEW us_outbound.v_budgets AS
WITH periods (system, budget_key, unit, tz) AS (
  VALUES
    ('apollo', 'apollo_monthly_credits', 'credits', 'Europe/London'),
    ('clay', 'clay_monthly_credits', 'credits', 'Europe/London'),
    ('claude', 'claude_monthly_cap_usd', 'usd', 'UTC')
),
bounds AS (
  SELECT
    p.system,
    p.budget_key,
    p.unit,
    p.tz,
    date_trunc('month', now() AT TIME ZONE p.tz) AT TIME ZONE p.tz AS starts,
    (date_trunc('month', now() AT TIME ZONE p.tz) + INTERVAL '1 month') AT TIME ZONE p.tz AS ends,
    (now() AT TIME ZONE p.tz)::date AS today
  FROM periods AS p
),
days AS (
  SELECT
    b.system,
    count(*) FILTER (WHERE date_part('isodow', d) < 6) AS weekdays_in_month,
    count(*) FILTER (WHERE date_part('isodow', d) < 6 AND d::date <= b.today) AS weekdays_through_today
  FROM bounds AS b
  CROSS JOIN LATERAL generate_series(
    (b.starts AT TIME ZONE b.tz)::date, (b.ends AT TIME ZONE b.tz)::date - 1, INTERVAL '1 day'
  ) AS d
  GROUP BY b.system
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
    b.system,
    sum(COALESCE(l.credits, 0)) AS credits,
    sum(COALESCE(l.usd, 0)) AS usd,
    count(l.entry_id) AS entries
  FROM bounds AS b
  LEFT JOIN us_outbound.credit_ledger AS l
    ON l.system = b.system AND l.occurred_at >= b.starts AND l.occurred_at < b.ends
  GROUP BY b.system
),
joined AS (
  SELECT
    b.system,
    b.starts,
    b.unit,
    b.budget_key,
    COALESCE(s.credits, 0) AS credits_used,
    COALESCE(s.usd, 0) AS usd_used,
    CASE WHEN b.unit = 'usd' THEN COALESCE(s.usd, 0) ELSE COALESCE(s.credits, 0) END AS used,
    COALESCE(s.entries, 0) AS entries,
    d.weekdays_in_month,
    d.weekdays_through_today,
    CASE
      WHEN g.value ~ '^[+-]?([0-9]+[.]?[0-9]*|[.][0-9]+)([eE][+-]?[0-9]+)?$' THEN g.value::double precision
    END AS budget
  FROM bounds AS b
  LEFT JOIN spend AS s
    ON s.system = b.system
  LEFT JOIN days AS d
    ON d.system = b.system
  LEFT JOIN general AS g
    ON g."key" = b.budget_key
),
paced AS (
  SELECT
    j.*,
    j.budget * j.weekdays_through_today / NULLIF(j.weekdays_in_month, 0) AS expected_by_now,
    j.used / NULLIF(j.weekdays_through_today, 0) * j.weekdays_in_month AS projected
  FROM joined AS j
)
SELECT
  p.system,
  'month' AS period,
  p.starts AS period_start,
  p.unit,
  p.credits_used,
  p.usd_used,
  p.used,
  p.budget_key,
  p.budget,
  p.budget - p.used AS remaining,
  p.used / NULLIF(p.budget, 0) AS share_used,
  p.weekdays_in_month,
  p.weekdays_through_today,
  p.expected_by_now,
  p.projected,
  CASE
    WHEN p.expected_by_now IS NULL OR p.expected_by_now <= 0 THEN 'on'
    WHEN p.used > p.expected_by_now * 1.1 THEN 'ahead'
    WHEN p.used < p.expected_by_now / 1.1 THEN 'behind'
    ELSE 'on'
  END AS pace,
  p.entries
FROM paced AS p
ORDER BY p.system;
COMMENT ON VIEW us_outbound.v_budgets IS 'Per system: Apollo and Clay credits this calendar month (UK time) and Claude dollars this UTC month, from credit_ledger, against the General budgets, with what is left and the month''s pace (SPEC 1.1, 1.6, 8).';
