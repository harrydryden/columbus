-- v_readout_weekly: the numbers behind the Monday readout, per ISO week (SPEC 12).
-- SPEC 12: "accounts enrolled; reply rate by industry group and by copy version;
-- meetings booked; site visits before and after the first email."
-- Weeks are ISO weeks (Monday start) in UK time (SPEC 9: all times are UK time).
-- One row per (week, cut, cut_value). cut is 'all' (cut_value 'all'), 'industry_group'
-- or 'copy_version'; a missing value shows as '(none)'.
--   Cohort columns, by the week of the account's first step 1 (v_account_outcomes):
--     accounts_enrolled, accounts_delivered, accounts_replied, accounts_positive and
--     the rates; replies count within 28 days of step 1, so a week's rates settle
--     three weeks after it ends.
--   Activity columns, by the week the event happened: meetings_booked (events type
--     meeting_booked) and site_visit events, split by whether they came before the
--     account's first email (or it has none yet) or after it.
--   copy_version is the step-1 contact's (contacts.copy_version).
CREATE OR REPLACE VIEW us_outbound.v_readout_weekly AS
WITH first_send AS (
  SELECT o.*, c.copy_version
  FROM us_outbound.v_account_outcomes AS o
  LEFT JOIN us_outbound.contacts AS c
    ON c.contact_id = o.contact_id
),
account_cuts AS (
  SELECT a.account_id, cut.name AS cut, cut.value AS cut_value
  FROM us_outbound.accounts AS a
  LEFT JOIN first_send AS f
    ON f.account_id = a.account_id
  CROSS JOIN LATERAL (
    VALUES
      ('all', 'all'),
      ('industry_group', COALESCE(a.industry_group, '(none)')),
      ('copy_version', COALESCE(f.copy_version, '(none)'))
  ) AS cut (name, value)
),
cohort AS (
  SELECT
    date_trunc('week', f.step1_at AT TIME ZONE 'Europe/London')::date AS week_start,
    k.cut,
    k.cut_value,
    count(*) AS accounts_enrolled,
    count(*) FILTER (WHERE f.delivered) AS accounts_delivered,
    count(*) FILTER (WHERE f.delivered AND f.replied_in_window) AS accounts_replied,
    count(*) FILTER (WHERE f.delivered AND f.positive_in_window) AS accounts_positive
  FROM first_send AS f
  JOIN account_cuts AS k
    ON k.account_id = f.account_id
  GROUP BY week_start, k.cut, k.cut_value
),
activity AS (
  SELECT
    date_trunc('week', e.occurred_at AT TIME ZONE 'Europe/London')::date AS week_start,
    k.cut,
    k.cut_value,
    count(DISTINCT e.event_id) FILTER (WHERE e.type = 'meeting_booked') AS meetings_booked,
    count(DISTINCT e.event_id) FILTER (
      WHERE e.type = 'site_visit' AND (f.step1_at IS NULL OR e.occurred_at < f.step1_at)
    ) AS site_visits_before_first_email,
    count(DISTINCT e.event_id) FILTER (
      WHERE e.type = 'site_visit' AND e.occurred_at >= f.step1_at
    ) AS site_visits_after_first_email
  FROM us_outbound.events AS e
  JOIN account_cuts AS k
    ON k.account_id = e.account_id
  LEFT JOIN first_send AS f
    ON f.account_id = e.account_id
  WHERE e.type IN ('meeting_booked', 'site_visit')
  GROUP BY week_start, k.cut, k.cut_value
)
SELECT
  week_start,
  date_part('isoyear', week_start)::integer AS iso_year,
  date_part('week', week_start)::integer AS iso_week,
  cut,
  cut_value,
  COALESCE(c.accounts_enrolled, 0) AS accounts_enrolled,
  COALESCE(c.accounts_delivered, 0) AS accounts_delivered,
  COALESCE(c.accounts_replied, 0) AS accounts_replied,
  COALESCE(c.accounts_positive, 0) AS accounts_positive,
  c.accounts_replied::double precision / NULLIF(c.accounts_delivered, 0) AS reply_rate,
  c.accounts_positive::double precision / NULLIF(c.accounts_delivered, 0) AS positive_rate,
  COALESCE(v.meetings_booked, 0) AS meetings_booked,
  COALESCE(v.site_visits_before_first_email, 0) AS site_visits_before_first_email,
  COALESCE(v.site_visits_after_first_email, 0) AS site_visits_after_first_email
FROM cohort AS c
FULL OUTER JOIN activity AS v
  USING (week_start, cut, cut_value)
ORDER BY week_start DESC NULLS LAST, cut, cut_value;
COMMENT ON VIEW us_outbound.v_readout_weekly IS 'Per ISO week (UK time), overall and by industry group and copy version: accounts enrolled, reply and positive rates within 28 days of step 1, meetings booked, and site visits before and after the first email (SPEC 12).';
