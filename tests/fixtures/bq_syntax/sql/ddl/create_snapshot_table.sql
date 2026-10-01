CREATE SNAPSHOT TABLE `kumosql.kumosql_messy.bq_syntax_snap` CLONE `kumosql.kumosql_messy.raw_users` OPTIONS (expiration_timestamp = TIMESTAMP_ADD(CURRENT_TIMESTAMP(), INTERVAL 7 DAY))
