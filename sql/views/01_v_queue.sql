-- v_queue: the accounts waiting to be enrolled, in enrollment order (SPEC 9).
-- SPEC 9, daily enrollment: "Of that number, control_share comes from the Control tier.
-- The rest comes from Priority, then Standard, ordered by score, then by size band
-- (20 to 99 first), then by industry priority."
--   in the queue:  status verified or queued, tier Priority, Standard or Control.
--   left out:      a domain suppressed now (directly or through an alias), a partner
--                  domain (never prospected, SPEC 9), or an industry switched off on the
--                  Industries tab. An industry label not on the tab is kept.
--   verified:      Clay has checked it; only verified accounts may be emailed (SPEC 2:
--                  every account passes through Clay before it can be emailed).
-- Order: tier_rank, then rank_in_tier (score desc with no score last, size_band_rank,
-- industry_priority, first_seen, account_id). A view's ORDER BY is not kept by queries on
-- it, so readers order by tier_rank, rank_in_tier themselves.
-- Settings values are sheet text: priority counts only when it is a whole number.
CREATE OR REPLACE VIEW us_outbound.v_queue AS
WITH industries_raw AS (
  SELECT DISTINCT ON ("key")
    "key" AS industry,
    trim("values" ->> 'priority') AS priority,
    lower(trim("values" ->> 'active')) IN ('yes', 'true') AS active
  FROM us_outbound.settings
  WHERE tab = 'Industries' AND effective_to IS NULL
  ORDER BY "key", effective_from DESC
),
industries AS (
  SELECT
    industry,
    CASE WHEN priority ~ '^[+-]?[0-9]{1,9}$' THEN priority::integer END AS priority,
    active
  FROM industries_raw
),
active_suppression AS (
  -- Domain rows only: a row with an email hash suppresses just that email (suppression.py).
  SELECT lower(trim(domain)) AS domain
  FROM us_outbound.suppression
  WHERE domain IS NOT NULL
    AND email_sha256 IS NULL
    AND (expires_at IS NULL OR expires_at > now())
),
suppressed_domains AS (
  SELECT domain FROM active_suppression
  UNION
  SELECT lower(al.root_domain)
  FROM active_suppression AS s
  JOIN us_outbound.domain_aliases AS al
    ON lower(al.alias) = s.domain
  WHERE al.root_domain IS NOT NULL
),
queue AS (
  SELECT
    a.account_id,
    a.domain,
    a.clean_name,
    a.tier,
    a.score,
    a.size_band,
    a.industry,
    a.industry_group,
    a.hq_city,
    a.hq_state,
    a.angle,
    a.tier_reason,
    a.sender,
    a.status,
    a.source,
    a.employees,
    a.apollo_org_id,
    a.clay_checked_at,
    a.first_seen,
    a.last_scored,
    a.status = 'verified' AS verified,
    CASE a.tier WHEN 'Priority' THEN 1 WHEN 'Standard' THEN 2 WHEN 'Control' THEN 3 END AS tier_rank,
    CASE a.size_band
      WHEN '20-49' THEN 1
      WHEN '50-99' THEN 1
      WHEN '100-249' THEN 2
      WHEN '10-19' THEN 3
      ELSE 4
    END AS size_band_rank,
    COALESCE(i.priority, 99) AS industry_priority
  FROM us_outbound.accounts AS a
  LEFT JOIN industries AS i
    ON i.industry = a.industry
  WHERE a.status IN ('verified', 'queued')
    AND a.tier IN ('Priority', 'Standard', 'Control')
    AND COALESCE(i.active, TRUE)
    AND NOT EXISTS (
      SELECT 1 FROM suppressed_domains AS sd WHERE sd.domain = lower(a.domain)
    )
    AND NOT EXISTS (
      SELECT 1 FROM us_outbound.partners AS p WHERE lower(p.domain) = lower(a.domain)
    )
)
SELECT
  q.*,
  row_number() OVER (
    PARTITION BY q.tier
    ORDER BY q.score DESC NULLS LAST, q.size_band_rank, q.industry_priority, q.first_seen, q.account_id
  ) AS rank_in_tier
FROM queue AS q
ORDER BY q.tier_rank, rank_in_tier;
COMMENT ON VIEW us_outbound.v_queue IS 'Accounts waiting to be enrolled (status verified or queued; tier Priority, Standard or Control; not suppressed), in enrollment order: tier, score, size band (20-99 first), industry priority (SPEC 9).';
