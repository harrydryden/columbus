-- raw_site_visits: Apollo website-visitor records for spill.chat (SPEC 7, site_visits), as received (SPEC 6, raw loads).
-- Append-only. key is the visiting organization's Apollo id or domain; payload is the record as received, as JSON text.
-- Retention: SPEC 6 sets no rule for raw loads. erase --email must cover any row holding personal data.
CREATE TABLE IF NOT EXISTS `{project}.us_outbound.raw_site_visits` (
  loaded_at TIMESTAMP,
  run_id STRING OPTIONS (description = "The heartbeats run that loaded it."),
  `key` STRING OPTIONS (description = "Apollo organization id or domain of the visitor."),
  payload STRING OPTIONS (description = "JSON text: the record as received.")
)
PARTITION BY DATE(loaded_at)
CLUSTER BY `key`
OPTIONS (
  description = "Apollo website-visitor records for the tracked domain spill.chat (SPEC 7) (SPEC 6, raw loads: as received)."
);
