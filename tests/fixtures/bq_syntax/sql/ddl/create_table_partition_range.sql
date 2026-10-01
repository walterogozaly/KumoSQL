CREATE TABLE `kumosql.kumosql_messy.bq_syntax_t3` (id INT64) PARTITION BY RANGE_BUCKET(id, GENERATE_ARRAY(0, 100, 10))
