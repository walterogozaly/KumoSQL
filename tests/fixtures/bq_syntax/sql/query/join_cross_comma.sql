SELECT u.id, i.id AS iid FROM `kumosql.kumosql_messy.raw_users` AS u, `kumosql.kumosql_messy.raw_order_items` AS i WHERE u.id = i.user_id
