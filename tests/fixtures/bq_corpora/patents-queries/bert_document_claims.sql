-- Adapted from google/patents-public-data: examples/Document_representation_from_BERT.ipynb, cell 12, string 0. Python format fields replaced:
-- Replaced '{}' by the JavaScript string that cell 12 defines first
-- Replaced '{}' by "('US-8000000-B2', 'US-2007186831-A1', 'US-2009030261-A1', 'US-10722718-B2')"
  #standardSQL
  CREATE TEMPORARY FUNCTION breakout_claims(text STRING) RETURNS ARRAY<STRING> 
  LANGUAGE js AS """
    // Regex to find the separations of the claims data
  var pattern = new RegExp(/[.][\\s]+[0-9]+[\\s]*[.]/, 'g');
  if (pattern.test(text)) {
    return text.split(pattern);
  }
  """; 

  SELECT 
    pubs.publication_number, 
    title.text as title, 
    breakout_claims(claims.text) as claims
  FROM `patents-public-data.patents.publications` as pubs,
    UNNEST(claims_localized) as claims,
    UNNEST(title_localized) as title
  WHERE
    publication_number in ('US-8000000-B2', 'US-2007186831-A1', 'US-2009030261-A1', 'US-10722718-B2')
