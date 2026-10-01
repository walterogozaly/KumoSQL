CREATE OR REPLACE ROW ACCESS POLICY bq_syntax_rap ON `kumosql.kumosql_messy.raw_users` GRANT TO ('user:someone@example.com') FILTER USING (state = 'CA')
