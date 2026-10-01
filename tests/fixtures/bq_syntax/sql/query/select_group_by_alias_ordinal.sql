SELECT state, COUNT(*) AS n FROM `kumosql.kumosql_messy.raw_users` GROUP BY 1 HAVING n > 1 ORDER BY n DESC
