-- domain_aliases: one row per alias domain (build addition). SPEC 13: "Domain: the root
-- domain, lower case, without www. Follow one redirect, and keep an alias list."
CREATE TABLE IF NOT EXISTS us_outbound.domain_aliases (
  alias text NOT NULL,
  root_domain text,
  source text,
  added_at timestamptz,
  PRIMARY KEY (alias)
);
CREATE INDEX IF NOT EXISTS domain_aliases_root_domain_idx ON us_outbound.domain_aliases (root_domain);
COMMENT ON TABLE us_outbound.domain_aliases IS 'One row per alias domain (build addition): the alias list of SPEC 13, so one account per root domain holds across redirects.';
COMMENT ON COLUMN us_outbound.domain_aliases.alias IS 'A domain that resolves to root_domain, lower case.';
COMMENT ON COLUMN us_outbound.domain_aliases.root_domain IS 'The account''s root domain.';
COMMENT ON COLUMN us_outbound.domain_aliases.source IS 'How it was found, e.g. redirect, clay, apollo, override.';
