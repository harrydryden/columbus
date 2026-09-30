-- raw_layoffs: layoffs.fyi and WARN notices (SPEC 7, layoffs), as received (SPEC 6, raw loads).
-- Append-only. key is the company domain or name; payload is the record as received, as JSON text.
-- Retention: SPEC 6 sets no rule for raw loads. erase --email must cover any row holding personal data.
CREATE TABLE IF NOT EXISTS `{project}.us_outbound.raw_layoffs` (
  loaded_at TIMESTAMP,
  run_id STRING OPTIONS (description = "The heartbeats run that loaded it."),
  `key` STRING OPTIONS (description = "Company domain, or name when there is none."),
  payload STRING OPTIONS (description = "JSON text: the record as received.")
)
PARTITION BY DATE(loaded_at)
CLUSTER BY `key`
OPTIONS (
  description = "Layoffs.fyi and Big Local News WARN notices (SPEC 7) (SPEC 6, raw loads: as received)."
);
