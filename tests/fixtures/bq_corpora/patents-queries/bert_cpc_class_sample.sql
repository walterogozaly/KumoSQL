-- Adapted from google/patents-public-data: examples/BERT_For_Patents.ipynb, cell 23, string 0. Python format fields replaced:
-- Replaced '{}' by '200'
  #standardSQL
  SELECT DISTINCT
    substr(cpc.code, 0, 1) as cpc_class,
    res.abstract
  FROM `patents-public-data.google_patents_research.publications` res,
    UNNEST(cpc) as cpc
    INNER JOIN `patents-public-data.patents.publications` pub ON 
      res.publication_number = pub.publication_number
  WHERE 
    pub.publication_date >= 20000101 AND
    res.country = 'United States' AND
    cpc.first = True AND
    RAND() < 0.1
  LIMIT 200
