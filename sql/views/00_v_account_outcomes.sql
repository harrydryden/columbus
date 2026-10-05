-- v_account_outcomes: one row per account that has been sent step 1 (helper view, build).
-- It holds the SPEC 12 reply-rate definition in one place, for v_signal_value and
-- v_readout_weekly: "human replies within 28 days of step 1 ÷ accounts with step 1
-- delivered". SPEC 12 says 21 days, a week after its day-15 step; the steps are now a week
-- apart, to day 21 (Harry, 30 Sep 2026), so the window is 28 days (instantly.REPLY_WINDOW_DAYS).
-- The company is the unit (SPEC 2), so a reply from anyone at the account counts.
--   step 1:     the account's first events row with type 'sent' and step 1.
--   delivered:  that contact has no 'bounced' event for step 1 (or with no step).
--   human:      any reply class except out_of_office; not yet classified (NULL) counts.
--   positive:   reply_class positive or referral, in the same 28 days.
--   opener_arm, opener_source: that contact's email 1 opener at enrollment (contacts; enrol/openers.py),
--               so a readout can compare opener against the holdout (Harry, 2 Oct 2026).
--   signals_at_enrol, score_at_enrol, tier_at_enrol: the account's signals, score and tier when that
--               contact was enrolled (Harry, 5 Oct 2026), so v_signal_value judges what it showed then.
CREATE OR REPLACE VIEW us_outbound.v_account_outcomes AS
WITH step1 AS (
  SELECT DISTINCT ON (account_id)
    account_id, contact_id, mailbox, event_id, occurred_at AS step1_at
  FROM us_outbound.events
  WHERE type = 'sent' AND step = 1 AND account_id IS NOT NULL
  ORDER BY account_id, occurred_at, event_id
),
bounced AS (
  SELECT DISTINCT contact_id
  FROM us_outbound.events
  WHERE type = 'bounced' AND (step = 1 OR step IS NULL) AND contact_id IS NOT NULL
),
replies AS (
  SELECT
    s.account_id,
    min(r.occurred_at) AS first_reply_at,
    bool_or(r.reply_class IN ('positive', 'referral')) AS positive
  FROM step1 AS s
  JOIN us_outbound.events AS r
    ON r.account_id = s.account_id
  WHERE r.type = 'replied'
    AND COALESCE(r.reply_class, '') <> 'out_of_office'
    AND r.occurred_at >= s.step1_at
    AND r.occurred_at < s.step1_at + INTERVAL '28 days'
  GROUP BY s.account_id
)
SELECT
  s.account_id,
  s.contact_id,
  s.mailbox,
  s.event_id AS step1_event_id,
  s.step1_at,
  b.contact_id IS NULL AS delivered,
  r.first_reply_at,
  r.account_id IS NOT NULL AS replied_in_window,
  COALESCE(r.positive, FALSE) AS positive_in_window,
  now() >= s.step1_at + INTERVAL '28 days' AS window_closed,
  c.opener_arm,
  c.opener_source,
  c.signals_at_enrol,
  c.score_at_enrol,
  c.tier_at_enrol
FROM step1 AS s
LEFT JOIN bounced AS b
  ON b.contact_id = s.contact_id
LEFT JOIN replies AS r
  ON r.account_id = s.account_id
LEFT JOIN us_outbound.contacts AS c
  ON c.contact_id = s.contact_id;
COMMENT ON VIEW us_outbound.v_account_outcomes IS 'One row per account sent step 1 (helper for v_signal_value and v_readout_weekly): delivered, and whether a human or positive reply came within 28 days of step 1 (SPEC 12); the opener arm, to compare opener against none; and the account''s signals, score and tier at enrollment.';
