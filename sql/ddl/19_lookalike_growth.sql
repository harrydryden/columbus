-- lookalike_growth: Spill's own HubSpot customers counted by industry group and 12-month headcount growth
-- band (build addition; Harry, 5 Oct 2026: "Sharpen up lookalike for scoring to include industry, size and
-- growth rate"). Written by the lookalikes job (sources/lookalikes.py) from Apollo's organization search by
-- headcount growth range; it replaces the whole set when every customer was searched, so a part-read never
-- skews it. Counts only: never a customer's domain or name, a contact or anything about a person.
CREATE TABLE IF NOT EXISTS us_outbound.lookalike_growth (
  cell_id text NOT NULL,
  industry_group text,
  growth_band text,
  active_customers integer,
  churned_customers integer,
  strength double precision,
  computed_at timestamptz,
  run_id text,
  PRIMARY KEY (cell_id)
);
COMMENT ON TABLE us_outbound.lookalike_growth IS 'One row per industry group and growth band: Spill''s HubSpot customers counted by 12-month headcount growth from Apollo (build addition, Harry 5 Oct 2026; SPEC 5 Signals, SPEC 7 sources). Company-level counts only. Replaced whole by a lookalikes run that searched every customer.';
COMMENT ON COLUMN us_outbound.lookalike_growth.cell_id IS 'industry_group|growth_band, with - for a customer whose HubSpot industry maps to no website label.';
COMMENT ON COLUMN us_outbound.lookalike_growth.industry_group IS 'The industry group on the Industries tab (as lookalike_cells); NULL when the HubSpot industry maps to no label.';
COMMENT ON COLUMN us_outbound.lookalike_growth.growth_band IS 'One of: shrinking, flat, growing, fast, unknown. Headcount growth over 12 months: under 0%, 0-10%, 10-30%, 30% or more; unknown when no band''s search returned the customer.';
COMMENT ON COLUMN us_outbound.lookalike_growth.active_customers IS 'Active customers in this group and band (as lookalike_cells.active_customers).';
COMMENT ON COLUMN us_outbound.lookalike_growth.churned_customers IS 'Churned customers in this group and band (as lookalike_cells.churned_customers).';
COMMENT ON COLUMN us_outbound.lookalike_growth.strength IS 'Weighted count: an active customer 1, a churned one 0.25, a US one double (sources/lookalikes.py).';
COMMENT ON COLUMN us_outbound.lookalike_growth.computed_at IS 'When the lookalikes run that counted it started.';
COMMENT ON COLUMN us_outbound.lookalike_growth.run_id IS 'The heartbeats run that computed it.';
