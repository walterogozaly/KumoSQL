FROM `kumosql.kumosql_messy.raw_users` |> EXTEND age + 1 AS next_age |> SET state = UPPER(state) |> DROP city |> RENAME country AS ctry
