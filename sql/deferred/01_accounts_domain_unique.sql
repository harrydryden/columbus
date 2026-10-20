-- One account per root domain, held by the database (W3-3, 20 Oct 2026; code-sources B7; SPEC 13). Not in sql/ddl:
-- `db apply` (the pre-deploy) runs every DDL statement in one transaction, and a CREATE UNIQUE INDEX over a domain two
-- accounts still share would fail it and block every deploy (RS §4.8). `us-outbound accounts dedupe --live` applies
-- this file once its merges leave no duplicate domain (ops/ddl.apply_deferred). accounts_domain_idx (01_accounts.sql)
-- stays: it is what the reads use until this exists, and dropping it is not additive.
CREATE UNIQUE INDEX IF NOT EXISTS accounts_domain_unique_idx ON us_outbound.accounts (domain);
