DECLARE sql_text STRING DEFAULT 'SELECT @a + @b AS s';
DECLARE result_var INT64;
EXECUTE IMMEDIATE sql_text INTO result_var USING 1 AS a, 2 AS b
