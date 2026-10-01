SELECT id, x FROM (SELECT id, [1, 2] AS xs FROM `kumosql.kumosql_messy.raw_users` LIMIT 2), UNNEST(xs) AS x
