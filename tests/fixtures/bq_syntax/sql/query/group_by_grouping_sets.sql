SELECT state, gender, COUNT(*) AS n, GROUPING(state) AS g FROM `kumosql.kumosql_messy.raw_users` GROUP BY GROUPING SETS ((state), (gender), ())
