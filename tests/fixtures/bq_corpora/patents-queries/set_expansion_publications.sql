-- Adapted from google/patents-public-data: examples/patent_set_expansion.ipynb, cell 6, string 0. Three string pieces joined:
-- Replaced the search term and the count between its three string pieces by the notebook's own defaults, 'neural network' and 250
  WITH 
  pubs as (
    SELECT DISTINCT 
      pub.publication_number
    FROM `patents-public-data.patents.publications` pub
      INNER JOIN `patents-public-data.google_patents_research.publications` gpr ON
        pub.publication_number = gpr.publication_number
    WHERE 
      pub.country_code = 'US' 
      AND "neural network" IN UNNEST(gpr.top_terms)
      AND pub.grant_date >= 20050101 AND pub.grant_date < 20100101
  )

  SELECT
    publication_number, url, 
    embedding_v1
  FROM 
    `patents-public-data.google_patents_research.publications`
  WHERE
    publication_number in (SELECT publication_number from pubs)
    AND RAND() <= 250/(SELECT COUNT(*) FROM pubs)
  
