DECLARE n INT64 DEFAULT 1;
CASE
  WHEN EXISTS (SELECT 1 FROM `kumosql.kumosql_messy.raw_users`) THEN
    CASE n
      WHEN 1 THEN SELECT 'one';
      ELSE SELECT 'other';
    END CASE;
  ELSE
    SELECT 'none';
END CASE
