-- The schema us_outbound, the system of record (SPEC 6), in the Railway Postgres database.
-- SPEC 1.2 (as decided by Harry: BigQuery moved to Railway Postgres): the jobs write only
-- to this schema. ops/ddl.py refuses any statement naming an object outside it.
CREATE SCHEMA IF NOT EXISTS us_outbound;
COMMENT ON SCHEMA us_outbound IS 'US Outbound: the system of record (SPEC 6). Written only by the US Outbound jobs; holds no secrets (SPEC 3).';
