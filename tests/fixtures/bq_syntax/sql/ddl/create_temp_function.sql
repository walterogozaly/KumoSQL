CREATE TEMP FUNCTION f(x INT64) RETURNS INT64 AS (x * 2);
SELECT f(age) AS a FROM `kumosql.kumosql_messy.raw_users`
