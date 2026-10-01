FOR r IN (SELECT state FROM `kumosql.kumosql_messy.raw_users` LIMIT 3) DO
  SELECT r.state;
END FOR
