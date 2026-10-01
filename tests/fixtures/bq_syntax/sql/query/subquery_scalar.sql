SELECT id, (SELECT MAX(sale_price) FROM `kumosql.kumosql_messy.raw_order_items` WHERE raw_order_items.user_id = raw_users.id) AS mx FROM `kumosql.kumosql_messy.raw_users`
