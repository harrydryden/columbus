-- contacts: one row per person (SPEC 6), plus last_step_at and enrolled_at (added by the build).
-- Retention (SPEC 6): "Contacts who never replied: deleted 12 months after their last
-- step." The retention job does this from last_step_at; erase --email deletes on request.
CREATE TABLE IF NOT EXISTS us_outbound.contacts (
  contact_id text NOT NULL,
  account_id text,
  role text,
  title text,
  first_name text,
  last_name text,
  email text,
  email_sha256 text,
  email_status text,
  email_source text,
  person_state text,
  enrolment_month text,
  angle text,
  copy_version text,
  test_id text,
  mailbox text,
  instantly_campaign text,
  instantly_lead_id text,
  hubspot_contact_id text,
  suppressed boolean,
  suppressed_reason text,
  created_at timestamptz,
  last_step_at timestamptz,
  enrolled_at timestamptz,
  PRIMARY KEY (contact_id)
);
-- For databases created before enrolled_at existed.
ALTER TABLE us_outbound.contacts ADD COLUMN IF NOT EXISTS enrolled_at timestamptz;
CREATE INDEX IF NOT EXISTS contacts_account_id_idx ON us_outbound.contacts (account_id);
CREATE INDEX IF NOT EXISTS contacts_email_sha256_idx ON us_outbound.contacts (email_sha256);
COMMENT ON TABLE us_outbound.contacts IS 'One row per person (SPEC 6). Contacts who never replied are deleted 12 months after their last step by the retention job.';
COMMENT ON COLUMN us_outbound.contacts.role IS 'Role name from the Roles tab, e.g. People leader, Founder or executive, Operations.';
COMMENT ON COLUMN us_outbound.contacts.email_sha256 IS 'sha256 of the lower-cased, trimmed email (logs.hash_email). One contact per email hash (SPEC 13).';
COMMENT ON COLUMN us_outbound.contacts.email_status IS 'Verification status as the source gave it: Apollo (verified) or Clay (valid, catch_all_valid, invalid, not_found; SPEC 8).';
COMMENT ON COLUMN us_outbound.contacts.email_source IS 'One of: apollo, clay.';
COMMENT ON COLUMN us_outbound.contacts.person_state IS 'USPS code. Never CA or WA (SPEC 1.5).';
COMMENT ON COLUMN us_outbound.contacts.enrolment_month IS 'YYYY-MM of first enrollment (unenrol --month YYYY-MM, SPEC 13).';
COMMENT ON COLUMN us_outbound.contacts.mailbox IS 'The sender''s address that sent step 1.';
COMMENT ON COLUMN us_outbound.contacts.instantly_campaign IS 'The sender''s campaign, named ''US Outbound – '' plus the owner name (SPEC 9).';
COMMENT ON COLUMN us_outbound.contacts.last_step_at IS 'When the last sequence step was sent (build addition, for retention).';
COMMENT ON COLUMN us_outbound.contacts.enrolled_at IS 'When the lead was added to its sender''s campaign (build addition). The send forecast dates each lead''s later steps from it, and the weekly target counts it.';
