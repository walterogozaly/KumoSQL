SELECT id, first_name AS name, age + 1 AS next_age FROM `kumosql.kumosql_messy.raw_users` WHERE age > 18 ORDER BY id LIMIT 10 OFFSET 2
