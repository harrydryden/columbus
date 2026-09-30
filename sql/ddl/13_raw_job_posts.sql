-- raw_job_posts: careers-site feed postings from Greenhouse, Lever, Ashby and Workable (SPEC 7, job_posts), as received (SPEC 6, raw loads).
-- Append-only. key is the account domain and posting id; payload is the record as received, as jsonb. No primary key (TABLE_KEYS None).
-- Retention: SPEC 6 sets no rule for raw loads. erase --email must cover any row holding personal data.
CREATE TABLE IF NOT EXISTS us_outbound.raw_job_posts (
  loaded_at timestamptz,
  run_id text,
  "key" text,
  payload jsonb
);
CREATE INDEX IF NOT EXISTS raw_job_posts_key_idx ON us_outbound.raw_job_posts ("key");
COMMENT ON TABLE us_outbound.raw_job_posts IS 'Careers-site feed postings: Greenhouse, Lever, Ashby and Workable (SPEC 7) (SPEC 6, raw loads: as received).';
COMMENT ON COLUMN us_outbound.raw_job_posts.run_id IS 'The heartbeats run that loaded it.';
COMMENT ON COLUMN us_outbound.raw_job_posts."key" IS 'Account domain and posting id, joined with |.';
COMMENT ON COLUMN us_outbound.raw_job_posts.payload IS 'JSON: the record as received.';
