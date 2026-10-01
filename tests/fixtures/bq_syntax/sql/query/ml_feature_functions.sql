SELECT ML.STANDARD_SCALER(age) OVER () AS s, ML.MIN_MAX_SCALER(age) OVER () AS m, ML.BUCKETIZE(age, [10, 20, 30]) AS b FROM `kumosql.kumosql_messy.raw_users`
