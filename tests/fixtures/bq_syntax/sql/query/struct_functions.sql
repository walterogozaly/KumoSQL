SELECT s.a, s.b.c, STRUCT(1 AS a, 'x' AS b) AS t, (1, 'x') AS tuple, STRUCT<a INT64, b STRING>(1, 'x') AS typed FROM (SELECT STRUCT(1 AS a, STRUCT(2 AS c) AS b) AS s)
