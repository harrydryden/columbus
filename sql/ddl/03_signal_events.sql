-- signal_events: one row per observation with evidence (SPEC 6, 7).
-- SPEC 7: "Each source module writes facts {account_id, source, fact, value, quote, url,
-- observed_at} into signal_events. The scoring module matches facts to the Signals tab."
-- The scoring job also writes one row per (account, matched signal) with source =
-- 'scoring' and fact = 'signal_matched'; v_signal_value reads those.
CREATE TABLE IF NOT EXISTS `{project}.us_outbound.signal_events` (
  event_id STRING NOT NULL,
  account_id STRING,
  source STRING OPTIONS (description = "A SPEC 7 source key (apollo_org, apollo_people, apollo_jobs, site_visits, clay_careers, clay_funding, job_posts, irs_bmf, layoffs, calendar), or scoring."),
  fact STRING OPTIONS (description = "The field name for field sources (SPEC 7), the kind of text for text sources, or signal_matched for scoring rows."),
  value STRING OPTIONS (description = "JSON text. For signal_matched rows, an object with signal, weight and evidence."),
  quote STRING OPTIONS (description = "Evidence quote, at most 300 characters (SPEC 6)."),
  source_url STRING,
  observed_at TIMESTAMP
)
PARTITION BY DATE(observed_at)
CLUSTER BY account_id
OPTIONS (
  description = "One row per observation with evidence (SPEC 6). Sources write facts; scoring matches them to the Signals tab, so adding a keyword signal needs no code (SPEC 7)."
);
