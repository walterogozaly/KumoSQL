SELECT id FROM `kumosql.kumosql_messy.raw_users` WHERE state IN UNNEST(['CA', 'NY', 'TX'])
