FROM `kumosql.kumosql_messy.raw_users` |> AGGREGATE COUNT(*) AS n GROUP AND ORDER BY state DESC
