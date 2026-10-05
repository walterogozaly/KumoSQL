-- Adapted from google/patents-public-data: models/landscaping/expansion.py, string 1. Python format fields replaced:
            SELECT
              COUNT(publication_number) AS num_patents
            FROM
              `patents-public-data.patents.publications` AS b
            WHERE
              country_code = 'US'
        
