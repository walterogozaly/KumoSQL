WITH a AS (SELECT id, state FROM `kumosql.kumosql_messy.raw_users`), b AS (SELECT state, COUNT(*) AS n FROM a GROUP BY state) SELECT * FROM b
