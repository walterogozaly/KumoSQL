SELECT * FROM ML.GENERATE_TEXT(MODEL `kumosql.kumosql_messy.some_remote_model`, (SELECT 'hi' AS prompt))
