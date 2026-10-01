CREATE OR REPLACE TABLE `kumosql.kumosql_messy.bq_syntax_t5` PARTITION BY DATE(created_at) CLUSTER BY state AS SELECT id, state, created_at FROM `kumosql.kumosql_messy.raw_users`
