-- config_log: each change made to what leads already in a campaign share (build addition; Harry, 7 Oct 2026).
-- A lead's own emails are rendered at enrolment and never rewritten; the campaign's step template, delays,
-- schedule and settings are shared by every lead in it, so a change to one reaches the leads in flight too.
-- `campaigns ensure --fix` writes a campaign_change row for each drift it puts right other than the daily limit
-- and sending list (registry/mailboxes.AUTO_FIX), with the leads then in flight; `mailbox check --fix` writes a
-- sender_name row for each From name it sets. Live only: a dry run changes nothing, so it logs nothing.
CREATE TABLE IF NOT EXISTS us_outbound.config_log (
  log_id text NOT NULL,
  changed_at timestamptz,
  kind text,
  campaign text,
  changed_keys jsonb,
  detail jsonb,
  leads_in_flight integer,
  code_sha text,
  changed_by text,
  run_id text,
  PRIMARY KEY (log_id)
);
CREATE INDEX IF NOT EXISTS config_log_changed_at_idx ON us_outbound.config_log (changed_at);
COMMENT ON TABLE us_outbound.config_log IS 'One row per change to what the leads already in a US Outbound campaign share: its step template, delays, schedule or settings, or a mailbox''s From name (build addition, Harry 7 Oct 2026; SPEC 9, 13). The cohort report lists them between two cohorts.';
COMMENT ON COLUMN us_outbound.config_log.kind IS 'One of: campaign_change, sender_name. campaign_change: drift put right by campaigns ensure --fix (with --in-flight when it reached leads in flight). sender_name: a mailbox''s From name set by mailbox check --fix.';
COMMENT ON COLUMN us_outbound.config_log.campaign IS 'The campaign, "US Outbound – " plus the owner name.';
COMMENT ON COLUMN us_outbound.config_log.changed_keys IS 'JSON list: the drift keys put right (registry/mailboxes.campaign_drift), e.g. steps.1, schedule.timing; or the mailbox address for sender_name.';
COMMENT ON COLUMN us_outbound.config_log.detail IS 'JSON: each key''s expected and found values before the change, or the From name set.';
COMMENT ON COLUMN us_outbound.config_log.leads_in_flight IS 'Leads in the campaign with a step still to send when it changed (enrol/capacity.in_flight).';
COMMENT ON COLUMN us_outbound.config_log.code_sha IS 'The code that made the change (config_version.code_sha).';
COMMENT ON COLUMN us_outbound.config_log.changed_by IS 'The job that made it: campaigns_ensure or mailbox_health.';
COMMENT ON COLUMN us_outbound.config_log.run_id IS 'The heartbeats run that made it.';
