-- accounts: one row per company, keyed by root domain (SPEC 6).
-- SPEC 2: "The company is the unit: it has one score, one tier and one contact in v1."
-- Retention (SPEC 6): "Universe rows not refreshed in 12 months: deleted." That is the
-- retention job's work, not a table expiry; it uses last_scored (else first_seen).
CREATE TABLE IF NOT EXISTS `{project}.us_outbound.accounts` (
  account_id STRING NOT NULL OPTIONS (description = "uuid. Also HubSpot us_outbound_account_id (SPEC 11)."),
  domain STRING OPTIONS (description = "Root domain, lower case, without www (SPEC 13). One account per root domain."),
  clean_name STRING OPTIONS (description = "Cleaned company name (SPEC 13)."),
  legal_name STRING OPTIONS (description = "Legal name, kept separately from clean_name (SPEC 13)."),
  apollo_org_id STRING,
  hq_city STRING,
  hq_state STRING OPTIONS (description = "USPS code."),
  industry STRING OPTIONS (description = "Website industry label; the key of the Industries tab."),
  industry_group STRING OPTIONS (description = "Website industry group (Industries tab)."),
  naics STRING,
  employees INT64,
  us_employees INT64,
  size_band STRING OPTIONS (description = "One of: 10-19, 20-49, 50-99, 100-249."),
  founded_year INT64,
  source STRING OPTIONS (description = "One of: apollo, irs, site_visit."),
  score INT64 OPTIONS (description = "Sum of fresh Score signal weights, each capped at max_weight, total capped at score_cap (SPEC 9)."),
  tier STRING OPTIONS (description = "One of: Priority, Standard, Control, Held, Excluded (SPEC 9)."),
  tier_reason STRING,
  angle STRING OPTIONS (description = "Angle name from the Angles tab. Control accounts always get General (SPEC 9)."),
  sender STRING OPTIONS (description = "Mailbox owner name; set at first enrollment, never changed (SPEC 9, sender continuity)."),
  status STRING OPTIONS (description = "One of: new, queued, verified, enrolled, engaged, demo_requested, demo_booked, disqualified."),
  clay_checked_at TIMESTAMP,
  clay_credits_used FLOAT64,
  hubspot_company_id STRING,
  first_seen TIMESTAMP,
  last_scored TIMESTAMP
)
CLUSTER BY account_id
OPTIONS (
  description = "One row per company (root domain) (SPEC 6). The company is the unit: one score, one tier and one contact in v1 (SPEC 2). Universe rows not refreshed in 12 months are deleted by the retention job."
);
