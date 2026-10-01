SELECT id, state FROM `kumosql.kumosql_messy.raw_users` UNION ALL CORRESPONDING SELECT state, id FROM `kumosql.kumosql_messy.raw_users`
