SELECT * FROM `kumosql.kumosql_messy.raw_users` WHERE SEARCH((first_name, last_name), 'alice', analyzer => 'LOG_ANALYZER')
