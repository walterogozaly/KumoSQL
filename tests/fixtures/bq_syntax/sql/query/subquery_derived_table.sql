SELECT t.state, t.n FROM (SELECT state, COUNT(*) AS n FROM `kumosql.kumosql_messy.raw_users` GROUP BY state) AS t WHERE t.n > 1
