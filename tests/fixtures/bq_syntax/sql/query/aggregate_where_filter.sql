SELECT state, COUNT(* WHERE age > 30) AS a, SUM(DISTINCT age WHERE age > 0) AS b, ARRAY_AGG(first_name IGNORE NULLS WHERE age > 30) AS c FROM `kumosql.kumosql_messy.raw_users` GROUP BY state
