SELECT x, u.id FROM (UNNEST([1, 2, 3]) AS x JOIN `kumosql.kumosql_messy.raw_users` AS u ON x = u.id)
