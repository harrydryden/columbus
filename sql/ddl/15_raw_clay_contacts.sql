-- raw_clay_contacts: the raw output of the Clay function US Outbound – Contacts (SPEC 8), as received (SPEC 6, raw loads).
-- Append-only. key is the domain and person sent to Clay; payload is the record as received, as JSON text.
-- Retention: SPEC 6 sets no rule for raw loads. erase --email must cover any row holding personal data.
CREATE TABLE IF NOT EXISTS `{project}.us_outbound.raw_clay_contacts` (
  loaded_at TIMESTAMP,
  run_id STRING OPTIONS (description = "The heartbeats run that loaded it."),
  `key` STRING OPTIONS (description = "Domain and full name sent to Clay, joined with |."),
  payload STRING OPTIONS (description = "JSON text: the record as received.")
)
PARTITION BY DATE(loaded_at)
CLUSTER BY `key`
OPTIONS (
  description = "Raw output of the Clay function US Outbound – Contacts (SPEC 8). Holds personal data, so erase --email must cover it (SPEC 6, raw loads: as received)."
);
