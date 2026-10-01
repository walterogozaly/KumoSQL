DECLARE n INT64;
SET n = (SELECT COUNT(*) FROM `kumosql.kumosql_messy.raw_users`);
SELECT n
