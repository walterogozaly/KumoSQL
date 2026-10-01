EXPORT DATA OPTIONS (uri = 'gs://some-bucket/exp/*.csv', format = 'CSV', overwrite = TRUE, header = TRUE, field_delimiter = ';') AS SELECT id, state FROM `kumosql.kumosql_messy.raw_users`
