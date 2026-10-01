"""SMT-backed equivalence proofs for a core subset of BigQuery SQL.

This is a SPES-style prover. Each query is compiled into a normal form over
symbolic table rows and the equivalence conditions are discharged with Z3:

* select-project-join blocks (inner and cross joins, ``WHERE``, derived
  tables and CTEs that can be inlined) are compared under bag semantics by
  searching for a bijection between table occurrences that preserves the
  filter and the projected row;
* ``SELECT DISTINCT`` and ``UNION DISTINCT`` are compared under set semantics
  with containment mappings in both directions, which also proves redundant
  self-join elimination;
* ``GROUP BY`` blocks are compared by proving that both sides form the same
  groups and that their aggregates agree row by row;
* ``UNION ALL`` branches are matched one to one.

Values use three-valued logic with explicit NULL flags. Columns are modeled
as an untyped value domain (number, string or boolean); proofs therefore hold
for every column typing, which keeps them sound without a schema.

Anything outside the subset (outer joins, windows, ``LIMIT``, correlated or
predicate subqueries, nondeterministic functions, ...) yields ``not_proven``.
When a proof fails the prover looks for a small concrete database on which
the two queries return different rows; if it finds one the result is
``not_equivalent`` with that database attached. A counterexample is only
reported when every function in both queries is modeled exactly, so it never
rests on an uninterpreted function.

``z3-solver`` is an optional dependency: install ``kumosql[smt]``.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from enum import Enum
from fractions import Fraction
import argparse
import itertools
import json
import re
import sys

import sqlglot
from sqlglot import exp

try:  # pragma: no cover - exercised by the import itself
    import z3
except ImportError:  # pragma: no cover
    z3 = None


class SmtStatus(str, Enum):
    """Outcomes of the SMT prover."""

    PROVEN_EQUIVALENT = "proven_equivalent"
    NOT_EQUIVALENT = "not_equivalent"
    NOT_PROVEN = "not_proven"


@dataclass(frozen=True)
class Counterexample:
    """A database on which the two queries return different result bags."""

    tables: dict[str, list[dict[str, object]]]
    left_rows: list[tuple]
    right_rows: list[tuple]


@dataclass(frozen=True)
class SmtEquivalenceResult:
    status: SmtStatus
    reason: str
    counterexample: Counterexample | None = None
    assumptions: tuple[str, ...] = ()

    @property
    def proven(self) -> bool:
        return self.status is SmtStatus.PROVEN_EQUIVALENT


@dataclass(frozen=True)
class TableConstraints:
    """Declared integrity constraints of one table (names lower-case).

    ``not_null`` lists columns that never hold NULL; each tuple in ``keys`` is a
    set of columns that is unique across rows (a primary key, or a UNIQUE
    constraint whose columns are also NOT NULL). Rows that agree on every column
    of a key are the same row.
    """

    not_null: frozenset = frozenset()
    keys: tuple = ()


BASE_ASSUMPTIONS = (
    "FLOAT64 values are never NaN",
    "runtime errors (division by zero, overflow, failed casts) are not modeled",
    "SUM and AVG are treated as independent of row order",
    "result column types are not compared; confirm schemas with a BigQuery dry run",
)
EXACT_ARITHMETIC_ASSUMPTION = "+, - and * are exact (no FLOAT64 rounding or INT64 overflow)"

_NONDETERMINISTIC_NAMES = {
    "ANY_VALUE",
    "APPROX_COUNT_DISTINCT",
    "APPROX_QUANTILES",
    "APPROX_TOP_COUNT",
    "APPROX_TOP_SUM",
    "ARRAY_AGG",
    "CURRENT_DATE",
    "CURRENT_DATETIME",
    "CURRENT_TIME",
    "CURRENT_TIMESTAMP",
    "GENERATE_UUID",
    "RAND",
    "SESSION_USER",
    "STRING_AGG",
}
_NONDETERMINISTIC_TYPES = {
    "AnyValue",
    "ApproxDistinct",
    "ApproxQuantile",
    "ArrayAgg",
    "CurrentDate",
    "CurrentDatetime",
    "CurrentTime",
    "CurrentTimestamp",
    "CurrentUser",
    "GroupConcat",
    "MaxBy",
    "MinBy",
    "ArgMax",
    "ArgMin",
    "Rand",
    "TableSample",
    "Uuid",
}
_SUPPORTED_AGGREGATES = {
    exp.Count: "COUNT",
    exp.Sum: "SUM",
    exp.Min: "MIN",
    exp.Max: "MAX",
    exp.Avg: "AVG",
    exp.CountIf: "COUNTIF",
    exp.LogicalAnd: "LOGICAL_AND",
    exp.LogicalOr: "LOGICAL_OR",
}
_DATEISH = re.compile(r"^\s*[+-]?\d{1,5}-\d{1,2}-\d{1,2}")
_CANONICAL_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_MAX_MAPPINGS = 5000
_MAX_EVAL_COMBINATIONS = 50000


class Unsupported(Exception):
    """The query uses something outside the modeled subset."""


class _Unknown(Exception):
    """The solver timed out."""


# --------------------------------------------------------------------------
# Z3 value domain
# --------------------------------------------------------------------------

_VALUE_SORT = None


def _value_sort():
    global _VALUE_SORT
    if _VALUE_SORT is None:
        value = z3.Datatype("SqlValue")
        value.declare("Num", ("num", z3.RealSort()))
        value.declare("Str", ("str", z3.StringSort()))
        value.declare("Bool", ("bool", z3.BoolSort()))
        _VALUE_SORT = value.create()
    return _VALUE_SORT


@dataclass(frozen=True)
class _Val:
    """A nullable SQL value: ``null`` flag and payload."""

    null: object
    val: object


@dataclass(frozen=True)
class _Pred:
    """A three-valued predicate: ``t`` when TRUE, ``f`` when FALSE."""

    t: object
    f: object


def _canon(v: _Val):
    V = _value_sort()
    return z3.If(v.null, V.Num(0), v.val)


def _null_eq(a: _Val, b: _Val):
    """Row/grouping equality: NULL matches NULL."""

    return z3.And(a.null == b.null, z3.Or(a.null, a.val == b.val))


def _rows_eq(left: list[_Val], right: list[_Val]):
    if len(left) != len(right):
        return z3.BoolVal(False)
    return z3.And(*[_null_eq(a, b) for a, b in zip(left, right)]) if left else z3.BoolVal(True)


def _lt(a, b):
    """A strict total order on values that agrees with SQL within a type."""

    V = _value_sort()

    def rank(v):
        return z3.If(V.is_Num(v), 0, z3.If(V.is_Str(v), 1, 2))

    return z3.If(
        z3.And(V.is_Num(a), V.is_Num(b)),
        V.num(a) < V.num(b),
        z3.If(
            z3.And(V.is_Str(a), V.is_Str(b)),
            V.str(a) < V.str(b),
            z3.If(
                z3.And(V.is_Bool(a), V.is_Bool(b)),
                z3.And(z3.Not(V.bool(a)), V.bool(b)),
                rank(a) < rank(b),
            ),
        ),
    )


def _pair_lt(a: _Val, b: _Val):
    ca, cb = _canon(a), _canon(b)
    return z3.Or(z3.And(z3.Not(a.null), b.null), z3.And(a.null == b.null, _lt(ca, cb)))


def _box(p: _Pred) -> _Val:
    V = _value_sort()
    return _Val(z3.And(z3.Not(p.t), z3.Not(p.f)), V.Bool(p.t))


def _const_pred(value: bool) -> _Pred:
    return _Pred(z3.BoolVal(value), z3.BoolVal(not value))


# --------------------------------------------------------------------------
# Normal forms
# --------------------------------------------------------------------------


class _Occ:
    """One occurrence of a relation in FROM, with lazily created columns."""

    def __init__(self, table: str, uid: str, columns: list[str] | None, opaque: bool = False):
        self.table = table
        self.uid = uid
        self.columns = columns
        self.opaque = opaque
        self.cols: dict[str, _Val] = {}

    def col(self, name: str) -> _Val:
        if name not in self.cols:
            V = _value_sort()
            self.cols[name] = _Val(
                z3.Bool(f"{self.uid}.{name}#null"), z3.Const(f"{self.uid}.{name}", V)
            )
        return self.cols[name]

    def copy(self, uid: str) -> "_Occ":
        clone = _Occ(self.table, uid, self.columns, self.opaque)
        for name in self.cols:
            clone.col(name)
        return clone


@dataclass
class _Spj:
    occs: list[_Occ]
    cond: _Pred
    outputs: list[_Val]
    names: list[str]
    distinct: bool = False
    facts: list = field(default_factory=list)


@dataclass
class _AggCall:
    func: str
    distinct: bool
    arg: _Val | None
    var: _Val


@dataclass
class _Agg:
    occs: list[_Occ]
    cond: _Pred
    keys: list[_Val]
    aggs: list[_AggCall]
    having: _Pred | None
    outputs: list[_Val]
    names: list[str]
    is_global: bool
    distinct: bool = False
    facts: list = field(default_factory=list)


@dataclass
class _Union:
    branches: list
    distinct: bool
    column_names: list | None = None

    @property
    def names(self) -> list[str]:
        if self.column_names is not None:
            return self.column_names
        return self.branches[0].names


@dataclass
class _Source:
    occ: _Occ | None = None
    cols: dict[str, _Val] | None = None
    order: list[str] = field(default_factory=list)

    def lookup(self, name: str) -> _Val | None:
        if self.cols is not None:
            return self.cols.get(name)
        if self.occ.columns is not None and name not in self.occ.columns:
            return None
        return self.occ.col(name)

    def may_have(self, name: str) -> bool | None:
        """True/False when known, None when the schema is unknown."""

        if self.cols is not None:
            return name in self.cols
        if self.occ.columns is None:
            return None
        return name in self.occ.columns

    def star(self) -> list[tuple[str, _Val]]:
        if self.cols is not None:
            return [(name, self.cols[name]) for name in self.order]
        if self.occ.columns is None:
            raise Unsupported(f"SELECT * over {self.occ.table} needs a schema")
        return [(name, self.occ.col(name)) for name in self.occ.columns]


# --------------------------------------------------------------------------
# Compiler: sqlglot AST -> normal form
# --------------------------------------------------------------------------


def _from_clause(select: exp.Select):
    return select.args.get("from") or select.args.get("from_")


def _with_clause(query: exp.Expression):
    return query.args.get("with") or query.args.get("with_")


def _parse_number(text: str) -> Fraction:
    try:
        dec = Decimal(text)
    except InvalidOperation as error:
        raise Unsupported(f"numeric literal {text!r}") from error
    if not dec.is_finite():
        raise Unsupported(f"numeric literal {text!r}")
    value = Fraction(dec)
    if value.denominator == 1 and "." not in text and "e" not in text.lower():
        if abs(value) > 2**53:
            raise Unsupported(f"integer literal {text} is not exactly representable as FLOAT64")
        return value
    # A decimal with at most 15 significant digits maps to a unique FLOAT64,
    # and rounding preserves its order against every other FLOAT64 value.
    if len(dec.normalize().as_tuple().digits) > 15:
        raise Unsupported(f"numeric literal {text} has more than 15 significant digits")
    return value


class _AggCtx:
    def __init__(self, compiler: "_Compiler"):
        self.compiler = compiler
        self.calls: list[_AggCall] = []


class _Compiler:
    def __init__(self, schema: dict[str, list[str]] | None, exact_arithmetic: bool, dialect: str = "bigquery"):
        self.dialect = dialect
        self.schema = {
            key.lower(): [c.lower() for c in cols] for key, cols in (schema or {}).items()
        }
        self.exact = exact_arithmetic
        self.counter = itertools.count()
        self.uses_uf = False
        self.functions: dict[tuple[str, int], tuple] = {}
        # Hypotheses every valid query satisfies, e.g. arithmetic operands are
        # numbers in exact mode.
        self.facts: list = []

    def fresh(self, prefix: str) -> str:
        return f"{prefix}{next(self.counter)}"

    # ---- queries -------------------------------------------------------

    def compile(self, sql: str) -> _Union:
        statements = [s for s in sqlglot.parse(sql, read=self.dialect) if s is not None]
        if len(statements) != 1:
            raise Unsupported(f"expected one statement, found {len(statements)}")
        statement = statements[0]
        self._check_nondeterminism(statement)
        return self._query(statement, {})

    def _check_nondeterminism(self, node: exp.Expression) -> None:
        for sub in node.walk():
            name = type(sub).__name__
            if name in _NONDETERMINISTIC_TYPES:
                raise Unsupported(f"nondeterministic: {sub.sql(dialect='bigquery')}")
            if isinstance(sub, exp.Anonymous) and (sub.name or "").upper() in _NONDETERMINISTIC_NAMES:
                raise Unsupported(f"nondeterministic: {sub.sql(dialect='bigquery')}")
            if isinstance(sub, (exp.Limit, exp.Offset, exp.Window)):
                raise Unsupported(f"{name.upper()} is not modeled")

    def _query(self, node: exp.Expression, ctes: dict) -> _Union:
        while isinstance(node, exp.Subquery):
            node = node.this
        with_clause = _with_clause(node)
        if with_clause is not None:
            if with_clause.args.get("recursive"):
                raise Unsupported("recursive CTEs")
            ctes = dict(ctes)
            for cte in with_clause.expressions:
                ctes[cte.alias_or_name.lower()] = (cte.this, dict(ctes))
        if type(node) is exp.Union:
            if node.args.get("by_name") or node.args.get("side") or node.args.get("kind") or node.args.get("on"):
                raise Unsupported("UNION variants other than ALL/DISTINCT")
            distinct = bool(node.args.get("distinct"))
            branches = []
            for child in (node.this, node.expression):
                sub = self._query(child, ctes)
                if not sub.distinct or distinct:
                    branches.extend(sub.branches)
                elif len(sub.branches) == 1:
                    sub.branches[0].distinct = True
                    branches.append(sub.branches[0])
                else:
                    raise Unsupported("UNION DISTINCT nested inside UNION ALL")
            widths = {len(b.outputs) for b in branches}
            if len(widths) != 1:
                raise Unsupported("UNION branches have different widths")
            return _Union(branches, distinct)
        if isinstance(node, exp.Select):
            return _Union([self._select(node, ctes)], False)
        raise Unsupported(f"{type(node).__name__} statements")

    def _select(self, node: exp.Select, ctes: dict):
        for key in ("qualify", "laterals", "pivots", "connect", "match", "prewhere", "windows", "into"):
            if node.args.get(key):
                raise Unsupported(f"{key.upper()} clause")
        distinct_node = node.args.get("distinct")
        if distinct_node is not None and distinct_node.args.get("on"):
            raise Unsupported("DISTINCT ON")
        if node.args.get("kind"):
            raise Unsupported("SELECT AS STRUCT/VALUE")

        facts_start = len(self.facts)
        occs: list[_Occ] = []
        conds: list[_Pred] = []
        env: dict[str, _Source] = {}
        from_clause = _from_clause(node)
        items = []
        if from_clause is not None:
            items.append((from_clause.this, None))
            for join in node.args.get("joins") or []:
                if join.args.get("side") or join.args.get("method") or join.args.get("using"):
                    raise Unsupported(f"join: {join.sql(dialect='bigquery')}")
                kind = (join.args.get("kind") or "").upper()
                if kind not in ("", "INNER", "CROSS"):
                    raise Unsupported(f"{kind} JOIN")
                items.append((join.this, join))
        for source_node, join in items:
            alias, source = self._source(source_node, ctes, occs, conds)
            if alias in env:
                raise Unsupported(f"duplicate alias {alias}")
            env[alias] = source
            if join is not None and join.args.get("on") is not None:
                conds.append(self._pred(join.args["on"], env, None, None))
        if node.args.get("where") is not None:
            conds.append(self._pred(node.args["where"].this, env, None, None))
        cond = _const_pred(True)
        for c in conds:
            cond = _Pred(z3.And(cond.t, c.t), z3.Or(cond.f, c.f))

        group = node.args.get("group")
        having = node.args.get("having")
        agg_ctx = _AggCtx(self)
        names: list[str] = []
        outputs: list[_Val] = []
        aliases: dict[str, exp.Expression] = {}
        for item in node.expressions:
            if isinstance(item, exp.Star) or (isinstance(item, exp.Column) and isinstance(item.this, exp.Star)):
                if item.args.get("except") or item.args.get("replace"):
                    raise Unsupported("SELECT * EXCEPT/REPLACE")
                if isinstance(item, exp.Star):
                    sources = list(env.values())
                else:
                    table = item.table.lower()
                    if table not in env:
                        raise Unsupported(f"unknown alias {table}")
                    sources = [env[table]]
                for source in sources:
                    for name, val in source.star():
                        names.append(name)
                        outputs.append(val)
                continue
            expr = item.this if isinstance(item, exp.Alias) else item
            name = item.alias_or_name.lower() if isinstance(item, (exp.Alias, exp.Column)) else ""
            if name:
                aliases[name] = expr
            names.append(name)
            outputs.append(self._val(expr, env, agg_ctx, None))

        is_agg = group is not None or bool(agg_ctx.calls) or having is not None
        if not is_agg:
            return _Spj(occs, cond, outputs, names, distinct=distinct_node is not None, facts=self.facts[facts_start:])

        keys: list[_Val] = []
        if group is not None:
            for key in ("rollup", "cube", "grouping_sets", "totals"):
                if group.args.get(key):
                    raise Unsupported(f"GROUP BY {key.upper()}")
            for key_expr in group.expressions:
                if isinstance(key_expr, exp.Literal) and not key_expr.is_string:
                    position = int(key_expr.this)
                    if not 1 <= position <= len(outputs):
                        raise Unsupported(f"GROUP BY {position}")
                    keys.append(outputs[position - 1])
                else:
                    keys.append(self._val(key_expr, env, None, aliases))
        having_pred = self._pred(having.this, env, agg_ctx, aliases) if having is not None else None
        return _Agg(
            occs,
            cond,
            keys,
            agg_ctx.calls,
            having_pred,
            outputs,
            names,
            is_global=group is None,
            distinct=distinct_node is not None,
            facts=self.facts[facts_start:],
        )

    def _source(self, node, ctes, occs, conds) -> tuple[str, _Source]:
        if isinstance(node, exp.Table):
            if node.args.get("joins") or node.args.get("pivots") or node.args.get("laterals"):
                raise Unsupported("table modifiers")
            if isinstance(node.this, (exp.Anonymous, exp.Func)):
                raise Unsupported("table functions")
            alias = node.alias_or_name.lower()
            name = node.name.lower()
            if not node.db and not node.catalog and name in ctes:
                body, cte_env = ctes[name]
                return alias, self._derived(body, cte_env, occs, conds)
            key = self._table_key(node)
            occ = _Occ(key, self.fresh("r"), self.schema.get(key.lower()))
            occs.append(occ)
            return alias, _Source(occ=occ)
        if isinstance(node, exp.Subquery):
            alias = node.alias_or_name.lower()
            if not alias:
                raise Unsupported("unaliased subquery in FROM")
            return alias, self._derived(node.this, ctes, occs, conds)
        raise Unsupported(f"FROM item {type(node).__name__}")

    @staticmethod
    def _table_key(node: exp.Table) -> str:
        parts = [p.name for p in node.parts]
        return ".".join(parts)

    def _derived(self, body, ctes, occs, conds) -> _Source:
        saved = len(self.facts)
        try:
            sub = self._query(body, ctes)
        except Unsupported:
            del self.facts[saved:]
            return self._opaque(body, ctes, occs)
        if len(sub.branches) == 1 and isinstance(sub.branches[0], _Spj) and not sub.branches[0].distinct:
            branch = sub.branches[0]
            if len(set(branch.names)) != len(branch.names) or "" in branch.names:
                raise Unsupported("derived table with unnamed or duplicate columns")
            occs.extend(branch.occs)
            conds.append(branch.cond)
            return _Source(cols=dict(zip(branch.names, branch.outputs)), order=list(branch.names))
        return self._opaque(body, ctes, occs)

    def _opaque(self, body, ctes, occs) -> _Source:
        """A derived relation kept whole: identified by its CTE-expanded SQL."""

        body = self._expand_ctes(body.copy(), ctes)
        self._check_nondeterminism(body)
        # Kept whole, the relation must not depend on the outer query.
        if any(isinstance(node, (exp.Unnest, exp.Lateral)) for node in body.walk()):
            raise Unsupported("UNNEST or LATERAL inside a derived relation")
        declared = {
            node.alias_or_name.lower()
            for node in body.walk()
            if isinstance(node, (exp.Table, exp.Subquery, exp.CTE))
        }
        for column in body.find_all(exp.Column):
            if column.table and column.table.lower() not in declared:
                raise Unsupported(f"derived relation references outer alias {column.table}")
        inner = body
        while isinstance(inner, exp.Subquery):
            inner = inner.this
        first = inner
        while isinstance(first, exp.Union):
            first = first.this
            while isinstance(first, exp.Subquery):
                first = first.this
        if not isinstance(first, exp.Select):
            raise Unsupported("derived relation shape")
        names = []
        for item in first.expressions:
            if isinstance(item, exp.Star) or (isinstance(item, exp.Column) and isinstance(item.this, exp.Star)):
                raise Unsupported("SELECT * inside a derived relation")
            names.append(item.alias_or_name.lower())
        if "" in names or len(set(names)) != len(names):
            raise Unsupported("derived relation with unnamed or duplicate columns")
        key = "(" + body.sql(dialect="bigquery", normalize_functions="upper") + ")"
        occ = _Occ(key, self.fresh("d"), names, opaque=True)
        occs.append(occ)
        return _Source(occ=occ)

    def _expand_ctes(self, body, ctes):
        local = dict(ctes)
        with_clause = _with_clause(body)
        if with_clause is not None:
            for cte in with_clause.expressions:
                local.pop(cte.alias_or_name.lower(), None)
        for table in list(body.find_all(exp.Table)):
            name = table.name.lower()
            if table.db or table.catalog or name not in local:
                continue
            inner_body, inner_env = local[name]
            expanded = self._expand_ctes(inner_body.copy(), inner_env)
            table.replace(exp.Subquery(this=expanded, alias=exp.TableAlias(this=exp.to_identifier(table.alias_or_name))))
        return body

    # ---- expressions ---------------------------------------------------

    def _column(self, col: exp.Column, env: dict[str, _Source], aliases) -> _Val | exp.Expression:
        if isinstance(col.this, exp.Star):
            raise Unsupported("star in expression")
        if col.args.get("db") or col.args.get("catalog"):
            raise Unsupported(f"column path {col.sql(dialect='bigquery')}")
        name = col.name.lower()
        table = col.table.lower()
        if table:
            if table not in env:
                raise Unsupported(f"unknown or correlated alias {table}")
            val = env[table].lookup(name)
            if val is None:
                raise Unsupported(f"{table}.{name} is not a column of {table}")
            return val
        definite = [s for s in env.values() if s.may_have(name) is True]
        maybe = [s for s in env.values() if s.may_have(name) is None]
        if aliases is not None and name in aliases:
            alias_expr = aliases[name]
            same_column = isinstance(alias_expr, exp.Column) and alias_expr.name.lower() == name
            if not same_column:
                if definite or maybe:
                    raise Unsupported(f"{name} is both a SELECT alias and a possible column")
                return alias_expr
        if len(definite) == 1 and not maybe:
            return definite[0].lookup(name)
        if not definite and len(maybe) == 1 and len(env) == 1:
            return maybe[0].lookup(name)
        raise Unsupported(f"cannot resolve column {name} without a schema")

    def _pred(self, e, env, agg, aliases) -> _Pred:
        if isinstance(e, exp.Paren):
            return self._pred(e.this, env, agg, aliases)
        if isinstance(e, exp.And):
            a, b = self._pred(e.this, env, agg, aliases), self._pred(e.expression, env, agg, aliases)
            return _Pred(z3.And(a.t, b.t), z3.Or(a.f, b.f))
        if isinstance(e, exp.Or):
            a, b = self._pred(e.this, env, agg, aliases), self._pred(e.expression, env, agg, aliases)
            return _Pred(z3.Or(a.t, b.t), z3.And(a.f, b.f))
        if isinstance(e, exp.Not):
            a = self._pred(e.this, env, agg, aliases)
            return _Pred(a.f, a.t)
        if isinstance(e, exp.Boolean):
            return _const_pred(bool(e.this))
        if isinstance(e, exp.Null):
            return _Pred(z3.BoolVal(False), z3.BoolVal(False))
        comparisons = {exp.EQ: "=", exp.NEQ: "<>", exp.LT: "<", exp.LTE: "<=", exp.GT: ">", exp.GTE: ">="}
        for cls, op in comparisons.items():
            if type(e) is cls:
                return self._compare(op, self._val(e.this, env, agg, aliases), self._val(e.expression, env, agg, aliases))
        if isinstance(e, (exp.NullSafeEQ, exp.NullSafeNEQ)):
            eq = _null_eq(self._val(e.this, env, agg, aliases), self._val(e.expression, env, agg, aliases))
            eq = eq if isinstance(e, exp.NullSafeEQ) else z3.Not(eq)
            return _Pred(eq, z3.Not(eq))
        if isinstance(e, exp.Is):
            target = e.expression
            if isinstance(target, exp.Null):
                v = self._val(e.this, env, agg, aliases)
                return _Pred(v.null, z3.Not(v.null))
            if isinstance(target, exp.Boolean):
                p = self._pred(e.this, env, agg, aliases)
                hit = p.t if target.this else p.f
                return _Pred(hit, z3.Not(hit))
            raise Unsupported(f"IS {target.sql(dialect='bigquery')}")
        if isinstance(e, exp.In):
            if e.args.get("query") is not None or e.args.get("unnest") is not None or e.args.get("field") is not None:
                raise Unsupported("IN subquery/UNNEST")
            left = self._val(e.this, env, agg, aliases)
            result = _const_pred(False)
            for item in e.expressions:
                c = self._compare("=", left, self._val(item, env, agg, aliases))
                result = _Pred(z3.Or(result.t, c.t), z3.And(result.f, c.f))
            return result
        if isinstance(e, exp.Between):
            v = self._val(e.this, env, agg, aliases)
            lo = self._compare(">=", v, self._val(e.args["low"], env, agg, aliases))
            hi = self._compare("<=", v, self._val(e.args["high"], env, agg, aliases))
            return _Pred(z3.And(lo.t, hi.t), z3.Or(lo.f, hi.f))
        if isinstance(e, (exp.Exists, exp.Subquery, exp.Select)):
            raise Unsupported("predicate subqueries")
        v = self._val(e, env, agg, aliases)
        V = _value_sort()
        # Only BOOL values are TRUE or FALSE; other types cannot reach here in
        # a valid query, so they are modeled as neither.
        known = z3.And(z3.Not(v.null), V.is_Bool(v.val))
        return _Pred(z3.And(known, V.bool(v.val)), z3.And(known, z3.Not(V.bool(v.val))))

    @staticmethod
    def _compare(op: str, a: _Val, b: _Val) -> _Pred:
        if op == "=":
            r = a.val == b.val
        elif op == "<>":
            r = a.val != b.val
        elif op == "<":
            r = _lt(a.val, b.val)
        elif op == ">":
            r = _lt(b.val, a.val)
        elif op == "<=":
            r = z3.Or(_lt(a.val, b.val), a.val == b.val)
        else:
            r = z3.Or(_lt(b.val, a.val), a.val == b.val)
        known = z3.And(z3.Not(a.null), z3.Not(b.null))
        return _Pred(z3.And(known, r), z3.And(known, z3.Not(r)))

    def _val(self, e, env, agg: _AggCtx | None, aliases) -> _Val:
        V = _value_sort()
        if isinstance(e, exp.Paren):
            return self._val(e.this, env, agg, aliases)
        if isinstance(e, exp.Column):
            resolved = self._column(e, env, aliases)
            if isinstance(resolved, exp.Expression):
                return self._val(resolved, env, agg, None)
            return resolved
        if isinstance(e, exp.Literal):
            return self._literal(e, negate=False)
        if isinstance(e, exp.Null):
            return _Val(z3.BoolVal(True), V.Num(0))
        if isinstance(e, exp.Boolean):
            return _Val(z3.BoolVal(False), V.Bool(z3.BoolVal(bool(e.this))))
        if isinstance(e, exp.Neg) and isinstance(e.this, exp.Literal) and not e.this.is_string:
            return self._literal(e.this, negate=True)
        if isinstance(e, (exp.And, exp.Or, exp.Not, exp.EQ, exp.NEQ, exp.LT, exp.LTE, exp.GT, exp.GTE,
                          exp.Is, exp.In, exp.Between, exp.NullSafeEQ, exp.NullSafeNEQ)):
            return _box(self._pred(e, env, agg, aliases))
        agg_cls = next((cls for cls in _SUPPORTED_AGGREGATES if type(e) is cls), None)
        if agg_cls is not None:
            return self._aggregate(_SUPPORTED_AGGREGATES[agg_cls], e, env, agg)
        if isinstance(e, exp.AggFunc):
            raise Unsupported(f"aggregate {e.sql(dialect='bigquery')}")
        if isinstance(e, exp.Coalesce):
            args = [self._val(a, env, agg, aliases) for a in [e.this, *e.expressions]]
            result = args[-1]
            for a in reversed(args[:-1]):
                result = _Val(z3.And(a.null, result.null), z3.If(a.null, result.val, a.val))
            return result
        if isinstance(e, exp.If):
            cond = self._pred(e.this, env, agg, aliases)
            then = self._val(e.args["true"], env, agg, aliases)
            other = e.args.get("false")
            other = self._val(other, env, agg, aliases) if other is not None else _Val(z3.BoolVal(True), V.Num(0))
            return _Val(z3.If(cond.t, then.null, other.null), z3.If(cond.t, then.val, other.val))
        if isinstance(e, exp.Case):
            operand = e.args.get("this")
            operand_val = self._val(operand, env, agg, aliases) if operand is not None else None
            default = e.args.get("default")
            result = self._val(default, env, agg, aliases) if default is not None else _Val(z3.BoolVal(True), V.Num(0))
            for branch in reversed(e.args.get("ifs") or []):
                if operand_val is not None:
                    cond = self._compare("=", operand_val, self._val(branch.this, env, agg, aliases))
                else:
                    cond = self._pred(branch.this, env, agg, aliases)
                then = self._val(branch.args["true"], env, agg, aliases)
                result = _Val(z3.If(cond.t, then.null, result.null), z3.If(cond.t, then.val, result.val))
            return result
        if isinstance(e, exp.Nullif):
            a = self._val(e.this, env, agg, aliases)
            eq = self._compare("=", a, self._val(e.expression, env, agg, aliases))
            return _Val(z3.Or(a.null, eq.t), a.val)
        if (
            isinstance(e, (exp.Add, exp.Sub, exp.Mul))
            and self.exact
            and not any(isinstance(side, exp.Interval) for side in (e.this, e.expression))
        ):
            a, b = self._val(e.this, env, agg, aliases), self._val(e.expression, env, agg, aliases)
            self._numeric(a)
            self._numeric(b)
            x, y = V.num(a.val), V.num(b.val)
            r = x + y if isinstance(e, exp.Add) else x - y if isinstance(e, exp.Sub) else x * y
            return _Val(z3.Or(a.null, b.null), V.Num(r))
        if isinstance(e, exp.Neg) and self.exact:
            a = self._val(e.this, env, agg, aliases)
            self._numeric(a)
            return _Val(a.null, V.Num(-V.num(a.val)))
        if isinstance(e, (exp.Add, exp.Mul)):
            a, b = self._val(e.this, env, agg, aliases), self._val(e.expression, env, agg, aliases)
            # Commutative: apply the function to the arguments in a canonical order.
            swap = _pair_lt(b, a)
            first = _Val(z3.If(swap, b.null, a.null), z3.If(swap, b.val, a.val))
            second = _Val(z3.If(swap, a.null, b.null), z3.If(swap, a.val, b.val))
            null_fn, val_fn = self._function(type(e).__name__, 2)
            args = self._uf_args([first, second])
            return _Val(z3.Or(a.null, b.null), val_fn(*args))
        if isinstance(e, (exp.Sub, exp.Neg, exp.Div, exp.Mod, exp.DPipe)):
            parts = [e.this] + ([e.expression] if isinstance(e, exp.Binary) else [])
            vals = [self._val(p, env, agg, aliases) for p in parts]
            null_fn, val_fn = self._function(type(e).__name__, len(vals))
            args = self._uf_args(vals)
            nulls = z3.Or(*[v.null for v in vals])
            if isinstance(e, exp.Div):
                nulls = z3.Or(nulls, null_fn(*args))  # SAFE_DIVIDE-like shapes stay possible
            return _Val(nulls, val_fn(*args))
        if isinstance(e, exp.Cast) and not isinstance(e, exp.TryCast):
            to = e.args["to"]
            if (
                isinstance(e.this, exp.Literal)
                and e.this.is_string
                and to.is_type(exp.DataType.Type.DATE)
            ):
                return self._literal(e.this, negate=False)
            v = self._val(e.this, env, agg, aliases)
            _, val_fn = self._function(f"CAST:{to.sql(dialect='bigquery')}", 1)
            return _Val(v.null, val_fn(*self._uf_args([v])))
        if isinstance(e, (exp.Func, exp.Binary, exp.Unary)):
            return self._generic(e, env, agg, aliases)
        raise Unsupported(f"expression {type(e).__name__}: {e.sql(dialect='bigquery')}")

    def _numeric(self, v: _Val) -> None:
        self.facts.append(z3.Implies(z3.Not(v.null), _value_sort().is_Num(v.val)))

    def _literal(self, e: exp.Literal, negate: bool) -> _Val:
        V = _value_sort()
        if e.is_string:
            text = e.this
            if _DATEISH.match(text) and not _CANONICAL_DATE.match(text):
                raise Unsupported(f"date/time-like literal {text!r} (only 'YYYY-MM-DD' is modeled)")
            return _Val(z3.BoolVal(False), V.Str(z3.StringVal(text)))
        value = _parse_number(e.this)
        if negate:
            value = -value
        return _Val(z3.BoolVal(False), V.Num(z3.RealVal(f"{value.numerator}/{value.denominator}")))

    def _function(self, name: str, arity: int):
        self.uses_uf = True
        key = (name, arity)
        if key not in self.functions:
            V = _value_sort()
            domain = [z3.BoolSort(), V] * arity
            self.functions[key] = (
                z3.Function(f"null:{name}/{arity}", *domain, z3.BoolSort()) if arity else z3.Bool(f"null:{name}"),
                z3.Function(f"val:{name}/{arity}", *domain, V) if arity else z3.Const(f"val:{name}", V),
            )
        return self.functions[key]

    @staticmethod
    def _uf_args(vals: list[_Val]) -> list:
        args = []
        for v in vals:
            args.extend([v.null, _canon(v)])
        return args

    def _generic(self, e, env, agg, aliases) -> _Val:
        if isinstance(e, (exp.Lambda, exp.Window)):
            raise Unsupported(type(e).__name__)
        name_parts = [e.name.upper() if isinstance(e, exp.Anonymous) else type(e).__name__]
        vals: list[_Val] = []
        for key, value in e.args.items():
            if key.startswith("_") or value is None:
                continue
            children = value if isinstance(value, list) else [value]
            for child in children:
                if isinstance(child, (exp.Var, exp.DataType, exp.Identifier)) or not isinstance(child, exp.Expression):
                    name_parts.append(f"{key}={child.sql(dialect='bigquery') if isinstance(child, exp.Expression) else child}")
                elif isinstance(child, (exp.Subquery, exp.Select, exp.Exists, exp.Lambda, exp.Star)):
                    raise Unsupported(f"{type(child).__name__} inside {type(e).__name__}")
                else:
                    name_parts.append(f"{key}=?")
                    vals.append(self._val(child, env, agg, aliases))
        null_fn, val_fn = self._function("|".join(name_parts), len(vals))
        if not vals:
            return _Val(null_fn, val_fn)
        args = self._uf_args(vals)
        return _Val(null_fn(*args), val_fn(*args))

    def _aggregate(self, func: str, e, env, agg: _AggCtx | None) -> _Val:
        if agg is None:
            raise Unsupported(f"aggregate in a non-aggregate position: {e.sql(dialect='bigquery')}")
        V = _value_sort()
        target = e.this
        distinct = False
        if isinstance(target, exp.Distinct):
            if len(target.expressions) != 1:
                raise Unsupported("multi-argument DISTINCT aggregate")
            distinct = True
            target = target.expressions[0]
        if func in ("MIN", "MAX") and e.args.get("expressions"):
            raise Unsupported(f"{func} with several arguments")
        for key in ("having_max", "ignore_nulls", "order", "limit", "separator"):
            if e.args.get(key):
                raise Unsupported(f"aggregate modifier {key}")
        if func in ("MIN", "MAX", "LOGICAL_AND", "LOGICAL_OR"):
            distinct = False
        if func == "COUNT" and (target is None or isinstance(target, exp.Star)):
            arg = None
        elif func in ("COUNTIF", "LOGICAL_AND", "LOGICAL_OR"):
            arg = _box(self._pred(target, env, None, None))
        else:
            arg = self._val(target, env, None, None)
        for call in agg.calls:
            same_arg = (call.arg is None and arg is None) or (
                call.arg is not None and arg is not None
                and call.arg.null.eq(arg.null) and call.arg.val.eq(arg.val)
            )
            if call.func == func and call.distinct == distinct and same_arg:
                return call.var
        uid = self.fresh("agg")
        never_null = func in ("COUNT", "COUNTIF")
        var = _Val(z3.BoolVal(False) if never_null else z3.Bool(f"{uid}#null"), z3.Const(uid, V))
        agg.calls.append(_AggCall(func, distinct, arg, var))
        return var


# --------------------------------------------------------------------------
# Prover
# --------------------------------------------------------------------------


def _subst(term, pairs):
    return z3.substitute(term, *pairs) if pairs else term


def _subst_val(v: _Val, pairs) -> _Val:
    return _Val(_subst(v.null, pairs), _subst(v.val, pairs))


def _occ_pairs(src: _Occ, dst: _Occ) -> list:
    pairs = []
    for name, v in list(src.cols.items()):
        target = dst.col(name)
        pairs.append((v.null, target.null))
        pairs.append((v.val, target.val))
    return pairs


class _Prover:
    def __init__(self, timeout_ms: int, constraints: dict[str, TableConstraints] | None = None):
        self.timeout_ms = timeout_ms
        self.constraints = {k.lower(): v for k, v in (constraints or {}).items()}
        self.unknown = False
        self.candidates: list[tuple[object, list[_Occ]]] = []

    @staticmethod
    def _typing(occs: list[_Occ]):
        """Every non-NULL value in one table column has the same type."""

        V = _value_sort()
        by_column: dict[tuple[str, str], list[_Val]] = {}
        for occ in occs:
            for name, v in occ.cols.items():
                by_column.setdefault((occ.table, name), []).append(v)
        facts = []
        for vals in by_column.values():
            for a, b in zip(vals, vals[1:]):
                facts.append(
                    z3.Implies(
                        z3.And(z3.Not(a.null), z3.Not(b.null)),
                        z3.And(V.is_Num(a.val) == V.is_Num(b.val), V.is_Str(a.val) == V.is_Str(b.val)),
                    )
                )
        return facts

    def _constraint_facts(self, occs: list[_Occ]):
        """NOT NULL columns, and rows with equal keys being the same row."""

        facts = []
        if not self.constraints:
            return facts
        for occ in occs:
            constraint = self.constraints.get(occ.table.lower())
            if constraint is None:
                continue
            for name in list(occ.cols):
                if name in constraint.not_null:
                    facts.append(z3.Not(occ.cols[name].null))
        for i, first in enumerate(occs):
            for second in occs[i + 1:]:
                constraint = self.constraints.get(first.table.lower())
                if constraint is None or first.table != second.table or first.opaque or second.opaque:
                    continue
                for key in constraint.keys:
                    same_key = z3.And(
                        *[
                            z3.And(z3.Not(first.col(k).null), z3.Not(second.col(k).null), _null_eq(first.col(k), second.col(k)))
                            for k in key
                        ]
                    )
                    names = sorted(set(first.cols) | set(second.cols))
                    same_row = z3.And(*[_null_eq(first.col(n), second.col(n)) for n in names])
                    facts.append(z3.Implies(same_key, same_row))
        return facts

    def valid(self, formula, occs: list[_Occ], facts=()) -> bool:
        solver = z3.Solver()
        solver.set("timeout", self.timeout_ms)
        solver.add(*self._typing(occs))
        solver.add(*self._constraint_facts(occs))
        solver.add(*facts)
        solver.add(z3.Not(formula))
        result = solver.check()
        if result == z3.unsat:
            return True
        if result == z3.sat:
            self.candidates.append((self._counterexample(solver, occs), list(occs)))
        else:
            self.unknown = True
        return False

    def _counterexample(self, solver, occs):
        """A model of the last satisfiable check, preferring an integral one.

        The plain model is read first: ``_nice_model`` runs further checks, and
        after those the solver holds no model to fall back on.
        """

        base = solver.model()
        return self._nice_model(solver, occs) or base

    def _nice_model(self, solver, occs):
        """Prefer integer-valued counterexamples; they fit INT64 and FLOAT64."""

        V = _value_sort()
        values = [v for occ in occs for v in occ.cols.values()]
        integral = [z3.Implies(V.is_Num(v.val), z3.IsInt(V.num(v.val))) for v in values]
        numeric = [z3.Implies(z3.Not(v.null), V.is_Num(v.val)) for v in values]
        for extra in (integral + numeric, integral):
            solver.push()
            solver.add(*extra)
            model = solver.model() if solver.check() == z3.sat else None
            solver.pop()
            if model is not None:
                return model
        return None

    def witness(self, pred, occs: list[_Occ], facts=()) -> None:
        solver = z3.Solver()
        solver.set("timeout", self.timeout_ms)
        solver.add(*self._typing(occs))
        solver.add(*self._constraint_facts(occs))
        solver.add(*facts)
        solver.add(pred)
        if solver.check() == z3.sat:
            self.candidates.append((self._counterexample(solver, occs), list(occs)))

    def unsatisfiable(self, pred, occs: list[_Occ], facts=()) -> bool:
        solver = z3.Solver()
        solver.set("timeout", self.timeout_ms)
        solver.add(*self._typing(occs))
        solver.add(*self._constraint_facts(occs))
        solver.add(*facts)
        solver.add(pred)
        return solver.check() == z3.unsat

    def merge_key_occurrences(self, block):
        """Fold occurrences of one table that a key forces onto the same row.

        If the block's condition makes two occurrences agree on a key, each
        row of the table pairs with itself exactly once, so the second
        occurrence adds nothing: the multiplicity is unchanged.
        """

        if not self.constraints or any(o.opaque for o in block.occs):
            return block
        changed = True
        while changed:
            changed = False
            for i, first in enumerate(block.occs):
                for second in block.occs[i + 1:]:
                    constraint = self.constraints.get(first.table.lower())
                    if constraint is None or first.table != second.table:
                        continue
                    for key in constraint.keys:
                        same_key = z3.And(
                            *[
                                z3.And(z3.Not(first.col(k).null), z3.Not(second.col(k).null), _null_eq(first.col(k), second.col(k)))
                                for k in key
                            ]
                        )
                        implied = z3.Implies(block.cond.t, same_key)
                        saved = len(self.candidates)
                        if self.valid(implied, block.occs, block.facts):
                            self._merge(block, first, second)
                            changed = True
                            break
                        del self.candidates[saved:]
                    if changed:
                        break
                if changed:
                    break
        return block

    def _merge(self, block, keep: _Occ, drop: _Occ) -> None:
        pairs = _occ_pairs(drop, keep)
        block.occs = [o for o in block.occs if o is not drop]
        block.cond = _Pred(_subst(block.cond.t, pairs), _subst(block.cond.f, pairs))
        block.outputs = [_subst_val(v, pairs) for v in block.outputs]
        block.facts = [_subst(f, pairs) for f in block.facts]
        if isinstance(block, _Agg):
            block.keys = [_subst_val(k, pairs) for k in block.keys]
            for call in block.aggs:
                if call.arg is not None:
                    call.arg = _subst_val(call.arg, pairs)
            if block.having is not None:
                block.having = _Pred(_subst(block.having.t, pairs), _subst(block.having.f, pairs))

    def legal_database(self, db: dict[str, list[dict]]) -> dict[str, list[dict]] | None:
        """Respect declared constraints: ``None`` if the database cannot satisfy them."""

        if not self.constraints:
            return db
        legal = {}
        for table, rows in db.items():
            constraint = self.constraints.get(table.lower())
            if constraint is None:
                legal[table] = rows
                continue
            for row in rows:
                if any(row.get(c, 1) is None for c in constraint.not_null):
                    return None
            if constraint.keys:
                unique = list({tuple(sorted(r.items(), key=lambda kv: kv[0])): r for r in rows}.values())
                for key in constraint.keys:
                    seen = set()
                    for row in unique:
                        value = tuple(row.get(k) for k in key)
                        if any(v is None for v in value):
                            continue
                        if value in seen:
                            return None
                        seen.add(value)
                rows = unique
            legal[table] = rows
        return legal

    # ---- mappings ------------------------------------------------------

    @staticmethod
    def _bijections(occs1: list[_Occ], occs2: list[_Occ]):
        tables1 = Counter(o.table for o in occs1)
        if tables1 != Counter(o.table for o in occs2):
            return
        groups = sorted(tables1)
        per_table = []
        for table in groups:
            left = [o for o in occs1 if o.table == table]
            right = [o for o in occs2 if o.table == table]
            per_table.append([list(zip(right, perm)) for perm in itertools.permutations(left)])
        for count, combo in enumerate(itertools.product(*per_table)):
            if count >= _MAX_MAPPINGS:
                return
            yield [pair for group in combo for pair in group]

    @staticmethod
    def _homomorphisms(src: list[_Occ], dst: list[_Occ]):
        """Maps from src occurrences to dst occurrences of the same table."""

        choices = []
        for occ in src:
            options = [d for d in dst if d.table == occ.table]
            if not options:
                return
            choices.append([(occ, d) for d in options])
        for count, combo in enumerate(itertools.product(*choices)):
            if count >= _MAX_MAPPINGS:
                return
            yield list(combo)

    @staticmethod
    def _pairs(mapping) -> list:
        pairs = []
        for src, dst in mapping:
            pairs.extend(_occ_pairs(src, dst))
        return pairs

    # ---- block comparisons ---------------------------------------------

    def spj_bag_equal(self, a: _Spj, b: _Spj) -> bool:
        for mapping in self._bijections(a.occs, b.occs):
            pairs = self._pairs(mapping)
            facts = a.facts + [_subst(f, pairs) for f in b.facts]
            cond_b = _subst(b.cond.t, pairs)
            outs_b = [_subst_val(v, pairs) for v in b.outputs]
            if not self.valid(a.cond.t == cond_b, a.occs, facts):
                continue
            if self.valid(z3.Implies(a.cond.t, _rows_eq(a.outputs, outs_b)), a.occs, facts):
                return True
        return False

    def spj_set_contained(self, a: _Spj, b: _Spj) -> bool:
        """Every row of ``a`` is a row of ``b`` (set semantics)."""

        for mapping in self._homomorphisms(b.occs, a.occs):
            pairs = self._pairs(mapping)
            facts = a.facts + [_subst(f, pairs) for f in b.facts]
            cond_b = _subst(b.cond.t, pairs)
            outs_b = [_subst_val(v, pairs) for v in b.outputs]
            if self.valid(z3.Implies(a.cond.t, z3.And(cond_b, _rows_eq(a.outputs, outs_b))), a.occs, facts):
                return True
        return False

    def agg_equal(self, a: _Agg, b: _Agg) -> bool:
        if a.is_global != b.is_global or len(a.outputs) != len(b.outputs):
            return False
        for mapping in self._bijections(a.occs, b.occs):
            if self._agg_equal_under(a, b, mapping):
                return True
        return False

    def _agg_equal_under(self, a: _Agg, b: _Agg, mapping) -> bool:
        pairs = self._pairs(mapping)
        facts = a.facts + [_subst(f, pairs) for f in b.facts]
        if not self.valid(a.cond.t == _subst(b.cond.t, pairs), a.occs, facts):
            return False
        keys_b = [_subst_val(k, pairs) for k in b.keys]
        if not a.is_global:
            copies = [o.copy(o.uid + "'") for o in a.occs]
            copy_pairs = []
            for o, c in zip(a.occs, copies):
                copy_pairs.extend(_occ_pairs(o, c))

            def key_eq(keys):
                other = [_subst_val(k, copy_pairs) for k in keys]
                return _rows_eq(keys, other)

            both = z3.And(a.cond.t, _subst(a.cond.t, copy_pairs))
            same_groups = z3.Implies(both, key_eq(a.keys) == key_eq(keys_b))
            copy_facts = facts + [_subst(f, copy_pairs) for f in facts]
            if not self.valid(same_groups, a.occs + copies, copy_facts):
                return False
        # Merge aggregates of ``a`` that always agree, then map ``b`` onto them.
        own_pairs = []
        for later, call in enumerate(a.aggs):
            for earlier in a.aggs[:later]:
                if self._same_aggregate(a, earlier, call.func, call.distinct, call.arg, facts):
                    own_pairs.extend([(call.var.null, earlier.var.null), (call.var.val, earlier.var.val)])
                    break
        own_pairs = [p for p in own_pairs if not p[0].eq(p[1])]
        agg_pairs = []
        for call_b in b.aggs:
            arg_b = _subst_val(call_b.arg, pairs) if call_b.arg is not None else None
            for call_a in a.aggs:
                if self._same_aggregate(a, call_a, call_b.func, call_b.distinct, arg_b, facts):
                    agg_pairs.extend([(call_b.var.null, call_a.var.null), (call_b.var.val, call_a.var.val)])
                    break
        all_pairs = pairs + [p for p in agg_pairs if not p[0].eq(p[1])]
        # Aggregate values are left free: the claim must hold for any group.
        guard = z3.BoolVal(True) if a.is_global else a.cond.t
        outs_a = [_subst_val(v, own_pairs) for v in a.outputs]
        outs_b = [_subst_val(v, own_pairs) for v in (_subst_val(v, all_pairs) for v in b.outputs)]
        if not self.valid(z3.Implies(guard, _rows_eq(outs_a, outs_b)), a.occs, facts):
            return False
        having_a = _subst(a.having.t, own_pairs) if a.having is not None else z3.BoolVal(True)
        having_b = _subst(_subst(b.having.t, all_pairs), own_pairs) if b.having is not None else z3.BoolVal(True)
        return self.valid(z3.Implies(guard, having_a == having_b), a.occs, facts)

    def _same_aggregate(self, a: _Agg, call_a: _AggCall, func: str, distinct: bool, arg: _Val | None, facts) -> bool:
        """Whether ``call_a`` and the other call agree on every group of ``a``."""

        if call_a.func != func or call_a.distinct != distinct:
            return False
        if (call_a.arg is None) != (arg is None):
            # COUNT(x) is COUNT(*) when x is never NULL.
            present = call_a.arg if call_a.arg is not None else arg
            return not distinct and self.valid(z3.Implies(a.cond.t, z3.Not(present.null)), a.occs, facts)
        if arg is None:
            return True
        return self.valid(z3.Implies(a.cond.t, _null_eq(call_a.arg, arg)), a.occs, facts)

    def as_distinct_spj(self, block):
        """``GROUP BY`` with no aggregates is ``SELECT DISTINCT`` when the
        output determines the group key."""

        if not isinstance(block, _Agg) or block.aggs or block.having is not None or block.is_global:
            return block
        copies = [o.copy(o.uid + "'") for o in block.occs]
        copy_pairs = []
        for o, c in zip(block.occs, copies):
            copy_pairs.extend(_occ_pairs(o, c))
        other_out = [_subst_val(v, copy_pairs) for v in block.outputs]
        other_keys = [_subst_val(v, copy_pairs) for v in block.keys]
        formula = z3.Implies(
            z3.And(block.cond.t, _subst(block.cond.t, copy_pairs), _rows_eq(block.outputs, other_out)),
            _rows_eq(block.keys, other_keys),
        )
        saved = len(self.candidates)
        facts = block.facts + [_subst(f, copy_pairs) for f in block.facts]
        if self.valid(formula, block.occs + copies, facts):
            return _Spj(block.occs, block.cond, block.outputs, block.names, distinct=True, facts=block.facts)
        del self.candidates[saved:]
        return block

    def branch_bag_equal(self, a, b) -> bool:
        if isinstance(a, _Spj) and isinstance(b, _Spj):
            if a.distinct != b.distinct:
                return False
            if a.distinct:
                return self.spj_set_contained(a, b) and self.spj_set_contained(b, a)
            return self.spj_bag_equal(a, b)
        if isinstance(a, _Agg) and isinstance(b, _Agg):
            return a.distinct == b.distinct and self.agg_equal(a, b)
        return False

    def branch_set_contained(self, a, b) -> bool:
        if isinstance(a, _Spj) and isinstance(b, _Spj):
            return self.spj_set_contained(a, b)
        if isinstance(a, _Agg) and isinstance(b, _Agg):
            return self.agg_equal(a, b)
        return False


_CONST_PAIRS_NULL = ("COUNT", "COUNTIF")


def _aggregate_of_nothing(call: _AggCall) -> _Val:
    """The value of an aggregate over an empty group."""

    V = _value_sort()
    return _Val(z3.BoolVal(call.func not in _CONST_PAIRS_NULL), V.Num(0))


def _prune(prover: "_Prover", union: _Union) -> None:
    """Drop blocks that cannot return a row; a global aggregate over nothing is one constant row."""

    kept = []
    for block in union.branches:
        if block.occs and any(o.opaque for o in block.occs):
            kept.append(block)
            continue
        if not prover.unsatisfiable(block.cond.t, block.occs, block.facts):
            kept.append(block)
            continue
        if isinstance(block, _Agg) and block.is_global and block.having is None:
            pairs = []
            for call in block.aggs:
                value = _aggregate_of_nothing(call)
                pairs.extend([(call.var.null, value.null), (call.var.val, value.val)])
            outputs = [_subst_val(v, pairs) for v in block.outputs]
            kept.append(_Spj([], _const_pred(True), outputs, block.names, distinct=block.distinct, facts=[]))
        elif isinstance(block, _Agg) and block.is_global:
            kept.append(block)  # HAVING over the empty group: leave to the general path
    union.branches = kept


def _is_set(u: _Union) -> bool:
    return u.distinct or (len(u.branches) == 1 and u.branches[0].distinct)


def _prove(prover: _Prover, left: _Union, right: _Union) -> tuple[bool, str]:
    left.column_names, right.column_names = list(left.names), list(right.names)
    for union in (left, right):
        _prune(prover, union)
        union.branches = [prover.merge_key_occurrences(prover.as_distinct_spj(b)) for b in union.branches]
    if not left.branches and not right.branches:
        return True, "both queries always return no rows"
    if not left.branches or not right.branches:
        return False, "only one query can return rows"
    if _is_set(left) and _is_set(right):
        for a in left.branches:
            if not any(prover.branch_set_contained(a, b) for b in right.branches):
                return False, "a left branch has rows the right side may lack"
        for b in right.branches:
            if not any(prover.branch_set_contained(b, a) for a in left.branches):
                return False, "a right branch has rows the left side may lack"
        return True, "set semantics: each side is contained in the other"
    if _is_set(left) != _is_set(right) and not (left.distinct or right.distinct):
        # One side removes duplicates, the other keeps them.
        return False, "only one side removes duplicate rows"
    if left.distinct or right.distinct or len(left.branches) != len(right.branches):
        return False, "UNION shapes differ"

    def match(i: int, used: frozenset) -> bool:
        if i == len(left.branches):
            return True
        for j, b in enumerate(right.branches):
            if j not in used and prover.branch_bag_equal(left.branches[i], b) and match(i + 1, used | {j}):
                return True
        return False

    if match(0, frozenset()):
        return True, "bag semantics: matching blocks are equivalent"
    return False, "no row-preserving mapping between the queries was found"


# --------------------------------------------------------------------------
# Concrete evaluation (counterexample checking)
# --------------------------------------------------------------------------


def _py(model, term):
    V = _value_sort()
    value = model.eval(term, model_completion=True)
    if z3.is_true(model.eval(V.is_Num(value), model_completion=True)):
        num = model.eval(V.num(value), model_completion=True)
        return Fraction(num.as_fraction()) if z3.is_rational_value(num) else Fraction(num.as_decimal(20).rstrip("?"))
    if z3.is_true(model.eval(V.is_Str(value), model_completion=True)):
        return model.eval(V.str(value), model_completion=True).as_string()
    return z3.is_true(model.eval(V.bool(value), model_completion=True))


def _z3_value(value):
    V = _value_sort()
    if isinstance(value, bool):
        return V.Bool(z3.BoolVal(value))
    if isinstance(value, Fraction):
        return V.Num(z3.RealVal(f"{value.numerator}/{value.denominator}"))
    return V.Str(z3.StringVal(value))


def _cell(model, v: _Val):
    if z3.is_true(model.eval(v.null, model_completion=True)):
        return None
    return _py(model, v.val)


class _NoCandidate(Exception):
    pass


def _eval_union(u: _Union, db: dict, model) -> Counter:
    total: Counter = Counter()
    for branch in u.branches:
        total.update(_eval_block(branch, db, model))
    if u.distinct:
        total = Counter(set(total))
    return total


def _combinations(occs: list[_Occ], db: dict):
    tables = [db.get(o.table, []) for o in occs]
    size = 1
    for rows in tables:
        size *= len(rows)
    if size > _MAX_EVAL_COMBINATIONS:
        raise _NoCandidate()
    for combo in itertools.product(*tables):
        pairs = []
        for occ, row in zip(occs, combo):
            for name, v in occ.cols.items():
                cell = row.get(name)
                if cell is None:
                    pairs.append((v.null, z3.BoolVal(True)))
                    pairs.append((v.val, _value_sort().Num(0)))
                else:
                    pairs.append((v.null, z3.BoolVal(False)))
                    pairs.append((v.val, _z3_value(cell)))
        yield pairs


def _eval_block(block, db: dict, model) -> Counter:
    rows: Counter = Counter()
    if isinstance(block, _Spj):
        for pairs in _combinations(block.occs, db):
            if z3.is_true(model.eval(_subst(block.cond.t, pairs), model_completion=True)):
                rows[tuple(_cell(model, _subst_val(v, pairs)) for v in block.outputs)] += 1
        return Counter(set(rows)) if block.distinct else rows

    groups: dict[tuple, list] = {}
    for pairs in _combinations(block.occs, db):
        if z3.is_true(model.eval(_subst(block.cond.t, pairs), model_completion=True)):
            key = tuple(_hashable(_cell(model, _subst_val(k, pairs))) for k in block.keys)
            groups.setdefault(key, []).append(pairs)
    if block.is_global and not groups:
        groups[()] = []
    for members in groups.values():
        agg_pairs = []
        for call in block.aggs:
            if call.arg is None:
                values = [0] * len(members)
            else:
                values = [_cell(model, _subst_val(call.arg, p)) for p in members]
            result = _aggregate(call, values)
            if not z3.is_false(call.var.null):
                agg_pairs.append((call.var.null, z3.BoolVal(result is None)))
            value = _value_sort().Num(0) if result is None else _z3_value(
                Fraction(result) if isinstance(result, int) and not isinstance(result, bool) else result
            )
            agg_pairs.append((call.var.val, value))
        all_pairs = (members[0] if members else []) + agg_pairs
        if block.having is not None and not z3.is_true(
            model.eval(_subst(block.having.t, all_pairs), model_completion=True)
        ):
            continue
        rows[tuple(_cell(model, _subst_val(v, all_pairs)) for v in block.outputs)] += 1
    return Counter(set(rows)) if block.distinct else rows


def _hashable(value):
    return ("null",) if value is None else (type(value).__name__, value)


def _aggregate(call: _AggCall, values: list):
    if call.func == "COUNT":
        if call.arg is None:
            return len(values)
        present = [v for v in values if v is not None]
        return len(set(map(_hashable, present))) if call.distinct else len(present)
    if call.func == "COUNTIF":
        return sum(1 for v in values if v is True)
    present = [v for v in values if v is not None]
    if call.distinct:
        present = list({_hashable(v): v for v in present}.values())
    if not present:
        return None
    kinds = {type(v) for v in present}
    if len(kinds) != 1:
        raise _NoCandidate()
    kind = kinds.pop()
    if call.func in ("SUM", "AVG"):
        if kind is not Fraction:
            raise _NoCandidate()
        total = sum(present, Fraction(0))
        return total if call.func == "SUM" else total / len(present)
    if call.func == "MIN":
        return min(present)
    if call.func == "MAX":
        return max(present)
    if call.func in ("LOGICAL_AND", "LOGICAL_OR"):
        if kind is not bool:
            raise _NoCandidate()
        return all(present) if call.func == "LOGICAL_AND" else any(present)
    raise _NoCandidate()


def _export(value):
    if isinstance(value, Fraction):
        return int(value) if value.denominator == 1 else float(value)
    return value


def _find_counterexample(prover: _Prover, left: _Union, right: _Union) -> Counterexample | None:
    blocks = list(left.branches) + list(right.branches)
    all_occs = [o for b in blocks for o in b.occs]
    if any(o.opaque for o in all_occs):
        return None
    for block in blocks:
        prover.witness(block.cond.t, block.occs, block.facts)
    empty = z3.Solver()
    empty.check()
    for model, occs in [(empty.model(), [])] + prover.candidates:
        db: dict[str, list[dict]] = {occ.table: [] for occ in all_occs}
        for occ in occs:
            db.setdefault(occ.table, []).append({name: _cell(model, v) for name, v in occ.cols.items()})
        db = prover.legal_database(db)
        if db is None:
            continue
        try:
            left_rows = _eval_union(left, db, model)
            right_rows = _eval_union(right, db, model)
        except _NoCandidate:
            continue
        if left_rows != right_rows:
            tables = {
                table: [{k: _export(v) for k, v in row.items()} for row in rows] for table, rows in db.items()
            }
            return Counterexample(
                tables=tables,
                left_rows=sorted((tuple(_export(v) for v in r) for r in left_rows.elements()), key=repr),
                right_rows=sorted((tuple(_export(v) for v in r) for r in right_rows.elements()), key=repr),
            )
    return None


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------


def prove_equivalent_smt(
    left_sql: str,
    right_sql: str,
    *,
    schema: dict[str, list[str]] | None = None,
    exact_arithmetic: bool = False,
    timeout_ms: int = 5000,
    constraints: dict[str, TableConstraints] | None = None,
    compare_names: bool = True,
    dialect: str = "bigquery",
) -> SmtEquivalenceResult:
    """Prove two BigQuery queries return the same result bag, or refute them.

    ``constraints`` maps table names to ``TableConstraints`` (NOT NULL columns and
    keys) the proof may rely on. ``compare_names=False`` ignores output column
    names, comparing only the rows. ``dialect`` is the sqlglot dialect of the input.

    ``schema`` optionally maps table names (as written in the query, e.g.
    ``"project.dataset.table"``) to column lists; it enables ``SELECT *`` and
    unqualified columns in joins. ``exact_arithmetic`` models ``+``, ``-``
    and ``*`` as exact arithmetic, which is only right for INT64/NUMERIC.
    """

    assumptions = BASE_ASSUMPTIONS + ((EXACT_ARITHMETIC_ASSUMPTION,) if exact_arithmetic else ())
    if z3 is None:
        return SmtEquivalenceResult(
            SmtStatus.NOT_PROVEN, "z3-solver is not installed (pip install kumosql[smt])"
        )
    compiler = _Compiler(schema, exact_arithmetic, dialect)
    try:
        left = compiler.compile(left_sql)
        right = compiler.compile(right_sql)
    except Unsupported as error:
        return SmtEquivalenceResult(SmtStatus.NOT_PROVEN, f"unsupported: {error}", assumptions=assumptions)
    except sqlglot.errors.ParseError as error:
        return SmtEquivalenceResult(SmtStatus.NOT_PROVEN, f"parse error: {error}", assumptions=assumptions)

    if len(left.names) != len(right.names):
        return SmtEquivalenceResult(
            SmtStatus.NOT_PROVEN,
            f"different column counts ({len(left.names)} vs {len(right.names)})",
            assumptions=assumptions,
        )
    for position, (a, b) in enumerate(zip(left.names, right.names), start=1):
        if compare_names and a != b:
            return SmtEquivalenceResult(
                SmtStatus.NOT_PROVEN,
                f"column {position} is named {a or '(unnamed)'} on the left and {b or '(unnamed)'} on the right",
                assumptions=assumptions,
            )

    prover = _Prover(timeout_ms, constraints)
    proven, reason = _prove(prover, left, right)
    if proven:
        return SmtEquivalenceResult(SmtStatus.PROVEN_EQUIVALENT, reason, assumptions=assumptions)
    if not compiler.uses_uf:
        counterexample = _find_counterexample(prover, left, right)
        if counterexample is not None:
            return SmtEquivalenceResult(
                SmtStatus.NOT_EQUIVALENT,
                "the queries differ on the attached database",
                counterexample=counterexample,
                assumptions=assumptions,
            )
    if prover.unknown:
        reason += " (the solver timed out on some checks)"
    return SmtEquivalenceResult(SmtStatus.NOT_PROVEN, reason, assumptions=assumptions)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Prove two BigQuery queries equivalent with Z3.")
    parser.add_argument("left", help="path to the left SQL file")
    parser.add_argument("right", help="path to the right SQL file")
    parser.add_argument("--schema", help="JSON file mapping table names to column lists")
    parser.add_argument("--exact-arithmetic", action="store_true", help="model + - * exactly (INT64/NUMERIC)")
    parser.add_argument("--timeout-ms", type=int, default=5000)
    args = parser.parse_args(argv)
    with open(args.left, encoding="utf-8") as handle:
        left = handle.read()
    with open(args.right, encoding="utf-8") as handle:
        right = handle.read()
    schema = None
    if args.schema:
        with open(args.schema, encoding="utf-8") as handle:
            schema = json.load(handle)
    result = prove_equivalent_smt(
        left, right, schema=schema, exact_arithmetic=args.exact_arithmetic, timeout_ms=args.timeout_ms
    )
    payload = {
        "status": result.status.value,
        "reason": result.reason,
        "assumptions": list(result.assumptions),
    }
    if result.counterexample is not None:
        payload["counterexample"] = {
            "tables": result.counterexample.tables,
            "left_rows": [list(r) for r in result.counterexample.left_rows],
            "right_rows": [list(r) for r in result.counterexample.right_rows],
        }
    json.dump(payload, sys.stdout, indent=2, default=str)
    sys.stdout.write("\n")
    return 0 if result.proven else 1
