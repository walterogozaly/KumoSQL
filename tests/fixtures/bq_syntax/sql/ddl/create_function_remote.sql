CREATE FUNCTION `kumosql.kumosql_messy.bq_syntax_remote`(x STRING) RETURNS STRING REMOTE WITH CONNECTION `kumosql.us.some_connection` OPTIONS (endpoint = 'https://example.com/fn')
