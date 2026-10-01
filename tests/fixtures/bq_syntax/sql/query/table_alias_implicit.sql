SELECT u.id FROM `kumosql.kumosql_messy.raw_users` u WHERE u.age BETWEEN 20 AND 30 AND u.state NOT IN ('CA', 'NY') AND u.city LIKE 'S%' AND u.first_name IS NOT NULL
