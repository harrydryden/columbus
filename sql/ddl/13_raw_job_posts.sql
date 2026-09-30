-- raw_job_posts: careers-site feed postings from Greenhouse, Lever, Ashby and Workable (SPEC 7, job_posts), as received (SPEC 6, raw loads).
-- Append-only. key is the account domain and posting id; payload is the record as received, as JSON text.
-- Retention: SPEC 6 sets no rule for raw loads. erase --email must cover any row holding personal data.
CREATE TABLE IF NOT EXISTS `{project}.us_outbound.raw_job_posts` (
  loaded_at TIMESTAMP,
  run_id STRING OPTIONS (description = "The heartbeats run that loaded it."),
  `key` STRING OPTIONS (description = "Account domain and posting id, joined with |."),
  payload STRING OPTIONS (description = "JSON text: the record as received.")
)
PARTITION BY DATE(loaded_at)
CLUSTER BY `key`
OPTIONS (
  description = "Careers-site feed postings: Greenhouse, Lever, Ashby and Workable (SPEC 7) (SPEC 6, raw loads: as received)."
);
