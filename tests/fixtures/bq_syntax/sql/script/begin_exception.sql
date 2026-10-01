BEGIN
  SELECT 1 / 0;
EXCEPTION WHEN ERROR THEN
  SELECT @@error.message, @@error.statement_text;
END
