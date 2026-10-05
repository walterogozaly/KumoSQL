-- Adapted from google/patents-public-data: models/landscaping/expansion.py, string 6. Python format fields replaced:
-- Replaced '{}' by 'patents._tmp_training'
            SELECT DISTINCT
                REGEXP_EXTRACT(LOWER(p.publication_number), r'[a-z]+-(\d+)-[a-z0-9]+') as pub_num,
                p.publication_number,
                p.family_id,
                p.priority_date,
                title.text as title_text,
                abstract.text as abstract_text,
                'unused' as claims_text,
                --SUBSTR(claims.text, 0, 5000) as claims_text,
                'unused' as description_text,
                --SUBSTR(description.text, 0, 5000) as description_text,
                STRING_AGG(citations.publication_number) AS refs,
                STRING_AGG(cpcs.code) AS cpcs
            FROM
              `patents-public-data.patents.publications` p,
              `patents._tmp_training` as tmp,
              UNNEST(p.title_localized) AS title,
              UNNEST(p.abstract_localized) AS abstract,
              UNNEST(p.claims_localized) AS claims,
              UNNEST(p.description_localized) AS description,
              UNNEST(p.title_localized) AS title_lang,
              UNNEST(p.abstract_localized) AS abstract_lang,
              UNNEST(p.claims_localized) AS claims_lang,
              UNNEST(p.description_localized) AS description_lang,
              UNNEST(citation) AS citations,
              UNNEST(cpc) AS cpcs
            WHERE
                p.publication_number = tmp.publication_number
                AND country_code = 'US'
                AND title_lang.language = 'en'
                AND abstract_lang.language = 'en'
                AND claims_lang.language = 'en'
                AND description_lang.language = 'en'
            GROUP BY p.publication_number, p.family_id, p.priority_date, title.text,
                        abstract.text, claims.text, description.text
            ;
        
