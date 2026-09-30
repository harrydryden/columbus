-- raw_clay_accounts: the raw output of the Clay function US Outbound – Accounts (SPEC 8), as received (SPEC 6, raw loads).
-- Append-only. key is the domain sent to Clay; payload is the record as received, as jsonb. No primary key (TABLE_KEYS None).
-- Retention: SPEC 6 sets no rule for raw loads. erase --email must cover any row holding personal data.
CREATE TABLE IF NOT EXISTS us_outbound.raw_clay_accounts (
  loaded_at timestamptz,
  run_id text,
  "key" text,
  payload jsonb
);
CREATE INDEX IF NOT EXISTS raw_clay_accounts_key_idx ON us_outbound.raw_clay_accounts ("key");
COMMENT ON TABLE us_outbound.raw_clay_accounts IS 'Raw output of the Clay function US Outbound – Accounts (SPEC 8: the raw output is stored here) (SPEC 6, raw loads: as received).';
COMMENT ON COLUMN us_outbound.raw_clay_accounts.run_id IS 'The heartbeats run that loaded it.';
COMMENT ON COLUMN us_outbound.raw_clay_accounts."key" IS 'The domain sent to Clay.';
COMMENT ON COLUMN us_outbound.raw_clay_accounts.payload IS 'JSON: the record as received.';
