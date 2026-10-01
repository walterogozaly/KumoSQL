SELECT DATE '2024-01-02' AS d, TIME '10:00:00' AS t, DATETIME '2024-01-02 10:00:00' AS dt, TIMESTAMP '2024-01-02 10:00:00+00' AS ts, NUMERIC '1.5' AS n, BIGNUMERIC '2.5' AS bn, JSON '{"a":1}' AS j, b'abc' AS bytes_lit, r'\d+' AS raw_str, """multi
line""" AS ml, 0x1F AS hexlit, 1e3 AS sci, INTERVAL 1 DAY AS iv, RANGE<DATE> '[2024-01-01, 2024-02-01)' AS rg
