SELECT id, state FROM `kumosql.kumosql_messy.raw_users` QUALIFY ROW_NUMBER() OVER (PARTITION BY state ORDER BY created_at DESC) = 1
