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
  opener_arm text,
  opener_source text,
  signals_at_enrol jsonb,
  score_at_enrol integer,
  tier_at_enrol text,
  data_record jsonb,
  subject_arm text,
  contact_slot integer,
  PRIMARY KEY (contact_id)
);
-- For databases created before enrolled_at, opener_arm, opener_source and the enrolment snapshot existed.
ALTER TABLE us_outbound.contacts ADD COLUMN IF NOT EXISTS enrolled_at timestamptz;
ALTER TABLE us_outbound.contacts ADD COLUMN IF NOT EXISTS opener_arm text;
ALTER TABLE us_outbound.contacts ADD COLUMN IF NOT EXISTS opener_source text;
-- What the account looked like when this contact was enrolled (Harry, 5 Oct 2026): the signals it showed,
-- its score and its tier. Scoring rewrites the account's matches every run, so without this the signal
-- readout (v_signal_value) would judge today's signals against replies to emails sent weeks ago.
ALTER TABLE us_outbound.contacts ADD COLUMN IF NOT EXISTS signals_at_enrol jsonb;
ALTER TABLE us_outbound.contacts ADD COLUMN IF NOT EXISTS score_at_enrol integer;
ALTER TABLE us_outbound.contacts ADD COLUMN IF NOT EXISTS tier_at_enrol text;
ALTER TABLE us_outbound.contacts ADD COLUMN IF NOT EXISTS data_record jsonb;
-- Email 1's subject arm (Harry, 5 Oct 2026): the General email1_subject or the Copy row's s1_subject.
ALTER TABLE us_outbound.contacts ADD COLUMN IF NOT EXISTS subject_arm text;
-- Which of the account's contacts this is (Harry, 6 Oct 2026): 1 the first, 2 the second contact (enrol/second.py).
ALTER TABLE us_outbound.contacts ADD COLUMN IF NOT EXISTS contact_slot integer;
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
COMMENT ON COLUMN us_outbound.contacts.opener_arm IS 'Email 1''s opener arm at enrollment (build addition; enrol/openers.py). One of: opener, holdout, none. holdout: the opener_holdout_share of accounts held out with no opener, by account hash, so replies compare opener against none.';
COMMENT ON COLUMN us_outbound.contacts.subject_arm IS 'Email 1''s subject arm at enrollment (build addition; render.subject_arm; Harry, 5 Oct 2026). One of: personal, copy. personal: the email1_subject_share of accounts, by a hash of the account id independent of the opener holdout''s, whose email 1 had the General email1_subject instead of the Copy row''s s1_subject, so replies compare the two. Emails 2 to 4 keep the Copy row''s subjects.';
COMMENT ON COLUMN us_outbound.contacts.opener_source IS 'The opener line used, or for a holdout the one it would have had: the signal and Signals-tab column (e.g. New People leader / opener_self), focus, or the generic line''s General key (e.g. opener_generic_ops) (build addition).';
COMMENT ON COLUMN us_outbound.contacts.signals_at_enrol IS 'The Score signals the account showed when this contact was enrolled: a JSON list of {signal, weight}, strongest first (enrol._record_enrolled; build addition, 5 Oct 2026).';
COMMENT ON COLUMN us_outbound.contacts.score_at_enrol IS 'The account''s score when this contact was enrolled (build addition, 5 Oct 2026).';
COMMENT ON COLUMN us_outbound.contacts.data_record IS 'Where the contact''s details came from and the lawful basis for emailing them, recorded at enrolment and never shown in an email: {contact_data, company_information, lawful_basis, shown_in_email} (render.data_record; Harry, 5 Oct 2026).';
COMMENT ON COLUMN us_outbound.contacts.tier_at_enrol IS 'The account''s tier when this contact was enrolled: Priority, Standard or Control (build addition, 5 Oct 2026).';
COMMENT ON COLUMN us_outbound.contacts.suppressed IS 'Never emailed: an opt-out or a bounce, or a person an approver declined at a send approval (enrol/approvals.py; Harry, 2 Oct 2026). A declined person is not added to the suppression table: they did not opt out.';
COMMENT ON COLUMN us_outbound.contacts.suppressed_reason IS 'Why it is suppressed, e.g. declined in Slack by U01ABCDEF at a send approval (2 Oct 2026).';
COMMENT ON COLUMN us_outbound.contacts.contact_slot IS 'Which of the account''s contacts this is, set at enrollment (build addition; Harry, 6 Oct 2026): 1 the first; 2 the second contact, a person of another role at an account of second_contact_min_employees or more staff, enrolled second_contact_delay_days after the first contact''s email 1 from the same sender (enrol/second.py). NULL: enrolled before 6 Oct 2026, read as 1.';
COMMENT ON COLUMN us_outbound.contacts.enrolled_at IS 'When the lead was added to its sender''s campaign (build addition). The send forecast dates each lead''s later steps from it, and the weekly target counts it.';
