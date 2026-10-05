-- Adapted from google/patents-public-data: examples/BERT_For_Patents.ipynb, cell 14, string 0. Python format fields replaced:
-- Replaced '{}' by "('US-8000000-B2', 'US-2007186831-A1', 'US-2009030261-A1')"
  SELECT publication_number, abstract, url
  FROM `patents-public-data.google_patents_research.publications` 
  WHERE publication_number in ('US-8000000-B2', 'US-2007186831-A1', 'US-2009030261-A1')
