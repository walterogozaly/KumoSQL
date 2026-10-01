CREATE TEMP FUNCTION f(x FLOAT64) RETURNS FLOAT64 LANGUAGE js AS r"""
  return x * 2;
""";
SELECT f(sale_price) AS d FROM `kumosql.kumosql_messy.raw_order_items`
