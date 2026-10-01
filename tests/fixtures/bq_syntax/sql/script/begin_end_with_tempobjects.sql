CREATE TEMP TABLE t AS SELECT id FROM `kumosql.kumosql_messy.raw_users`;
SELECT COUNT(*) FROM t;
DROP TABLE t
