-- raw_irs_bmf: IRS Exempt Organizations Business Master File rows (SPEC 7, irs_bmf), as received (SPEC 6, raw loads).
-- Append-only. key is the EIN; payload is the record as received, as JSON text.
-- Retention: SPEC 6 sets no rule for raw loads. erase --email must cover any row holding personal data.
CREATE TABLE IF NOT EXISTS `{project}.us_outbound.raw_irs_bmf` (
  loaded_at TIMESTAMP,
  run_id STRING OPTIONS (description = "The heartbeats run that loaded it."),
  `key` STRING OPTIONS (description = "The EIN."),
  payload STRING OPTIONS (description = "JSON text: the record as received.")
)
PARTITION BY DATE(loaded_at)
CLUSTER BY `key`
OPTIONS (
  description = "IRS Exempt Organizations Business Master File rows, by state (SPEC 7) (SPEC 6, raw loads: as received)."
);
