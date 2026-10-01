-- hitl_items: one row per human-in-the-loop item (build addition), SPEC 11:
-- "Any human-in-the-loop item not actioned within escalation_hours is emailed to
-- escalation_email. This covers reply approvals, the weekly hand-check, manual merges
-- and kill-rule alerts."
CREATE TABLE IF NOT EXISTS us_outbound.hitl_items (
  item_id text NOT NULL,
  kind text,
  account_id text,
  contact_id text,
  event_id text,
  slack_channel text,
  slack_ts text,
  payload jsonb,
  status text,
  created_at timestamptz,
  reposted_at timestamptz,
  escalated_at timestamptz,
  handled_at timestamptz,
  handled_by text,
  PRIMARY KEY (item_id)
);
CREATE INDEX IF NOT EXISTS hitl_items_account_id_idx ON us_outbound.hitl_items (account_id);
COMMENT ON TABLE us_outbound.hitl_items IS 'One row per human-in-the-loop item (build addition): reply approvals, hand-checks, manual merges and kill-rule alerts, re-posted at 2 hours and escalated at escalation_hours (SPEC 11).';
COMMENT ON COLUMN us_outbound.hitl_items.kind IS 'One of: reply, out_of_office, hand_check, manual_merge, kill_rule. reply: a reply waiting for a person (replies/poll.py holds the payload contract); out_of_office: a return date and its re-timing, status handled.';
COMMENT ON COLUMN us_outbound.hitl_items.event_id IS 'The events row it is about, e.g. the reply.';
COMMENT ON COLUMN us_outbound.hitl_items.slack_ts IS 'Slack message ts of the alert; approvals are thread replies to it.';
COMMENT ON COLUMN us_outbound.hitl_items.payload IS 'JSON: what the item needs (draft, links, ...).';
COMMENT ON COLUMN us_outbound.hitl_items.status IS 'One of: open, handled, escalated.';
COMMENT ON COLUMN us_outbound.hitl_items.reposted_at IS 'When it was re-posted after 2 hours unanswered (SPEC 11).';
COMMENT ON COLUMN us_outbound.hitl_items.escalated_at IS 'When it was emailed to escalation_email (SPEC 11).';
