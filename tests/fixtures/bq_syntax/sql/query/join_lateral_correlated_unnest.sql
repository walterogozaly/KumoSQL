SELECT u.id, tag FROM (SELECT 1 AS id, ['a','b'] AS tags) AS u CROSS JOIN UNNEST(u.tags) AS tag
