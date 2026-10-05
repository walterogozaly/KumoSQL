-- Adapted from google/patents-public-data: models/claim_breadth/preprocess.py, string 0. Python format fields replaced:
-- Replaced '{YEAR}' by '2005'
-- Replaced '{CPCS}' by "('A', 'B')"
-- Replaced '{KEEP_PCT}' by '0.2'
  #standardSQL
  with cpc4_quantiles as (
    SELECT
    cpc4, APPROX_QUANTILES(priority_yr, 4) as quantiles
    FROM (
      SELECT DISTINCT
      family_id,
      FLOOR(priority_date / 10000) as priority_yr,
      SUBSTR(cpc.code, 1, 4) cpc4 # Trim CPC Code to first 4 digits.
      FROM `patents-public-data.patents.publications`,
      UNNEST(cpc) as cpc
      WHERE country_code = 'US'
      AND FLOOR(priority_date / 10000) > 2005
      AND substr(cpc.code, 1,1) in ('A', 'B')
    )
    GROUP BY 1
  )


  SELECT DISTINCT
    t1.publication_number,
    t1.fullclaim,
    t1.priority_yr,
    t1.cpc4,
    q.quantiles[offset(2)] as median_priority_yr
  FROM (
    SELECT
      p.publication_number,
      claims.text fullclaim,
      cast(priority_date/10000 as int64) priority_yr,
      substr(any_value(cpc.code),1,4) cpc4
    FROM `patents-public-data.patents.publications` p
    ,UNNEST(claims_localized) claims
    ,UNNEST(cpc) cpc
    WHERE claims.language = 'en'
    AND cpc.inventive = true
    AND cpc.first = true
    AND country_code = 'US'
    AND claims.text is not null
    AND substr(cpc.code, 1,1) in ('A', 'B')
    AND FLOOR(priority_date / 10000) > 2005
    group by 1,2,3
  ) t1
  JOIN cpc4_quantiles q on t1.cpc4 = q.cpc4
  where rand() < 0.2
  
