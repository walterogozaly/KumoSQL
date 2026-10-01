CREATE OR REPLACE TABLE FUNCTION `kumosql.kumosql_messy.bq_syntax_tvf`(min_age INT64) AS SELECT id, state FROM `kumosql.kumosql_messy.raw_users` WHERE age > min_age
