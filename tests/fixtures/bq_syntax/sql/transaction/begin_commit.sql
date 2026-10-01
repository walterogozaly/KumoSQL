BEGIN TRANSACTION;
UPDATE `kumosql.kumosql_messy.raw_users` SET age = age + 1 WHERE id = 1;
COMMIT TRANSACTION
