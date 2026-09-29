-- domain_aliases: one row per alias domain (build addition). SPEC 13: "Domain: the root
-- domain, lower case, without www. Follow one redirect, and keep an alias list."
CREATE TABLE IF NOT EXISTS `{project}.us_outbound.domain_aliases` (
  alias STRING NOT NULL OPTIONS (description = "A domain that resolves to root_domain, lower case."),
  root_domain STRING OPTIONS (description = "The account's root domain."),
  source STRING OPTIONS (description = "How it was found, e.g. redirect, clay, apollo, override."),
  added_at TIMESTAMP
)
OPTIONS (
  description = "One row per alias domain (build addition): the alias list of SPEC 13, so one account per root domain holds across redirects."
);
