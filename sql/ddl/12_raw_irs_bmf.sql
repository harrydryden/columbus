-- raw_irs_bmf: IRS Exempt Organizations Business Master File rows (SPEC 7, irs_bmf), as received (SPEC 6, raw loads).
-- Append-only. key is the EIN; payload is the record as received, as jsonb. No primary key (TABLE_KEYS None).
-- Retention: SPEC 6 sets no rule for raw loads. erase --email must cover any row holding personal data.
CREATE TABLE IF NOT EXISTS us_outbound.raw_irs_bmf (
  loaded_at timestamptz,
  run_id text,
  "key" text,
  payload jsonb
);
CREATE INDEX IF NOT EXISTS raw_irs_bmf_key_idx ON us_outbound.raw_irs_bmf ("key");
COMMENT ON TABLE us_outbound.raw_irs_bmf IS 'IRS Exempt Organizations Business Master File rows, by state (SPEC 7) (SPEC 6, raw loads: as received).';
COMMENT ON COLUMN us_outbound.raw_irs_bmf.run_id IS 'The heartbeats run that loaded it.';
COMMENT ON COLUMN us_outbound.raw_irs_bmf."key" IS 'The EIN.';
COMMENT ON COLUMN us_outbound.raw_irs_bmf.payload IS 'JSON: the record as received.';
