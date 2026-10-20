-- metrics: the persisted series every report and loop reads (W3-1, 20 Oct 2026; design-final §4). Until W3-1 every
-- number lived in heartbeat JSON or 7-day logs, so a loop could read no series. metrics_daily (learn/metrics.py,
-- daily 08:50 UK) writes one row per (day, segment, metric, definition version): the day's process numbers for
-- yesterday, and each open step-1 cohort week's outcome numbers, upserted on every run. A change to a definition
-- bumps learn/metrics.DEFINITION_VERSION; the next run recomputes every open cohort and the last 8 closed ones under
-- the new version, and readers take the latest version per (day, segment, metric), so a series never silently mixes
-- definitions (RT §6.7). Additive: a new table, written only by metrics_daily. No personal data: counts, rates and
-- our own mailbox addresses.
CREATE TABLE IF NOT EXISTS us_outbound.metrics (
  day date NOT NULL,
  segment_key text NOT NULL,
  metric text NOT NULL,
  n integer,
  k integer,
  value double precision,
  definition_version text NOT NULL,
  computed_at timestamptz,
  PRIMARY KEY (day, segment_key, metric, definition_version)
);
CREATE INDEX IF NOT EXISTS metrics_metric_day_idx ON us_outbound.metrics (metric, day);
COMMENT ON TABLE us_outbound.metrics IS 'One row per day, segment, metric and definition version (W3-1, 20 Oct 2026; SPEC 12 measurement, design-final §4): the series the readout and the learning loops read, written by metrics_daily (learn/metrics.py). Readers take the latest definition_version per (day, segment_key, metric).';
COMMENT ON COLUMN us_outbound.metrics.day IS 'The day the row is about (UK date): yesterday for a process metric; the Monday of the step-1 week for a cohort row (segment_key cohort=...); the Monday of the week forecast for forecast: and forecast_score: rows.';
COMMENT ON COLUMN us_outbound.metrics.segment_key IS 'all, or one cut: group=, size_band=, state=, slice=, arm=, sham_arm=, source=, mailbox=, gate=, job=, cohort=<ISO week>.';
COMMENT ON COLUMN us_outbound.metrics.metric IS 'The metric''s name (learn/metrics.IDS maps each to its design-final §4 id, e.g. engaged_rate_45 is O2), forecast:<name> for a Monday prediction and forecast_score:<name> for its score the next Monday.';
COMMENT ON COLUMN us_outbound.metrics.n IS 'The denominator (companies, contacts, sends, judged rows); NULL for a plain count.';
COMMENT ON COLUMN us_outbound.metrics.k IS 'The numerator, or the count.';
COMMENT ON COLUMN us_outbound.metrics.value IS 'k / n for a rate (NULL when n is 0), the count, or the measured value (days of supply, hours, a forecast''s point).';
COMMENT ON COLUMN us_outbound.metrics.definition_version IS 'learn/metrics.DEFINITION_VERSION when the row was computed.';
COMMENT ON COLUMN us_outbound.metrics.computed_at IS 'When metrics_daily computed it.';
