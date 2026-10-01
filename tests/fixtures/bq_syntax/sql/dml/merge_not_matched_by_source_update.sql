MERGE `kumosql.kumosql_messy.raw_users` t USING (SELECT 1 AS id) s ON t.id = s.id WHEN NOT MATCHED BY SOURCE AND t.age > 100 THEN UPDATE SET t.age = 100
