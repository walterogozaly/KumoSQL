FROM `kumosql.kumosql_messy.raw_order_items` |> EXTEND SUM(sale_price) OVER (PARTITION BY user_id) AS total |> SELECT id, total
