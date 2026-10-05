-- Adapted from google/patents-public-data: examples/BERT_For_Patents.ipynb, cell 19, string 0. Python format fields replaced:
-- Replaced "cpc.code = '{}'" by "cpc.code = 'B41J2/165'"
-- Replaced '{}' by 'priming'
  SELECT publication_number, abstract, url
  FROM `patents-public-data.google_patents_research.publications`,
    UNNEST(cpc) as cpc
  WHERE 
    cpc.code = 'B41J2/165' AND
    cpc.first = True AND
    abstract like '% priming %'
  LIMIT 100
