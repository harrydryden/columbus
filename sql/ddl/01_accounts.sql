-- accounts: one row per company, keyed by root domain (SPEC 6).
-- SPEC 2: "The company is the unit: it has one score, one tier and one contact in v1."
-- Retention (SPEC 6): "Universe rows not refreshed in 12 months: deleted." That is the
-- retention job's work, not DDL; it uses last_scored (else first_seen).
CREATE TABLE IF NOT EXISTS us_outbound.accounts (
  account_id text NOT NULL,
  domain text,
  clean_name text,
  legal_name text,
  apollo_org_id text,
  hq_city text,
  hq_state text,
  hq_country text,
  industry text,
  industry_group text,
  label_source text,
  label_confidence text,
  label_checked_at timestamptz,
  naics text,
  employees integer,
  us_employees integer,
  size_band text,
  founded_year integer,
  source text,
  score integer,
  tier text,
  tier_reason text,
  angle text,
  sender text,
  status text,
  clay_checked_at timestamptz,
  clay_credits_used double precision,
  hubspot_company_id text,
  first_seen timestamptz,
  last_scored timestamptz,
  PRIMARY KEY (account_id)
);
CREATE INDEX IF NOT EXISTS accounts_domain_idx ON us_outbound.accounts (domain);
CREATE INDEX IF NOT EXISTS accounts_status_idx ON us_outbound.accounts (status);
COMMENT ON TABLE us_outbound.accounts IS 'One row per company (root domain) (SPEC 6). The company is the unit: one score, one tier and one contact in v1 (SPEC 2). Universe rows not refreshed in 12 months are deleted by the retention job.';
COMMENT ON COLUMN us_outbound.accounts.account_id IS 'uuid. Also HubSpot us_outbound_account_id (SPEC 11).';
COMMENT ON COLUMN us_outbound.accounts.domain IS 'Root domain, lower case, without www (SPEC 13). One account per root domain.';
COMMENT ON COLUMN us_outbound.accounts.clean_name IS 'Cleaned company name (SPEC 13).';
COMMENT ON COLUMN us_outbound.accounts.legal_name IS 'Legal name, kept separately from clean_name (SPEC 13).';
COMMENT ON COLUMN us_outbound.accounts.hq_state IS 'USPS code.';
COMMENT ON COLUMN us_outbound.accounts.industry IS 'Website industry label; the key of the Industries tab.';
COMMENT ON COLUMN us_outbound.accounts.industry_group IS 'Website industry group (Industries tab).';
COMMENT ON COLUMN us_outbound.accounts.size_band IS 'One of: 1-4, 5-9, 10-19, 20-49, 50-99, 100-249, 250-499, 500-999, 1000-2499, 2500-4999, 5000-9999. SPEC 2''s four bands inside a ladder, so General min_employees and max_employees can move (6 Oct 2026).';
COMMENT ON COLUMN us_outbound.accounts.source IS 'One of: apollo, irs, site_visit, named, lookalike. named: from the Named accounts tab; lookalike: found by Apollo as like a Spill customer, sources/lookalike_leads.py (build additions).';
COMMENT ON COLUMN us_outbound.accounts.score IS 'Sum of fresh Score signal weights, each capped at max_weight, total capped at score_cap (SPEC 9).';
COMMENT ON COLUMN us_outbound.accounts.tier IS 'One of: Priority, Standard, Control, Held, Excluded (SPEC 9).';
COMMENT ON COLUMN us_outbound.accounts.angle IS 'Angle name from the Angles tab. Control accounts always get General (SPEC 9).';
COMMENT ON COLUMN us_outbound.accounts.sender IS 'Mailbox owner name; set at first enrollment, never changed (SPEC 9, sender continuity).';
COMMENT ON COLUMN us_outbound.accounts.status IS 'One of: new, queued, verified, enrolled, engaged, demo_requested, demo_booked, disqualified.';

-- Build addition (Harry, 6 Oct 2026): Apollo's HQ country, so a website visitor with no HQ state is known to be in
-- the US (accounts.any_us_state: a visitor is never excluded on its state).
ALTER TABLE us_outbound.accounts ADD COLUMN IF NOT EXISTS hq_country text;
COMMENT ON COLUMN us_outbound.accounts.hq_country IS 'Apollo''s HQ country: United States, or blank when Apollo gives none (build addition, 6 Oct 2026).';

-- Build addition (Harry, 7 Oct 2026: "industry categorisation is critical"): the task model's check of the industry
-- label (us_outbound/labels.py), made once in verify_accounts. The full verdict (rules and model labels, evidence,
-- "what they do") is a label_verdict fact in signal_events; these say which copy the label earns.
ALTER TABLE us_outbound.accounts ADD COLUMN IF NOT EXISTS label_source text;
ALTER TABLE us_outbound.accounts ADD COLUMN IF NOT EXISTS label_confidence text;
ALTER TABLE us_outbound.accounts ADD COLUMN IF NOT EXISTS label_checked_at timestamptz;
COMMENT ON COLUMN us_outbound.accounts.label_source IS 'One of: override, approver, rules+model, model, umbrella, disputed, rules. Blank: not checked yet (the group''s copy). override: an Overrides row; approver: set at a send approval or by labels set; rules+model: the rules and the model agree; model: the model, sure, overruled the rules; umbrella: they disagree within a group, so the group''s label and copy; disputed: they disagree across groups, so the rules'' label with General copy; rules: not checked (label_check skip, or nothing to check against). The copy level follows: the label''s own copy for override, approver, rules+model and model; the group''s for umbrella, rules and blank; General for disputed (labels.copy_level).';
COMMENT ON COLUMN us_outbound.accounts.label_confidence IS 'One of: high, medium, low. The model''s confidence in its label (high for an approver''s); blank when not checked.';
COMMENT ON COLUMN us_outbound.accounts.label_checked_at IS 'When the label was last decided by the label check, an Overrides row or an approver.';
