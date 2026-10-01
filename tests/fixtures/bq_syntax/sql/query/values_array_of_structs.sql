SELECT * FROM UNNEST([STRUCT(1 AS a, 'x' AS b), (2, 'y')])
