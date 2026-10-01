CREATE FUNCTION `kumosql.kumosql_messy.bq_syntax_py`(x INT64) RETURNS INT64 LANGUAGE python OPTIONS (entry_point = 'f', runtime_version = 'python-3.11') AS r"""
def f(x):
  return x + 1
"""
