"""Shared loaders for the Calcite-derived equivalence corpora.

SPES (testData/calcite_tests.json), Cosette (examples/calcite/calcite_tests.json)
and SQLSolver (tests/fixtures/sqlsolver/calcite_pairs.txt) carry the same 232
Calcite RelOptRulesTest pairs in the same order; SQLSolver's file has no names
and edits some queries, so its names are taken by position from SPES.
"""

from __future__ import annotations

import json
from pathlib import Path
import re
import subprocess

import sqlglot
from sqlglot import exp

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures"

# SPES names that differ from Cosette's for the same (identical) pair.
SPES_NAME_FIXES = {107: "testDistinctNonDistinctTwoAggregatesWithGrouping"}


def canonical_name(name: str) -> str:
    """SPES marks a few names with a trailing ``*``."""

    return name.rstrip("*")


def norm(sql: str) -> str:
    """Whitespace, case and trailing-semicolon normalisation."""

    return re.sub(r"\s+", " ", sql.strip().rstrip(";").strip()).lower()


def norm_aliases(sql: str) -> str:
    """``norm`` plus table/subquery aliases renamed in order of appearance.

    Falls back to ``norm`` when sqlglot cannot read the query.
    """

    try:
        tree = sqlglot.parse_one(sql.strip().rstrip(";"), read="mysql")
    except Exception:  # noqa: BLE001
        return norm(sql)
    mapping: dict[str, str] = {}
    for node in tree.walk():
        if isinstance(node, (exp.Table, exp.Subquery)):
            alias = node.args.get("alias")
            if alias is not None and alias.this is not None and alias.name:
                key = alias.name.lower()
                mapping.setdefault(key, f"__a{len(mapping)}")
    for node in tree.walk():
        if isinstance(node, (exp.Table, exp.Subquery)):
            alias = node.args.get("alias")
            if alias is not None and alias.this is not None and alias.name.lower() in mapping:
                alias.set("this", exp.to_identifier(mapping[alias.name.lower()]))
        elif isinstance(node, exp.Column) and node.table and node.table.lower() in mapping:
            node.set("table", exp.to_identifier(mapping[node.table.lower()]))
    return norm(tree.sql(dialect="mysql"))


def git_head(path: Path) -> str:
    return subprocess.run(["git", "-C", str(path), "rev-parse", "HEAD"],
                          capture_output=True, text=True, check=True).stdout.strip()


def load_spes(spes: Path) -> list[dict]:
    rows = json.loads((spes / "testData" / "calcite_tests.json").read_text(encoding="utf-8"))
    out = []
    for i, r in enumerate(rows):
        name = SPES_NAME_FIXES.get(i, canonical_name(r["name"]))
        out.append({"index": i, "name": name, "spes_name": r["name"], "q1": r["q1"],
                    "q2": r["q2"]})
    return out


def load_cosette_json(cosette: Path) -> list[dict]:
    path = cosette / "examples" / "calcite" / "calcite_tests.json"
    return json.loads(path.read_text(encoding="utf-8"))


def load_sqlsolver_calcite() -> list[tuple[str, str]]:
    lines = (FIXTURES / "sqlsolver" / "calcite_pairs.txt").read_text(encoding="utf-8").split("\n")
    lines = [line for line in lines if line.strip()]
    return [(lines[i], lines[i + 1]) for i in range(0, len(lines) - 1, 2)]


SKIP_CATEGORIES = [
    ("empty relation `(VALUES)`", "empty_values_relation"),
    ("precondition not stated", "precondition_not_stated"),
    ("precondition not expressible", "precondition_not_expressible"),
    ("labelled ", "random_db_disagrees"),
    ("Calcite pair, but", "random_db_disagrees"),
    ("a query compares a column", "type_mismatch"),
    ("has no declared columns", "no_declared_columns"),
    ("ranges over", "multi_row_predicate"),
    ("folder says not equivalent", "label_disputed"),
    ("uses `||`", "concat_operator"),
    ("sqlglot", "sqlglot_parse_error"),
    ("duckdb fails", "duckdb_error"),
]


def skip_category(reason: str) -> str:
    for needle, category in SKIP_CATEGORIES:
        if needle in reason:
            return category
    return "other"
