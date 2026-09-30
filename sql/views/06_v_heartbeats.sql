-- v_heartbeats: the latest heartbeat of every job (SPEC 9, 13 Health).
-- SPEC 9: "Every job writes a heartbeat row; a missed heartbeat alerts in Slack."
-- The check that alerts compares age_minutes and minutes_since_ok with each job's
-- schedule; this view only reports.
--   age_minutes:       since the latest run finished, or started if it has not finished.
--   minutes_since_ok:  since the latest run with status ok finished (NULL if none).
--   last_alive_at:     the latest ok run, or skip for a reason of its own; a skip behind a
--                      run still going (or killed) is no sign of life (ops/heartbeat.py).
--   runs:              how many runs the job has on record.
-- Durations are whole seconds or minutes, rounded toward zero.
CREATE OR REPLACE VIEW us_outbound.v_heartbeats AS
WITH alive AS (
  SELECT
    job,
    max(COALESCE(finished_at, started_at)) FILTER (WHERE status = 'ok') AS last_ok_at,
    max(COALESCE(finished_at, started_at)) AS last_alive_at
  FROM us_outbound.heartbeats
  WHERE status = 'ok'
     OR (status = 'skipped' AND COALESCE(detail ->> 'reason', '') <> 'previous run still going')
  GROUP BY job
),
latest AS (
  -- Window functions run before DISTINCT ON, so runs counts every run of the job.
  SELECT DISTINCT ON (job)
    *,
    count(*) OVER (PARTITION BY job) AS runs
  FROM us_outbound.heartbeats
  WHERE job IS NOT NULL
  ORDER BY job, started_at DESC NULLS LAST, run_id DESC
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
  trunc(date_part('epoch', h.finished_at - h.started_at))::bigint AS duration_seconds,
  trunc(date_part('epoch', now() - COALESCE(h.finished_at, h.started_at)) / 60)::bigint AS age_minutes,
  o.last_ok_at,
  trunc(date_part('epoch', now() - o.last_ok_at) / 60)::bigint AS minutes_since_ok,
  o.last_alive_at,
  h.runs
FROM latest AS h
LEFT JOIN alive AS o
  ON o.job = h.job
ORDER BY h.job;
COMMENT ON VIEW us_outbound.v_heartbeats IS 'Latest heartbeat per job: status, dry_run, error, age in minutes, and minutes since the last ok run (SPEC 9, 13).';
