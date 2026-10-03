"""Counterexample search: run two queries on small random databases and compare their results.

Given a schema with integrity constraints, ``find_counterexample`` builds many small
databases that satisfy every constraint, executes both queries on DuckDB, and returns
the first database on which the result bags differ. A returned counterexample is a
*refutation*: it was observed by running both queries, so it needs no trust in the
prover. Finding none proves nothing.

The generator draws values from small domains seeded with the literals of the two
queries (a constant, one below, one above), so predicates, joins and ties are hit
often; it honors NOT NULL, primary keys, foreign keys, ``CHECK``-style predicates
(a NULL never satisfies one, which is the strict reading and so valid under both),
consecutive-id columns, and cross-table implications.
"""

from __future__ import annotations

import datetime as _dt
import itertools
import os
import random
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Callable

import sqlglot
from sqlglot import exp

from .duckdb_load import run_unoptimized
from .result_equivalence import DataRules

try:
    import duckdb
except ImportError:  # pragma: no cover
    duckdb = None


@dataclass
class Column:
    name: str
    type: str  # INT, VARCHAR, DATE, NUMERIC, TIME, BOOL, or ENUM
    not_null: bool = False
    values: tuple = ()  # the allowed values of an ENUM


@dataclass
class Table:
    name: str
    columns: list[Column]
    primary_key: tuple[str, ...] = ()
    unique: list[tuple[str, ...]] = field(default_factory=list)
    sequential: tuple[str, ...] = ()  # columns holding consecutive integers in row order

    def column(self, name: str) -> Column:
        for column in self.columns:
            if column.name == name:
                return column
        raise KeyError(name)


@dataclass
class Check:
    """A predicate over the rows of ``tables``: ``test`` gets one row (a dict) per table."""

    tables: tuple[str, ...]
    test: Callable[..., bool | None]


@dataclass
class Spec:
    tables: dict[str, Table]
    foreign_keys: list[tuple[str, str, str, str]] = field(default_factory=list)  # child table, column, parent table, column
    checks: list[Check] = field(default_factory=list)


@dataclass(frozen=True)
class Counterexample:
    tables: dict[str, list[tuple]]
    left_rows: list[tuple]
    right_rows: list[tuple]

    def script(self, spec: Spec) -> str:
        """A SQL script that recreates the database."""

        lines = []
        for name, rows in self.tables.items():
            table = spec.tables[name]
            lines.append(f"CREATE TABLE {name} ({', '.join(f'{c.name} {c.type}' for c in table.columns)});")
            for row in rows:
                lines.append(f"INSERT INTO {name} VALUES ({', '.join(_literal(v) for v in row)});")
        return "\n".join(lines)


def _literal(value) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)):
        return str(value)
    return "'" + str(value).replace("'", "''") + "'"


# --- domains -----------------------------------------------------------------------------

_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TIME = re.compile(r"^\d{2}:\d{2}(:\d{2})?$")


@dataclass
class Constants:
    ints: set = field(default_factory=set)
    strings: set = field(default_factory=set)
    dates: set = field(default_factory=set)
    times: set = field(default_factory=set)
    counts: set = field(default_factory=set)  # n in a ``COUNT(..) > n``-style test: groups of n to n + 2 rows matter


def constants_of(*queries: str, dialect: str = "mysql") -> Constants:
    found = Constants()
    for query in queries:
        try:
            tree = sqlglot.parse_one(_dollars(query), read=dialect)
        except sqlglot.errors.SqlglotError:
            continue
        for compare in tree.find_all(exp.GT, exp.GTE, exp.EQ, exp.LT, exp.LTE, exp.NEQ):
            if compare.find(exp.Count):
                for literal in compare.find_all(exp.Literal):
                    if not literal.is_string and literal.this.isdigit() and 1 <= int(literal.this) <= 12:
                        found.counts.add(int(literal.this))
        for literal in tree.find_all(exp.Literal):
            text = literal.this
            if literal.is_string:
                if _DATE.match(text):
                    found.dates.add(text)
                elif _TIME.match(text):
                    found.times.add(text if len(text) > 5 else text + ":00")
                else:
                    stripped = text.strip("%")
                    found.strings.update({text, stripped} - {""})
            else:
                try:
                    found.ints.add(int(float(text)))
                except ValueError:
                    pass
    return found


def _domain(column: Column, constants: Constants, wide: bool = False) -> list:
    kind = column.type.split("(")[0].upper()
    if kind == "ENUM":
        return list(column.values)
    if kind in ("INT", "INTEGER", "BIGINT", "SMALLINT"):
        base = {0, 1, 2, 3}
        for n in sorted(constants.ints)[:12]:
            base.update({n - 1, n, n + 1})
            if wide and n > 3:
                base.add(n // 2)  # two halves reach a ``SUM(..) >= n`` threshold exactly
        return sorted(base)
    if kind in ("NUMERIC", "DECIMAL", "FLOAT", "DOUBLE"):
        base = {0, 1, 2, 3, 0.5, 1.5, 0.333, 2.567}
        for n in sorted(constants.ints)[:12]:
            base.update({n - 1, n, n + 1})
            if wide and n > 3:
                base.add(n // 2)
        return sorted(base)
    if kind == "BOOL" or kind == "BOOLEAN":
        return [True, False]
    if kind == "DATE":
        base = {"2020-01-01", "2020-01-02", "2020-01-03", "2021-01-01"}
        for text in sorted(constants.dates)[:8]:
            try:
                day = _dt.date.fromisoformat(text)
            except ValueError:
                continue
            base.update({(day + _dt.timedelta(days=d)).isoformat() for d in (-1, 0, 1)})
        return sorted(base)
    if kind == "TIME":
        return sorted({"00:00:00", "08:00:00", "12:00:00", "23:59:59"} | constants.times)
    base = {"a", "b", "c"}
    base.update(sorted(constants.strings)[:12])
    return sorted(base)


def _duck_type(column: Column) -> str:
    kind = column.type.split("(")[0].upper()
    return {
        "INT": "BIGINT", "INTEGER": "BIGINT", "BIGINT": "BIGINT", "SMALLINT": "BIGINT",
        "NUMERIC": "DOUBLE", "DECIMAL": "DOUBLE", "FLOAT": "DOUBLE", "DOUBLE": "DOUBLE",
        "BOOL": "BOOLEAN", "BOOLEAN": "BOOLEAN", "DATE": "DATE", "TIME": "TIME",
    }.get(kind, "VARCHAR")


# --- database generation -----------------------------------------------------------------


class _NoRow(Exception):
    pass


class _Generator:
    def __init__(self, spec: Spec, constants: Constants, rng: random.Random, wide: bool = False):
        self.spec, self.rng, self.wide = spec, rng, wide
        self.domains = {
            (t.name, c.name): _domain(c, constants, wide) for t in spec.tables.values() for c in t.columns
        }
        # wide databases: tables of 3 to 8 rows and past any COUNT threshold in the queries, and some
        # columns drawn from their whole domain, for groups with many distinct values
        self.sizes = [3, 4, 5, 6, 8] + [m for n in sorted(constants.counts) for m in (n, n + 1, n + 2)]
        self.order = self._table_order()
        self.single_checks: dict[str, list[Check]] = {}
        self.global_checks: list[Check] = []
        for check in spec.checks:
            if len(set(check.tables)) == 1:
                self.single_checks.setdefault(check.tables[0], []).append(check)
            else:
                self.global_checks.append(check)

    def _table_order(self) -> list[str]:
        parents = {name: set() for name in self.spec.tables}
        for child, _, parent, _ in self.spec.foreign_keys:
            if child != parent:
                parents[child].add(parent)
        order, seen = [], set()

        def visit(name, stack=()):
            if name in seen or name in stack:
                return
            for parent in sorted(parents[name]):
                visit(parent, stack + (name,))
            seen.add(name)
            order.append(name)

        for name in self.spec.tables:
            visit(name)
        return order

    def _with_parents(self, used: set[str]) -> set[str]:
        """``used`` plus every table a foreign key of theirs points at: a child row needs a parent row to exist."""

        needed, todo = set(), list(used)
        while todo:
            name = todo.pop()
            if name in needed:
                continue
            needed.add(name)
            todo.extend(parent for child, _, parent, _ in self.spec.foreign_keys if child == name)
        return needed

    def database(self, used: set[str], max_rows: int, empty: str | None = None) -> dict[str, list[tuple]] | None:
        db: dict[str, list[tuple]] = {}
        needed = self._with_parents(used)
        self.shared_hot = {}
        self.null_p = self.rng.choice([0.15, 0.15, 0.15, 0.5, 0.0])
        for name in self.order:
            if name not in needed:
                db[name] = []
                continue
            db[name] = [] if name == empty else self._rows(self.spec.tables[name], db, max_rows)
        for check in self.global_checks:
            if not all(t in needed for t in check.tables):
                continue
            if not self._holds(check, db):
                return None
        return db

    def _holds(self, check: Check, db) -> bool:
        tables = [self.spec.tables[t] for t in check.tables]

        def rows_of(i):
            return [dict(zip((c.name for c in tables[i].columns), row)) for row in db[tables[i].name]]

        def go(i, picked):
            if i == len(tables):
                return check.test(*picked) is True
            return all(go(i + 1, picked + [row]) for row in rows_of(i))

        return go(0, [])

    def _rows(self, table: Table, db, max_rows: int) -> list[tuple]:
        rng = self.rng
        if self.wide:
            count = rng.choice(self.sizes)
        elif max_rows > 5:  # bulk databases, for counts like HAVING COUNT(*) >= 5
            count = rng.randint(max_rows - 3, max_rows)
        else:
            count = min(rng.choice([0, 1, 1, 2, 2, 3, 3, 4, 5][: 3 + max_rows * 2]), max_rows)
        # a hot subset of each domain makes repeated values (ties, join matches) common
        hot = {}
        for column in table.columns:
            domain = self.domains[(table.name, column.name)]
            key = (column.name.lower(), column.type)
            if key not in self.shared_hot:  # columns of the same name share their hot values, so joins match
                size = rng.choice([1, 2, 3, len(domain)] if self.wide else [1, 2, 3])
                self.shared_hot[key] = rng.sample(domain, min(len(domain), size)) if domain else [None]
            hot[column.name] = [v for v in self.shared_hot[key] if v in domain] or self.shared_hot[key]
        keys = ([table.primary_key] if table.primary_key else []) + list(table.unique)
        fk_of = {c: (p, pc) for ch, c, p, pc in self.spec.foreign_keys if ch == table.name}
        taken = {i: set() for i in range(len(keys))}
        rows: list[tuple] = []
        for index in range(count):
            for _ in range(25):
                row = {}
                try:
                    for column in table.columns:
                        row[column.name] = self._value(table, column, hot, fk_of, db, index)
                except _NoRow:
                    return rows
                good = True
                for i, key in enumerate(keys):
                    value = tuple(row[k] for k in key)
                    if None not in value and value in taken[i]:
                        good = False
                if good and all(c.test(row) is True for c in self.single_checks.get(table.name, [])):
                    for i, key in enumerate(keys):
                        taken[i].add(tuple(row[k] for k in key))
                    rows.append(tuple(row[c.name] for c in table.columns))
                    break
        return rows

    def _value(self, table: Table, column: Column, hot, fk_of, db, index):
        rng = self.rng
        if column.name in table.sequential:
            return index + 1
        if column.name in fk_of:
            parent, parent_column = fk_of[column.name]
            parent_table = self.spec.tables[parent]
            position = [c.name for c in parent_table.columns].index(parent_column)
            values = [row[position] for row in db.get(parent, []) if row[position] is not None]
            if values:
                return rng.choice(values)
            raise _NoRow  # no parent row to point at; a NULL reference is not generated (the strict reading)
        if not column.not_null and column.name not in table.primary_key and rng.random() < self.null_p:
            return None
        pool = hot[column.name] if rng.random() < 0.8 else self.domains[(table.name, column.name)]
        return rng.choice(pool)


# --- running -----------------------------------------------------------------------------


def _norm(value):
    if isinstance(value, float):
        return round(value, 6)
    if hasattr(value, "is_finite"):  # Decimal
        return round(float(value), 6)
    return value


def _bag(rows) -> Counter:
    return Counter(tuple(_norm(v) for v in row) for row in rows)


def _grouping_keys(item: exp.Expression):
    """The key expressions of one GROUP BY item, looking inside GROUPING SETS, ROLLUP and CUBE."""

    if isinstance(item, (exp.GroupingSets, exp.Rollup, exp.Cube, exp.Tuple)):
        for inner in item.expressions:
            yield from _grouping_keys(inner)
    elif isinstance(item, exp.Paren):
        yield from _grouping_keys(item.this)
    else:
        yield item


def relax_grouping(tree: exp.Expression) -> exp.Expression:
    """MySQL lets a grouped query read columns that are not grouped (they are arbitrary within the group).

    DuckDB refuses. Wrapping such columns in ``ANY_VALUE`` gives the same answer when the column is
    functionally determined by the grouping; ``Searcher`` verifies the queries stay deterministic by
    re-running them on shuffled data, and drops any counterexample that depended on the arbitrary pick.
    """

    for select in list(tree.find_all(exp.Select)):
        group = select.args.get("group")
        if group is None or not group.expressions or any(group.args.get(k) for k in ("grouping_sets", "cube", "rollup")):
            continue
        projections = select.expressions
        # MySQL reads an unqualified GROUP BY name as the select item of that name, DuckDB calls it ambiguous
        by_name = {}
        for item in projections:
            inner = item.this if isinstance(item, exp.Alias) else item
            if isinstance(inner, exp.Column) and inner.table:
                by_name.setdefault((item.alias if isinstance(item, exp.Alias) else inner.name).lower(), inner)
        for position, item in enumerate(list(group.expressions)):
            if isinstance(item, exp.Column) and not item.table and item.name.lower() in by_name:
                item.replace(by_name[item.name.lower()].copy())
        grouped = set()
        for item in (key for entry in group.expressions for key in _grouping_keys(entry)):
            if isinstance(item, exp.Literal) and not item.is_string:
                position = int(item.this) - 1
                if 0 <= position < len(projections):
                    item = projections[position]
                    item = item.this if isinstance(item, exp.Alias) else item
            if isinstance(item, exp.Column):
                grouped.add(item.name.lower())
            grouped.update(c.name.lower() for c in item.find_all(exp.Column))
            grouped.add(item.sql().lower())
        aliases = {p.alias.lower() for p in projections if isinstance(p, exp.Alias)}

        def inside_aggregate(node) -> bool:
            parent = node.parent
            while parent is not None and parent is not select:
                if isinstance(parent, (exp.AggFunc, exp.Window, exp.Filter)):
                    return True  # an aggregate's FILTER (WHERE ...) reads the group's rows too
                if isinstance(parent, exp.Select):
                    return True  # belongs to a nested select
                parent = parent.parent
            return False

        targets = list(projections) + [select.args[k] for k in ("having", "order", "qualify") if select.args.get(k) is not None]
        for target in targets:
            for column in list(target.find_all(exp.Column)):
                if inside_aggregate(column) or column.name.lower() in grouped or column.sql().lower() in grouped:
                    continue
                if not column.table and column.name.lower() in aliases:
                    continue
                wrapped = exp.Anonymous(this="ANY_VALUE", expressions=[column.copy()])
                if column.parent is select and column in projections:
                    wrapped = exp.alias_(wrapped, column.name, quoted=False)  # keep the output column's name
                column.replace(wrapped)
    return tree


def _date_functions(tree: exp.Expression) -> exp.Expression:
    """``SUBDATE(d, n)`` / ``ADDDATE(d, n)`` (a day count or an INTERVAL) and ``CROSS JOIN .. ON``, which DuckDB lacks."""

    for node in list(tree.find_all(exp.Anonymous)):
        name = str(node.this).upper()
        if name in ("SUBDATE", "ADDDATE") and len(node.expressions) == 2:
            day, count = node.expressions
            interval = count.copy() if isinstance(count, exp.Interval) else exp.Interval(this=count.copy(), unit=exp.var("DAY"))
            if isinstance(day, exp.Literal) and day.is_string:
                day = exp.cast(day.copy(), "date")
            node.replace((exp.Sub if name == "SUBDATE" else exp.Add)(this=day.copy(), expression=interval))
    for node in list(tree.find_all(exp.Sub, exp.Add)):
        left = node.this
        if isinstance(node.expression, exp.Interval) and isinstance(left, exp.Literal) and left.is_string:
            left.replace(exp.cast(left.copy(), "date"))
    for join in tree.find_all(exp.Join):
        if join.args.get("kind") == "CROSS" and join.args.get("on") is not None:
            join.set("kind", None)
    return tree


def _calcite_forms(tree: exp.Expression) -> exp.Expression:
    """Calcite-only forms (benchmark SQL printed by Calcite) spelled the way DuckDB accepts them.

    * ``COUNT(a, b)`` counts rows where every argument is non-NULL.
    * ``FIRST_VALUE(x)`` / ``LAST_VALUE(x)`` used as plain aggregates are DuckDB's ``first`` / ``last``
      (an arbitrary pick unless ``x`` is constant in the group; ``Searcher`` drops differences that
      depend on row order).
    * ``SINGLE_VALUE(x)`` is ``x`` of the only row, NULL with no rows, and an error with more than one;
      the error makes that database unusable, so ``Searcher`` skips it.
    * ``ORDER BY NULL`` (a MySQL idiom for "no order") is dropped; DuckDB refuses it.
    """

    for node in list(tree.find_all(exp.Count)):
        if node.expressions and not isinstance(node.this, exp.Distinct):
            arguments = [node.this, *node.expressions]
            test = exp.and_(*(exp.Not(this=exp.Is(this=a.copy(), expression=exp.Null())) for a in arguments))
            node.replace(exp.Count(this=exp.Case(ifs=[exp.If(this=test, true=exp.Literal.number(1))])))
    for node in list(tree.find_all(exp.FirstValue, exp.LastValue)):
        if not isinstance(node.parent, exp.Window):
            name = "first" if isinstance(node, exp.FirstValue) else "last"
            node.replace(exp.Anonymous(this=name, expressions=[node.this.copy()]))
    for node in list(tree.find_all(exp.Anonymous)):
        if str(node.this).upper() == "SINGLE_VALUE" and len(node.expressions) == 1:
            many = exp.GT(this=exp.Count(this=exp.Star()), expression=exp.Literal.number(1))
            fail = exp.Anonymous(this="error", expressions=[exp.Literal.string("SINGLE_VALUE: more than one row")])
            node.replace(exp.Case(ifs=[exp.If(this=many, true=fail)], default=exp.AnyValue(this=node.expressions[0].copy())))
    for order in list(tree.find_all(exp.Order)):
        kept = [o for o in order.expressions if not (isinstance(o, exp.Ordered) and isinstance(o.this, exp.Null))]
        if len(kept) != len(order.expressions):
            if kept:
                order.set("expressions", kept)
            else:
                order.pop()
    return tree


def _fill_empty_select_lists(tree: exp.Expression) -> exp.Expression:
    """Calcite prints a zero-column projection as ``SELECT FROM t``, which DuckDB refuses.

    One constant column keeps the row count, which is all a zero-column result holds. When such a
    select is a derived table read by ``*``, the star excludes that column again.
    """

    filler = "$empty"
    for select in list(tree.find_all(exp.Select)):
        if select.expressions:
            continue
        select.set("expressions", [exp.alias_(exp.Literal.number(1), filler, quoted=True)])
        holder = select.parent
        if isinstance(holder, exp.Subquery) and isinstance(holder.parent, exp.Lateral):
            holder = holder.parent
        outer = holder.parent.parent if holder is not None and isinstance(holder.parent, (exp.From, exp.Join)) else None
        if isinstance(outer, exp.Select):
            for star in outer.expressions:
                if isinstance(star, exp.Star):
                    key = "except_" if "except_" in exp.Star.arg_types else "except"  # renamed in sqlglot 30
                    star.set(key, [*(star.args.get(key) or []), exp.column(filler, quoted=True)])
    return tree


_PLAIN_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _quote_unusual_names(tree: exp.Expression) -> exp.Expression:
    """Quote identifiers DuckDB's parser cannot read bare, such as Calcite's ``$f0`` and ``EXPR$0``.

    DuckDB matches quoted identifiers case-insensitively, so quoting changes nothing else.
    """

    for identifier in tree.find_all(exp.Identifier):
        if not identifier.quoted and not _PLAIN_NAME.match(identifier.name):
            identifier.set("quoted", True)
    return tree


def _barewords(tree: exp.Expression, known: set[str]) -> exp.Expression:
    """The benchmark queries sometimes write a string as a bare word (``THEN YES ELSE NO``): read an
    unqualified name that is no column, alias or table anywhere in the schema or query as that string."""

    names = set(known)
    for node in tree.find_all(exp.Alias):
        names.add(node.alias.lower())
    for node in tree.find_all(exp.TableAlias):
        names.add(node.name.lower())
        names.update(c.name.lower() for c in node.columns)
    for node in tree.find_all(exp.Table):
        names.add(node.name.lower())
    for column in list(tree.find_all(exp.Column)):
        if not column.table and column.name.lower() not in names and not isinstance(column.parent, (exp.Dot,)):
            column.replace(exp.Literal.string(column.name))
    return tree


def _dollars(sql: str) -> str:
    """Calcite names columns ``$f9``, ``EXPR$0``: DuckDB rejects ``$`` in bare names."""

    return re.sub(r"(?<=[\w$])\$|\$(?=\w)", "_S_", sql)


def to_duckdb(sql: str, dialect: str = "mysql", known: set[str] | None = None) -> str:
    tree = sqlglot.parse_one(_dollars(sql), read=dialect)
    if known is not None:
        tree = _barewords(tree, known)
    tree = relax_grouping(_fill_empty_select_lists(_calcite_forms(_date_functions(tree))))
    if dialect == "bigquery":
        from .bigquery_on_duckdb import faithful

        tree = faithful(tree)
    return _quote_unusual_names(tree).sql(dialect="duckdb")


_TARGETED_TYPES = {"INT": "INT64", "VARCHAR": "STRING", "ENUM": "STRING", "TIME": "STRING", "DATE": "DATE", "NUMERIC": "FLOAT64", "BOOL": "BOOL"}


class Searcher:
    """Reusable search over one schema: parse once, then try many databases."""

    def __init__(self, spec: Spec, left: str, right: str, *, dialect: str = "mysql", predicates: dict[str, int] | None = None):
        """``predicates`` names uninterpreted boolean functions (name to arity) the queries call.

        Each gets one fixed, arbitrary interpretation (a hash of its arguments), so a difference found
        with it refutes the pair for every interpretation's sake: the pair must agree under all of them.
        """

        self.spec = spec
        self.constants = constants_of(left, right, dialect=dialect)
        known = {c.name.lower() for t in spec.tables.values() for c in t.columns}
        self.left_sql = to_duckdb(left, dialect, known)
        self.right_sql = to_duckdb(right, dialect, known)
        names = set()
        for sql in (left, right):
            for table in sqlglot.parse_one(_dollars(sql), read=dialect).find_all(exp.Table):
                names.add(table.name.lower())
        by_lower = {n.lower(): n for n in spec.tables}
        self.used = {by_lower[n] for n in names if n in by_lower}
        self.columns_used, self.star, self.having = set(), False, False
        for sql in (left, right):
            tree = sqlglot.parse_one(_dollars(sql), read=dialect)
            self.columns_used.update(c.name.lower() for c in tree.find_all(exp.Column))
            self.star = self.star or any(True for _ in tree.find_all(exp.Star))
            self.having = self.having or tree.find(exp.Having) is not None
        self.db = duckdb.connect(":memory:")
        self.bigquery = dialect == "bigquery"
        if self.bigquery:
            from .bigquery_on_duckdb import configure

            configure(self.db)
        for name, arity in (predicates or {}).items():
            if not _PLAIN_NAME.match(name):
                raise ValueError(f"predicate name {name!r}")
            parameters = [f"p{i}" for i in range(arity)]
            hashed = ", ".join([*parameters, _literal(name.lower())])
            self.db.execute(f"CREATE MACRO {name}({', '.join(parameters)}) AS (hash({hashed}) % 2 = 0)")
        for name in self.used:
            table = spec.tables[name]
            columns = ", ".join(f'"{c.name}" {_duck_type(c)}' for c in table.columns)
            self.db.execute(f'CREATE TABLE "{name}" ({columns})')

    def _load(self, data, order=None) -> None:
        """Replace the contents of every used table (one literal INSERT per table: ``executemany`` is slow)."""

        statements = []
        for name in self.used:
            statements.append(f'DELETE FROM "{name}";')
            rows = data[name]
            if rows:
                values = ", ".join("(" + ", ".join(_literal(v) for v in row) + ")" for row in rows)
                statements.append(f'INSERT INTO "{name}" VALUES {values};')
        self.db.execute(" ".join(statements))

    def _read(self, rows):
        """BigQuery-dialect rows as BigQuery returns them; a row it could not return fails like a query error."""

        if not self.bigquery:
            return rows
        from .bigquery_on_duckdb import UnfaithfulOutput, bigquery_rows

        try:
            return bigquery_rows(rows)
        except UnfaithfulOutput as error:
            raise duckdb.InvalidInputException(str(error)) from error

    def _rows(self, sql: str):
        return self._read(self.db.execute(sql).fetchall())

    def runs(self) -> bool:
        """Whether DuckDB accepts both queries on an empty database."""

        try:
            self._rows(self.left_sql)
            self._rows(self.right_sql)
        except duckdb.Error:
            return False
        return True

    def _small_domain(self, column: Column) -> list:
        """At most two values for a column (plus NULL when nullable): enough for equal and unequal, below and above."""

        kind = column.type.split("(")[0].upper()
        c = self.constants
        if kind == "ENUM":
            values = list(column.values)[:2]
        elif kind in ("INT", "INTEGER", "BIGINT", "SMALLINT", "NUMERIC", "DECIMAL", "FLOAT", "DOUBLE"):
            low, high = (min(c.ints), max(c.ints)) if c.ints else (0, 0)
            values = [low, high + 1] if low == high else [low, high + 1]
        elif kind in ("BOOL", "BOOLEAN"):
            values = [True, False]
        elif kind == "DATE":
            values = (sorted(c.dates) + ["2020-01-01", "2020-01-02"])[:2]
        elif kind == "TIME":
            values = (sorted(c.times) + ["00:00:00", "12:00:00"])[:2]
        else:
            values = (sorted(c.strings) + ["a", "b"])[:2]
        if not column.not_null and column.name.lower() != "":
            values = values + [None]
        return values

    def exhaustive(self, max_rows: int = 2, max_dbs: int = 20000):
        """Run both queries on *every* small database: each referenced table holds at most ``max_rows`` rows,
        each referenced column takes one of at most three values (NULL and two drawn from the queries' constants),
        unreferenced columns hold one fixed value. Returns ``("verified", None)`` when no database separates the
        queries, ``("found", counterexample)``, or ``("too_large", None)``."""

        generator = _Generator(self.spec, self.constants, random.Random(0))
        tables = sorted(self.used)
        referenced = lambda t, col: self.star or col.name.lower() in self.columns_used or col.name in t.primary_key
        for rows_allowed in range(max_rows, 0, -1):
            per_table = []
            for name in tables:
                table = self.spec.tables[name]
                pools = []
                for column in table.columns:
                    if referenced(table, column):
                        pools.append(self._small_domain(column))
                    else:
                        pools.append(self._small_domain(column)[:1])
                candidates = []
                for values in itertools.product(*pools):
                    row = dict(zip((c.name for c in table.columns), values))
                    if all(chk.test(row) is True for chk in generator.single_checks.get(name, [])):
                        candidates.append(tuple(values))
                keys = ([table.primary_key] if table.primary_key else []) + list(table.unique)
                index = {c.name: i for i, c in enumerate(table.columns)}
                sets = []
                for size in range(rows_allowed + 1):
                    for combo in itertools.combinations_with_replacement(candidates, size):
                        ok = True
                        for key in keys:
                            seen = [tuple(r[index[k]] for k in key) for r in combo]
                            real = [v for v in seen if None not in v]
                            if len(real) != len(set(real)):
                                ok = False
                                break
                        if ok:
                            sets.append(list(combo))
                    if len(sets) > max_dbs:
                        break
                per_table.append(sets)
            total = 1
            for sets in per_table:
                total *= max(len(sets), 1)
                if total > max_dbs:
                    break
            if total <= max_dbs:
                break
        else:
            return "too_large", None
        if total > max_dbs:
            return "too_large", None
        try:
            for combo in itertools.product(*per_table):
                data = dict(zip(tables, combo))
                if not self._satisfies(data, generator):
                    continue
                self._load(data)
                a = self._rows(self.left_sql)
                b = self._rows(self.right_sql)
                if _bag(a) != _bag(b) and self._stable(data, a, b, random.Random(1)):
                    return "found", Counterexample(data, a, b)
        except duckdb.Error:
            return "too_large", None
        return "verified", None

    def _satisfies(self, data, generator) -> bool:
        for child, column, parent, parent_column in self.spec.foreign_keys:
            if child not in data:
                continue
            ci = [c.name for c in self.spec.tables[child].columns].index(column)
            pi = [c.name for c in self.spec.tables[parent].columns].index(parent_column)
            parent_values = {r[pi] for r in data.get(parent, [])} if parent in data else None
            if parent_values is None:
                continue
            if any(r[ci] is not None and r[ci] not in parent_values for r in data[child]):
                return False
            if any(r[ci] is None for r in data[child]):
                return False  # strict reading: a foreign key is not NULL
        for check in generator.global_checks:
            if all(t in data for t in check.tables) and not generator._holds(check, data):
                return False
        return True

    def search(self, trials: int = 150, seed: int = 0, targeted: bool = True, *, wide: bool = False) -> Counterexample | None:
        """``wide`` draws bigger tables with more distinct values (see ``_Generator``)."""

        rng = random.Random(seed)
        generator = _Generator(self.spec, self.constants, rng, wide=wide)
        used = sorted(self.used)
        for trial in range(trials):
            largest = max(self.constants.ints, default=0)
            max_rows = 2 if trial < trials // 3 else 3 if trial < 2 * trials // 3 else 5 if trial < 5 * trials // 6 else max(5, min(8, largest + 1))
            # the first trials leave each referenced table empty in turn
            empty = used[trial] if trial < len(used) else None
            data = generator.database(self.used, max_rows, empty)
            if data is None:
                continue
            try:
                self._load(data)
                a = self._rows(self.left_sql)
                b = self._rows(self.right_sql)
            except duckdb.Error:
                continue  # a runtime error on this database (a failed cast, SINGLE_VALUE of two rows): try the next
            if _bag(a) != _bag(b) and self._stable(data, a, b, rng):
                return Counterexample({n: data[n] for n in self.used}, a, b)
        return self._search_targeted(rng) if targeted and os.environ.get("KUMOSQL_TARGETED", "1") != "0" else None

    def _search_targeted(self, rng) -> Counterexample | None:
        """Databases built around the queries' constants, joins and groups (:mod:`kumosql.targeted_data`).

        A database is tried only if it respects every declaration of the spec (NOT NULL, keys,
        foreign keys, enumerations, sequential columns and checks).
        """

        from .refute import repair_foreign_keys
        from .targeted_data import database_suite

        schema = {name.lower(): {c.name: _TARGETED_TYPES.get(c.type, "INT64") for c in self.spec.tables[name].columns} for name in self.used}
        rules = {
            name.lower(): DataRules(
                frozenset(c.name.lower() for c in self.spec.tables[name].columns if c.not_null or c.name in self.spec.tables[name].primary_key),
                tuple(tuple(k.lower() for k in key) for key in ([self.spec.tables[name].primary_key] if self.spec.tables[name].primary_key else []) + list(self.spec.tables[name].unique)),
            )
            for name in self.used
        }
        foreign = [(c.lower(), cc.lower(), p.lower(), pc.lower()) for c, cc, p, pc in self.spec.foreign_keys if c in self.used and p in self.used]
        generator = _Generator(self.spec, self.constants, rng)
        seen = set()
        for sql in (self.left_sql, self.right_sql):
            try:
                suite = database_suite(sql, schema, rules, dialect="duckdb", random_seeds=())
            except Exception:
                continue
            for labeled in suite:
                dataset = repair_foreign_keys(labeled.dataset, foreign, rules)
                data = {name: [tuple(r) for r in dataset.tables[name.lower()].rows] for name in self.used}
                key = tuple((n, tuple(data[n])) for n in sorted(data))
                if key in seen or not self._conforms(generator, data):
                    continue
                seen.add(key)
                try:
                    for name in self.used:
                        table = self.spec.tables[name]
                        self.db.execute(f'DELETE FROM "{name}"')
                        if data[name]:
                            self.db.executemany(f'INSERT INTO "{name}" VALUES ({", ".join("?" * len(table.columns))})', data[name])
                    a = self._rows(self.left_sql)
                    b = self._rows(self.right_sql)
                except duckdb.Error:
                    continue
                if _bag(a) != _bag(b) and self._stable(data, a, b, rng):
                    return Counterexample({n: data[n] for n in self.used}, a, b)
        return None

    def _conforms(self, generator: "_Generator", data) -> bool:
        spec = self.spec
        for name in self.used:
            table = spec.tables[name]
            names = [c.name for c in table.columns]
            rows = data[name]
            for column in table.columns:
                i = names.index(column.name)
                if column.type == "ENUM" and any(r[i] is not None and r[i] not in column.values for r in rows):
                    return False
            for i, column in enumerate(table.columns):
                if (column.not_null or column.name in table.primary_key) and any(r[i] is None for r in rows):
                    return False
            for column_name in table.sequential:
                i = names.index(column_name)
                if [r[i] for r in rows] != list(range(1, len(rows) + 1)):
                    return False
            for check in generator.single_checks.get(name, []):
                if not all(check.test(dict(zip(names, r))) is True for r in rows):
                    return False
        for child, column, parent, parent_column in spec.foreign_keys:
            if child in self.used and parent in self.used:
                ci = [c.name for c in spec.tables[child].columns].index(column)
                pi = [c.name for c in spec.tables[parent].columns].index(parent_column)
                allowed = {r[pi] for r in data[parent]}
                if any(r[ci] is None or r[ci] not in allowed for r in data[child]):
                    return False  # the strict reading: a NULL reference is not generated either
        return all(generator._holds(c, data) for c in generator.global_checks if all(t in self.used for t in c.tables))

    def _stable(self, data, a, b, rng) -> bool:
        """The difference must not depend on row order or on an arbitrary pick: shuffle and compare again."""

        expected = (_bag(a), _bag(b))
        try:
            # DuckDB's optimizer has returned wrong rows for some correlated subqueries: the same rows must
            # come back with the optimizer off, so a counterexample never rests on an engine bug
            self._load(data)
            if tuple(_bag(self._read(rows)) for rows in run_unoptimized(self.db, self.left_sql, self.right_sql)) != expected:
                return False
            # reversed and rotated first: three random shuffles of a two-row table keep its order 1 time in 8
            orders = [{name: list(reversed(data[name])) for name in self.used}]
            orders.append({name: list(data[name][1:]) + list(data[name][:1]) for name in self.used})
            orders += [{name: rng.sample(data[name], len(data[name])) for name in self.used} for _ in range(3)]
            for shuffled in orders:
                self._load(shuffled)
                if (_bag(self._rows(self.left_sql)), _bag(self._rows(self.right_sql))) != expected:
                    return False
        except duckdb.Error:
            return False
        return True


def find_counterexample(spec: Spec, left: str, right: str, *, dialect: str = "mysql", trials: int = 150, seed: int = 0):
    """A database on which the queries differ, ``None`` if none was found, ``False`` if DuckDB rejects a query."""

    try:
        searcher = Searcher(spec, left, right, dialect=dialect)
    except (sqlglot.errors.SqlglotError, duckdb.Error):
        return False
    if not searcher.runs():
        return False
    return searcher.search(trials, seed)
