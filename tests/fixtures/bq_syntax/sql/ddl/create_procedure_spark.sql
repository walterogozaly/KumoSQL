CREATE PROCEDURE `kumosql.kumosql_messy.bq_syntax_spark`() WITH CONNECTION `kumosql.us.some_connection` OPTIONS (engine = 'SPARK', runtime_version = '1.1') LANGUAGE PYTHON AS r"""
print(1)
"""
