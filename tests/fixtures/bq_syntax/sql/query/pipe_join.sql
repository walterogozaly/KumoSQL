FROM `kumosql.kumosql_messy.raw_users` AS u |> JOIN `kumosql.kumosql_messy.raw_order_items` AS i ON u.id = i.user_id |> SELECT u.id, i.sale_price
