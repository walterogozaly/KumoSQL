SELECT state, gender, COUNT(*) AS n FROM `kumosql.kumosql_messy.raw_users` GROUP BY CUBE (state, gender)
