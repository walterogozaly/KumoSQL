LOAD DATA OVERWRITE `kumosql.kumosql_messy.bq_syntax_load` (id INT64, name STRING) PARTITION BY DATE(ts) FROM FILES (format = 'PARQUET', uris = ['gs://some-bucket/in/*.parquet'])
