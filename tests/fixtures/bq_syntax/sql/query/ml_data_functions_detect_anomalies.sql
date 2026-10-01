SELECT * FROM ML.DETECT_ANOMALIES(MODEL `kumosql.kumosql_messy.some_model`, STRUCT(0.01 AS contamination), TABLE `kumosql.kumosql_messy.raw_users`)
