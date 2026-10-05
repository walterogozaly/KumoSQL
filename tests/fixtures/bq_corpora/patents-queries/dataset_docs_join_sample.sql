-- Adapted from google/patents-public-data: tools/generate_dataset_docs.py, string 0. Python format fields replaced:
-- Replaced '{first_column}' by 'publication_number'
-- Replaced '{first_table}' by 'patents.publications'
-- Replaced '{second_column}' by 'family_id'
-- Replaced '{second_table}' by 'patents.publications'
#standardSQL
SELECT
  COUNT(*) AS cnt,
  COUNT(second.second_column) AS second_cnt,
  ARRAY_AGG(first.publication_number IGNORE NULLS ORDER BY RAND() LIMIT 5) AS sample_value
FROM `patents.publications`AS first
LEFT JOIN (
  SELECT family_id AS second_column, COUNT(*) AS cnt
  FROM `patents.publications`
  GROUP BY 1
) AS second ON first.publication_number = second.second_column
