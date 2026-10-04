SELECT id, first_name LIKE ALL UNNEST(['a%', '%b']) AS a, first_name LIKE ANY UNNEST(['a%']) AS b, first_name NOT LIKE SOME UNNEST(['c%']) AS c FROM `kumosql.kumosql_messy.raw_users`
