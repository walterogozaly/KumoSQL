DELETE FROM `kumosql.kumosql_messy.raw_users` AS u WHERE NOT EXISTS (SELECT 1 FROM `kumosql.kumosql_messy.raw_order_items` AS i WHERE i.user_id = u.id)
