-- Adapted from google/patents-public-data: models/landscaping/expansion.py, string 2. Python format fields replaced:
-- Replaced '{}' by '1=1'
            SELECT
              cpcs.code,
              COUNT(cpcs.code) AS cpc_count
            FROM
              `patents-public-data.patents.publications` AS b,
              UNNEST(cpc) AS cpcs
            WHERE
            1=1
            AND cpcs.code != ''
            AND country_code = 'US'
            GROUP BY cpcs.code
            ORDER BY cpc_count DESC;
            
