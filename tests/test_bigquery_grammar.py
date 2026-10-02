"""BigQuery statements sqlglot rejects that kumosql.bigquery_syntax reads (TABLE arguments are in test_table_function_arguments.py)."""

import pytest
import sqlglot
from sqlglot import exp

import kumosql  # noqa: F401  (installs the BigQuery syntax additions)


@pytest.mark.parametrize(
    "sql",
    [
        "DROP TABLE FUNCTION `p.d.f`",
        "DROP TABLE FUNCTION IF EXISTS `p.d.f`",
        "drop table function if exists d.f",
    ],
)
def test_drop_table_function_is_a_drop_of_that_kind(sql):
    tree = sqlglot.parse_one(sql, read="bigquery")
    assert isinstance(tree, exp.Drop) and tree.args.get("kind") == "TABLE FUNCTION"
    assert tree.sql("bigquery").upper() == " ".join(sql.split()).upper()


def test_drop_table_and_drop_function_are_unchanged():
    table, function = sqlglot.parse("DROP TABLE `p.d.t`; DROP FUNCTION `p.d.f`", read="bigquery")
    assert table.args.get("kind") == "TABLE" and function.args.get("kind") == "FUNCTION"


def test_a_script_with_a_dropped_table_function_still_reads_its_other_statements():
    trees = sqlglot.parse("DROP TABLE FUNCTION IF EXISTS `p.d.f`;\nSELECT a FROM fn(TABLE `p.d.t`)", read="bigquery")
    assert trees[0].args.get("kind") == "TABLE FUNCTION"
    assert "t" in [table.name for table in trees[1].find_all(exp.Table)]
