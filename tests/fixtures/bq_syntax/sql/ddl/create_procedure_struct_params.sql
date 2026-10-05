CREATE OR REPLACE PROCEDURE `kumosql.kumosql_messy.bq_syntax_p3`(IN s STRUCT<a INT64, b STRING>, OUT r ARRAY<INT64>)
BEGIN
  SET r = [s.a];
END
