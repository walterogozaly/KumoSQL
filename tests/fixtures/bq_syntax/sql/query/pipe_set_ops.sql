FROM `kumosql.kumosql_messy.raw_users` |> SELECT id |> UNION ALL (SELECT id FROM `kumosql.kumosql_messy.raw_order_items`), (SELECT user_id FROM `kumosql.kumosql_messy.raw_order_items`)
