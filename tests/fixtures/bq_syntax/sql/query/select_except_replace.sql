SELECT * EXCEPT (last_name, city) REPLACE (UPPER(first_name) AS first_name) FROM `kumosql.kumosql_messy.raw_users`
