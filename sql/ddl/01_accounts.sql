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
  industry text,
  industry_group text,
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
COMMENT ON COLUMN us_outbound.accounts.size_band IS 'One of: 10-19, 20-49, 50-99, 100-249.';
COMMENT ON COLUMN us_outbound.accounts.source IS 'One of: apollo, irs, site_visit, named, lookalike. named: from the Named accounts tab; lookalike: found by Apollo as like a Spill customer, sources/lookalike_leads.py (build additions).';
COMMENT ON COLUMN us_outbound.accounts.score IS 'Sum of fresh Score signal weights, each capped at max_weight, total capped at score_cap (SPEC 9).';
COMMENT ON COLUMN us_outbound.accounts.tier IS 'One of: Priority, Standard, Control, Held, Excluded (SPEC 9).';
COMMENT ON COLUMN us_outbound.accounts.angle IS 'Angle name from the Angles tab. Control accounts always get General (SPEC 9).';
COMMENT ON COLUMN us_outbound.accounts.sender IS 'Mailbox owner name; set at first enrollment, never changed (SPEC 9, sender continuity).';
COMMENT ON COLUMN us_outbound.accounts.status IS 'One of: new, queued, verified, enrolled, engaged, demo_requested, demo_booked, disqualified.';
