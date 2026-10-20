-- heartbeats: one row per job run (build addition). SPEC 9: "Every job writes a
-- heartbeat row; a missed heartbeat alerts in Slack." ops/heartbeat.py writes the row
-- at start and upserts it by run_id at the end.
CREATE TABLE IF NOT EXISTS us_outbound.heartbeats (
  run_id text NOT NULL,
  job text,
  started_at timestamptz,
  finished_at timestamptz,
  status text,
  dry_run boolean,
  detail jsonb,
  error text,
  PRIMARY KEY (run_id)
);
CREATE INDEX IF NOT EXISTS heartbeats_job_started_at_idx ON us_outbound.heartbeats (job, started_at);
-- W2-4 (20 Oct 2026; ops/health.py): how long the run took and how it exited, so they outlive the logs.
ALTER TABLE us_outbound.heartbeats ADD COLUMN IF NOT EXISTS duration_s double precision;
ALTER TABLE us_outbound.heartbeats ADD COLUMN IF NOT EXISTS exit_code integer;
COMMENT ON TABLE us_outbound.heartbeats IS 'One row per job run (build addition). Every job writes a heartbeat; a missed heartbeat alerts in Slack (SPEC 9).';
COMMENT ON COLUMN us_outbound.heartbeats.finished_at IS 'NULL while the run is going (or if it died).';
COMMENT ON COLUMN us_outbound.heartbeats.status IS 'One of: running, ok, error, skipped. running is written at start; finished_at is NULL until the run ends.';
COMMENT ON COLUMN us_outbound.heartbeats.detail IS 'JSON: the job''s summary.';
COMMENT ON COLUMN us_outbound.heartbeats.run_id IS 'A run''s id; for a scheduled start "<job>:<due minute UTC>", claimed once (W2-4).';
COMMENT ON COLUMN us_outbound.heartbeats.exit_code IS '0 ok or skipped, 1 error, 2 refused, 3 guardrail, 143 SIGTERM; the scheduler''s own when the run died.';
