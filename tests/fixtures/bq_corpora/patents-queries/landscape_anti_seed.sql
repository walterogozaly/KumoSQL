-- Adapted from google/patents-public-data: models/landscaping/expansion.py, string 5. Python format fields replaced:
-- Replaced '{}' by 'patents.antiseed_tmp'
-- Replaced '{}' by '15000'
            SELECT DISTINCT
              b.publication_number,
              'AntiSeed' AS ExpansionLevel,
              rand() as random_num
            FROM
              `patents-public-data.patents.publications` AS b
            LEFT OUTER JOIN `patents.antiseed_tmp` AS tmp ON b.publication_number = tmp.pub_num
            WHERE
            tmp.pub_num IS NULL
            AND country_code = 'US'
            ORDER BY random_num
            LIMIT 15000
            # TODO: randomize results
            ;
        
