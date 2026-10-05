-- Adapted from google/patents-public-data: models/landscaping/expansion.py, string 3. Python format fields replaced:
-- Replaced '{}' by 'patents._l2_tmp'
            SELECT
              b.publication_number,
              'L2' AS ExpansionLevel,
              STRING_AGG(citations.publication_number) AS refs
            FROM
              `patents-public-data.patents.publications` AS b,
              `patents._l2_tmp` as tmp,
              UNNEST(citation) AS citations
            WHERE
            (
                b.publication_number = tmp.pub_num
            )
            AND citations.publication_number != ''
            GROUP BY b.publication_number
            ;
        
