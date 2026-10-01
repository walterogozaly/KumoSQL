SELECT WITH DIFFERENTIAL_PRIVACY OPTIONS (epsilon = 10, delta = 0.01, max_groups_contributed = 1, privacy_unit_column = id) state, COUNT(*) AS n FROM `kumosql.kumosql_messy.raw_users` GROUP BY state
