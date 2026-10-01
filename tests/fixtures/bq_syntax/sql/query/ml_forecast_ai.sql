SELECT * FROM AI.FORECAST(TABLE `kumosql.kumosql_messy.tbl_daily_sales`, data_col => 'revenue', timestamp_col => 'day', horizon => 3)
