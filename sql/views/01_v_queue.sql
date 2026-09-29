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
-- Order: tier_rank, then rank_in_tier (score desc, size_band_rank, industry_priority,
-- first_seen, account_id). A view's ORDER BY is not kept by queries on it, so readers
-- order by tier_rank, rank_in_tier themselves.
CREATE OR REPLACE VIEW `{project}.us_outbound.v_queue`
OPTIONS (
  description = "Accounts waiting to be enrolled (status verified or queued; tier Priority, Standard or Control; not suppressed), in enrollment order: tier, score, size band (20-99 first), industry priority (SPEC 9)."
)
AS
WITH industries AS (
  SELECT
    `key` AS industry,
    SAFE_CAST(TRIM(JSON_VALUE(`values`, '$.priority')) AS INT64) AS priority,
    LOWER(TRIM(JSON_VALUE(`values`, '$.active'))) IN ('yes', 'true') AS active
  FROM `{project}.us_outbound.settings`
  WHERE tab = 'Industries' AND effective_to IS NULL
  QUALIFY ROW_NUMBER() OVER (PARTITION BY `key` ORDER BY effective_from DESC) = 1
),
active_suppression AS (
  SELECT LOWER(TRIM(domain)) AS domain
  FROM `{project}.us_outbound.suppression`
  WHERE domain IS NOT NULL
    AND (expires_at IS NULL OR expires_at > CURRENT_TIMESTAMP())
),
suppressed_domains AS (
  SELECT domain FROM active_suppression
  UNION DISTINCT
  SELECT LOWER(al.root_domain)
  FROM active_suppression AS s
  JOIN `{project}.us_outbound.domain_aliases` AS al
    ON LOWER(al.alias) = s.domain
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
  FROM `{project}.us_outbound.accounts` AS a
  LEFT JOIN industries AS i
    ON i.industry = a.industry
  WHERE a.status IN ('verified', 'queued')
    AND a.tier IN ('Priority', 'Standard', 'Control')
    AND COALESCE(i.active, TRUE)
    AND NOT EXISTS (
      SELECT 1 FROM suppressed_domains AS sd WHERE sd.domain = LOWER(a.domain)
    )
    AND NOT EXISTS (
      SELECT 1 FROM `{project}.us_outbound.partners` AS p WHERE LOWER(p.domain) = LOWER(a.domain)
    )
)
SELECT
  *,
  ROW_NUMBER() OVER (
    PARTITION BY tier
    ORDER BY score DESC, size_band_rank, industry_priority, first_seen, account_id
  ) AS rank_in_tier
FROM queue
ORDER BY tier_rank, rank_in_tier;
