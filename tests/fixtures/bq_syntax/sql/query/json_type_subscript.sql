SELECT j.a AS a, j['b'] AS b, j.arr[0] AS c FROM (SELECT JSON '{"a":1,"b":2,"arr":[5,6]}' AS j)
