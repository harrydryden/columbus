-- suppression: one row per do-not-contact entry (SPEC 6), plus expires_at (build).
-- SPEC 6: "Suppression: kept indefinitely, as hashes." Entries from kill rules and
-- unsubscribes have expires_at NULL. A Suppress signal adds the domain for its
-- counts_for_days (SPEC 9), so those rows carry expires_at.
-- The key (email_sha256, domain) may have a NULL part (a domain-only entry has no hash, an
-- email-only entry no domain), so it is UNIQUE NULLS NOT DISTINCT (Postgres 15+) rather than a
-- primary key; PostgresStore.upsert names these columns in ON CONFLICT.
CREATE TABLE IF NOT EXISTS us_outbound.suppression (
  email_sha256 text,
  domain text,
  reason text,
  source text,
  added_at timestamptz,
  expires_at timestamptz,
  UNIQUE NULLS NOT DISTINCT (email_sha256, domain)
);
CREATE INDEX IF NOT EXISTS suppression_domain_idx ON us_outbound.suppression (domain);
COMMENT ON TABLE us_outbound.suppression IS 'One row per do-not-contact entry (SPEC 6), kept indefinitely, as hashes. Only Suppress-signal entries expire (expires_at).';
COMMENT ON COLUMN us_outbound.suppression.email_sha256 IS 'sha256 of the lower-cased, trimmed email; NULL for a domain-only entry.';
COMMENT ON COLUMN us_outbound.suppression.domain IS 'Root domain; NULL for an email-only entry.';
COMMENT ON COLUMN us_outbound.suppression.reason IS 'Why, e.g. unsubscribe, bounce, opt_out, kill_rule, or the name of a Suppress signal.';
COMMENT ON COLUMN us_outbound.suppression.source IS 'What added it, e.g. hubspot, instantly, reply, kill_rules, scoring.';
COMMENT ON COLUMN us_outbound.suppression.expires_at IS 'NULL means indefinite. Set only for Suppress signals: added_at plus counts_for_days (SPEC 9).';
