INSERT INTO `kumosql.kumosql_messy.raw_users` (id) WITH a AS (SELECT id FROM `kumosql.kumosql_messy.raw_users`) SELECT id FROM a
