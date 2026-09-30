-- partners: one row per partner organization (build addition). SPEC 9, hard exclusions:
-- "Brokers, insurers, HR-tech vendors, PEOs (NAICS 561330), HR consultancies (541612)
-- and behavioural-health providers. These go on a partner list and are never prospected."
CREATE TABLE IF NOT EXISTS us_outbound.partners (
  domain text NOT NULL,
  name text,
  reason text,
  naics text,
  added_at timestamptz,
  PRIMARY KEY (domain)
);
COMMENT ON TABLE us_outbound.partners IS 'One row per partner organization (build addition): brokers, insurers, HR-tech vendors, PEOs, HR consultancies and behavioral-health providers, never prospected (SPEC 9).';
COMMENT ON COLUMN us_outbound.partners.domain IS 'Root domain.';
COMMENT ON COLUMN us_outbound.partners.reason IS 'Which exclusion, e.g. broker, insurer, hr_tech, peo, hr_consultancy, behavioral_health.';
