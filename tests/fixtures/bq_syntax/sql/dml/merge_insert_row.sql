MERGE INTO `kumosql.kumosql_messy.raw_users` t USING `kumosql.kumosql_messy.raw_users` s ON FALSE WHEN NOT MATCHED THEN INSERT ROW
