"""Every query of the GoogleSQL feature checklist that BigQuery accepts parses and prints back to SQL that reads the same.

``tests/fixtures/googlesql_checklist.json`` holds one small query per GoogleSQL query feature, each accepted by a BigQuery
dry run. A case marked ``gap`` is not read yet and must still fail, so fixing it means updating the fixture; one marked
``since_sqlglot`` needs that sqlglot major version.
"""

import json
from pathlib import Path

import pytest
import sqlglot
from sqlglot import exp

import kumosql  # noqa: F401  (installs the BigQuery syntax additions)

CASES = json.loads((Path(__file__).parent / "fixtures" / "googlesql_checklist.json").read_text(encoding="utf-8"))["cases"]
MAJOR = int(sqlglot.__version__.split(".")[0])


@pytest.mark.parametrize("case", CASES, ids=[case["feature"] for case in CASES])
def test_checklist_query(case):
    sql = case["sql"]
    if "gap" in case:
        with pytest.raises(sqlglot.errors.ParseError):
            sqlglot.parse(sql, read="bigquery")
        return
    if MAJOR < case.get("since_sqlglot", 0):
        pytest.skip(f"needs sqlglot {case['since_sqlglot']}")
    trees = [tree for tree in sqlglot.parse(sql, read="bigquery") if tree is not None]
    assert trees and not any(isinstance(tree, exp.Command) for tree in trees)
    printed = [tree.sql("bigquery") for tree in trees]
    again = [tree.sql("bigquery") for tree in sqlglot.parse(";\n".join(printed), read="bigquery") if tree is not None]
    assert again == printed
