-- hitl_items: one row per human-in-the-loop item (build addition), SPEC 11:
-- "Any human-in-the-loop item not actioned within escalation_hours is emailed to
-- escalation_email. This covers reply approvals, the weekly hand-check, manual merges
-- and kill-rule alerts."
CREATE TABLE IF NOT EXISTS `{project}.us_outbound.hitl_items` (
  item_id STRING NOT NULL,
  kind STRING OPTIONS (description = "One of: reply_approval, hand_check, manual_merge, kill_rule."),
  account_id STRING,
  contact_id STRING,
  event_id STRING OPTIONS (description = "The events row it is about, e.g. the reply."),
  slack_channel STRING,
  slack_ts STRING OPTIONS (description = "Slack message ts of the alert; approvals are thread replies to it."),
  payload STRING OPTIONS (description = "JSON text: what the item needs (draft, links, ...)."),
  status STRING OPTIONS (description = "One of: open, handled, escalated."),
  created_at TIMESTAMP,
  reposted_at TIMESTAMP OPTIONS (description = "When it was re-posted after 2 hours unanswered (SPEC 11)."),
  escalated_at TIMESTAMP OPTIONS (description = "When it was emailed to escalation_email (SPEC 11)."),
  handled_at TIMESTAMP,
  handled_by STRING
)
CLUSTER BY account_id
OPTIONS (
  description = "One row per human-in-the-loop item (build addition): reply approvals, hand-checks, manual merges and kill-rule alerts, re-posted at 2 hours and escalated at escalation_hours (SPEC 11)."
);
