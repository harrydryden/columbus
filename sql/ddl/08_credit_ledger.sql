-- credit_ledger: one row per spend of Clay or Apollo credits or Claude dollars (build).
-- SPEC 8: "Before each batch, compute the month's remaining Clay budget: clay_monthly_credits
-- minus the credits recorded this month." The same ledger serves Apollo and the Claude
-- cap (SPEC 1.1, 1.6). Months are UTC calendar months.
CREATE TABLE IF NOT EXISTS `{project}.us_outbound.credit_ledger` (
  entry_id STRING NOT NULL,
  system STRING OPTIONS (description = "One of: clay, apollo, claude."),
  job STRING,
  run_id STRING,
  account_id STRING,
  credits FLOAT64 OPTIONS (description = "Clay or Apollo credits; 0 for Claude."),
  usd FLOAT64 OPTIONS (description = "Dollars; the Claude spend (SPEC 1.1 caps it at claude_monthly_cap_usd)."),
  occurred_at TIMESTAMP,
  note STRING
)
PARTITION BY DATE(occurred_at)
CLUSTER BY system, account_id
OPTIONS (
  description = "One row per spend of Clay or Apollo credits or Claude dollars (build addition). Budgets are checked against this month's sum before every batch (SPEC 1.6, 8)."
);
