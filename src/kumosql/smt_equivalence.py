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
import dataclasses
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


_UNNEST_TABLE = "$unnest"


class _Env(dict):
    """Alias -> source for one query scope; ``outer`` is the enclosing scope of a subquery."""

    outer: "_Env | None" = None


class Unsupported(Exception):
    """The query uses something outside the modeled subset."""


def _canonical_aliases(body: exp.Expression, schema: dict[str, list[str]] | None = None) -> exp.Expression:
    """A copy of ``body`` whose tables and derived tables carry positional aliases (``kq0``, ``kq1``..),
    so two spellings of the same relation get the same identity. A column is renamed through the
    nearest enclosing SELECT that declares its qualifier, so an alias reused in a nested scope is fine.
    With a schema, a bare column of a select over one known table is qualified first."""

    body = body.copy()
    ctes = {c.alias_or_name.lower() for c in body.find_all(exp.CTE)}
    fresh: dict[int, str] = {}
    for node in body.walk():
        if not isinstance(node, (exp.Table, exp.Subquery)) or isinstance(node.parent, exp.Table):
            continue
        if not node.parent or not isinstance(node.parent, (exp.From, exp.Join)):
            continue
        old = node.alias_or_name
        if not old or old.lower() in ctes or (isinstance(node, exp.Subquery) and not node.alias):
            continue
        fresh[id(node)] = f"kq{len(fresh)}"

    def declared(select: exp.Select) -> list[exp.Expression]:
        from_ = select.args.get("from_") or select.args.get("from")
        return ([from_.this] if from_ is not None else []) + [j.this for j in select.args.get("joins") or []]

    renames: list[tuple[exp.Column, str]] = []
    for column in body.find_all(exp.Column):
        qualifier = column.table.lower()
        if not qualifier and schema:
            scope = column.find_ancestor(exp.Select)
            sources = declared(scope) if scope is not None else []
            if len(sources) == 1 and isinstance(sources[0], exp.Table) and not isinstance(column.this, exp.Star):
                key = ".".join(p.name for p in sources[0].parts).lower()
                known = schema.get(key)
                if known is not None and column.name.lower() in [c.lower() for c in known]:
                    qualifier = (sources[0].alias_or_name or "").lower()
                    column.set("table", exp.to_identifier(sources[0].alias_or_name))
        if not qualifier:
            continue
        scope = column.find_ancestor(exp.Select)
        while scope is not None:
            hit = next((n for n in declared(scope) if (n.alias_or_name or "").lower() == qualifier), None)
            if hit is not None:
                if id(hit) in fresh:
                    renames.append((column, fresh[id(hit)]))
                break
            scope = scope.find_ancestor(exp.Select)
    for column, name in renames:
        column.set("table", exp.to_identifier(name))
    for node in body.walk():
        if id(node) in fresh:
            node.set("alias", exp.TableAlias(this=exp.to_identifier(fresh[id(node)])))
    return body


def _is_ground(term) -> bool:
    """Whether a Z3 term mentions no uninterpreted constant (it has one fixed value)."""

    stack = [term]
    while stack:
        node = stack.pop()
        if z3.is_const(node) and node.decl().kind() == z3.Z3_OP_UNINTERPRETED:
            return False
        stack.extend(node.children())
    return True


class _SetSourceUnresolved(Exception):
    """A DISTINCT derived table was not joined on all of its columns."""


class _Unknown(Exception):
    """The solver timed out."""


# --------------------------------------------------------------------------
# Z3 value domain
# --------------------------------------------------------------------------

_VALUE_SORT = None
_STRING_RANK = None


def _string_rank():
    """An order-embedding of strings into the reals: ``a < b`` iff ``rank(a) < rank(b)``.

    Z3's own string ordering takes seconds even for ``d < '1994-12-01'``. The
    prover only needs the ordering facts the queries use (see ``order_facts``),
    which hold in the real ordering, so a proof over the abstraction holds there too.
    """

    global _STRING_RANK
    if _STRING_RANK is None:
        _STRING_RANK = z3.Function("string_rank", z3.StringSort(), z3.RealSort())
    return _STRING_RANK


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
            _string_rank()(V.str(a)) < _string_rank()(V.str(b)),
            z3.If(
                z3.And(V.is_Bool(a), V.is_Bool(b)),
                z3.And(z3.Not(V.bool(a)), V.bool(b)),
                rank(a) < rank(b),
            ),
        ),
    )


def _rank_injective(a, b):
    """Equal ranks mean equal strings (the ranking is one-to-one), stated for the pair ``a``, ``b``."""

    V, rank = _value_sort(), _string_rank()
    sa, sb = V.str(a), V.str(b)
    return z3.Implies(rank(sa) == rank(sb), sa == sb)


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
class _State:
    """One way a FROM clause can produce rows (outer joins split into several)."""

    occs: list
    conds: list
    env: "_Env"
    subs: list = field(default_factory=list)

    def cond(self) -> "_Pred":
        result = _const_pred(True)
        for c in self.conds:
            result = _Pred(z3.And(result.t, c.t), z3.Or(result.f, c.f))
        return result


@dataclass
class _Sub:
    """An existence test: ``atom`` is TRUE iff some row of ``occs`` satisfies ``guard``.

    ``atom`` is a free Boolean in the formulas that mention it. Two subqueries
    proved to test the same thing may share an atom; nested tests inside the
    guard are in ``nested``.
    """

    atom: object
    occs: list
    guard: object
    nested: list = field(default_factory=list)
    # (block, [column values]) for a DISTINCT derived table read as a set: the
    # columns are free until the join pins them (see ``resolve_set_sources``).
    setsrc: object = None


@dataclass
class _Spj:
    occs: list[_Occ]
    cond: _Pred
    outputs: list[_Val]
    names: list[str]
    distinct: bool = False
    facts: list = field(default_factory=list)
    subs: list = field(default_factory=list)


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
    subs: list = field(default_factory=list)


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


def _is_limit_zero(node: exp.Expression) -> bool:
    limit = node.args.get("limit")
    value = limit.expression if limit is not None else None
    return isinstance(value, exp.Literal) and not value.is_string and value.this == "0"


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
        # Existence tests (EXISTS, IN) found while compiling the current select.
        self.collector: list = []
        self.ctes: dict = {}
        # Canonical SQL of each opaque derived relation, by identity key.
        self.opaque_bodies: dict[str, tuple[str, int]] = {}
        # Opaque derived relations that are sets (DISTINCT, or grouped on exactly their outputs).
        self.opaque_sets: set[str] = set()
        # Read DISTINCT derived tables as sets (existence tests) instead of opaque relations.
        self.semijoin = True
        self.used_setsrc = False
        self.limit_opaque = False
        self.window_opaque = False
        # Whether values are ordered (<, MIN, ..): then string ordering facts are needed.
        self.ordered = False
        self.string_literals: set[str] = set()

    def fresh(self, prefix: str) -> str:
        return f"{prefix}{next(self.counter)}"

    # ---- queries -------------------------------------------------------

    def compile(self, sql: str) -> _Union:
        statements = [s for s in sqlglot.parse(sql, read=self.dialect) if s is not None]
        if len(statements) != 1:
            raise Unsupported(f"expected one statement, found {len(statements)}")
        statement = statements[0]
        self._check_nondeterminism(statement)
        self.window_opaque = any(statement.find_all(exp.Window))
        return self._query(statement, {})

    def _check_nondeterminism(self, node: exp.Expression, allow_windows: bool = False) -> None:
        for sub in node.walk():
            name = type(sub).__name__
            if name in _NONDETERMINISTIC_TYPES:
                raise Unsupported(f"nondeterministic: {sub.sql(dialect='bigquery')}")
            if isinstance(sub, exp.Anonymous) and (sub.name or "").upper() in _NONDETERMINISTIC_NAMES:
                raise Unsupported(f"nondeterministic: {sub.sql(dialect='bigquery')}")
            if isinstance(sub, exp.Window) and not allow_windows:
                # The normalizer isolates window functions in a derived table named kqw*; it is kept whole.
                owner = sub.find_ancestor(exp.Select)
                holder = owner.parent if owner is not None else None
                if not (isinstance(holder, exp.Subquery) and (holder.alias or "").startswith("kqw")):
                    raise Unsupported(f"{name.upper()} is not modeled")

    def _query(self, node: exp.Expression, ctes: dict) -> _Union:
        while isinstance(node, exp.Subquery):
            node = node.this
        if node.args.get("limit") or node.args.get("offset"):
            if _is_limit_zero(node):
                # Nothing survives LIMIT 0: the columns, with no rows.
                bare = node.copy()
                for key in ("limit", "offset", "order"):
                    bare.set(key, None)
                result = self._query(bare, ctes)
                result.column_names = list(result.names)
                result.branches = []
                return result
            raise Unsupported("LIMIT is not modeled")
        with_clause = _with_clause(node)
        if with_clause is not None:
            if with_clause.args.get("recursive"):
                raise Unsupported("recursive CTEs")
            ctes = dict(ctes)
            for cte in with_clause.expressions:
                ctes[cte.alias_or_name.lower()] = (cte.this, dict(ctes))
        if type(node) in (exp.Intersect, exp.Except):
            return self._set_operation(node, ctes)
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
            result = self._select(node, ctes)
            return result if isinstance(result, _Union) else _Union([result], False)
        raise Unsupported(f"{type(node).__name__} statements")

    def _set_operation(self, node: exp.Expression, ctes: dict) -> _Union:
        """``A INTERSECT B`` and ``A EXCEPT B`` (set semantics) as existence tests.

        Each row of ``A`` is kept (INTERSECT) or dropped (EXCEPT) when some row
        of ``B`` equals it, NULLs comparing equal; the result has no duplicates.
        """

        if not node.args.get("distinct", True) or node.args.get("by_name") or node.args.get("side"):
            raise Unsupported(f"{type(node).__name__.upper()} ALL")
        left = self._query(node.this, ctes)
        right = self._query(node.expression, ctes)
        if len(left.names) != len(right.names) or any(
            len(b.outputs) != len(left.branches[0].outputs) for b in left.branches + right.branches
        ):
            raise Unsupported("set operation branches have different widths")
        if any(not isinstance(b, _Spj) for b in left.branches + right.branches):
            raise Unsupported("set operation over an aggregate")
        blocks = []
        for a in left.branches:
            subs = list(a.subs)
            atoms = []
            for b in right.branches:
                atom = z3.Bool(f"{self.fresh('ex')}#exists")
                guard = z3.And(b.cond.t, _rows_eq(a.outputs, b.outputs))
                subs.append(_Sub(atom, list(b.occs), guard, list(b.subs)))
                atoms.append(atom)
            found = z3.Or(*atoms) if atoms else z3.BoolVal(False)
            keep = found if type(node) is exp.Intersect else z3.Not(found)
            cond = _Pred(z3.And(a.cond.t, keep), z3.Not(z3.And(a.cond.t, keep)))
            blocks.append(
                _Spj(list(a.occs), cond, a.outputs, a.names, distinct=False, facts=a.facts + [f for b in right.branches for f in b.facts], subs=subs)
            )
        return _Union(blocks, True, column_names=list(left.names))

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
        outer_collector, self.collector = self.collector, []
        outer_ctes, self.ctes = self.ctes, ctes
        try:
            return self._select_body(node, ctes, distinct_node, facts_start)
        finally:
            self.collector = outer_collector
            self.ctes = outer_ctes

    def _scan(self, node: exp.Select, ctes: dict, outer: "_Env | None" = None) -> list[_State]:
        """Sources, joins and WHERE of one select, as one state per outer-join case."""

        first = _State([], [], _Env(), [])
        first.env.outer = outer
        states = [first]
        from_clause = _from_clause(node)
        items = []
        if from_clause is not None:
            items.append((from_clause.this, None))
            for join in node.args.get("joins") or []:
                kind = (join.args.get("kind") or "").upper()
                side = (join.args.get("side") or "").upper()
                if join.args.get("method") or join.args.get("using") or kind not in ("", "INNER", "CROSS", "OUTER"):
                    raise Unsupported(f"join: {join.sql(dialect='bigquery')}")
                if side not in ("", "LEFT", "RIGHT", "FULL"):
                    raise Unsupported(f"join: {join.sql(dialect='bigquery')}")
                if kind == "OUTER" and not side:
                    raise Unsupported(f"join: {join.sql(dialect='bigquery')}")
                items.append((join.this, join))
        for source_node, join in items:
            side = (join.args.get("side") or "").upper() if join is not None else ""
            if isinstance(source_node, exp.Unnest):
                if side or (join is not None and join.args.get("on") is not None) or len(source_node.expressions) != 1:
                    raise Unsupported("UNNEST outside a cross join")
                alias_node = source_node.args.get("alias")
                alias = (alias_node.name if alias_node is not None else "") or self.fresh("unnest")
                alias = alias.lower()
                columns = alias_node.args.get("columns") if alias_node is not None else None
                element = (columns[0].name if columns else alias).lower()
                offset_arg = source_node.args.get("offset")
                offset = ((offset_arg.name if isinstance(offset_arg, exp.Expression) and offset_arg.name else "offset") if offset_arg else "").lower()
                occ = _Occ(_UNNEST_TABLE, self.fresh("u"), ["arr", "value", "offset"])
                cols = {element: occ.col("value")}
                if offset:
                    cols[offset] = occ.col("offset")
                source = _Source(cols=cols, order=list(cols))
                new_states = []
                for st in states:
                    if alias in st.env:
                        raise Unsupported(f"duplicate alias {alias}")
                    saved, self.collector = self.collector, st.subs
                    try:
                        array = self._val(source_node.expressions[0], st.env, None, None)
                    finally:
                        self.collector = saved
                    arr = occ.col("arr")
                    linked = z3.And(z3.Not(array.null), z3.Not(arr.null), array.val == arr.val)
                    joined_env = _Env(st.env)
                    joined_env.outer = st.env.outer
                    joined_env[alias] = source
                    new_states.append(_State(st.occs + [occ], st.conds + [_Pred(linked, z3.Not(linked))], joined_env, list(st.subs)))
                states = new_states
                continue
            b_occs: list[_Occ] = []
            b_conds: list[_Pred] = []
            b_subs: list = []
            saved, self.collector = self.collector, b_subs
            try:
                alias, source = self._source(source_node, ctes, b_occs, b_conds)
            finally:
                self.collector = saved
            on = join.args.get("on") if join is not None else None
            new_states: list[_State] = []
            for st in states:
                if alias in st.env:
                    raise Unsupported(f"duplicate alias {alias}")
                joined_env = _Env(st.env)
                joined_env.outer = st.env.outer
                joined_env[alias] = source
                on_subs: list = []
                saved, self.collector = self.collector, on_subs
                try:
                    c = self._pred(on, joined_env, None, None) if on is not None else _const_pred(True)
                finally:
                    self.collector = saved
                new_states.append(
                    _State(st.occs + b_occs, st.conds + b_conds + ([c] if on is not None else []), joined_env, st.subs + b_subs + on_subs)
                )
                if side in ("LEFT", "FULL"):
                    atom = z3.Bool(f"{self.fresh('ex')}#exists")
                    guard = z3.And(*[x.t for x in b_conds], c.t)
                    anti = _Sub(atom, list(b_occs), guard, b_subs + on_subs)
                    env2 = _Env(st.env)
                    env2.outer = st.env.outer
                    env2[alias] = self._null_source(source)
                    new_states.append(
                        _State(list(st.occs), st.conds + [_Pred(z3.Not(atom), atom)], env2, st.subs + [anti])
                    )
                if side in ("RIGHT", "FULL"):
                    atom = z3.Bool(f"{self.fresh('ex')}#exists")
                    guard = z3.And(*[x.t for x in st.conds], c.t)
                    anti = _Sub(atom, list(st.occs), guard, st.subs + on_subs)
                    env2 = self._null_env(st.env)
                    env2[alias] = source
                    new_states.append(
                        _State(list(b_occs), b_conds + [_Pred(z3.Not(atom), atom)], env2, b_subs + [anti])
                    )
            states = new_states
        if len(states) > 1 and outer is not None:
            raise Unsupported("outer join inside a subquery")
        for st in states:
            if node.args.get("where") is not None:
                saved, self.collector = self.collector, st.subs
                try:
                    st.conds.append(self._pred(node.args["where"].this, st.env, None, None))
                finally:
                    self.collector = saved
        return states

    def _null_source(self, source: "_Source") -> "_Source":
        V = _value_sort()
        null = _Val(z3.BoolVal(True), V.Num(0))
        names = [name for name, _ in source.star()]
        return _Source(cols={name: null for name in names}, order=list(names))

    def _null_env(self, env: "_Env") -> "_Env":
        """The scope with every source's columns replaced by NULL (the unmatched side of an outer join)."""

        result = _Env()
        result.outer = env.outer
        for alias, source in env.items():
            result[alias] = self._null_source(source)
        return result

    def _select_body(self, node: exp.Select, ctes: dict, distinct_node, facts_start: int):
        states = self._scan(node, ctes)
        blocks = []
        for st in states:
            saved, self.collector = self.collector, st.subs
            try:
                blocks.append(self._finish_select(node, distinct_node, facts_start, st, len(states) > 1))
            finally:
                self.collector = saved
        if len(blocks) == 1:
            return blocks[0]
        union = _Union(blocks, distinct_node is not None)
        for block in blocks:
            block.distinct = False
        return union

    def _finish_select(self, node: exp.Select, distinct_node, facts_start: int, state: _State, split: bool):
        occs, cond, env = state.occs, state.cond(), state.env
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
        if is_agg and split:
            raise Unsupported("aggregate over an outer join")
        if not is_agg:
            return _Spj(
                occs, cond, outputs, names, distinct=distinct_node is not None, facts=self.facts[facts_start:],
                subs=state.subs,
            )

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
            subs=state.subs,
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
            alias = node.alias_or_name.lower() or self.fresh("anon")
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
            self.collector.extend(branch.subs)
            return _Source(cols=dict(zip(branch.names, branch.outputs)), order=list(branch.names))
        if self.semijoin and len(sub.branches) == 1 and not sub.distinct:
            branch = sub.branches[0]
            is_set = isinstance(branch, _Spj) and branch.distinct
            is_set = is_set or (isinstance(branch, _Agg) and not branch.aggs and branch.having is None and not branch.is_global)
            names = list(branch.names)
            if is_set and names and len(set(names)) == len(names) and "" not in names:
                return self._set_source(branch, names, conds)
        return self._opaque(body, ctes, occs)

    def _set_source(self, branch, names, conds) -> _Source:
        """A DISTINCT (or key-only GROUP BY) derived table as an existence test.

        Its columns are free values; the atom says some row of the table has
        exactly those values. Joined on all of its columns, each outer row matches
        at most one row, so the join is that test (``resolve_set_sources``).
        """

        V = _value_sort()
        uid = self.fresh("set")
        columns = [_Val(z3.BoolVal(False), z3.Const(f"{uid}.{i}", V)) for i in range(len(names))]
        match = [z3.And(z3.Not(o.null), o.val == c.val) for o, c in zip(branch.outputs, columns)]
        atom = z3.Bool(f"{self.fresh('ex')}#exists")
        guard = z3.And(branch.cond.t, *match)
        self.collector.append(_Sub(atom, list(branch.occs), guard, list(branch.subs), setsrc=(branch, columns)))
        conds.append(_Pred(atom, z3.Not(atom)))
        self.used_setsrc = True
        return _Source(cols=dict(zip(names, columns)), order=list(names))

    def _opaque(self, body, ctes, occs) -> _Source:
        """A derived relation kept whole: identified by its CTE-expanded SQL."""

        body = self._expand_ctes(body.copy(), ctes)
        self._check_nondeterminism(body, allow_windows=True)
        self.window_opaque = self.window_opaque or any(body.find_all(exp.Window))
        if any(isinstance(node, (exp.Limit, exp.Offset)) for node in body.walk()):
            self.limit_opaque = True
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
        canonical = _canonical_aliases(body, self.schema)
        root = canonical
        while isinstance(root, exp.Subquery):
            root = root.this
        if isinstance(root, exp.Select) and len(root.expressions) == len(names):
            # Columns are identified by position, so two spellings of the output names are one relation,
            # unless the body refers to its own output names (GROUP BY alias, HAVING alias).
            in_select = {id(c) for item in root.expressions for c in item.find_all(exp.Column)}
            outside = {c.name.lower() for c in root.find_all(exp.Column) if not c.table and id(c) not in in_select}
            if not outside & set(names):
                root.set(
                    "expressions",
                    [exp.alias_((i.this if isinstance(i, exp.Alias) else i).copy(), f"c{n}") for n, i in enumerate(root.expressions)],
                )
        key = "(" + canonical.sql(dialect="bigquery", normalize_functions="upper") + ")"
        occ = _Occ(key, self.fresh("d"), names, opaque=True)
        occs.append(occ)
        self.opaque_bodies[key] = (key[1:-1], len(names))
        if isinstance(inner, exp.Select) and _selects_a_set(inner):
            self.opaque_sets.add(key)
        return _Source(cols={name: occ.col(f"c{i}") for i, name in enumerate(names)}, order=list(names))

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
            scope = env
            while scope is not None and table not in scope:
                scope = getattr(scope, "outer", None)
            if scope is None:
                raise Unsupported(f"unknown alias {table}")
            val = scope[table].lookup(name)
            if val is None:
                raise Unsupported(f"{table}.{name} is not a column of {table}")
            return val
        definite = [s for s in env.values() if s.may_have(name) is True]
        maybe = [s for s in env.values() if s.may_have(name) is None]
        outer = getattr(env, "outer", None)
        if not definite and not maybe and outer is not None:
            return self._column(col, outer, aliases)
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
            if e.args.get("query") is not None and e.args.get("unnest") is None and e.args.get("field") is None:
                return self._in_subquery(e, env, agg, aliases)
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
        if isinstance(e, exp.Exists):
            atom = self._existence(e.this, env)
            return _Pred(atom, z3.Not(atom))
        if isinstance(e, (exp.Subquery, exp.Select)):
            raise Unsupported("predicate subqueries")
        v = self._val(e, env, agg, aliases)
        V = _value_sort()
        # Only BOOL values are TRUE or FALSE; other types cannot reach here in
        # a valid query, so they are modeled as neither.
        known = z3.And(z3.Not(v.null), V.is_Bool(v.val))
        return _Pred(z3.And(known, V.bool(v.val)), z3.And(known, z3.Not(V.bool(v.val))))

    def _existence(self, query, env, match=None) -> object:
        """A Boolean that is TRUE iff the subquery returns a row satisfying ``match``.

        ``match`` maps the subquery's scope to an extra condition (used by IN).
        Correlated column references resolve through ``env``.
        """

        node = query
        while isinstance(node, exp.Subquery):
            node = node.this
        if not isinstance(node, exp.Select):
            raise Unsupported(f"{type(node).__name__} inside a predicate subquery")
        for key in ("group", "having", "qualify", "windows", "limit", "offset", "laterals", "pivots", "with_", "with"):
            if node.args.get(key):
                raise Unsupported(f"{key.upper()} inside a predicate subquery")
        if node.args.get("kind"):
            raise Unsupported("subquery shape")
        outer_collector, self.collector = self.collector, []
        try:
            states = self._scan(node, self.ctes, env)
            if len(states) != 1:
                raise Unsupported("outer join inside a subquery")
            occs, cond, inner = states[0].occs, states[0].cond(), states[0].env
            self.collector.extend(states[0].subs)
            guard = cond.t if match is None else z3.And(cond.t, match(inner, node))
            nested = self.collector
        finally:
            self.collector = outer_collector
        atom = z3.Bool(f"{self.fresh('ex')}#exists")
        self.collector.append(_Sub(atom, occs, guard, nested))
        return atom

    def _in_subquery(self, e, env, agg, aliases) -> _Pred:
        """``x IN (SELECT y ...)`` as two existence tests.

        TRUE iff some y equals x; FALSE iff every y compares FALSE with x (so an
        empty subquery gives FALSE and a NULL on either side gives UNKNOWN).
        """

        query = e.args["query"]
        inner_node = query
        while isinstance(inner_node, exp.Subquery):
            inner_node = inner_node.this
        lefts = list(e.this.expressions) if isinstance(e.this, exp.Tuple) else [e.this]
        if not isinstance(inner_node, exp.Select) or len(inner_node.expressions) != len(lefts):
            raise Unsupported("IN subquery shape")
        if any(isinstance(i, exp.Star) for i in inner_node.expressions) or any(
            call.find_ancestor(exp.Select) is inner_node for call in inner_node.find_all(exp.AggFunc)
        ):
            raise Unsupported("IN subquery shape")
        left_values = [self._val(item, env, agg, aliases) for item in lefts]

        def compare(scope, node):
            tests = []
            for left, item in zip(left_values, node.expressions):
                right = self._val(item.this if isinstance(item, exp.Alias) else item, scope, None, None)
                tests.append(self._compare("=", left, right))
            # A row value equals another when every pair is equal; it differs when some pair differs.
            return _Pred(z3.And(*[t.t for t in tests]), z3.Or(*[t.f for t in tests]))

        true_atom = self._existence(query, env, lambda scope, node: compare(scope, node).t)
        unknown_or_true = self._existence(query, env, lambda scope, node: z3.Not(compare(scope, node).f))
        return _Pred(true_atom, z3.Not(unknown_or_true))

    def _compare(self, op: str, a: _Val, b: _Val) -> _Pred:
        if op not in ("=", "<>"):
            self.ordered = True
            # Equal ranks mean equal strings (the ranking is one-to-one); stated for the compared pair.
            self.facts.append(_rank_injective(a.val, b.val))
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
        if isinstance(e, exp.Exists):
            # EXISTS is never UNKNOWN: a Boolean value built on the existence atom
            atom = self._existence(e.this, env)
            return _Val(z3.BoolVal(False), V.Bool(atom))
        if isinstance(e, exp.Array) and not any(isinstance(n, exp.Column) for n in e.walk()):
            return _Val(z3.BoolVal(False), V.Str(z3.StringVal("\x00array:" + e.sql(dialect="bigquery"))))
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
            self.ordered = True
            self.facts.append(_rank_injective(_canon(a), _canon(b)))
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
            self.string_literals.add(text)
            if _DATEISH.match(text) and not _CANONICAL_DATE.match(text):
                raise Unsupported(f"date/time-like literal {text!r} (only 'YYYY-MM-DD' is modeled)")
            return _Val(z3.BoolVal(False), V.Str(z3.StringVal(text)))
        value = _parse_number(e.this)
        if negate:
            value = -value
        return _Val(z3.BoolVal(False), V.Num(z3.RealVal(f"{value.numerator}/{value.denominator}")))

    def order_facts(self) -> list:
        """Facts about the string ordering that the compiled queries may rely on."""

        if not self.ordered or not self.string_literals:
            return []
        rank = _string_rank()
        literals = sorted(self.string_literals)
        facts = [rank(z3.StringVal(a)) < rank(z3.StringVal(b)) for a, b in zip(literals, literals[1:])]
        return facts

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
        if func == "AVG" and e.this is not None and not any(e.args.get(k) for k in ("having_max", "ignore_nulls", "order", "limit", "separator")):
            # AVG(x) is SUM(x) / COUNT(x): NULL when no value is present, and shared with a spelled-out quotient.
            total = self._aggregate("SUM", exp.Sum(this=e.this.copy()), env, agg)
            count = self._aggregate("COUNT", exp.Count(this=e.this.copy()), env, agg)
            null_fn, val_fn = self._function("Div", 2)
            args = self._uf_args([total, count])
            return _Val(z3.Or(total.null, null_fn(*args)), val_fn(*args))
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
        all_null = arg is not None and func in ("COUNT", "SUM", "MIN", "MAX", "AVG") and z3.is_true(z3.simplify(arg.null))
        if all_null:
            # An aggregate of values that are all NULL: COUNT is 0, the others are NULL. The call is still
            # registered, so the query keeps its aggregate shape (one row for a global aggregate).
            uid = self.fresh("agg")
            agg.calls.append(_AggCall(func, distinct, arg, _Val(z3.BoolVal(False), z3.Const(uid, V))))
            return _Val(z3.BoolVal(False), V.Num(0)) if func == "COUNT" else _Val(z3.BoolVal(True), V.Num(0))
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


def _with_atoms(block, pairs):
    """``block`` with its existence atoms replaced (condition and HAVING only)."""

    if not pairs:
        return block
    changes = {"cond": _Pred(_subst(block.cond.t, pairs), _subst(block.cond.f, pairs))}
    if isinstance(block, _Agg) and block.having is not None:
        changes["having"] = _Pred(_subst(block.having.t, pairs), _subst(block.having.f, pairs))
    return dataclasses.replace(block, **changes)


def _subst_sub(sub: _Sub, pairs) -> _Sub:
    return _Sub(
        sub.atom,
        sub.occs,
        _subst(sub.guard, pairs),
        [_subst_sub(n, pairs) for n in sub.nested],
        sub.setsrc,
    )


def _pinned_guard(sub: _Sub, pairs, ids) -> object:
    """The guard of a set-source test once its columns are pinned; the pinned outer columns are known non-NULL."""

    known = [
        z3.Not(z3.Bool(pin.decl().name() + "#null"))
        for _, pin in pairs
        if z3.is_const(pin) and pin.decl().kind() == z3.Z3_OP_UNINTERPRETED
    ]
    return z3.And(_subst(sub.guard, pairs), *known)


def _atom_copy_pairs(subs: list, tag: str) -> list:
    """Fresh atoms for a copy of a block: an existence test depends on the row, so a
    second row cannot share the first row's atoms."""

    return [(sub.atom, z3.Bool(f"{sub.atom}{tag}")) for sub in subs]


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
        # UNNEST is read as a table of (array, element, offset) rows with one row per array and offset.
        self.constraints[_UNNEST_TABLE] = TableConstraints(keys=(("arr", "offset"),))
        self.unknown = False
        self.opaque_sets: set[str] = set()
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

    def unify_subs(self, a_subs: list, b_subs: list, base_pairs: list, scope: list, facts):
        """Group equivalent existence tests so equal tests share one atom.

        Returns substitutions for ``a``'s atoms and for ``b``'s atoms (after the
        outer variables of ``b`` are mapped by ``base_pairs``). Tests with no
        equivalent partner keep their own free atom, which can only make a
        proof fail, never succeed.
        """

        moved = [_subst_sub(sb, base_pairs) for sb in b_subs]
        entries = [(sub, "a") for sub in a_subs] + [(sub, "b") for sub in moved]
        reps: list[tuple[_Sub, object]] = []
        a_pairs, b_pairs = [], []
        for sub, side in entries:
            rep = None
            for known, atom in reps:
                if self._sub_implies(known, sub, scope, facts) and self._sub_implies(sub, known, scope, facts):
                    rep = atom
                    break
            if rep is None:
                reps.append((sub, sub.atom))
                rep = sub.atom
            if not rep.eq(sub.atom):
                (a_pairs if side == "a" else b_pairs).append((sub.atom, rep))
        # ``b`` atoms keep their identity in ``b_subs``; moved copies share atoms.
        return a_pairs, b_pairs

    def _sub_implies(self, sa: _Sub, sb: _Sub, scope: list, facts) -> bool:
        """Every row satisfying ``sa`` forces some row satisfying ``sb`` (same outer variables)."""

        for mapping in self._homomorphisms(sb.occs, sa.occs):
            pairs = self._pairs(mapping)
            a_nested, b_nested = self.unify_subs(sa.nested, sb.nested, pairs, scope + sa.occs, facts)
            guard_a = _subst(sa.guard, a_nested)
            target = _subst(sb.guard, pairs + b_nested)
            saved = len(self.candidates)
            if self.valid(z3.Implies(guard_a, target), scope + sa.occs + sb.occs, facts):
                return True
            del self.candidates[saved:]
        return False

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

    def inline_unique_subs(self, block, require_unique: bool = True):
        """Turn a required existence test into a join (when it has at most one witness).

        With ``require_unique=False`` (valid only where duplicate rows do not
        matter, i.e. under DISTINCT) any required existence test becomes a join.

        ``EXISTS (SELECT .. FROM u WHERE u.id = t.id)`` with ``u.id`` a key has
        at most one matching row per outer row, so it multiplies the block's
        rows by exactly 1 where it holds: the same as joining ``u`` in.
        """

        if (require_unique and not self.constraints) or any(o.opaque for o in block.occs):
            return block
        for sub in list(block.subs):
            if any(o.opaque for o in sub.occs) or not sub.occs:
                continue
            saved = len(self.candidates)
            required = self.valid(z3.Implies(block.cond.t, sub.atom), block.occs, block.facts)
            del self.candidates[saved:]
            if not required or (require_unique and not self._single_witness(sub, block)):
                continue
            guard = sub.guard
            block.occs = block.occs + sub.occs
            block.cond = _Pred(
                z3.And(_subst(block.cond.t, [(sub.atom, z3.BoolVal(True))]), guard),
                z3.Not(z3.And(_subst(block.cond.t, [(sub.atom, z3.BoolVal(True))]), guard)),
            )
            block.subs = [x for x in block.subs if x is not sub] + sub.nested
        return block

    def _single_witness(self, sub: _Sub, block) -> bool:
        """Two rows satisfying the guard for the same outer row share a key in every table."""

        copies = [o.copy(o.uid + "'") for o in sub.occs]
        copy_pairs = _atom_copy_pairs(sub.nested, "'")
        for o, c in zip(sub.occs, copies):
            copy_pairs.extend(_occ_pairs(o, c))
        clauses = []
        for o, c in zip(sub.occs, copies):
            constraint = self.constraints.get(o.table.lower())
            if constraint is None or not constraint.keys:
                return False
            clauses.append(
                z3.Or(
                    *[
                        z3.And(*[z3.And(z3.Not(o.col(k).null), _null_eq(o.col(k), c.col(k))) for k in key])
                        for key in constraint.keys
                    ]
                )
            )
        both = z3.And(sub.guard, _subst(sub.guard, copy_pairs))
        saved = len(self.candidates)
        ok = self.valid(z3.Implies(both, z3.And(*clauses)), block.occs + sub.occs + copies, block.facts)
        del self.candidates[saved:]
        return ok

    def _merge(self, block, keep: _Occ, drop: _Occ) -> None:
        pairs = _occ_pairs(drop, keep)
        block.occs = [o for o in block.occs if o is not drop]
        block.cond = _Pred(_subst(block.cond.t, pairs), _subst(block.cond.f, pairs))
        block.outputs = [_subst_val(v, pairs) for v in block.outputs]
        block.facts = [_subst(f, pairs) for f in block.facts]
        block.subs = [_subst_sub(sub, pairs) for sub in block.subs]
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
            a_atoms, b_atoms = self.unify_subs(a.subs, b.subs, pairs, a.occs, facts)
            cond_a = _subst(a.cond.t, a_atoms)
            pairs = pairs + b_atoms
            cond_b = _subst(b.cond.t, pairs)
            outs_b = [_subst_val(v, pairs) for v in b.outputs]
            if not self.valid(cond_a == cond_b, a.occs, facts):
                continue
            if self.valid(z3.Implies(cond_a, _rows_eq(a.outputs, outs_b)), a.occs, facts):
                return True
        return False

    def spj_set_contained(self, a: _Spj, b: _Spj) -> bool:
        """Every row of ``a`` is a row of ``b`` (set semantics)."""

        for mapping in self._homomorphisms(b.occs, a.occs):
            pairs = self._pairs(mapping)
            facts = a.facts + [_subst(f, pairs) for f in b.facts]
            a_atoms, b_atoms = self.unify_subs(a.subs, b.subs, pairs, a.occs, facts)
            cond_a = _subst(a.cond.t, a_atoms)
            pairs = pairs + b_atoms
            cond_b = _subst(b.cond.t, pairs)
            outs_b = [_subst_val(v, pairs) for v in b.outputs]
            if self.valid(z3.Implies(cond_a, z3.And(cond_b, _rows_eq(a.outputs, outs_b))), a.occs, facts):
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
        a_atoms, b_atoms = self.unify_subs(a.subs, b.subs, pairs, a.occs, facts)
        a = _with_atoms(a, a_atoms)
        pairs = pairs + b_atoms
        if not self.valid(a.cond.t == _subst(b.cond.t, pairs), a.occs, facts):
            return False
        keys_b = [_subst_val(k, pairs) for k in b.keys]
        if not a.is_global:
            copies = [o.copy(o.uid + "'") for o in a.occs]
            copy_pairs = _atom_copy_pairs(a.subs, "'")
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

    def resolve_set_sources(self, block):
        """Pin the columns of DISTINCT derived tables to the outer columns they are joined on.

        Each such table is, per outer row, either matched by exactly one row or
        not at all, so the join is an existence test. Raises ``_SetSourceUnresolved``
        when some column is not pinned to a column that cannot be NULL.
        """

        def walk(subs):
            for sub in subs:
                yield sub
                yield from walk(sub.nested)

        if not any(sub.setsrc for sub in walk(block.subs)):
            return block
        if not isinstance(block, (_Spj, _Agg)) or any(o.opaque for o in block.occs):
            raise _SetSourceUnresolved
        conjuncts, stack = [], [block.cond.t]
        while stack:
            term = stack.pop()
            if z3.is_and(term):
                stack.extend(term.children())
            else:
                conjuncts.append(term)
        ids = {c.get_id() for c in conjuncts}
        pairs = []
        for sub in block.subs:
            if not sub.setsrc:
                continue
            inner, columns = sub.setsrc
            if sub.atom.get_id() not in ids or any(x.setsrc for x in walk(sub.nested)):
                raise _SetSourceUnresolved
            distinct = self.as_distinct_spj(inner)
            if not (isinstance(distinct, _Spj) and distinct.distinct):
                raise _SetSourceUnresolved
            for position, column in enumerate(columns):
                produced = inner.outputs[position] if position < len(inner.outputs) else None
                if produced is not None and _is_ground(produced.val) and _is_ground(produced.null):
                    pairs.append((column.val, produced.val))  # a constant column is pinned by what it is
                    continue
                for term in conjuncts:
                    if not z3.is_eq(term):
                        continue
                    left, right = term.children()
                    if right.eq(column.val):
                        left, right = right, left
                    if not left.eq(column.val) or not z3.is_const(right) or right.decl().kind() != z3.Z3_OP_UNINTERPRETED:
                        continue
                    if z3.Not(z3.Bool(right.decl().name() + "#null")).get_id() in ids:
                        pairs.append((column.val, right))
                        break
                else:
                    raise _SetSourceUnresolved
        # The test already requires the pinned columns to be non-NULL, so the
        # condition need not repeat it (the existence atom is a free Boolean).
        known = [
            (z3.Not(z3.Bool(pin.decl().name() + "#null")), z3.BoolVal(True))
            for _, pin in pairs
            if z3.is_const(pin) and pin.decl().kind() == z3.Z3_OP_UNINTERPRETED
        ]
        changes = {
            "cond": _Pred(_subst(_subst(block.cond.t, pairs), known), _subst(block.cond.f, pairs)),
            "outputs": [_subst_val(v, pairs) for v in block.outputs],
            "subs": [
                dataclasses.replace(_subst_sub(sub, pairs), setsrc=None, guard=_pinned_guard(sub, pairs, ids))
                if sub.setsrc
                else _subst_sub(sub, pairs)
                for sub in block.subs
            ],
        }
        if isinstance(block, _Agg):
            changes["keys"] = [_subst_val(k, pairs) for k in block.keys]
            changes["aggs"] = [
                _AggCall(c.func, c.distinct, _subst_val(c.arg, pairs) if c.arg is not None else None, c.var)
                for c in block.aggs
            ]
            if block.having is not None:
                changes["having"] = _Pred(_subst(block.having.t, pairs), _subst(block.having.f, pairs))
        return dataclasses.replace(block, **changes)

    def sub_consequences(self, block):
        """Facts ``atom => condition on outer columns`` implied by each existence test.

        A witness row satisfies the guard, so a guard condition over columns that
        the guard equates to outer columns holds of those outer columns. The atom
        is otherwise free, so without this a required test would not tell the
        prover that, e.g., ``d.x = e.x AND e.x > 7`` forces ``d.x > 7``.
        """

        if not block.subs or any(o.opaque for o in block.occs):
            return block
        extra = []
        for sub in block.subs:
            if any(o.opaque for o in sub.occs):
                continue
            inner_ids = set()
            for occ in sub.occs:
                for v in occ.cols.values():
                    inner_ids.update((v.null.get_id(), v.val.get_id()))
            conjuncts, stack = [], [sub.guard]
            while stack:
                term = stack.pop()
                if z3.is_and(term):
                    stack.extend(term.children())
                else:
                    conjuncts.append(term)

            def consts(term):
                found, todo = set(), [term]
                while todo:
                    t = todo.pop()
                    if z3.is_const(t) and t.decl().kind() == z3.Z3_OP_UNINTERPRETED:
                        found.add(t.get_id())
                    todo.extend(t.children())
                return found

            ids = {c.get_id() for c in conjuncts}
            pairs = []
            for occ in sub.occs:
                for v in occ.cols.values():
                    for term in conjuncts:
                        if not z3.is_eq(term):
                            continue
                        left, right = term.children()
                        if right.eq(v.val):
                            left, right = right, left
                        if not left.eq(v.val) or consts(right) & inner_ids:
                            continue
                        # ``right`` is an outer term; the pair is known non-NULL when both sides' flags are.
                        outer_null = z3.Bool(right.decl().name() + "#null") if z3.is_const(right) else None
                        if (
                            outer_null is not None
                            and z3.Not(v.null).get_id() in ids
                            and z3.Not(outer_null).get_id() in ids
                        ):
                            pairs.append((v.val, right))
                            pairs.append((v.null, outer_null))
                            break
            for term in conjuncts:
                used = consts(term)
                if not used:
                    continue
                moved = _subst(term, pairs)
                if consts(moved) & inner_ids:
                    continue
                extra.append(z3.Implies(sub.atom, moved))
        if extra:
            block.facts = block.facts + extra
        return block

    def merge_equivalent_subs(self, block):
        """Two existence tests of one block that agree under its plain conditions share one atom.

        ``o.k = l.k AND EXISTS(.. b.k = o.k) AND EXISTS(.. b.k = l.k)`` tests the same thing twice: the
        conditions that mention no existence atom make the two guards equal, so both atoms mean the same.
        """

        subs = [sub for sub in block.subs if sub.setsrc is None]
        if len(block.subs) < 2 or len(subs) < 2:
            return block
        atoms = set()

        def collect(items):
            for sub in items:
                atoms.add(sub.atom.get_id())
                collect(sub.nested)

        collect(block.subs)

        def mentions_atom(term) -> bool:
            stack = [term]
            while stack:
                t = stack.pop()
                if z3.is_const(t) and t.get_id() in atoms:
                    return True
                stack.extend(t.children())
            return False

        conjuncts, todo = [], [block.cond.t]
        while todo:
            term = todo.pop()
            if z3.is_and(term):
                todo.extend(term.children())
            else:
                conjuncts.append(term)
        plain = [c for c in conjuncts if not mentions_atom(c)]
        facts = block.facts + plain
        pairs, dropped = [], set()
        for i, first in enumerate(subs):
            if id(first) in dropped:
                continue
            for second in subs[i + 1:]:
                if id(second) in dropped:
                    continue
                if self._sub_implies(first, second, block.occs, facts) and self._sub_implies(second, first, block.occs, facts):
                    pairs.append((second.atom, first.atom))
                    dropped.add(id(second))
        if not pairs:
            return block
        block = _with_atoms(block, pairs)
        block.subs = [sub for sub in block.subs if id(sub) not in dropped]
        return block

    def drop_never_null_having(self, block):
        """``HAVING MIN(x) IS NOT NULL`` holds for every group when ``x`` is never NULL in the group's rows."""

        if not isinstance(block, _Agg) or block.is_global or block.having is None or block.subs:
            return block
        never_null = {
            c.var.null.get_id()
            for c in block.aggs
            if c.func in ("MIN", "MAX", "SUM", "AVG") and not c.distinct and c.arg is not None and c.var.null.decl().kind() == z3.Z3_OP_UNINTERPRETED
            and self.valid(z3.Implies(block.cond.t, z3.Not(c.arg.null)), block.occs, block.facts)
        }
        if not never_null:
            return block
        top = block.having.t
        conjuncts = list(top.children()) if z3.is_and(top) else [top]
        kept = [c for c in conjuncts if not (z3.is_not(c) and c.arg(0).get_id() in never_null)]
        if len(kept) == len(conjuncts):
            return block
        if kept:
            rest = z3.And(*kept) if len(kept) > 1 else kept[0]
            block.having = _Pred(rest, z3.Not(rest))
        else:
            block.having = None
        return block

    def push_having(self, block):
        """``HAVING`` on group keys only is a ``WHERE`` filter (every row of a group agrees on it)."""

        if (
            not isinstance(block, _Agg)
            or block.is_global
            or block.having is None
            or block.subs
            or any(o.opaque for o in block.occs)
        ):
            return block
        agg_terms = {t.get_id() for c in block.aggs for t in (c.var.null, c.var.val) if z3.is_const(t)}

        def has_aggregate(root) -> bool:
            stack = [root]
            while stack:
                term = stack.pop()
                if z3.is_const(term) and term.decl().kind() == z3.Z3_OP_UNINTERPRETED and term.get_id() in agg_terms:
                    return True
                stack.extend(term.children())
            return False

        top = block.having.t
        conjuncts = list(top.children()) if z3.is_and(top) else [top]
        candidates = [c for c in conjuncts if not has_aggregate(c)]
        if not candidates:
            return block
        copies = [o.copy(o.uid + "'") for o in block.occs]
        copy_pairs = []
        for o, c in zip(block.occs, copies):
            copy_pairs.extend(_occ_pairs(o, c))
        keys_same = _rows_eq(block.keys, [_subst_val(k, copy_pairs) for k in block.keys])
        both = z3.And(block.cond.t, _subst(block.cond.t, copy_pairs), keys_same)
        facts = block.facts + [_subst(f, copy_pairs) for f in block.facts]
        pushed, kept = [], [c for c in conjuncts if has_aggregate(c)]

        def invariant(term) -> bool:
            saved = len(self.candidates)
            ok = self.valid(z3.Implies(both, term == _subst(term, copy_pairs)), block.occs + copies, facts)
            del self.candidates[saved:]
            return ok

        together = z3.And(*candidates) if len(candidates) > 1 else candidates[0]
        if invariant(together):
            pushed = list(candidates)  # conjuncts can depend on each other (a NULL test guards a comparison)
        else:
            for conjunct in candidates:
                (pushed if invariant(conjunct) else kept).append(conjunct)
        if not pushed:
            return block
        t = z3.And(block.cond.t, *pushed)
        block.cond = _Pred(t, z3.Not(t))
        if kept:
            rest = z3.And(*kept) if len(kept) > 1 else kept[0]
            block.having = _Pred(rest, z3.Not(rest))
        else:
            block.having = None
        return block

    def as_distinct_spj(self, block):
        """``GROUP BY`` with no aggregates is ``SELECT DISTINCT`` when the
        output determines the group key."""

        if not isinstance(block, _Agg) or block.aggs or block.having is not None or block.is_global:
            return block
        copies = [o.copy(o.uid + "'") for o in block.occs]
        copy_pairs = _atom_copy_pairs(block.subs, "'")
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
            return _Spj(
                block.occs, block.cond, block.outputs, block.names, distinct=True, facts=block.facts, subs=block.subs
            )
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

    def branch_unique(self, a) -> bool:
        """No two rows of this select are equal: equal outputs force the same row of every table.

        Needs a declared key for every table occurrence. Two copies of the block are checked: if both
        satisfy the condition and agree on the outputs, each pair of occurrences agrees on a key.
        """

        if not isinstance(a, _Spj) or a.distinct or not a.occs or any(o.opaque and o.table not in self.opaque_sets for o in a.occs):
            return False
        if not self.constraints and not any(o.opaque for o in a.occs):
            return False
        copies = [o.copy(o.uid + "'") for o in a.occs]
        pairs: list = _atom_copy_pairs(a.subs, "'")
        for o, c in zip(a.occs, copies):
            pairs.extend(_occ_pairs(o, c))
        same = []
        for o, c in zip(a.occs, copies):
            if o.opaque:
                names = [f"c{i}" for i in range(len(o.columns or []))]
                same.append(_rows_eq([o.col(n) for n in names], [c.col(n) for n in names]))
                continue
            constraint = (self.constraints or {}).get(o.table.lower())
            if constraint is None or not constraint.keys:
                return False
            same.append(
                z3.Or(
                    *[
                        z3.And(*[z3.And(z3.Not(o.col(k).null), z3.Not(c.col(k).null), _null_eq(o.col(k), c.col(k))) for k in key])
                        for key in constraint.keys
                    ]
                )
            )
        other_cond = _subst(a.cond.t, pairs)
        other_outs = [_subst_val(v, pairs) for v in a.outputs]
        claim = z3.Implies(z3.And(a.cond.t, other_cond, _rows_eq(a.outputs, other_outs)), z3.And(*same))
        facts = a.facts + [_subst(f, pairs) for f in a.facts]
        saved = len(self.candidates)
        result = self.valid(claim, a.occs + copies, facts)
        del self.candidates[saved:]
        return result

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


def _constant_global(block):
    """A global aggregate whose outputs do not depend on any aggregate (``SELECT COUNT(NULL) FROM t``)
    returns exactly one constant row, like a SELECT without FROM."""

    if (
        isinstance(block, _Agg)
        and block.is_global
        and block.having is None
        and not block.subs
        and all(_is_ground(v.val) and _is_ground(v.null) for v in block.outputs)
    ):
        return _Spj([], _const_pred(True), block.outputs, block.names, distinct=block.distinct, facts=[])
    return block


def _selects_a_set(select: exp.Select) -> bool:
    """A DISTINCT select, or one grouped on exactly its (aggregate-free) outputs, never repeats a row."""

    if select.args.get("distinct") and not select.args.get("group"):
        return not any(select.find_all(exp.Window))
    group = select.args.get("group")
    if group is None or any(group.args.get(k) for k in ("rollup", "cube", "grouping_sets", "totals")):
        return False
    outputs = [(i.this if isinstance(i, exp.Alias) else i) for i in select.expressions]
    if any(isinstance(n, (exp.AggFunc, exp.Window)) for o in outputs for n in o.walk()):
        return False
    return {o.sql() for o in outputs} == {g.sql() for g in group.expressions}


def _is_set(u: _Union) -> bool:
    return u.distinct or (len(u.branches) == 1 and u.branches[0].distinct)


def _prove(prover: _Prover, left: _Union, right: _Union) -> tuple[bool, str]:
    left.column_names, right.column_names = list(left.names), list(right.names)
    for union in (left, right):
        _prune(prover, union)
        branches = []
        for b in union.branches:
            b = _constant_global(b)
            b = prover.resolve_set_sources(b)
            b = prover.drop_never_null_having(b)
            b = prover.push_having(b)
            b = prover.as_distinct_spj(b)
            b = prover.inline_unique_subs(b)
            b = prover.merge_equivalent_subs(b)
            b = prover.sub_consequences(b)
            if union.distinct or b.distinct:
                b = prover.inline_unique_subs(b, require_unique=False)
            branches.append(prover.merge_key_occurrences(b))
        union.branches = branches
    if not left.branches and not right.branches:
        return True, "both queries always return no rows"
    if not left.branches or not right.branches:
        return False, "only one query can return rows"
    for keep, other in ((left, right), (right, left)):
        # A side whose rows are provably distinct (by keys) is as good as a DISTINCT one.
        if _is_set(keep) and not _is_set(other) and len(other.branches) == 1 and prover.branch_unique(other.branches[0]):
            other.branches = [dataclasses.replace(other.branches[0], distinct=True)]
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
    if any(o.opaque or o.table == _UNNEST_TABLE for o in all_occs) or any(b.subs for b in blocks):
        return None
    for block in blocks:
        prover.witness(block.cond.t, block.occs, block.facts)
    empty = z3.Solver()
    empty.check()
    for model, occs in [(empty.model(), [])] + prover.candidates:
        db: dict[str, list[dict]] = {occ.table: [] for occ in all_occs}
        for occ in occs:
            db.setdefault(occ.table, []).append({name: _cell(model, v) for name, v in occ.cols.items()})
        # A column only the other query reads is still a column of the row: give it the model's
        # value (never NULL by default) rather than leaving it to read as NULL.
        other_columns: dict[str, dict] = {}
        for occ in all_occs:
            for name, v in occ.cols.items():
                other_columns.setdefault(occ.table, {}).setdefault(name, v)
        for table, rows in db.items():
            for row in rows:
                for name, v in other_columns.get(table, {}).items():
                    if name not in row:
                        row[name] = _cell(model, v)
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


_OPAQUE_PROOFS = 6
_nesting = [0]


def _all_occs(unions) -> list[_Occ]:
    found: list[_Occ] = []

    def from_subs(subs):
        for sub in subs:
            found.extend(sub.occs)
            from_subs(getattr(sub, "nested", None) or [])

    for union in unions:
        for branch in union.branches:
            found.extend(branch.occs)
            from_subs(branch.subs)
    return found


def _unify_opaque(compiler: "_Compiler", unions, **settings) -> None:
    """Give two differently written derived relations one identity when they are provably equal.

    A derived table kept whole is identified by its text, so a subquery written with another join
    order, extra IS NOT NULL guards or nested pass-through layers would read as a different relation.
    The bodies are compared by the prover itself; each proven pair shares one table name.
    """

    bodies = compiler.opaque_bodies
    if len(bodies) < 2 or _nesting[0] >= 2:
        return

    def tables(sql: str) -> frozenset:
        return frozenset(t.name.lower() for t in sqlglot.parse_one(sql, read="bigquery").find_all(exp.Table))

    representatives: list[str] = []
    mapping: dict[str, str] = {}
    budget = _OPAQUE_PROOFS
    for key, (sql, arity) in bodies.items():
        for rep in representatives:
            rep_sql, rep_arity = bodies[rep]
            if rep_arity != arity or budget <= 0 or tables(rep_sql) != tables(sql):
                continue
            budget -= 1
            _nesting[0] += 1
            try:
                result = _prove_core(rep_sql, sql, compare_names=False, dialect="bigquery", **settings)
            finally:
                _nesting[0] -= 1
            if result.status is SmtStatus.PROVEN_EQUIVALENT:
                mapping[key] = rep
                break
        else:
            representatives.append(key)
    if mapping:
        for occ in _all_occs(unions):
            if occ.opaque and occ.table in mapping:
                occ.table = mapping[occ.table]


def _prove_core(
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
    used = [False]

    def attempt(semijoin: bool) -> SmtEquivalenceResult:
        compiler = _Compiler(schema, exact_arithmetic, dialect)
        compiler.semijoin = semijoin
        try:
            left = compiler.compile(left_sql)
            right = compiler.compile(right_sql)
        except Unsupported as error:
            return SmtEquivalenceResult(SmtStatus.NOT_PROVEN, f"unsupported: {error}", assumptions=assumptions)
        except sqlglot.errors.ParseError as error:
            return SmtEquivalenceResult(SmtStatus.NOT_PROVEN, f"parse error: {error}", assumptions=assumptions)
        _unify_opaque(
            compiler,
            (left, right),
            schema=schema,
            exact_arithmetic=exact_arithmetic,
            timeout_ms=timeout_ms,
            constraints=constraints,
        )
        assumed = assumptions + ((LIMIT_SOURCE_ASSUMPTION,) if compiler.limit_opaque else ()) + (
            (WINDOW_SOURCE_ASSUMPTION,) if compiler.window_opaque else ()
        )
        extra = compiler.order_facts()
        if extra:
            for union in (left, right):
                for block in union.branches:
                    block.facts = block.facts + extra

        if len(left.names) != len(right.names):
            return SmtEquivalenceResult(
                SmtStatus.NOT_PROVEN,
                f"different column counts ({len(left.names)} vs {len(right.names)})",
                assumptions=assumed,
            )
        for position, (a, b) in enumerate(zip(left.names, right.names), start=1):
            if compare_names and a != b:
                return SmtEquivalenceResult(
                    SmtStatus.NOT_PROVEN,
                    f"column {position} is named {a or '(unnamed)'} on the left and {b or '(unnamed)'} on the right",
                    assumptions=assumed,
                )

        prover = _Prover(timeout_ms, constraints)
        prover.opaque_sets = compiler.opaque_sets
        used[0] = compiler.used_setsrc
        try:
            proven, reason = _prove(prover, left, right)
        except _SetSourceUnresolved:
            proven, reason = False, "a derived table is not joined on all of its columns"
        if proven:
            return SmtEquivalenceResult(SmtStatus.PROVEN_EQUIVALENT, reason, assumptions=assumed)
        if not compiler.uses_uf:
            counterexample = _find_counterexample(prover, left, right)
            if counterexample is not None:
                return SmtEquivalenceResult(
                    SmtStatus.NOT_EQUIVALENT,
                    "the queries differ on the attached database",
                    counterexample=counterexample,
                    assumptions=assumed,
                )
        if prover.unknown:
            reason += " (the solver timed out on some checks)"
        return SmtEquivalenceResult(SmtStatus.NOT_PROVEN, reason, assumptions=assumed)

    result = attempt(True)
    if result.status is not SmtStatus.NOT_PROVEN or not used[0]:
        return result
    # A DISTINCT derived table read as a set can fail to match what the opaque
    # reading would; try that reading too before giving up.
    fallback = attempt(False)
    return fallback if fallback.status is not SmtStatus.NOT_PROVEN else result


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


WINDOW_SOURCE_ASSUMPTION = "window functions over the same input with the same text give the same values (ties in ORDER BY resolve alike)"
LIMIT_SOURCE_ASSUMPTION = "a LIMIT subquery with the same text returns the same rows each time"
TIE_ASSUMPTION = "rows tied on the ORDER BY are cut by LIMIT the same way for equal inputs"


def _split_limit(sql: str, dialect: str):
    """``(core_sql, spec)`` for a query ending in ORDER BY .. LIMIT, ``(sql, None)`` without a limit.

    ``spec`` is ``(limit, offset, ordering, covers_all)`` where ``ordering`` lists
    ``(output position, descending, nulls first)``; ``None`` as the core means the
    shape is not handled (``spec`` then says why).
    """

    tree = sqlglot.parse_one(sql, read=dialect)
    root = tree
    while isinstance(root, exp.Subquery):
        root = root.this
    if not isinstance(root, (exp.Select, exp.Union, exp.Intersect, exp.Except)):
        return sql, None
    limit, offset, order = root.args.get("limit"), root.args.get("offset"), root.args.get("order")
    if (limit is None and offset is None) or _is_limit_zero(root):
        return sql, None
    if limit is None or order is None:
        return None, "LIMIT without ORDER BY or OFFSET without LIMIT picks arbitrary rows"
    if not isinstance(limit.expression, exp.Literal) or limit.expression.is_string:
        return None, "LIMIT is not a constant"
    if offset is not None and (not isinstance(offset.expression, exp.Literal) or offset.expression.is_string):
        return None, "OFFSET is not a constant"
    first = root
    while isinstance(first, (exp.Union, exp.Intersect, exp.Except, exp.Subquery)):
        first = first.this
    if not isinstance(first, exp.Select) or any(isinstance(e, exp.Star) for e in first.expressions):
        return None, "ORDER BY with a star select list"
    outputs = [(e.alias_or_name.lower(), (e.this if isinstance(e, exp.Alias) else e).sql()) for e in first.expressions]
    ordering = []
    hidden: list[exp.Expression] = []  # order keys that are not output columns, read as extra columns
    for item in order.expressions:
        if not isinstance(item, exp.Ordered):
            return None, "ORDER BY item"
        key = item.this
        position = None
        if isinstance(key, exp.Literal) and not key.is_string and 1 <= int(key.this) <= len(outputs):
            position = int(key.this) - 1
        else:
            text = key.sql()
            matches = [
                i
                for i, (name, expr) in enumerate(outputs)
                if (isinstance(key, exp.Column) and not key.table and key.name.lower() == name) or expr == text
            ]
            if len(matches) == 1 or (matches and len({outputs[i] for i in matches}) == 1):
                position = matches[0]
        if position is None and root is first and not first.args.get("distinct") and not any(key.find_all(exp.Subquery, exp.Window)):
            hidden.append(key.copy())
            position = len(outputs) + len(hidden) - 1
        if position is None:
            return None, "ORDER BY on an expression that is not an output column"
        desc = bool(item.args.get("desc"))
        nulls_first = item.args.get("nulls_first")
        ordering.append((position, desc, (not desc) if nulls_first is None else bool(nulls_first)))
    covers = {p for p, _, _ in ordering} >= set(range(len(outputs)))
    core = tree.copy()
    stripped = core
    while isinstance(stripped, exp.Subquery):
        stripped = stripped.this
    for key in ("limit", "offset", "order"):
        stripped.set(key, None)
    for index, key in enumerate(hidden):
        stripped.set("expressions", list(stripped.expressions) + [exp.alias_(key, f"kq_ord{index}")])
    spec = (
        int(limit.expression.this),
        int(offset.expression.this) if offset is not None else 0,
        tuple(ordering),
        covers,
    )
    return core.sql(dialect=dialect), spec


def prove_equivalent_smt(left_sql: str, right_sql: str, **kwargs) -> SmtEquivalenceResult:
    """Prove two BigQuery queries return the same result bag, or refute them.

    A query ending in ``ORDER BY .. LIMIT n [OFFSET m]`` is handled when both sides
    have the same limit, offset and ordering (by output column) and the queries
    without them are equivalent: equal inputs give equal first rows, up to rows
    tied on the ordering, which is recorded in the result's assumptions unless the
    ordering covers every output column.

    See ``_prove_core`` for the options.
    """

    dialect = kwargs.get("dialect", "bigquery")
    try:
        left_core, left_spec = _split_limit(left_sql, dialect)
        right_core, right_spec = _split_limit(right_sql, dialect)
    except sqlglot.errors.SqlglotError:
        return _prove_core(left_sql, right_sql, **kwargs)
    if left_spec is None and right_spec is None and left_core is not None and right_core is not None:
        return _prove_core(left_sql, right_sql, **kwargs)
    if left_core is None or right_core is None:
        reason = left_spec if left_core is None else right_spec
        return SmtEquivalenceResult(SmtStatus.NOT_PROVEN, f"unsupported: {reason}")
    if left_spec is None or right_spec is None:
        return SmtEquivalenceResult(SmtStatus.NOT_PROVEN, "only one query has a LIMIT")
    if left_spec[:3] != right_spec[:3]:
        return SmtEquivalenceResult(SmtStatus.NOT_PROVEN, "the queries differ in LIMIT, OFFSET or ORDER BY")
    result = _prove_core(left_core, right_core, **kwargs)
    if result.status is SmtStatus.NOT_EQUIVALENT:
        # The rows before the cut differ, but the first rows may still agree.
        return SmtEquivalenceResult(SmtStatus.NOT_PROVEN, "the queries differ before the LIMIT", assumptions=result.assumptions)
    if result.proven and not left_spec[3]:
        result = dataclasses.replace(result, assumptions=tuple(result.assumptions) + (TIE_ASSUMPTION,))
    return result
