-- Adapted from google/patents-public-data: examples/claim-text/claim_text_extraction.ipynb, cell 26, string 0. Python format fields replaced:
-- Replaced '%s.%s.%s' by 'my_project_name.claims_analysis.20k_G06F_pubs_after_1994_split_first_claim'
#standardSQL
with elements as (
  SELECT 
  publication_number,
  priority_yr,
  SPLIT(first_claim, ';') element
  FROM `my_project_name.claims_analysis.20k_G06F_pubs_after_1994_split_first_claim`
)

SELECT 
  priority_yr, 
  avg(num_elements) avg_element_cnt
FROM (
  SELECT
  publication_number,
  priority_yr,
  count(*) as num_elements
  from elements, unnest(element)
  group by 1,2
)
GROUP BY 1
ORDER BY 1
