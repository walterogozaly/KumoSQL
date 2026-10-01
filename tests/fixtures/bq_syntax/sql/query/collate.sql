SELECT state FROM `kumosql.kumosql_messy.raw_users` WHERE COLLATE(state, 'und:ci') = 'ca' ORDER BY state COLLATE 'und:ci'
