SELECT u.id, (SELECT MAX(i.sale_price) FROM `kumosql.kumosql_messy.raw_order_items` AS i WHERE i.user_id = u.id) AS mx FROM `kumosql.kumosql_messy.raw_users` AS u
