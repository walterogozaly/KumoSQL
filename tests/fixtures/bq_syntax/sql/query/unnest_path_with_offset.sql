SELECT t.id, elem, off FROM (SELECT id, ['a', 'b'] AS tags FROM `kumosql.kumosql_messy.raw_users`) AS t, t.tags elem WITH OFFSET off
