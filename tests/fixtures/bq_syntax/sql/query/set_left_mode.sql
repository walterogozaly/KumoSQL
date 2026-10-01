SELECT id, state FROM `kumosql.kumosql_messy.raw_users` LEFT UNION ALL BY NAME SELECT state, id, city FROM `kumosql.kumosql_messy.raw_users`
