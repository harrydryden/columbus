-- contacts: one row per person (SPEC 6), plus last_step_at (added by the build).
-- Retention (SPEC 6): "Contacts who never replied: deleted 12 months after their last
-- step." The retention job does this from last_step_at; erase --email deletes on request.
CREATE TABLE IF NOT EXISTS `{project}.us_outbound.contacts` (
  contact_id STRING NOT NULL,
  account_id STRING,
  role STRING OPTIONS (description = "Role name from the Roles tab, e.g. People leader, Founder or executive, Operations."),
  title STRING,
  first_name STRING,
  last_name STRING,
  email STRING,
  email_sha256 STRING OPTIONS (description = "sha256 of the lower-cased, trimmed email (logs.hash_email). One contact per email hash (SPEC 13)."),
  email_status STRING OPTIONS (description = "Verification status as the source gave it: Apollo (verified) or Clay (valid, catch_all_valid, invalid, not_found; SPEC 8)."),
  email_source STRING OPTIONS (description = "One of: apollo, clay."),
  person_state STRING OPTIONS (description = "USPS code. Never CA or WA (SPEC 1.5)."),
  enrolment_month STRING OPTIONS (description = "YYYY-MM of first enrollment (unenrol --month YYYY-MM, SPEC 13)."),
  angle STRING,
  copy_version STRING,
  test_id STRING,
  mailbox STRING OPTIONS (description = "The sender's address that sent step 1."),
  instantly_campaign STRING OPTIONS (description = "The sender's campaign, named 'US Outbound – ' plus the owner name (SPEC 9)."),
  instantly_lead_id STRING,
  hubspot_contact_id STRING,
  suppressed BOOL,
  suppressed_reason STRING,
  created_at TIMESTAMP,
  last_step_at TIMESTAMP OPTIONS (description = "When the last sequence step was sent (build addition, for retention).")
)
CLUSTER BY account_id
OPTIONS (
  description = "One row per person (SPEC 6). Contacts who never replied are deleted 12 months after their last step by the retention job."
);
