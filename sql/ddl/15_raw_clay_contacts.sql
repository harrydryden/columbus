-- raw_clay_contacts: the raw output of the Clay function US Outbound – Contacts (SPEC 8), as received (SPEC 6, raw loads).
-- Append-only. key is the domain and person sent to Clay; payload is the record as received, as jsonb. No primary key (TABLE_KEYS None).
-- Retention: SPEC 6 sets no rule for raw loads. erase --email must cover any row holding personal data, and the
-- retention job deletes the rows holding a contact's address when it deletes the contact (ops/retention.py).
CREATE TABLE IF NOT EXISTS us_outbound.raw_clay_contacts (
  loaded_at timestamptz,
  run_id text,
  "key" text,
  payload jsonb
);
CREATE INDEX IF NOT EXISTS raw_clay_contacts_key_idx ON us_outbound.raw_clay_contacts ("key");
COMMENT ON TABLE us_outbound.raw_clay_contacts IS 'Raw output of the Clay function US Outbound – Contacts (SPEC 8). Holds personal data, so erase --email must cover it (SPEC 6, raw loads: as received).';
COMMENT ON COLUMN us_outbound.raw_clay_contacts.run_id IS 'The heartbeats run that loaded it.';
COMMENT ON COLUMN us_outbound.raw_clay_contacts."key" IS 'Domain and full name sent to Clay, joined with |.';
COMMENT ON COLUMN us_outbound.raw_clay_contacts.payload IS 'JSON: the record as received.';
