"""Shrink a failing query pair, its schema and its database, keeping the failure.

A failure here is a query pair whose results differ on a database (a
counterexample to equivalence, or the original versus a faulty variant). The
minimizer makes the three parts small while the pair still *differs on the
database*, in the way SQLess reduces bug-triggering test cases. It is **not** an
equivalence-preserving simplifier: the reduced queries are different queries
from the ones given, kept only because they still disagree, and the reduced
database is what shows it.

Order of work: query edits made on both queries at once (drop a conjunct, ``HAVING``,
``ORDER BY``/``LIMIT``, ``DISTINCT`` or a set-operation branch that both queries share,
or the same select item), accepted while the pair still differs on the current
database; a part only one side has is the difference and is kept; then rows (delta
debugging, whole tables first); then cell values (to NULL, then to the smallest
domain value). Declared NOT NULL columns and keys stay respected throughout.
A reduced case is a JSON document (``to_json``/``from_json``) that ``replay``
runs from scratch on a new engine.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
import time
from typing import Any, Mapping

import sqlglot
from sqlglot import exp

from .result_equivalence import (
    _DOMAINS,
    DataRules,
    DatasetRunner,
    ExecutionError,
    Schema,
    SyntheticDataset,
    SyntheticTable,
    compare_outputs,
    respect_rules,
)


@dataclass(frozen=True)
class CaseSize:
    rows: int
    tables: int
    non_null_cells: int
    sql_chars: int
    sql_nodes: int


@dataclass(frozen=True)
class Minimized:
    left_sql: str
    right_sql: str
    dataset: SyntheticDataset
    schema: Schema
    before: CaseSize
    after: CaseSize
    seconds: float
    checks: int


def _differs(runner: DatasetRunner, left: str, right: str, dataset: SyntheticDataset, **compare) -> bool:
    try:
        a, b = runner.run(left, dataset), runner.run(right, dataset)
    except ExecutionError:
        return False
    return not compare_outputs(a, b, **compare)[0]


def _nodes(sql: str, dialect: str) -> int:
    return sum(1 for _ in sqlglot.parse_one(sql, read=dialect).walk())


def measure(left: str, right: str, dataset: SyntheticDataset, dialect: str = "bigquery") -> CaseSize:
    rows = sum(len(t.rows) for t in dataset.tables.values())
    return CaseSize(
        rows=rows,
        tables=sum(1 for t in dataset.tables.values() if t.rows),
        non_null_cells=sum(v is not None for t in dataset.tables.values() for r in t.rows for v in r),
        sql_chars=len(left) + len(right),
        sql_nodes=_nodes(left, dialect) + _nodes(right, dialect),
    )


# -- query edits ------------------------------------------------------------


def _edits(tree: exp.Expression):
    """Yield ((kind, removed text), edited copy) for each single reduction of ``tree``.

    The key names what is removed, so the same reduction can be made on both
    queries of a pair; a part present on one side only (the very thing the two
    queries disagree about) has no partner and is never removed.
    """

    nodes = list(tree.walk())
    for site, node in enumerate(nodes):
        variants = []
        if isinstance(node, exp.And):
            variants += [("conjunct", node.expression, node.this), ("conjunct", node.this, node.expression)]
        elif isinstance(node, (exp.Union, exp.Intersect, exp.Except)):
            variants += [("set_branch", node.expression, node.this), ("set_branch", node.this, node.expression)]
        for label, removed, kept in variants:
            copy = tree.copy()
            target = list(copy.walk())[site]
            replacement = (target.this if kept is node.this else target.expression).copy()
            if target is copy:
                yield (label, removed.sql()), replacement
            else:
                target.replace(replacement)
                yield (label, removed.sql()), copy
        for arg in ("where", "having", "order", "limit", "distinct", "qualify"):
            if isinstance(node, exp.Select) and node.args.get(arg) is not None:
                copy = tree.copy()
                list(copy.walk())[site].set(arg, None)
                yield (arg, node.args[arg].sql()), copy


def _drop_select_item(tree: exp.Expression, index: int):
    copy = tree.copy()
    select = copy if isinstance(copy, exp.Select) else copy.find(exp.Select)
    if select is None or len(select.expressions) <= 1:
        return None
    select.set("expressions", [e for i, e in enumerate(select.expressions) if i != index])
    return copy


def _reduce_queries(runner, left, right, dataset, dialect, compare, deadline):
    changed = True
    while changed and time.monotonic() < deadline:
        changed = False
        lt, rt = (sqlglot.parse_one(q, read=dialect) for q in (left, right))
        right_edits: dict = {}
        for key, edited in _edits(rt):
            right_edits.setdefault(key, edited)
        for key, edited in _edits(lt):
            partner = right_edits.get(key)
            if partner is None:
                continue
            candidate = (edited.sql(dialect=dialect), partner.sql(dialect=dialect))
            if candidate != (left, right) and _differs(runner, *candidate, dataset, **compare):
                left, right = candidate
                changed = True
                break
        if changed:
            continue
        # the same select item from both sides, while the column counts agree
        ls = lt if isinstance(lt, exp.Select) else lt.find(exp.Select)
        rs = rt if isinstance(rt, exp.Select) else rt.find(exp.Select)
        if ls is not None and rs is not None and len(ls.expressions) == len(rs.expressions) > 1:
            for index in range(len(ls.expressions)):
                a, b = _drop_select_item(lt, index), _drop_select_item(rt, index)
                if a is None or b is None:
                    continue
                candidate = (a.sql(dialect=dialect), b.sql(dialect=dialect))
                if _differs(runner, *candidate, dataset, **compare):
                    left, right = candidate
                    changed = True
                    break
    return left, right


# -- database reduction -------------------------------------------------------


def _with_rows(dataset: SyntheticDataset, rows_by_table: Mapping[str, list[tuple]]) -> SyntheticDataset:
    return SyntheticDataset(
        dataset.seed,
        {k: SyntheticTable(t.columns, tuple(rows_by_table[k])) for k, t in dataset.tables.items()},
    )


def _reduce_rows(runner, left, right, dataset, compare, deadline):
    rows = {k: list(t.rows) for k, t in dataset.tables.items()}
    flat = [(k, i) for k in sorted(rows) for i in range(len(rows[k]))]
    current = flat
    chunk = max(len(current) // 2, 1)
    while current and time.monotonic() < deadline:
        reduced = False
        for start in range(0, len(current), chunk):
            keep = current[:start] + current[start + chunk :]
            trial: dict[str, list[tuple]] = {k: [] for k in rows}
            for k, i in keep:
                trial[k].append(rows[k][i])
            if _differs(runner, left, right, _with_rows(dataset, trial), **compare):
                current = keep
                reduced = True
                break
        if not reduced:
            if chunk == 1:
                break
            chunk = max(chunk // 2, 1)
    final: dict[str, list[tuple]] = {k: [] for k in rows}
    for k, i in current:
        final[k].append(rows[k][i])
    return _with_rows(dataset, final)


def _simplest(col_type: str) -> Any:
    return _DOMAINS[col_type][1] if col_type in ("INT64", "FLOAT64", "NUMERIC") else _DOMAINS[col_type][0]


def _reduce_cells(runner, left, right, dataset, rules, compare, deadline):
    tables = {k: [list(r) for r in t.rows] for k, t in dataset.tables.items()}

    def build():
        return SyntheticDataset(
            dataset.seed,
            {k: SyntheticTable(t.columns, tuple(tuple(r) for r in tables[k])) for k, t in dataset.tables.items()},
        )

    for key, table in dataset.tables.items():
        for r in range(len(tables[key])):
            for c, (_, col_type) in enumerate(table.columns):
                if time.monotonic() > deadline:
                    return build()
                original = tables[key][r][c]
                for value in (None, _simplest(col_type)):
                    if value == original and type(value) is type(original):
                        continue
                    tables[key][r][c] = value
                    legal = len(respect_rules(table.columns, [tuple(x) for x in tables[key]], rules.get(key.lower()) if rules else None)) == len(tables[key])
                    if legal and _differs(runner, left, right, build(), **compare):
                        break
                    tables[key][r][c] = original
    return build()


def minimize_failure(
    left_sql: str,
    right_sql: str,
    schema: Schema,
    dataset: SyntheticDataset,
    rules: Mapping[str, DataRules] | None = None,
    *,
    dialect: str = "bigquery",
    time_limit: float = 30.0,
    compare: Mapping[str, Any] | None = None,
    runner_class=DatasetRunner,
) -> Minimized:
    """Reduce the pair and database; raises ``ValueError`` if the pair does not differ on ``dataset``."""

    compare = dict(compare or {"check_column_names": False})
    start = time.monotonic()
    deadline = start + time_limit
    with runner_class(schema) as runner:
        if not _differs(runner, left_sql, right_sql, dataset, **compare):
            raise ValueError("the query pair does not differ on the given database")
        before = measure(left_sql, right_sql, dataset, dialect)
        left, right = _reduce_queries(runner, left_sql, right_sql, dataset, dialect, compare, deadline)
        reduced = _reduce_rows(runner, left, right, dataset, compare, deadline)
        reduced = _reduce_cells(runner, left, right, reduced, rules, compare, deadline)
        # smaller queries may let a table go, and fewer rows may free another edit
        left, right = _reduce_queries(runner, left, right, reduced, dialect, compare, deadline)
        reduced = _reduce_rows(runner, left, right, reduced, compare, deadline)
        assert _differs(runner, left, right, reduced, **compare)
    return Minimized(
        left, right, reduced, schema, before, measure(left, right, reduced, dialect), time.monotonic() - start, 0
    )


# -- replayable documents ---------------------------------------------------


def _encode(value: Any) -> Any:
    if isinstance(value, Decimal):
        return {"decimal": str(value)}
    if isinstance(value, datetime):
        return {"timestamp": value.isoformat(sep=" ")}
    if isinstance(value, date):
        return {"date": value.isoformat()}
    return value


def _decode(value: Any) -> Any:
    if isinstance(value, dict):
        if "decimal" in value:
            return Decimal(value["decimal"])
        if "timestamp" in value:
            return datetime.fromisoformat(value["timestamp"])
        if "date" in value:
            return date.fromisoformat(value["date"])
    return value


def to_json(case: Minimized) -> dict:
    """A self-contained document: schema, both queries and the database."""

    return {
        "left": case.left_sql,
        "right": case.right_sql,
        "schema": {k: dict(v) for k, v in case.schema.items()},
        "tables": {
            k: {
                "columns": [list(c) for c in t.columns],
                "rows": [[_encode(v) for v in r] for r in t.rows],
            }
            for k, t in case.dataset.tables.items()
        },
    }


def replay(document: Mapping[str, Any], *, runner_class=DatasetRunner, **compare) -> bool:
    """True when the document's queries differ on its database, on a fresh engine."""

    compare = compare or {"check_column_names": False}
    tables = {
        k: SyntheticTable(
            tuple((n, t) for n, t in v["columns"]),
            tuple(tuple(_decode(x) for x in row) for row in v["rows"]),
        )
        for k, v in document["tables"].items()
    }
    dataset = SyntheticDataset(0, tables)
    with runner_class(document["schema"]) as runner:
        return _differs(runner, document["left"], document["right"], dataset, **compare)
