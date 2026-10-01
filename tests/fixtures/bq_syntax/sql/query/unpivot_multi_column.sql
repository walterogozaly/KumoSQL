SELECT * FROM (SELECT 1 AS id, 10 AS a1, 20 AS b1, 30 AS a2, 40 AS b2) UNPIVOT ((a, b) FOR n IN ((a1, b1) AS 'one', (a2, b2) AS 'two'))
