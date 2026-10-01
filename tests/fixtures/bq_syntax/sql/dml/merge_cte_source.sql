MERGE `kumosql.kumosql_messy.raw_users` t USING (WITH x AS (SELECT id, state FROM `kumosql.kumosql_messy.raw_users`) SELECT * FROM x) s ON t.id = s.id WHEN MATCHED THEN UPDATE SET state = s.state
