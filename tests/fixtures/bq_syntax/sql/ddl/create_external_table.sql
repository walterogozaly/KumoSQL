CREATE EXTERNAL TABLE `kumosql.kumosql_messy.bq_syntax_ext` OPTIONS (format = 'CSV', uris = ['gs://some-bucket/path/*.csv'], skip_leading_rows = 1)
