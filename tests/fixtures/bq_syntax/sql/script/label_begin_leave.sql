blk: BEGIN
  SELECT 1;
  IF (SELECT COUNT(*) FROM `kumosql.kumosql_messy.raw_users`) > 0 THEN
    LEAVE blk;
  END IF;
  SELECT 2;
END blk
