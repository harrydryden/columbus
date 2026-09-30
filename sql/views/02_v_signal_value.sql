-- v_signal_value: what each active signal is worth (SPEC 12, shown in the Monday readout).
-- SPEC 12: "for each active signal: how many accounts showed it; their reply rate and
-- positive-reply rate; the Control tier's rates, for comparison. A signal is flagged if
-- its accounts reply below the Control rate after 200 accounts." Nothing re-weights
-- itself: Harry changes weights in the sheet.
--   active signal:   Signals tab row in force with active yes.
--   showed it:       scoring wrote a signal_events row (source 'scoring', fact
--                    'signal_matched', value.signal = the signal) for the account.
--   rates:           over accounts with step 1 delivered, from v_account_outcomes.
--   Control:         accounts whose tier is Control now.
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
matched AS (
  SELECT DISTINCT value ->> 'signal' AS signal, account_id
  FROM us_outbound.signal_events
  WHERE source = 'scoring' AND fact = 'signal_matched' AND account_id IS NOT NULL
),
per_signal AS (
  SELECT
    m.signal,
    count(*) AS accounts,
    count(*) FILTER (WHERE o.delivered) AS accounts_delivered,
    count(*) FILTER (WHERE o.delivered AND o.replied_in_window) AS accounts_replied,
    count(*) FILTER (WHERE o.delivered AND o.positive_in_window) AS accounts_positive
  FROM matched AS m
  LEFT JOIN us_outbound.v_account_outcomes AS o
    ON o.account_id = m.account_id
  GROUP BY m.signal
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
  WHERE a.tier = 'Control'
)
SELECT
  s.signal,
  s.source,
  s.action,
  s.weight,
  s.max_weight,
  s.suggests_angle,
  COALESCE(p.accounts, 0) AS accounts,
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
  ) AS below_control
FROM signals AS s
LEFT JOIN per_signal AS p
  ON p.signal = s.signal
CROSS JOIN control AS c
ORDER BY accounts DESC, s.signal;
COMMENT ON VIEW us_outbound.v_signal_value IS 'Per active signal: accounts that showed it, their reply and positive-reply rates within 28 days of step 1, the Control tier''s rates, and a below_control flag after 200 delivered accounts (SPEC 12).';
