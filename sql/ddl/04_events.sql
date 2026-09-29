-- events: one row per send, reply, visit, meeting, deal or escalation (SPEC 6).
-- event_id is the idempotency key: the Instantly email id or the HubSpot object id.
-- Retention (SPEC 6): "Reply text: purged after 90 days." The retention job sets
-- reply_text to NULL on rows whose occurred_at is more than 90 days old; not DDL.
CREATE TABLE IF NOT EXISTS `{project}.us_outbound.events` (
  event_id STRING NOT NULL OPTIONS (description = "Idempotency key: the Instantly email id or the HubSpot object id (SPEC 6)."),
  contact_id STRING,
  account_id STRING,
  type STRING OPTIONS (description = "One of: sent, bounced, replied, unsubscribed, site_visit, meeting_booked, demo_held, deal_created, escalated."),
  step INT64 OPTIONS (description = "Sequence step, 1 to 4 (SPEC 10)."),
  mailbox STRING OPTIONS (description = "The registry mailbox that sent or received it."),
  reply_class STRING OPTIONS (description = "One of: positive, referral, objection, not_now, negative, out_of_office, wrong_person, unsubscribe, other (SPEC 11)."),
  reply_text STRING OPTIONS (description = "Purged after 90 days (SPEC 6) by the retention job."),
  language_terms STRING OPTIONS (description = "JSON text: the list of language terms from classification (SPEC 11)."),
  competitor_named STRING,
  approval STRING OPTIONS (description = "One of: approved, edited, skipped."),
  approved_by STRING,
  occurred_at TIMESTAMP
)
PARTITION BY DATE(occurred_at)
CLUSTER BY account_id
OPTIONS (
  description = "One row per send, reply, visit, meeting, deal or escalation (SPEC 6). event_id is the idempotency key. Reply text is purged after 90 days by the retention job."
);
