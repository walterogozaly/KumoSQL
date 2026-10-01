CREATE OR REPLACE PROCEDURE `kumosql.kumosql_messy.bq_syntax_p1`(IN a INT64, OUT b INT64, INOUT c STRING)
BEGIN
  SET b = a + 1;
  SET c = CONCAT(c, 'x');
END
