CREATE VECTOR INDEX bq_syntax_vi ON `kumosql.kumosql_messy.some_table` (embedding) OPTIONS (index_type = 'IVF', distance_type = 'COSINE')
