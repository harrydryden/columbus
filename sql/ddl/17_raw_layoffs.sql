-- raw_layoffs: layoffs.fyi and WARN notices (SPEC 7, layoffs), as received (SPEC 6, raw loads).
-- Append-only. key is the company domain or name; payload is the record as received, as jsonb. No primary key (TABLE_KEYS None).
-- Retention: SPEC 6 sets no rule for raw loads. erase --email must cover any row holding personal data.
CREATE TABLE IF NOT EXISTS us_outbound.raw_layoffs (
  loaded_at timestamptz,
  run_id text,
  "key" text,
  payload jsonb
);
CREATE INDEX IF NOT EXISTS raw_layoffs_key_idx ON us_outbound.raw_layoffs ("key");
COMMENT ON TABLE us_outbound.raw_layoffs IS 'Layoffs.fyi and Big Local News WARN notices (SPEC 7) (SPEC 6, raw loads: as received).';
COMMENT ON COLUMN us_outbound.raw_layoffs.run_id IS 'The heartbeats run that loaded it.';
COMMENT ON COLUMN us_outbound.raw_layoffs."key" IS 'Company domain, or name when there is none.';
COMMENT ON COLUMN us_outbound.raw_layoffs.payload IS 'JSON: the record as received.';
