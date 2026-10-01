UPDATE `kumosql.kumosql_messy.raw_users` AS u SET u.state = i.status FROM `kumosql.kumosql_messy.raw_order_items` AS i WHERE u.id = i.user_id
