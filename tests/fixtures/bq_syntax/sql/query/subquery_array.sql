SELECT id, ARRAY(SELECT sale_price FROM `kumosql.kumosql_messy.raw_order_items` WHERE raw_order_items.user_id = raw_users.id ORDER BY id LIMIT 3) AS prices FROM `kumosql.kumosql_messy.raw_users`
