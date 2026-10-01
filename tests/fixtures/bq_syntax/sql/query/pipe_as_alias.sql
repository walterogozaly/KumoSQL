FROM `kumosql.kumosql_messy.raw_users` |> AS u |> WHERE u.age > 1 |> SELECT u.id
