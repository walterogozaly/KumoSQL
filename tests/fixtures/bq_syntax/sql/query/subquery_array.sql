SELECT ARRAY(SELECT sale_price FROM `kumosql.kumosql_messy.raw_order_items` ORDER BY id LIMIT 3) AS prices
