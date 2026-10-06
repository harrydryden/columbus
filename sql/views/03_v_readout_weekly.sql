-- v_readout_weekly: the numbers behind the Monday readout, per ISO week (SPEC 12).
-- SPEC 12: "accounts enrolled; reply rate by industry group and by copy version;
-- meetings booked; site visits before and after the first email." Harry, 6 Oct 2026 ("push ahead
-- with building"): last week's sends, replies, positive replies, meetings, bounces and unsubscribes,
-- by tier, angle, industry group, sender and step (learn/readout.py reads this view).
-- Weeks are ISO weeks (Monday start) in UK time (SPEC 9: all times are UK time).
-- One row per (week, cut, cut_value). cut is 'all' (cut_value 'all'), 'tier', 'angle',
-- 'industry_group', 'sender', 'copy_version' or 'step'; a missing value shows as '(none)'.
--   Account cuts: tier is the step-1 contact's tier_at_enrol, else the account's tier now; angle is
--     the step-1 contact's, else the account's; industry_group and sender (the mailbox owner,
--     accounts.sender) are the account's; copy_version is the step-1 contact's.
--   step: activity only, for the events that carry one ('step 1' to 'step 4'): sends, bounces and
--     replies (the step a reply answers, replies/outcomes.step_before).
--   Cohort columns, by the week of the account's first step 1 (v_account_outcomes):
--     accounts_enrolled (sent step 1), accounts_delivered, accounts_replied, accounts_positive,
--     accounts_meeting (a meeting booked any time after step 1), accounts_window_closed (its 28 days
--     are over) and the rates; replies count within 28 days of step 1, so a week's rates settle four
--     weeks after it starts.
--   Activity columns, by the week the event happened: sends, bounces, replies (human: every class
--     but out_of_office, unclassified included), positive_replies (positive or referral),
--     meetings_booked (companies with a meeting_booked event that week, so a meeting and a deal for
--     one booking count once), unsubscribes, complaints (events type complained, learn/kill_rules.py),
--     and site_visit events, split by whether they came before the account's first email (or it has
--     none yet) or after it.
CREATE OR REPLACE VIEW us_outbound.v_readout_weekly AS
WITH account_cuts AS (
  SELECT a.account_id, cut.name AS cut, cut.value AS cut_value
  FROM us_outbound.accounts AS a
  LEFT JOIN us_outbound.v_account_outcomes AS f
    ON f.account_id = a.account_id
  CROSS JOIN LATERAL (
    VALUES
      ('all', 'all'),
      ('tier', COALESCE(f.tier_at_enrol, a.tier, '(none)')),
      ('angle', COALESCE(f.angle, a.angle, '(none)')),
      ('industry_group', COALESCE(a.industry_group, '(none)')),
      ('sender', COALESCE(a.sender, '(none)')),
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
    count(*) FILTER (WHERE f.delivered AND f.positive_in_window) AS accounts_positive,
    count(*) FILTER (WHERE f.delivered AND f.meeting_booked) AS accounts_meeting,
    count(*) FILTER (WHERE f.delivered AND f.window_closed) AS accounts_window_closed
  FROM us_outbound.v_account_outcomes AS f
  JOIN account_cuts AS k
    ON k.account_id = f.account_id
  GROUP BY week_start, k.cut, k.cut_value
),
happened AS (
  SELECT e.event_id, e.account_id, e.type, e.reply_class, e.occurred_at, k.cut, k.cut_value
  FROM us_outbound.events AS e
  JOIN account_cuts AS k
    ON k.account_id = e.account_id
  WHERE e.type IN ('sent', 'bounced', 'replied', 'meeting_booked', 'unsubscribed', 'complained', 'site_visit')
  UNION ALL
  SELECT e.event_id, e.account_id, e.type, e.reply_class, e.occurred_at, 'step' AS cut, 'step ' || e.step::text AS cut_value
  FROM us_outbound.events AS e
  WHERE e.type IN ('sent', 'bounced', 'replied') AND e.step IS NOT NULL AND e.account_id IS NOT NULL
),
activity AS (
  SELECT
    date_trunc('week', h.occurred_at AT TIME ZONE 'Europe/London')::date AS week_start,
    h.cut,
    h.cut_value,
    count(DISTINCT h.event_id) FILTER (WHERE h.type = 'sent') AS sends,
    count(DISTINCT h.event_id) FILTER (WHERE h.type = 'bounced') AS bounces,
    count(DISTINCT h.event_id) FILTER (
      WHERE h.type = 'replied' AND COALESCE(h.reply_class, '') <> 'out_of_office'
    ) AS replies,
    count(DISTINCT h.event_id) FILTER (
      WHERE h.type = 'replied' AND h.reply_class IN ('positive', 'referral')
    ) AS positive_replies,
    count(DISTINCT h.account_id) FILTER (WHERE h.type = 'meeting_booked') AS meetings_booked,
    count(DISTINCT h.event_id) FILTER (WHERE h.type = 'unsubscribed') AS unsubscribes,
    count(DISTINCT h.event_id) FILTER (WHERE h.type = 'complained') AS complaints,
    count(DISTINCT h.event_id) FILTER (
      WHERE h.type = 'site_visit' AND (f.step1_at IS NULL OR h.occurred_at < f.step1_at)
    ) AS site_visits_before_first_email,
    count(DISTINCT h.event_id) FILTER (
      WHERE h.type = 'site_visit' AND h.occurred_at >= f.step1_at
    ) AS site_visits_after_first_email
  FROM happened AS h
  LEFT JOIN us_outbound.v_account_outcomes AS f
    ON f.account_id = h.account_id
  WHERE h.occurred_at IS NOT NULL
  GROUP BY week_start, h.cut, h.cut_value
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
  COALESCE(v.site_visits_after_first_email, 0) AS site_visits_after_first_email,
  COALESCE(c.accounts_meeting, 0) AS accounts_meeting,
  COALESCE(c.accounts_window_closed, 0) AS accounts_window_closed,
  c.accounts_meeting::double precision / NULLIF(c.accounts_delivered, 0) AS meeting_rate,
  COALESCE(v.sends, 0) AS sends,
  COALESCE(v.bounces, 0) AS bounces,
  COALESCE(v.replies, 0) AS replies,
  COALESCE(v.positive_replies, 0) AS positive_replies,
  COALESCE(v.unsubscribes, 0) AS unsubscribes,
  COALESCE(v.complaints, 0) AS complaints
FROM cohort AS c
FULL OUTER JOIN activity AS v
  USING (week_start, cut, cut_value)
ORDER BY week_start DESC NULLS LAST, cut, cut_value;
COMMENT ON VIEW us_outbound.v_readout_weekly IS 'Per ISO week (UK time), overall and by tier, angle, industry group, sender, copy version and step: accounts sent step 1 and their reply, positive and meeting rates (replies within 28 days of step 1); and the week''s sends, bounces, replies, positive replies, meetings booked, unsubscribes, complaints, and site visits before and after the first email (SPEC 12).';
