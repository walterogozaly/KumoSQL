UPDATE `kumosql.kumosql_messy.raw_users` SET state = UPPER(state), age = age + 1 WHERE state IS NOT NULL
