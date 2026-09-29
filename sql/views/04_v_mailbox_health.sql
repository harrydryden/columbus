-- v_mailbox_health: sends and bounces per registry mailbox (SPEC 9 mailbox_health, 12, 13).
-- SPEC 13: "30 sends per mailbox per day, with warmup always on." SPEC 12 kill rules
-- read bounce rates on the latest sends.
--   mailboxes:    the Mailboxes tab rows in force (address, owner, status, daily_cap).
--   sends_today:  events type 'sent' from the mailbox on today's UK date.
--   last 100:     the mailbox's 100 most recent sends; a send counts as bounced when a
--                 'bounced' event for the same contact, at the same step or with no
--                 step, came at or after it.
CREATE OR REPLACE VIEW `{project}.us_outbound.v_mailbox_health`
OPTIONS (
  description = "Per registry mailbox: sends today against its daily cap from the Mailboxes tab, bounces and bounce rate over its last 100 sends, and its last send (SPEC 9, 12, 13)."
)
AS
WITH mailboxes AS (
  SELECT
    LOWER(TRIM(`key`)) AS address,
    JSON_VALUE(`values`, '$.domain') AS domain,
    JSON_VALUE(`values`, '$.owner_name') AS owner_name,
    JSON_VALUE(`values`, '$.status') AS status,
    JSON_VALUE(`values`, '$.instantly_account_id') AS instantly_account_id,
    SAFE_CAST(TRIM(JSON_VALUE(`values`, '$.daily_cap')) AS INT64) AS daily_cap
  FROM `{project}.us_outbound.settings`
  WHERE tab = 'Mailboxes' AND effective_to IS NULL
  QUALIFY ROW_NUMBER() OVER (PARTITION BY LOWER(TRIM(`key`)) ORDER BY effective_from DESC) = 1
),
sends AS (
  SELECT
    LOWER(TRIM(mailbox)) AS address,
    event_id,
    contact_id,
    step,
    occurred_at,
    ROW_NUMBER() OVER (PARTITION BY LOWER(TRIM(mailbox)) ORDER BY occurred_at DESC, event_id DESC) AS recency
  FROM `{project}.us_outbound.events`
  WHERE type = 'sent' AND mailbox IS NOT NULL
),
bounces AS (
  SELECT contact_id, step, occurred_at
  FROM `{project}.us_outbound.events`
  WHERE type = 'bounced' AND contact_id IS NOT NULL
),
recent AS (
  SELECT
    s.address,
    s.event_id,
    LOGICAL_OR(b.contact_id IS NOT NULL) AS bounced
  FROM sends AS s
  LEFT JOIN bounces AS b
    ON b.contact_id = s.contact_id
    AND (b.step IS NULL OR b.step = s.step)
    AND b.occurred_at >= s.occurred_at
  WHERE s.recency <= 100
  GROUP BY s.address, s.event_id
),
last_100 AS (
  SELECT address, COUNT(*) AS sends_last_100, COUNTIF(bounced) AS bounces_last_100
  FROM recent
  GROUP BY address
),
totals AS (
  SELECT
    address,
    COUNTIF(DATE(occurred_at, 'Europe/London') = CURRENT_DATE('Europe/London')) AS sends_today,
    COUNT(*) AS sends_total,
    MAX(occurred_at) AS last_send_at
  FROM sends
  GROUP BY address
)
SELECT
  m.address,
  m.domain,
  m.owner_name,
  m.status,
  m.instantly_account_id,
  m.daily_cap,
  COALESCE(t.sends_today, 0) AS sends_today,
  m.daily_cap - COALESCE(t.sends_today, 0) AS cap_left_today,
  SAFE_DIVIDE(COALESCE(t.sends_today, 0), m.daily_cap) AS share_of_cap_today,
  COALESCE(t.sends_total, 0) AS sends_total,
  COALESCE(l.sends_last_100, 0) AS sends_last_100,
  COALESCE(l.bounces_last_100, 0) AS bounces_last_100,
  SAFE_DIVIDE(l.bounces_last_100, l.sends_last_100) AS bounce_rate_last_100,
  t.last_send_at,
  TIMESTAMP_DIFF(CURRENT_TIMESTAMP(), t.last_send_at, HOUR) AS hours_since_last_send
FROM mailboxes AS m
LEFT JOIN totals AS t
  ON t.address = m.address
LEFT JOIN last_100 AS l
  ON l.address = m.address
ORDER BY m.domain, m.address;
