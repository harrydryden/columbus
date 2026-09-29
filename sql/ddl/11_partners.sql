-- partners: one row per partner organization (build addition). SPEC 9, hard exclusions:
-- "Brokers, insurers, HR-tech vendors, PEOs (NAICS 561330), HR consultancies (541612)
-- and behavioural-health providers. These go on a partner list and are never prospected."
CREATE TABLE IF NOT EXISTS `{project}.us_outbound.partners` (
  domain STRING NOT NULL OPTIONS (description = "Root domain."),
  name STRING,
  reason STRING OPTIONS (description = "Which exclusion, e.g. broker, insurer, hr_tech, peo, hr_consultancy, behavioral_health."),
  naics STRING,
  added_at TIMESTAMP
)
OPTIONS (
  description = "One row per partner organization (build addition): brokers, insurers, HR-tech vendors, PEOs, HR consultancies and behavioral-health providers, never prospected (SPEC 9)."
);
