SELECT id FROM `kumosql.kumosql_messy.raw_users` AS u TABLESAMPLE SYSTEM (50 PERCENT) JOIN `kumosql.kumosql_messy.raw_order_items` AS i ON u.id = i.user_id
