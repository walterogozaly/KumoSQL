SELECT p.k, p.v FROM UNNEST([STRUCT('a' AS k, 1 AS v), STRUCT('b' AS k, 2 AS v)]) AS p
