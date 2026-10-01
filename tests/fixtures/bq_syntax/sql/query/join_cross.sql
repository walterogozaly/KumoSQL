SELECT a.id, b.id AS bid FROM (SELECT id FROM `kumosql.kumosql_messy.raw_users` LIMIT 3) AS a CROSS JOIN (SELECT id FROM `kumosql.kumosql_messy.raw_order_items` LIMIT 3) AS b
