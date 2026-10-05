WITH g AS (SELECT x, u.id FROM (UNNEST([1, 2]) AS x JOIN `kumosql.kumosql_messy.raw_users` AS u ON x = u.id)) SELECT * FROM g WHERE id IN (SELECT user_id FROM `kumosql.kumosql_messy.raw_order_items`)
