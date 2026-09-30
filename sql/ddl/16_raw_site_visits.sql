-- raw_site_visits: Apollo website-visitor records for spill.chat (SPEC 7, site_visits), as received (SPEC 6, raw loads).
-- Append-only. key is the visiting organization's Apollo id or domain; payload is the record as received, as jsonb. No primary key (TABLE_KEYS None).
-- Retention: SPEC 6 sets no rule for raw loads. erase --email must cover any row holding personal data.
CREATE TABLE IF NOT EXISTS us_outbound.raw_site_visits (
  loaded_at timestamptz,
  run_id text,
  "key" text,
  payload jsonb
);
CREATE INDEX IF NOT EXISTS raw_site_visits_key_idx ON us_outbound.raw_site_visits ("key");
COMMENT ON TABLE us_outbound.raw_site_visits IS 'Apollo website-visitor records for the tracked domain spill.chat (SPEC 7) (SPEC 6, raw loads: as received).';
COMMENT ON COLUMN us_outbound.raw_site_visits.run_id IS 'The heartbeats run that loaded it.';
COMMENT ON COLUMN us_outbound.raw_site_visits."key" IS 'Apollo organization id or domain of the visitor.';
COMMENT ON COLUMN us_outbound.raw_site_visits.payload IS 'JSON: the record as received.';
