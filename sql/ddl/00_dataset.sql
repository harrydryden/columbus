-- The dataset us_outbound, the system of record (SPEC 6).
-- SPEC 6: "Use the same location as the project's existing datasets and a dedicated
-- service account." The location is a placeholder (EU for spill-warehouse-test, see
-- docs/phase0-facts.md); the service account is IAM, not DDL.
-- SPEC 1.2: the jobs write only to this dataset. ops/ddl.py refuses any other.
CREATE SCHEMA IF NOT EXISTS `{project}.us_outbound`
OPTIONS (
  location = "{location}",
  description = "US Outbound: the system of record (SPEC 6). Written only by the US Outbound jobs; holds no secrets (SPEC 3)."
);
