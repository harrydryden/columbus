-- v_account_outcomes: one row per account that has been sent step 1: the one outcomes definition in SQL (W2-3).
-- Its Python twin is us_outbound/learn/outcomes.py (outcomes.per_company), which every report reads;
-- tests/test_learn_outcomes.py holds the two to each other on (account_id, delivered, replied_in_window,
-- positive_in_window, meeting_booked as meeting_any, meeting_in_45). v_readout_weekly reads it. The reply-rate
-- definition: "human replies within 28 days of step 1 ÷ accounts with step 1 delivered". SPEC 12 says 21 days, a
-- week after its day-15 step; the steps are now a week apart, to day 21 (Harry, 30 Sep 2026), so the window is 28
-- days (instantly.REPLY_WINDOW_DAYS). The company is the unit (SPEC 2), so a reply from anyone at the account counts.
--   step 1:     the account's first events row with type 'sent' and step 1.
--   delivered:  that contact has no 'bounced' event for step 1 (or with no step).
--   a reply's account: its account_id, else the account its contact was sent to.
--   a reply's class: its latest reply_class_corrected event's (a person's correction: events.source is the corrected
--               reply's event_id; the latest occurred_at wins), else its own (W2-3; RT §1.1).
--   human:      any reply class except out_of_office; not yet classified (NULL) counts.
--   positive:   reply_class positive or referral, in the same 28 days; first_positive_at the first at any time
--               at or after step 1.
--   meeting_booked, first_meeting_at: a meeting_booked event at the account at or after step 1
--               (crm/readback.py: a meeting on Harry's calendar, or a Spill 3.0 deal at Demo requested or
--               later; events.source says which). No 28-day limit: a demo booked in week six still counts
--               (Harry, 6 Oct 2026: the readout counts bookings); meeting_in_45 the first within 45 days of step 1
--               (the north star's clock, design-final §2.1), window_closed_45 once 45 days have passed.
--   opener_arm, opener_source: that contact's email 1 opener at enrollment (contacts; enrol/openers.py),
--               so a readout can compare opener against the holdout (Harry, 2 Oct 2026).
--   signals_at_enrol, score_at_enrol, tier_at_enrol: the account's signals, score and tier when that
--               contact was enrolled (Harry, 5 Oct 2026).
--   subject_arm: that contact's email 1 subject arm at enrollment, personal (General email1_subject) or copy
--               (the Copy row's s1_subject), so a readout can compare the two (Harry, 5 Oct 2026).
--   angle, copy_version, test_id: that contact's at enrollment, for the readout's cuts (6 Oct 2026).
--   arm: that contact's explore arm, random or ranked (contacts.arm, stamped at enrolment; W1-6), NULL before it
--               existed, which reads as ranked: v_readout_weekly's arm cut (W2-2, 20 Oct 2026).
-- v_signal_value read this view; it went with W2-3 (its below_control flagged about 46% of signals by chance:
-- metrics-audit H5), so it is dropped, before ops/ddl.py refreshes the views it read.
DROP VIEW IF EXISTS us_outbound.v_signal_value;
CREATE OR REPLACE VIEW us_outbound.v_account_outcomes AS
WITH step1 AS (
  SELECT DISTINCT ON (account_id)
    account_id, contact_id, mailbox, event_id, occurred_at AS step1_at
  FROM us_outbound.events
  WHERE type = 'sent' AND step = 1 AND account_id IS NOT NULL AND occurred_at IS NOT NULL
  ORDER BY account_id, occurred_at, event_id
),
bounced AS (
  SELECT DISTINCT contact_id
  FROM us_outbound.events
  WHERE type = 'bounced' AND (step = 1 OR step IS NULL) AND contact_id IS NOT NULL AND occurred_at IS NOT NULL
),
contact_account AS (
  SELECT DISTINCT ON (contact_id) contact_id, account_id
  FROM us_outbound.events
  WHERE type = 'sent' AND contact_id IS NOT NULL AND account_id IS NOT NULL AND occurred_at IS NOT NULL
  ORDER BY contact_id, occurred_at, event_id
),
corrected AS (
  SELECT DISTINCT ON (source) source AS reply_event_id, reply_class
  FROM us_outbound.events
  WHERE type = 'reply_class_corrected' AND source IS NOT NULL AND occurred_at IS NOT NULL
  ORDER BY source, occurred_at DESC, event_id DESC
),
classed AS (
  SELECT
    COALESCE(NULLIF(r.account_id, ''), ca.account_id) AS account_id,
    r.occurred_at,
    lower(btrim(COALESCE(CASE WHEN c.reply_event_id IS NOT NULL THEN c.reply_class ELSE r.reply_class END, '')))
      AS reply_class
  FROM us_outbound.events AS r
  LEFT JOIN corrected AS c
    ON c.reply_event_id = r.event_id
  LEFT JOIN contact_account AS ca
    ON ca.contact_id = r.contact_id
  WHERE r.type = 'replied' AND r.occurred_at IS NOT NULL
),
replies AS (
  SELECT
    s.account_id,
    min(r.occurred_at) AS first_reply_at,
    bool_or(r.reply_class IN ('positive', 'referral')) AS positive
  FROM step1 AS s
  JOIN classed AS r
    ON r.account_id = s.account_id
  WHERE r.reply_class <> 'out_of_office'
    AND r.occurred_at >= s.step1_at
    AND r.occurred_at < s.step1_at + INTERVAL '28 days'
  GROUP BY s.account_id
),
positives AS (
  SELECT s.account_id, min(r.occurred_at) AS first_positive_at
  FROM step1 AS s
  JOIN classed AS r
    ON r.account_id = s.account_id
  WHERE r.reply_class IN ('positive', 'referral')
    AND r.occurred_at >= s.step1_at
  GROUP BY s.account_id
),
meetings AS (
  SELECT s.account_id, min(m.occurred_at) AS first_meeting_at
  FROM step1 AS s
  JOIN us_outbound.events AS m
    ON m.account_id = s.account_id
  WHERE m.type = 'meeting_booked'
    AND m.occurred_at >= s.step1_at
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
  c.tier_at_enrol,
  c.subject_arm,
  mt.first_meeting_at,
  mt.account_id IS NOT NULL AS meeting_booked,
  c.angle,
  c.copy_version,
  c.test_id,
  c.arm,
  p.first_positive_at,
  COALESCE(mt.first_meeting_at < s.step1_at + INTERVAL '45 days', FALSE) AS meeting_in_45,
  now() >= s.step1_at + INTERVAL '45 days' AS window_closed_45
FROM step1 AS s
LEFT JOIN bounced AS b
  ON b.contact_id = s.contact_id
LEFT JOIN replies AS r
  ON r.account_id = s.account_id
LEFT JOIN positives AS p
  ON p.account_id = s.account_id
LEFT JOIN meetings AS mt
  ON mt.account_id = s.account_id
LEFT JOIN us_outbound.contacts AS c
  ON c.contact_id = s.contact_id;
COMMENT ON VIEW us_outbound.v_account_outcomes IS 'One row per account sent step 1 (SPEC 12), the one outcomes definition in SQL (learn/outcomes.py is its Python twin): delivered, and whether a human or positive reply came within 28 days of step 1 (a person''s correction of a reply''s class counts over the classifier''s), and whether a meeting was booked after it (any time, and within 45 days); the opener and subject arms, and the account''s signals, score, tier, angle, copy version, test and explore arm at enrollment.';
