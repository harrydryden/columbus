-- config_versions: one row per config version, what a cohort of contacts was enrolled under (build addition;
-- Harry, 7 Oct 2026: "ensure that it's easy to continue developing the system whilst live, i.e. that there is a
-- cohort system in place for contacts that have started not being interrupted by changes").
-- The id is a hash of the snapshot (config_version.py): the content tabs' versions, the sendable Copy rows'
-- hashes, the General content keys, the signature template, the campaign constants and the code. Each contact
-- carries its id (contacts.config_version). Written the first time an enrol run computes the version, in
-- dry-run too (SPEC 0.3 allows database writes); never changed after. The cohort report (learn/cohorts.py) diffs
-- two rows to say what changed between two cohorts.
CREATE TABLE IF NOT EXISTS us_outbound.config_versions (
  config_version text NOT NULL,
  first_seen timestamptz,
  code_sha text,
  campaign_fingerprint text,
  signature_hash text,
  step_days jsonb,
  settings_versions jsonb,
  copy_hashes jsonb,
  general jsonb,
  labels_hash text,
  run_id text,
  PRIMARY KEY (config_version)
);
-- Build addition (Harry, 7 Oct 2026): the industry label check's prompt and label list (labels.labels_hash).
ALTER TABLE us_outbound.config_versions ADD COLUMN IF NOT EXISTS labels_hash text;
COMMENT ON TABLE us_outbound.config_versions IS 'One row per config version: the settings, copy, templates, campaign constants and code that contacts were enrolled under (build addition, Harry 7 Oct 2026; SPEC 6, 12). Contacts carry the id in contacts.config_version; the cohort report diffs two rows.';
COMMENT ON COLUMN us_outbound.config_versions.config_version IS 'The first 12 hex characters of the sha256 of the snapshot (the other columns but first_seen and run_id) as canonical JSON.';
COMMENT ON COLUMN us_outbound.config_versions.first_seen IS 'When an enrol run first computed this version.';
COMMENT ON COLUMN us_outbound.config_versions.code_sha IS 'The first 12 characters of RAILWAY_GIT_COMMIT_SHA, or dev outside a Railway deploy.';
COMMENT ON COLUMN us_outbound.config_versions.campaign_fingerprint IS '12 hex characters over STEP_DAYS, CAMPAIGN_SETTINGS and the step templates (clients/instantly.py, registry/mailboxes.campaign_steps): what every lead in a campaign shares. tests/test_cohorts.py pins it.';
COMMENT ON COLUMN us_outbound.config_versions.signature_hash IS '12 hex characters over templates/copy/signature.txt.';
COMMENT ON COLUMN us_outbound.config_versions.step_days IS 'JSON list: the days after email 1 each step is due (STEP_DAYS).';
COMMENT ON COLUMN us_outbound.config_versions.settings_versions IS 'JSON object: tab to the effective_from of its version in force, for the content tabs (Signals, Angles, Industries, Roles, Overrides).';
COMMENT ON COLUMN us_outbound.config_versions.copy_hashes IS 'JSON object: copy_version to CopyRow.content_hash, for every sendable Copy row (approved, QA passed on that wording).';
COMMENT ON COLUMN us_outbound.config_versions.general IS 'JSON object: the General content keys (config_version.CONTENT_KEYS) as text, so a report can say x to y.';
COMMENT ON COLUMN us_outbound.config_versions.run_id IS 'The heartbeats run that first computed it.';
COMMENT ON COLUMN us_outbound.config_versions.labels_hash IS '12 hex characters over the industry label check''s prompt version, instructions, label list, definitions and keywords (labels.labels_hash; Harry, 7 Oct 2026): which label, and so which copy, a new company earns.';
