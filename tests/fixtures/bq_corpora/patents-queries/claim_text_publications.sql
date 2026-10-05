-- Adapted from google/patents-public-data: examples/claim-text/claim_text_extraction.ipynb, cell 10, string 0. Python format fields replaced:
-- Replaced '{}.{}.{}' by 'my_project_name.claims_analysis.claim_text_publications'
#standardSQL

WITH P AS (
  SELECT 
  DISTINCT publication_number, 
  substr(cpc.code, 1,4) cpc4,
  floor(priority_date / 10000) priority_yr
  FROM `patents-public-data.patents.publications`,
  unnest(cpc) as cpc
  WHERE substr(cpc.code, 1,4) = 'G06F'
  AND floor(priority_date / 10000) >= 1995
  AND country_code = 'US'
)

SELECT 
P.publication_number,
P.priority_yr,
P.cpc4,
claims.text
FROM `patents-public-data.patents.publications` as pubs,
UNNEST(claims_localized) as claims
JOIN P 
  ON P.publication_number = pubs.publication_number
JOIN `my_project_name.claims_analysis.claim_text_publications` my_pubs
  ON pubs.publication_number = my_pubs.publication_number
WHERE claims.language = 'en'
