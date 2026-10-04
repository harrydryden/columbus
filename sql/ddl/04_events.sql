-- events: one row per send, reply, visit, meeting, deal or escalation (SPEC 6).
-- event_id is the idempotency key: the Instantly email id or the HubSpot object id.
-- Retention (SPEC 6): "Reply text: purged after 90 days." The retention job sets
-- reply_text to NULL on rows whose occurred_at is more than 90 days old; not DDL.
CREATE TABLE IF NOT EXISTS us_outbound.events (
  event_id text NOT NULL,
  contact_id text,
  account_id text,
  type text,
  step integer,
  mailbox text,
  reply_class text,
  reply_text text,
  language_terms jsonb,
  competitor_named text,
  approval text,
  approved_by text,
  occurred_at timestamptz,
  PRIMARY KEY (event_id)
);
CREATE INDEX IF NOT EXISTS events_account_id_idx ON us_outbound.events (account_id);
CREATE INDEX IF NOT EXISTS events_contact_id_idx ON us_outbound.events (contact_id);
CREATE INDEX IF NOT EXISTS events_occurred_at_idx ON us_outbound.events (occurred_at);
CREATE INDEX IF NOT EXISTS events_type_occurred_at_idx ON us_outbound.events (type, occurred_at);
COMMENT ON TABLE us_outbound.events IS 'One row per send, reply, visit, meeting, deal or escalation (SPEC 6). event_id is the idempotency key. Reply text is purged after 90 days by the retention job.';
COMMENT ON COLUMN us_outbound.events.event_id IS 'Idempotency key: the Instantly email id or the HubSpot object id (SPEC 6).';
COMMENT ON COLUMN us_outbound.events.type IS 'One of: sent, bounced, replied, unsubscribed, site_visit, meeting_booked, demo_held, deal_created, escalated, send_approval, reply_sent. sent: a campaign step Instantly sent. send_approval: the closing decision on a send approval, event_id send-approval:{item_id}, step 1 (enrol/approvals.py; Harry, 2 Oct 2026). reply_sent: a reply the reply desk sent from the mailbox the prospect wrote to, with approval and approved_by, step NULL (replies/desk.py); it is no campaign send, so nothing that counts sends reads it.';
COMMENT ON COLUMN us_outbound.events.step IS 'Sequence step, 1 to 4 (SPEC 10).';
COMMENT ON COLUMN us_outbound.events.mailbox IS 'The registry mailbox that sent or received it.';
COMMENT ON COLUMN us_outbound.events.reply_class IS 'One of: positive, referral, objection, not_now, negative, out_of_office, wrong_person, unsubscribe, other (SPEC 11).';
COMMENT ON COLUMN us_outbound.events.reply_text IS 'Purged after 90 days (SPEC 6) by the retention job.';
COMMENT ON COLUMN us_outbound.events.language_terms IS 'JSON: the list of language terms from classification (SPEC 11).';
COMMENT ON COLUMN us_outbound.events.approval IS 'One of: approved, edited, skipped, approved_edited, contact_rejected, company_rejected, expired, blocked. A reply is approved, edited or skipped; a send approval has its outcome: approved, approved_edited, contact_rejected, company_rejected, expired or blocked, with approved_by a Slack user id, cli or system.';
