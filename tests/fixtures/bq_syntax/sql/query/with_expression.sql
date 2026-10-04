SELECT id, WITH(a AS age + 1, b AS a * 2, a + b) AS w FROM `kumosql.kumosql_messy.raw_users`
