FROM `kumosql.kumosql_messy.raw_order_items` |> SELECT id, ROW_NUMBER() OVER (PARTITION BY user_id ORDER BY id) AS rn |> WHERE rn = 1
