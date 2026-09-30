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
CREATE OR REPLACE VIEW `{project}.us_outbound.v_signal_value`
OPTIONS (
  description = "Per active signal: accounts that showed it, their reply and positive-reply rates within 21 days of step 1, the Control tier's rates, and a below_control flag after 200 delivered accounts (SPEC 12)."
)
AS
WITH signals AS (
  SELECT
    `key` AS signal,
    JSON_VALUE(`values`, '$.source') AS source,
    JSON_VALUE(`values`, '$.action') AS action,
    SAFE_CAST(REGEXP_REPLACE(JSON_VALUE(`values`, '$.weight'), r'[\s,+]', '') AS INT64) AS weight,
    SAFE_CAST(REGEXP_REPLACE(JSON_VALUE(`values`, '$.max_weight'), r'[\s,+]', '') AS INT64) AS max_weight,
    JSON_VALUE(`values`, '$.suggests_angle') AS suggests_angle
  FROM `{project}.us_outbound.settings`
  WHERE tab = 'Signals'
    AND effective_to IS NULL
    AND LOWER(TRIM(JSON_VALUE(`values`, '$.active'))) IN ('yes', 'true')
  QUALIFY ROW_NUMBER() OVER (PARTITION BY `key` ORDER BY effective_from DESC) = 1
),
matched AS (
  SELECT DISTINCT JSON_VALUE(value, '$.signal') AS signal, account_id
  FROM `{project}.us_outbound.signal_events`
  WHERE source = 'scoring' AND fact = 'signal_matched' AND account_id IS NOT NULL
),
per_signal AS (
  SELECT
    m.signal,
    COUNT(*) AS accounts,
    COUNTIF(o.delivered) AS accounts_delivered,
    COUNTIF(o.delivered AND o.replied_21d) AS accounts_replied,
    COUNTIF(o.delivered AND o.positive_21d) AS accounts_positive
  FROM matched AS m
  LEFT JOIN `{project}.us_outbound.v_account_outcomes` AS o
    ON o.account_id = m.account_id
  GROUP BY m.signal
),
control AS (
  SELECT
    COUNTIF(o.delivered) AS accounts_delivered,
    SAFE_DIVIDE(COUNTIF(o.delivered AND o.replied_21d), COUNTIF(o.delivered)) AS reply_rate,
    SAFE_DIVIDE(COUNTIF(o.delivered AND o.positive_21d), COUNTIF(o.delivered)) AS positive_rate
  FROM `{project}.us_outbound.v_account_outcomes` AS o
  JOIN `{project}.us_outbound.accounts` AS a
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
  SAFE_DIVIDE(p.accounts_replied, p.accounts_delivered) AS reply_rate,
  SAFE_DIVIDE(p.accounts_positive, p.accounts_delivered) AS positive_rate,
  c.accounts_delivered AS control_accounts_delivered,
  c.reply_rate AS control_reply_rate,
  c.positive_rate AS control_positive_rate,
  COALESCE(
    COALESCE(p.accounts_delivered, 0) >= 200
      AND SAFE_DIVIDE(p.accounts_replied, p.accounts_delivered) < c.reply_rate,
    FALSE
  ) AS below_control
FROM signals AS s
LEFT JOIN per_signal AS p
  ON p.signal = s.signal
CROSS JOIN control AS c
ORDER BY accounts DESC, s.signal;
