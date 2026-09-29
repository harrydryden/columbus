-- heartbeats: one row per job run (build addition). SPEC 9: "Every job writes a
-- heartbeat row; a missed heartbeat alerts in Slack." ops/heartbeat.py writes the row
-- at start and upserts it by run_id at the end.
CREATE TABLE IF NOT EXISTS `{project}.us_outbound.heartbeats` (
  run_id STRING NOT NULL,
  job STRING,
  started_at TIMESTAMP,
  finished_at TIMESTAMP OPTIONS (description = "NULL while the run is going (or if it died)."),
  status STRING OPTIONS (description = "One of: ok, error, skipped."),
  dry_run BOOL,
  detail STRING OPTIONS (description = "JSON text: the job's summary."),
  error STRING
)
PARTITION BY DATE(started_at)
CLUSTER BY job
OPTIONS (
  description = "One row per job run (build addition). Every job writes a heartbeat; a missed heartbeat alerts in Slack (SPEC 9)."
);
