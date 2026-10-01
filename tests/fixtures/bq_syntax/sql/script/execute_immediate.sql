DECLARE sql_text STRING DEFAULT 'SELECT @a + @b AS s';
EXECUTE IMMEDIATE sql_text INTO result_var USING 1 AS a, 2 AS b
