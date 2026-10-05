-- Adapted from google/patents-public-data: models/claim_breadth/preprocess_test.py, string 0. Python format fields replaced:
-- Replaced '{half_max}' by '50'
    #standardSQL
    with fake_applications as (
      SELECT
      'US-1234567-A1' as publication_number,
      substr(claims.text, 0, 2000) as fullclaim,
      2000 as priority_yr,
      'C08F' as cpc4,
      2003 as median_priority_yr
      FROM `patents-public-data.patents.publications` p
      ,UNNEST(claims_localized) claims
      WHERE claims.language = 'en'
      AND country_code = 'US'
      AND claims.text is not null
      AND FLOOR(priority_date / 10000) > 2005
      limit 50
    )

    , fake_issued as (
      SELECT
      'US-1234567-B2' as publication_number,
      substr(claims.text, 0, 2000) as fullclaim,
      2012 as priority_yr,
      'C08F' as cpc4,
      2003 as median_priority_yr
      FROM `patents-public-data.patents.publications` p
      ,UNNEST(claims_localized) claims
      WHERE claims.language = 'en'
      AND country_code = 'US'
      AND claims.text is not null
      AND FLOOR(priority_date / 10000) > 2005
      limit 50
    )

    select * from fake_applications
    union all
    select * from fake_issued
    
