-- v_mailbox_health: sends and bounces per registry mailbox (SPEC 9 mailbox_health, 12, 13).
-- SPEC 13: "30 sends per mailbox per day, with warmup always on." SPEC 12 kill rules
-- read bounce rates on the latest sends.
--   mailboxes:    the Mailboxes tab rows in force (address, owner, status, daily_cap).
--   sends_today:  events type 'sent' from the mailbox on today's UK date.
--   last 100:     the mailbox's 100 most recent sends; a send counts as bounced when a
--                 'bounced' event for the same contact, at the same step or with no
--                 step, came at or after it.
-- daily_cap is sheet text: it counts only when it is a whole number.
CREATE OR REPLACE VIEW us_outbound.v_mailbox_health AS
WITH mailboxes_raw AS (
  SELECT DISTINCT ON (lower(trim("key")))
    lower(trim("key")) AS address,
    "values" ->> 'domain' AS domain,
    "values" ->> 'owner_name' AS owner_name,
    "values" ->> 'status' AS status,
    "values" ->> 'instantly_account_id' AS instantly_account_id,
    trim("values" ->> 'daily_cap') AS daily_cap
  FROM us_outbound.settings
  WHERE tab = 'Mailboxes' AND effective_to IS NULL
  ORDER BY lower(trim("key")), effective_from DESC
),
mailboxes AS (
  SELECT
    address,
    domain,
    owner_name,
    status,
    instantly_account_id,
    CASE WHEN daily_cap ~ '^[+-]?[0-9]{1,9}$' THEN daily_cap::integer END AS daily_cap
  FROM mailboxes_raw
),
sends AS (
  SELECT
    lower(trim(mailbox)) AS address,
    event_id,
    contact_id,
    step,
    occurred_at,
    row_number() OVER (
      PARTITION BY lower(trim(mailbox)) ORDER BY occurred_at DESC NULLS LAST, event_id DESC
    ) AS recency
  FROM us_outbound.events
  WHERE type = 'sent' AND mailbox IS NOT NULL
),
bounces AS (
  SELECT contact_id, step, occurred_at
  FROM us_outbound.events
  WHERE type = 'bounced' AND contact_id IS NOT NULL
),
recent AS (
  SELECT
    s.address,
    s.event_id,
    bool_or(b.contact_id IS NOT NULL) AS bounced
  FROM sends AS s
  LEFT JOIN bounces AS b
    ON b.contact_id = s.contact_id
    AND (b.step IS NULL OR b.step = s.step)
    AND b.occurred_at >= s.occurred_at
  WHERE s.recency <= 100
  GROUP BY s.address, s.event_id
),
last_100 AS (
  SELECT address, count(*) AS sends_last_100, count(*) FILTER (WHERE bounced) AS bounces_last_100
  FROM recent
  GROUP BY address
),
totals AS (
  SELECT
    address,
    count(*) FILTER (
      WHERE (occurred_at AT TIME ZONE 'Europe/London')::date = (now() AT TIME ZONE 'Europe/London')::date
    ) AS sends_today,
    count(*) AS sends_total,
    max(occurred_at) AS last_send_at
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
  COALESCE(t.sends_today, 0)::double precision / NULLIF(m.daily_cap, 0) AS share_of_cap_today,
  COALESCE(t.sends_total, 0) AS sends_total,
  COALESCE(l.sends_last_100, 0) AS sends_last_100,
  COALESCE(l.bounces_last_100, 0) AS bounces_last_100,
  l.bounces_last_100::double precision / NULLIF(l.sends_last_100, 0) AS bounce_rate_last_100,
  t.last_send_at,
  trunc(date_part('epoch', now() - t.last_send_at) / 3600)::bigint AS hours_since_last_send
FROM mailboxes AS m
LEFT JOIN totals AS t
  ON t.address = m.address
LEFT JOIN last_100 AS l
  ON l.address = m.address
ORDER BY m.domain, m.address;
COMMENT ON VIEW us_outbound.v_mailbox_health IS 'Per registry mailbox: sends today against its daily cap from the Mailboxes tab, bounces and bounce rate over its last 100 sends, and its last send (SPEC 9, 12, 13).';
