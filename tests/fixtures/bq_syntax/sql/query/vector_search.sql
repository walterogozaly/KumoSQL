SELECT * FROM VECTOR_SEARCH(TABLE `kumosql.kumosql_messy.raw_users`, 'age', (SELECT [1.0] AS q), top_k => 1)
