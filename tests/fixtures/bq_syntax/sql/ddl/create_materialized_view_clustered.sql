CREATE MATERIALIZED VIEW `kumosql.kumosql_messy.bq_syntax_mv2` CLUSTER BY state AS SELECT state, COUNT(*) AS n FROM `kumosql.kumosql_messy.raw_users` GROUP BY state
