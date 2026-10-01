EXPORT DATA OPTIONS (format = 'CLOUD_PUBSUB', uri = 'https://pubsub.googleapis.com/projects/kumosql/topics/t') AS SELECT TO_JSON_STRING(STRUCT(id)) AS message FROM `kumosql.kumosql_messy.raw_users`
