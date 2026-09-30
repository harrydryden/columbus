-- credit_ledger: one row per spend of Clay or Apollo credits or Claude dollars (build).
-- SPEC 8: "Before each batch, compute the month's remaining Clay budget: clay_monthly_credits
-- minus the credits recorded this month." The same ledger serves Apollo and the Claude
-- cap (SPEC 1.1, 1.6). Months are UTC calendar months.
CREATE TABLE IF NOT EXISTS us_outbound.credit_ledger (
  entry_id text NOT NULL,
  system text,
  job text,
  run_id text,
  account_id text,
  credits double precision,
  usd double precision,
  occurred_at timestamptz,
  note text,
  PRIMARY KEY (entry_id)
);
CREATE INDEX IF NOT EXISTS credit_ledger_system_occurred_at_idx ON us_outbound.credit_ledger (system, occurred_at);
COMMENT ON TABLE us_outbound.credit_ledger IS 'One row per spend of Clay or Apollo credits or Claude dollars (build addition). Budgets are checked against this month''s sum before every batch (SPEC 1.6, 8).';
COMMENT ON COLUMN us_outbound.credit_ledger.system IS 'One of: clay, apollo, claude.';
COMMENT ON COLUMN us_outbound.credit_ledger.credits IS 'Clay or Apollo credits; 0 for Claude.';
COMMENT ON COLUMN us_outbound.credit_ledger.usd IS 'Dollars; the Claude spend (SPEC 1.1 caps it at claude_monthly_cap_usd).';
