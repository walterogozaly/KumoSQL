-- Adapted from google/patents-public-data: models/landscaping/expansion.py, string 0. Python format fields replaced:
-- Replaced '{}' by "'8000000', '10722718'"
        SELECT
          b.publication_number,
          'Seed' as ExpansionLevel,
          STRING_AGG(citations.publication_number) AS refs,
          STRING_AGG(cpcs.code) AS cpc_codes
        FROM
          `patents-public-data.patents.publications` AS b,
          UNNEST(citation) AS citations,
          UNNEST(cpc) AS cpcs
        WHERE
        REGEXP_EXTRACT(b.publication_number, r'\w+-(\d+)-\w+') IN
        (
        '8000000', '10722718'
        )
        AND citations.publication_number != ''
        AND cpcs.code != ''
        GROUP BY b.publication_number
        ;
        
