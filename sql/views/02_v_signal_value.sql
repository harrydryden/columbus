-- v_signal_value: what each active signal is worth (SPEC 12, shown in the Monday readout).
-- SPEC 12: "for each active signal: how many accounts showed it; their reply rate and
-- positive-reply rate; the Control tier's rates, for comparison. A signal is flagged if
-- its accounts reply below the Control rate after 200 accounts." Nothing re-weights
-- itself: Harry changes weights in the sheet.
-- Harry, 6 Oct 2026 ("push ahead with building" the learning loop): for each signal, over the
-- enrolled accounts, how many had it at enrolment, were sent, replied, replied positively and booked
-- a meeting, and the rates against the emailed accounts without it, with a plain caution when the
-- counts are small. learn/signal_value.py reads it for `us-outbound signals value` and the readout.
--   active signal:   Signals tab row in force with active yes.
--   enrolled:        an account with a contact the enrol job enrolled (contacts.enrolled_at), or one
--                    already sent step 1. Its snapshot is the step-1 contact's signals_at_enrol, else its
--                    first enrolled contact's.
--   showed it:       for an enrolled account with a snapshot, the signal is in it (what it showed when
--                    enrolled; Harry, 5 Oct 2026); for any other account, scoring wrote a signal_events
--                    row (source 'scoring', fact 'signal_matched', value.signal = the signal) for it.
--   accounts:        every account that showed it, queued ones included; accounts_enrolled: the
--                    enrolled ones; accounts_sent: those sent step 1.
--   rates:           over accounts with step 1 delivered, from v_account_outcomes: replies (human,
--                    within 28 days of step 1), positive replies, and meetings (any time after step 1).
--   without_*:       the same for the accounts with step 1 delivered that did not show it.
--   too_few:         under 30 accounts with step 1 delivered on either side (learn/signal_value.py
--                    MIN_TO_READ): the rates are shown, but too few to read.
--   Control:         accounts whose tier was Control when enrolled (else, with no snapshot, now).
--   below_control:   at least 200 accounts with step 1 delivered, and a reply rate
--                    below the Control rate.
-- weight and max_weight are sheet text: spaces, commas and + are dropped, then they count
-- only when a whole number is left.
CREATE OR REPLACE VIEW us_outbound.v_signal_value AS
WITH signals_raw AS (
  SELECT DISTINCT ON ("key")
    "key" AS signal,
    "values" ->> 'source' AS source,
    "values" ->> 'action' AS action,
    regexp_replace("values" ->> 'weight', '[\s,+]', '', 'g') AS weight,
    regexp_replace("values" ->> 'max_weight', '[\s,+]', '', 'g') AS max_weight,
    "values" ->> 'suggests_angle' AS suggests_angle
  FROM us_outbound.settings
  WHERE tab = 'Signals'
    AND effective_to IS NULL
    AND lower(trim("values" ->> 'active')) IN ('yes', 'true')
  ORDER BY "key", effective_from DESC
),
signals AS (
  SELECT
    signal,
    source,
    action,
    CASE WHEN weight ~ '^-?[0-9]{1,9}$' THEN weight::integer END AS weight,
    CASE WHEN max_weight ~ '^-?[0-9]{1,9}$' THEN max_weight::integer END AS max_weight,
    suggests_angle
  FROM signals_raw
),
first_enrolled AS (
  SELECT DISTINCT ON (c.account_id)
    c.account_id, c.signals_at_enrol
  FROM us_outbound.contacts AS c
  WHERE c.enrolled_at IS NOT NULL AND c.account_id IS NOT NULL
  ORDER BY c.account_id, c.enrolled_at, c.contact_id
),
enrolled AS (
  SELECT o.account_id, o.signals_at_enrol
  FROM us_outbound.v_account_outcomes AS o
  UNION ALL
  SELECT f.account_id, f.signals_at_enrol
  FROM first_enrolled AS f
  WHERE NOT EXISTS (
    SELECT 1 FROM us_outbound.v_account_outcomes AS o2 WHERE o2.account_id = f.account_id
  )
),
snapshots AS (
  SELECT account_id, signals_at_enrol
  FROM enrolled
  WHERE jsonb_typeof(signals_at_enrol) = 'array'
),
matched AS (
  SELECT DISTINCT e ->> 'signal' AS signal, s.account_id
  FROM snapshots AS s
  CROSS JOIN LATERAL jsonb_array_elements(s.signals_at_enrol) AS e
  UNION
  SELECT DISTINCT value ->> 'signal' AS signal, account_id
  FROM us_outbound.signal_events
  WHERE source = 'scoring' AND fact = 'signal_matched' AND account_id IS NOT NULL
    AND account_id NOT IN (SELECT account_id FROM snapshots)
),
shown AS (
  SELECT
    m.signal,
    count(*) AS accounts,
    count(*) FILTER (WHERE en.account_id IS NOT NULL) AS accounts_enrolled
  FROM matched AS m
  LEFT JOIN enrolled AS en
    ON en.account_id = m.account_id
  GROUP BY m.signal
),
sides AS (
  SELECT
    s.signal,
    m.account_id IS NOT NULL AS has_it,
    o.delivered,
    o.replied_in_window,
    o.positive_in_window,
    o.meeting_booked
  FROM signals AS s
  CROSS JOIN us_outbound.v_account_outcomes AS o
  LEFT JOIN matched AS m
    ON m.signal = s.signal AND m.account_id = o.account_id
),
emailed AS (
  SELECT
    signal,
    count(*) FILTER (WHERE has_it) AS accounts_sent,
    count(*) FILTER (WHERE has_it AND delivered) AS accounts_delivered,
    count(*) FILTER (WHERE has_it AND delivered AND replied_in_window) AS accounts_replied,
    count(*) FILTER (WHERE has_it AND delivered AND positive_in_window) AS accounts_positive,
    count(*) FILTER (WHERE has_it AND delivered AND meeting_booked) AS accounts_meeting,
    count(*) FILTER (WHERE NOT has_it AND delivered) AS without_delivered,
    count(*) FILTER (WHERE NOT has_it AND delivered AND replied_in_window) AS without_replied,
    count(*) FILTER (WHERE NOT has_it AND delivered AND positive_in_window) AS without_positive,
    count(*) FILTER (WHERE NOT has_it AND delivered AND meeting_booked) AS without_meeting
  FROM sides
  GROUP BY signal
),
control AS (
  SELECT
    count(*) FILTER (WHERE o.delivered) AS accounts_delivered,
    (count(*) FILTER (WHERE o.delivered AND o.replied_in_window))::double precision
      / NULLIF(count(*) FILTER (WHERE o.delivered), 0) AS reply_rate,
    (count(*) FILTER (WHERE o.delivered AND o.positive_in_window))::double precision
      / NULLIF(count(*) FILTER (WHERE o.delivered), 0) AS positive_rate
  FROM us_outbound.v_account_outcomes AS o
  JOIN us_outbound.accounts AS a
    ON a.account_id = o.account_id
  WHERE COALESCE(o.tier_at_enrol, a.tier) = 'Control'
)
SELECT
  s.signal,
  s.source,
  s.action,
  s.weight,
  s.max_weight,
  s.suggests_angle,
  COALESCE(w.accounts, 0) AS accounts,
  COALESCE(p.accounts_delivered, 0) AS accounts_delivered,
  COALESCE(p.accounts_replied, 0) AS accounts_replied,
  COALESCE(p.accounts_positive, 0) AS accounts_positive,
  p.accounts_replied::double precision / NULLIF(p.accounts_delivered, 0) AS reply_rate,
  p.accounts_positive::double precision / NULLIF(p.accounts_delivered, 0) AS positive_rate,
  c.accounts_delivered AS control_accounts_delivered,
  c.reply_rate AS control_reply_rate,
  c.positive_rate AS control_positive_rate,
  COALESCE(
    COALESCE(p.accounts_delivered, 0) >= 200
      AND p.accounts_replied::double precision / NULLIF(p.accounts_delivered, 0) < c.reply_rate,
    FALSE
  ) AS below_control,
  COALESCE(w.accounts_enrolled, 0) AS accounts_enrolled,
  COALESCE(p.accounts_sent, 0) AS accounts_sent,
  COALESCE(p.accounts_meeting, 0) AS accounts_meeting,
  p.accounts_meeting::double precision / NULLIF(p.accounts_delivered, 0) AS meeting_rate,
  COALESCE(p.without_delivered, 0) AS without_delivered,
  COALESCE(p.without_replied, 0) AS without_replied,
  COALESCE(p.without_positive, 0) AS without_positive,
  COALESCE(p.without_meeting, 0) AS without_meeting,
  p.without_replied::double precision / NULLIF(p.without_delivered, 0) AS without_reply_rate,
  p.without_positive::double precision / NULLIF(p.without_delivered, 0) AS without_positive_rate,
  p.without_meeting::double precision / NULLIF(p.without_delivered, 0) AS without_meeting_rate,
  COALESCE(p.accounts_delivered, 0) < 30 OR COALESCE(p.without_delivered, 0) < 30 AS too_few
FROM signals AS s
LEFT JOIN shown AS w
  ON w.signal = s.signal
LEFT JOIN emailed AS p
  ON p.signal = s.signal
CROSS JOIN control AS c
ORDER BY accounts_enrolled DESC, accounts DESC, s.signal;
COMMENT ON VIEW us_outbound.v_signal_value IS 'Per active signal: accounts that showed it, enrolled with it, sent with it; their reply, positive-reply and meeting rates against the emailed accounts without it, with too_few under 30 on either side; the Control tier''s rates; and a below_control flag after 200 delivered accounts (SPEC 12).';
