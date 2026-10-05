-- Adapted from google/patents-public-data: models/landscaping/expansion.py, string 4. Python format fields replaced:
-- Replaced '{}' by "'G06F', 'H04L'"
-- Replaced '{}' by 'patents._l1_tmp'
            SELECT DISTINCT publication_number, ExpansionLevel, refs
            FROM
            (
            SELECT
              b.publication_number,
              'L1' as ExpansionLevel,
              STRING_AGG(citations.publication_number) AS refs
            FROM
              `patents-public-data.patents.publications` AS b,
              UNNEST(citation) AS citations,
              UNNEST(cpc) AS cpcs
            WHERE
            (
                cpcs.code IN
                (
                'G06F', 'H04L'
                )
            )
            AND citations.publication_number != ''
            AND country_code IN ('US')
            GROUP BY b.publication_number

            UNION ALL

            SELECT
              b.publication_number,
              'L1' as ExpansionLevel,
              STRING_AGG(citations.publication_number) AS refs
            FROM
              `patents-public-data.patents.publications` AS b,
              `patents._l1_tmp` as tmp,
              UNNEST(citation) AS citations
            WHERE
            (
                b.publication_number = tmp.pub_num
            )
            AND citations.publication_number != ''
            GROUP BY b.publication_number
            )
            ;
        
