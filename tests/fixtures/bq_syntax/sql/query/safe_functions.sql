SELECT SAFE_DIVIDE(age, 0) AS a, SAFE.LOG(age) AS b, SAFE_CAST(state AS INT64) AS c, SAFE.PARSE_DATE('%Y', 'x') AS d, SAFE_ADD(age, 1) AS e FROM `kumosql.kumosql_messy.raw_users`
