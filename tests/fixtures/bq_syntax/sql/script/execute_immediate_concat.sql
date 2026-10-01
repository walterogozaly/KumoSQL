DECLARE t STRING DEFAULT 'raw_users';
EXECUTE IMMEDIATE FORMAT('SELECT COUNT(*) FROM `kumosql.kumosql_messy.%s`', t)
