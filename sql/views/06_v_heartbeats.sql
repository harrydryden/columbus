-- v_heartbeats: the latest heartbeat of every job (SPEC 9, 13 Health).
-- SPEC 9: "Every job writes a heartbeat row; a missed heartbeat alerts in Slack."
-- The check that alerts compares age_minutes and minutes_since_ok with each job's
-- schedule; this view only reports.
--   age_minutes:       since the latest run finished, or started if it has not finished.
--   minutes_since_ok:  since the latest run with status ok finished (NULL if none).
--   last_alive_at:     the latest ok run, or skip for a reason of its own; a skip behind a
--                      run still going (or killed) is no sign of life (ops/heartbeat.py).
--   runs:              how many runs the job has on record.
CREATE OR REPLACE VIEW `{project}.us_outbound.v_heartbeats`
OPTIONS (
  description = "Latest heartbeat per job: status, dry_run, error, age in minutes, and minutes since the last ok run (SPEC 9, 13)."
)
AS
WITH alive AS (
  SELECT
    job,
    MAX(IF(status = 'ok', COALESCE(finished_at, started_at), NULL)) AS last_ok_at,
    MAX(COALESCE(finished_at, started_at)) AS last_alive_at
  FROM `{project}.us_outbound.heartbeats`
  WHERE status = 'ok'
     OR (status = 'skipped' AND IFNULL(JSON_VALUE(detail, '$.reason'), '') != 'previous run still going')
  GROUP BY job
),
latest AS (
  SELECT *, COUNT(*) OVER (PARTITION BY job) AS runs
  FROM `{project}.us_outbound.heartbeats`
  WHERE job IS NOT NULL
  QUALIFY ROW_NUMBER() OVER (PARTITION BY job ORDER BY started_at DESC, run_id DESC) = 1
)
SELECT
  h.job,
  h.run_id,
  h.status,
  h.dry_run,
  h.started_at,
  h.finished_at,
  h.finished_at IS NULL AS unfinished,
  h.error,
  TIMESTAMP_DIFF(h.finished_at, h.started_at, SECOND) AS duration_seconds,
  TIMESTAMP_DIFF(CURRENT_TIMESTAMP(), COALESCE(h.finished_at, h.started_at), MINUTE) AS age_minutes,
  o.last_ok_at,
  TIMESTAMP_DIFF(CURRENT_TIMESTAMP(), o.last_ok_at, MINUTE) AS minutes_since_ok,
  o.last_alive_at,
  h.runs
FROM latest AS h
LEFT JOIN alive AS o
  ON o.job = h.job
ORDER BY h.job;
