CREATE SEARCH INDEX IF NOT EXISTS bq_syntax_si ON `kumosql.kumosql_messy.raw_users` (ALL COLUMNS) OPTIONS (analyzer = 'LOG_ANALYZER')
