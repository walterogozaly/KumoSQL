"""Source tables for a scan, inferred from the columns a model's query reads.

``infer_sources`` rebuilds one :class:`kumosql.incremental.SourceTable` per table the full query reads, with the
columns it uses and a type guessed from each column's name (an ``id`` column is the table's unique key, the first
timestamp-shaped column its event time). A verdict is therefore about the model on those assumed sources.

The columns are found **per SELECT**, so a bare column is read from the one table (or pass-through CTE) its own
SELECT reads, not only when the whole query reads a single table:

* ``WITH base AS (SELECT * FROM t)`` is a pass-through: a column read from ``base`` is a column of ``t``;
* ``a.*`` names no column (the columns that are read by name are the table's columns);
* ``FROM t AS a, UNNEST(a.items) AS item`` with ``item.qty`` and ``item.price`` makes ``items`` an
  ``ARRAY<STRUCT<qty INT64, price INT64>>`` (an ``ARRAY<INT64>`` when ``item`` is read whole), so the simulator
  generates arrays instead of rejecting the unnest of a scalar.

Anything else a SELECT reads (a derived table, a column of an aggregate CTE) is not attributed to a table.
"""

from __future__ import annotations

import re

import sqlglot
from sqlglot import exp

from .ast_utils import binding_cte
from .incremental import SourceTable

_TIME = re.compile(r"(^|_)(ts|timestamp|time|at)$|^(created|updated|modified|loaded|event)(_at|_time|_ts)?$")
_STRINGY = re.compile(r"(^|_)(name|status|type|category|country|email|label|code|currency|channel|region|segment)$")
_DATE = re.compile(r"(^|_)date$")
_BOOL = re.compile(r"^(is|has)_")


def guess_type(column: str) -> str:
    name = column.lower()
    if _TIME.search(name):
        return "TIMESTAMP"
    if _DATE.search(name):
        return "DATE"
    if _BOOL.match(name):
        return "BOOL"
    if _STRINGY.search(name):
        return "STRING"
    return "INT64"


def _passthrough(cte: exp.CTE) -> str | None:
    """The table ``cte`` re-exposes unchanged (``SELECT * FROM t [WHERE ...]``), None when it is anything else."""

    body = cte.this
    if not isinstance(body, exp.Select) or len(body.expressions) != 1 or not isinstance(body.expressions[0], exp.Star):
        return None
    star = body.expressions[0]
    if star.args.get("except") or star.args.get("replace") or star.args.get("rename"):
        return None
    if any(v for k, v in body.args.items() if k not in ("expressions", "from", "from_", "where")):
        return None
    source = body.args.get("from_") or body.args.get("from")
    table = source.this if source is not None else None
    if not isinstance(table, exp.Table):
        return None
    inner = binding_cte(table)
    return _passthrough(inner) if inner is not None else table.name


def _items(select: exp.Select, target: str) -> dict[str, str | None]:
    """Each FROM or JOIN item of ``select`` by alias: the real table it reads, or None (a derived table, the model itself)."""

    source = select.args.get("from_") or select.args.get("from")
    nodes = ([source.this] if source is not None else []) + [j.this for j in select.args.get("joins") or []]
    items: dict[str, str | None] = {}
    for node in nodes:
        if isinstance(node, exp.Table):
            alias = (node.alias or node.name).lower()
            cte = binding_cte(node)
            if cte is not None:
                items[alias] = _passthrough(cte)
            elif node.name.lower() == target.lower():
                items[alias] = None
            else:
                items[alias] = node.name
        elif isinstance(node, exp.Subquery):
            items[(node.alias or "").lower()] = None
    return items


def _owner(column: exp.Column, scopes: dict[int, dict[str, str | None]]) -> str | None:
    """The table ``column`` reads: by its qualifier, else the one item of its SELECT (outer SELECTs for a correlated read)."""

    select = column.find_ancestor(exp.Select)
    while select is not None:
        items = scopes.get(id(select), {})
        if column.table:
            if column.table.lower() in items:
                return items[column.table.lower()]
        elif len(items) == 1:
            return next(iter(items.values()))
        else:
            return None
        select = select.find_ancestor(exp.Select)
    return None


def _unnest_alias(node: exp.Unnest) -> str | None:
    alias = node.args.get("alias")
    if alias is None:
        return None
    name = alias.name or (alias.columns[0].name if alias.columns else "")  # BigQuery names the element, not a table
    return name.lower() or None


def infer_sources(model, overrides: dict[str, dict[str, str]] | None = None) -> dict[str, SourceTable]:
    """Source tables read by the full query, with the columns it uses."""

    tree = sqlglot.parse_one(model.full_sql, read="bigquery")
    scopes = {id(s): _items(s, model.target) for s in tree.find_all(exp.Select)}
    columns: dict[str, dict[str, str]] = {}
    for items in scopes.values():
        for name in items.values():
            if name is not None:
                columns.setdefault(name, {})
    for column in tree.find_all(exp.Column):
        if isinstance(column.this, exp.Star):
            continue
        owner = _owner(column, scopes)
        if owner is not None:
            columns[owner].setdefault(column.name, guess_type(column.name))
    for unnest in tree.find_all(exp.Unnest):
        alias, arrays = _unnest_alias(unnest), unnest.expressions
        if alias is None or len(arrays) != 1 or not isinstance(arrays[0], exp.Column):
            continue
        owner = _owner(arrays[0], scopes)
        if owner is None:
            continue
        scope = unnest.find_ancestor(exp.Select) or tree
        fields = {c.name: guess_type(c.name) for c in scope.find_all(exp.Column) if c.table.lower() == alias}
        whole = any(not c.table and c.name.lower() == alias for c in scope.find_all(exp.Column))
        if fields and not whole:
            columns[owner][arrays[0].name] = "ARRAY<STRUCT<" + ", ".join(f"{n} {t}" for n, t in fields.items()) + ">>"
        elif whole and not fields:
            columns[owner][arrays[0].name] = "ARRAY<INT64>"
    sources: dict[str, SourceTable] = {}
    for name, cols in columns.items():
        if overrides and name in overrides:
            cols = dict(overrides[name])
        if not cols:
            cols = {"id": "INT64"}
        time_column = next((c for c, t in cols.items() if t == "TIMESTAMP"), None)
        key = ("id",) if "id" in cols else ()
        sources[name] = SourceTable(cols, key, time_column)
    return sources
