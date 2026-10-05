-- signal_events: one row per observation with evidence (SPEC 6, 7).
-- SPEC 7: "Each source module writes facts {account_id, source, fact, value, quote, url,
-- observed_at} into signal_events. The scoring module matches facts to the Signals tab."
-- The scoring job also writes one row per (account, matched signal) with source =
-- 'scoring' and fact = 'signal_matched'; v_signal_value reads those.
CREATE TABLE IF NOT EXISTS us_outbound.signal_events (
  event_id text NOT NULL,
  account_id text,
  source text,
  fact text,
  value jsonb,
  quote text,
  source_url text,
  observed_at timestamptz,
  PRIMARY KEY (event_id)
);
CREATE INDEX IF NOT EXISTS signal_events_account_id_idx ON us_outbound.signal_events (account_id);
CREATE INDEX IF NOT EXISTS signal_events_observed_at_idx ON us_outbound.signal_events (observed_at);
CREATE INDEX IF NOT EXISTS signal_events_source_fact_idx ON us_outbound.signal_events (source, fact);
COMMENT ON TABLE us_outbound.signal_events IS 'One row per observation with evidence (SPEC 6). Sources write facts; scoring matches them to the Signals tab, so adding a keyword signal needs no code (SPEC 7).';
COMMENT ON COLUMN us_outbound.signal_events.source IS 'A SPEC 7 source key (apollo_org, apollo_people, apollo_jobs, site_visits, clay_careers, careers_pages, clay_funding, job_posts, irs_bmf, layoffs, calendar, named, lookalike), a job''s own facts (pick_contacts, verify_accounts), or scoring.';
COMMENT ON COLUMN us_outbound.signal_events.fact IS 'The field name for field sources (SPEC 7), the kind of text for text sources, or signal_matched for scoring rows.';
COMMENT ON COLUMN us_outbound.signal_events.value IS 'JSON. For signal_matched rows, an object with signal, weight and evidence.';
COMMENT ON COLUMN us_outbound.signal_events.quote IS 'Evidence quote, at most 300 characters (SPEC 6).';
