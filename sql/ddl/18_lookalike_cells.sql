-- lookalike_cells: one row per lookalike cell, an industry label x size band of Spill's own
-- HubSpot customers (build addition; Harry, 1 Oct 2026: "I'm happy using Spill companies from
-- HubSpot to help inform lookalike target lists"). Written by the lookalikes job
-- (sources/lookalikes.py), which replaces the whole set each run, so a re-run changes nothing.
-- Company level only: counts, never a company name, a contact or anything about a person.
CREATE TABLE IF NOT EXISTS us_outbound.lookalike_cells (
  cell_id text NOT NULL,
  industry_label text,
  industry_group text,
  size_band text,
  active_customers integer,
  churned_customers integer,
  us_active integer,
  us_churned integer,
  strength double precision,
  computed_at timestamptz,
  run_id text,
  PRIMARY KEY (cell_id)
);
COMMENT ON TABLE us_outbound.lookalike_cells IS 'One row per lookalike cell: Spill''s HubSpot customers counted by website industry label and size band (build addition, Harry 1 Oct 2026; SPEC 5 Signals, SPEC 7 sources). Company-level counts only. Replaced whole by each lookalikes run.';
COMMENT ON COLUMN us_outbound.lookalike_cells.cell_id IS 'industry_label|size_band, with - for a customer whose HubSpot industry maps to no website label.';
COMMENT ON COLUMN us_outbound.lookalike_cells.industry_label IS 'Website industry label (Industries tab); NULL when the HubSpot industry maps to none.';
COMMENT ON COLUMN us_outbound.lookalike_cells.industry_group IS 'The label''s industry group on the Industries tab; the lookalike signal reads cells by group.';
COMMENT ON COLUMN us_outbound.lookalike_cells.size_band IS 'One of: 1-9, 10-49, 50-99, 100-249, 250+, unknown. Spill''s evidence bands (05 §3.1); a prospect at 10-19 or 20-49 reads 10-49.';
COMMENT ON COLUMN us_outbound.lookalike_cells.active_customers IS 'Companies with an active Spill subscription, a won or onboarding Spill 3.0 deal, or lifecycle customer.';
COMMENT ON COLUMN us_outbound.lookalike_cells.churned_customers IS 'Companies whose subscription churned, or whose lifecycle or Spill 3.0 deal is Churned.';
COMMENT ON COLUMN us_outbound.lookalike_cells.us_active IS 'Active customers headquartered in the United States.';
COMMENT ON COLUMN us_outbound.lookalike_cells.us_churned IS 'Churned customers headquartered in the United States.';
COMMENT ON COLUMN us_outbound.lookalike_cells.strength IS 'Weighted count: an active customer 1, a churned one 0.25, a US one double (sources/lookalikes.py).';
COMMENT ON COLUMN us_outbound.lookalike_cells.run_id IS 'The heartbeats run that computed it.';
