ALTER TABLE `kumosql.kumosql_messy.bq_syntax_t1` ADD PRIMARY KEY (id) NOT ENFORCED, ADD CONSTRAINT fk FOREIGN KEY (id) REFERENCES `kumosql.kumosql_messy.raw_users` (id) NOT ENFORCED
