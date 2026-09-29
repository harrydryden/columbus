-- suppression: one row per do-not-contact entry (SPEC 6), plus expires_at (build).
-- SPEC 6: "Suppression: kept indefinitely, as hashes." Entries from kill rules and
-- unsubscribes have expires_at NULL. A Suppress signal adds the domain for its
-- counts_for_days (SPEC 9), so those rows carry expires_at. No table expiry.
CREATE TABLE IF NOT EXISTS `{project}.us_outbound.suppression` (
  email_sha256 STRING OPTIONS (description = "sha256 of the lower-cased, trimmed email; NULL for a domain-only entry."),
  domain STRING OPTIONS (description = "Root domain; NULL for an email-only entry."),
  reason STRING OPTIONS (description = "Why, e.g. unsubscribe, bounce, opt_out, kill_rule, or the name of a Suppress signal."),
  source STRING OPTIONS (description = "What added it, e.g. hubspot, instantly, reply, kill_rules, scoring."),
  added_at TIMESTAMP,
  expires_at TIMESTAMP OPTIONS (description = "NULL means indefinite. Set only for Suppress signals: added_at plus counts_for_days (SPEC 9).")
)
OPTIONS (
  description = "One row per do-not-contact entry (SPEC 6), kept indefinitely, as hashes. Only Suppress-signal entries expire (expires_at)."
);
