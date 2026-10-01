FROM `kumosql.kumosql_messy.raw_users` |> WHERE age > 18 |> AGGREGATE COUNT(*) AS n GROUP BY state |> ORDER BY n DESC |> LIMIT 5
