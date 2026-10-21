-- source_state: the sources' own place and plans, one row per key (W3-3, 20 Oct 2026; code-sources B11). Until W3-3
-- the jobs read their state back out of credit_ledger notes (source_universe's cursors, site_visits' quiet runs and
-- the visitors it judged), so a note's shape moved a cursor; the ledger now records money only. Additive: a new
-- table, read and written by sources/source_state.py. No personal data: search keys, page numbers, Apollo
-- organization ids and counts.
CREATE TABLE IF NOT EXISTS us_outbound.source_state (
  key text NOT NULL,
  value jsonb,
  updated_at timestamptz,
  PRIMARY KEY (key)
);
COMMENT ON TABLE us_outbound.source_state IS 'One row per key of the sources'' own state (W3-3, 20 Oct 2026; SPEC 7 sources, SPEC 9 source_universe and site_visits): each Apollo search''s place this month, the week''s allocation plan, the website visitors judged and the visitor list last read. Kept out of credit_ledger, which records money only (code-sources B11).';
COMMENT ON COLUMN us_outbound.source_state.key IS 'cursor:<YYYY-MM>:<search key> (source_universe: the last page read this UK month and the pages the search has), plan:<ISO week> (sources/allocation.py: the pages per state and what is left of them), site_visits:list, site_visits:quiet_runs, site_visits:judged, or migrated:<what> (the one-off copy from the ledger notes).';
COMMENT ON COLUMN us_outbound.source_state.value IS 'JSON: the state, as sources/source_state.py documents for each key.';
COMMENT ON COLUMN us_outbound.source_state.updated_at IS 'When it was last written.';
