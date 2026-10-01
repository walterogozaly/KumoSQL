CREATE TEMP FUNCTION f(x STRING) RETURNS STRING LANGUAGE js OPTIONS (library = ['gs://some-bucket/lib.js']) AS "return lib(x);";
SELECT f(state) AS d FROM `kumosql.kumosql_messy.raw_users`
