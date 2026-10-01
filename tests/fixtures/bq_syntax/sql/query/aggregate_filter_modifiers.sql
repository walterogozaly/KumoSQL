SELECT ARRAY_AGG(DISTINCT state IGNORE NULLS) AS a, ARRAY_AGG(first_name RESPECT NULLS ORDER BY id) AS b, COUNT(*) AS c, SUM(IF(age > 30, 1, 0)) AS d FROM `kumosql.kumosql_messy.raw_users`
