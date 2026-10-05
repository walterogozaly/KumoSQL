"""Aggregate functions: sqlglot node -> BigQuery name and arguments -> an :class:`AggSpec`.

``is_aggregate(node)`` says whether a node is an aggregate call (including the ``IGNORE NULLS`` / ``RESPECT NULLS``
wrappers sqlglot puts around it). ``compile_aggregate(compiler, node, cx)`` types the call once and returns a spec
with the result ``type`` and ``compute(rows, env)``, a pure function of the rows of one group (or one window frame)
and of the env the group is evaluated in. Specs hold no state, so the window evaluator can call ``compute`` once per
frame.

Shapes sqlglot gives BigQuery's modifiers (checked on sqlglot 30.21)::

    ARRAY_AGG(DISTINCT x ORDER BY x LIMIT 3)  ArrayAgg(Limit(Order(Distinct(x))))
    ARRAY_AGG(x IGNORE NULLS ...)             IgnoreNulls(ArrayAgg(...))
    ANY_VALUE(x HAVING MAX y)                 AnyValue(HavingMax(x, y, max=True))     (MIN: max=False)
    MAX_BY(x, y) / MIN_BY(x, y)               ArgMax / ArgMin
    STRING_AGG(x, sep ORDER BY y)             GroupConcat(Order(x), separator=sep)
    VAR_SAMP and VARIANCE are both Variance, STDDEV and STDDEV_SAMP are Stddev and StddevSamp (one function each).

Every argument of a node is read through ``_only`` so a flag that is not handled raises ``Unsupported``. Wherever the
result could depend on something BigQuery leaves unspecified, or the rule is not known for certain, the function
raises ``Unsupported`` (or marks the context nondeterministic) instead of picking an answer.
"""

from __future__ import annotations

import math
from decimal import ROUND_HALF_UP, Decimal
from fractions import Fraction
from itertools import accumulate
from typing import Any, Callable

from sqlglot import exp

from . import types as T
from . import values as V
from .errors import AnalysisError, EvalError, Unsupported
from .functions import register, register_node
from .runtime import E, Env


class AggSpec:
    """A compiled aggregate: its result type and ``compute(rows, env)``."""

    __slots__ = ("type", "compute", "name")

    def __init__(self, type: T.Type, compute: Callable[[list, Env], Any], name: str):
        self.type = type
        self.compute = compute
        self.name = name

    def __repr__(self) -> str:
        return f"AggSpec({self.name}, {self.type})"


def _c():
    from . import compiler

    return compiler


def _only(node, *allowed):
    _c()._only(node, *allowed)


# ---------------------------------------------------------------------------------------------
# which nodes are aggregates
# ---------------------------------------------------------------------------------------------

# Analytic-only functions sqlglot files under AggFunc: they are never aggregates here (the window code owns them).
_ANALYTIC = (
    exp.Rank, exp.DenseRank, exp.CumeDist, exp.PercentRank, exp.Ntile, exp.FirstValue, exp.LastValue, exp.NthValue,
    exp.Lag, exp.Lead, exp.PercentileCont, exp.PercentileDisc, exp.Grouping, exp.GroupingId,
)

_ANON_AGGREGATES = {
    "ST_UNION_AGG", "ST_EXTENT", "ST_CLUSTERDBSCAN", "APPROX_COUNT_DISTINCT", "APPROX_QUANTILES", "APPROX_TOP_COUNT",
    "APPROX_TOP_SUM", "COUNT_IF", "ANY_VALUE", "STRING_AGG", "ARRAY_AGG", "ARRAY_CONCAT_AGG", "LOGICAL_AND",
    "LOGICAL_OR", "BIT_AND", "BIT_OR", "BIT_XOR", "MAX_BY", "MIN_BY", "CORR", "COVAR_POP", "COVAR_SAMP", "STDDEV",
    "STDDEV_POP", "STDDEV_SAMP", "VARIANCE", "VAR_POP", "VAR_SAMP", "SUM", "AVG", "MIN", "MAX", "COUNT", "COUNTIF",
    "ARRAY_UNION_AGG", "JSON_ARRAY_AGG", "JSON_OBJECT_AGG",
}


def is_aggregate(node) -> bool:
    if isinstance(node, (exp.IgnoreNulls, exp.RespectNulls)):
        return is_aggregate(node.this)
    if isinstance(node, exp.AggFunc):
        return not isinstance(node, _ANALYTIC)
    if isinstance(node, exp.Anonymous):
        return str(node.this).upper() in _ANON_AGGREGATES
    return False


# ---------------------------------------------------------------------------------------------
# shared helpers
# ---------------------------------------------------------------------------------------------


def _non_null(values: list) -> list:
    return [v for v in values if v is not None]


def _key(t: T.Type, value: Any) -> Any:
    key = V.group_key(t, value)
    try:
        hash(key)
    except TypeError:
        return ("repr", repr(value))
    return key


def _distinct(t: T.Type, values: list) -> list:
    seen = set()
    out = []
    for value in values:
        key = _key(t, value)
        if key not in seen:
            seen.add(key)
            out.append(value)
    return out


def _all_identical(t: T.Type, values: list) -> bool:
    if len(values) < 2:
        return True
    first = _key(t, values[0])
    return all(_key(t, v) == first for v in values[1:])


def _reject_nulls(nulls: str | None, name: str) -> None:
    if nulls:
        raise Unsupported(f"{name} with {nulls} NULLS")


def _check_type(value: E, name: str) -> None:
    if value.type.foreign:
        raise Unsupported(f"{name} of GoogleSQL-only type {value.type}")


def _argument(compiler, cx, node, name: str, distinct_ok: bool = False) -> tuple[E, bool]:
    """The one plain argument of an aggregate without ORDER BY or LIMIT: ``(compiled argument, distinct)``."""

    if node is None:
        raise AnalysisError(f"{name} needs an argument")
    if isinstance(node, (exp.Limit, exp.Order)):
        raise AnalysisError(f"{name} does not accept ORDER BY or LIMIT")
    distinct = False
    if isinstance(node, exp.Distinct):
        _only(node, "expressions")
        if len(node.expressions) != 1:
            raise AnalysisError(f"{name}(DISTINCT ...) takes exactly one argument")
        if not distinct_ok:
            raise Unsupported(f"{name}(DISTINCT ...)")
        distinct = True
        node = node.expressions[0]
    if isinstance(node, exp.Star):
        raise AnalysisError(f"{name}(*) is not allowed")
    if isinstance(node, exp.HavingMax):
        raise Unsupported(f"{name} with HAVING MAX/MIN")
    value = compiler.expr(node, cx)
    _check_type(value, name)
    return value, distinct


def _values(fn, rows: list, env: Env) -> list:
    ctx, ctes = env.ctx, env.ctes
    return [fn(Env(row, env, ctx, ctes)) for row in rows]


def _naive_sum(values: list) -> float:
    total = 0.0
    for v in values:
        total += v
    return total


def _floats_sum_is_exact(values: list) -> bool:
    """Whether every partial sum of these floats is exact in any order (whole numbers adding up below 2**53)."""

    limit = 0.0
    for v in values:
        if v != v or v in (math.inf, -math.inf) or v != math.floor(v):
            return False
        limit += abs(v)
    return limit <= 2.0**53


# ---------------------------------------------------------------------------------------------
# COUNT, COUNTIF
# ---------------------------------------------------------------------------------------------


def _count(compiler, node, cx, nulls) -> AggSpec:
    _only(node, "this", "expressions", "big_int")
    _reject_nulls(nulls, "COUNT")
    if node.args.get("expressions"):
        raise AnalysisError("COUNT takes one argument")
    this = node.this
    if this is None:
        raise AnalysisError("COUNT needs an argument")
    if isinstance(this, exp.Star):
        _only(this)
        return AggSpec(T.INT64, lambda rows, env: len(rows), "COUNT(*)")
    value, distinct = _argument(compiler, cx, this, "COUNT", distinct_ok=True)
    fn, t = value.fn, value.type
    if distinct:
        if not T.groupable(t):
            raise AnalysisError(f"COUNT(DISTINCT) does not support {t}")

        def count_distinct(rows, env):
            return len({_key(t, v) for v in _values(fn, rows, env) if v is not None})

        return AggSpec(T.INT64, count_distinct, "COUNT(DISTINCT)")
    if value.lit == "literal" and value.value is not None:
        return AggSpec(T.INT64, lambda rows, env: len(rows), "COUNT(literal)")

    def count(rows, env):
        ctx, ctes = env.ctx, env.ctes
        return sum(1 for row in rows if fn(Env(row, env, ctx, ctes)) is not None)

    return AggSpec(T.INT64, count, "COUNT")


def _countif(compiler, node, cx, nulls) -> AggSpec:
    _only(node, "this")
    _reject_nulls(nulls, "COUNTIF")
    value, _ = _argument(compiler, cx, node.this, "COUNTIF")
    value = compiler.coerce(value, T.BOOL, "COUNTIF argument")
    fn = value.fn

    def countif(rows, env):
        ctx, ctes = env.ctx, env.ctes
        return sum(1 for row in rows if fn(Env(row, env, ctx, ctes)) is True)

    return AggSpec(T.INT64, countif, "COUNTIF")


# ---------------------------------------------------------------------------------------------
# SUM, AVG
# ---------------------------------------------------------------------------------------------


def _numeric_argument(compiler, cx, node, name: str) -> tuple[E, bool]:
    value, distinct = _argument(compiler, cx, node.this, name, distinct_ok=True)
    kind = value.type.kind
    if kind == "INTERVAL":
        raise Unsupported(f"{name} of INTERVAL")
    if not value.type.is_numeric:
        raise AnalysisError(f"No matching signature for aggregate function {name} for argument type {value.type}")
    return value, distinct


def _fit(value: Decimal, kind: str) -> Decimal:
    """``value`` as a NUMERIC or BIGNUMERIC payload (``V.numeric`` takes ``abs`` under the default 28-digit context,
    which rounds the largest NUMERIC values up to the limit, so the range check is done here with exact comparisons)."""

    if kind == "NUMERIC":
        rounded = value.quantize(V.NUMERIC_SCALE, rounding=ROUND_HALF_UP, context=V.DEC)
        if not (-V.NUMERIC_LIMIT < rounded < V.NUMERIC_LIMIT):
            raise EvalError("numeric overflow")
        return rounded
    return V.bignumeric(value)


def _decimal_total(values: list, kind: str) -> tuple[Decimal, bool]:
    """The exact sum, and whether some prefix sum left the type's range."""

    add = V.DEC.add
    if kind == "NUMERIC":
        limit = V.NUMERIC_LIMIT

        def in_range(x):
            return -limit < x < limit
    else:
        low, high = V.BIGNUMERIC_MIN, V.BIGNUMERIC_MAX

        def in_range(x):
            return low <= x <= high
    total = Decimal(0)
    over = False
    for v in values:
        total = add(total, v)
        if not over and not in_range(total):
            over = True
    return total, over


def _sum(compiler, node, cx, nulls) -> AggSpec:
    _only(node, "this")
    _reject_nulls(nulls, "SUM")
    value, distinct = _numeric_argument(compiler, cx, node, "SUM")
    fn, t = value.fn, value.type
    kind = t.kind

    def gather(rows, env):
        values = _non_null(_values(fn, rows, env))
        return _distinct(t, values) if distinct else values

    if kind == "INT64":

        def sum_int(rows, env):
            values = gather(rows, env)
            if not values:
                return None
            total = sum(values)
            if total < V.INT64_MIN or total > V.INT64_MAX:
                raise EvalError("int64 overflow")
            if len(values) > 2 and sum(abs(v) for v in values) > V.INT64_MAX:
                partial = list(accumulate(values))
                if min(partial) < V.INT64_MIN or max(partial) > V.INT64_MAX:
                    # BigQuery adds in an order it does not specify: whether this fails is not known
                    raise Unsupported("SUM whose partial sums overflow INT64 although the total does not")
            return total

        return AggSpec(T.INT64, sum_int, "SUM")
    if kind in ("NUMERIC", "BIGNUMERIC"):

        def sum_decimal(rows, env):
            values = gather(rows, env)
            if not values:
                return None
            total, over = _decimal_total(values, kind)
            result = _fit(total, kind)  # EvalError when the total is out of range
            if over:
                raise Unsupported(f"SUM whose partial sums overflow {kind} although the total does not")
            return result

        return AggSpec(t, sum_decimal, "SUM")

    def sum_float(rows, env):
        values = gather(rows, env)
        if not values:
            return None
        total = _naive_sum(values)
        if math.isinf(total) and not any(math.isinf(v) for v in values):
            raise Unsupported("SUM of FLOAT64 overflowing to infinity")
        if len(values) > 1 and not _floats_sum_is_exact(values):
            env.ctx.inexact = True
        return total

    return AggSpec(T.FLOAT64, sum_float, "SUM")


def _divide_decimal(total: Decimal, count: int, kind: str) -> Decimal:
    scale = 9 if kind == "NUMERIC" else 38
    scaled = int(total.scaleb(scale, context=V.DEC))
    quotient, remainder = divmod(abs(scaled), count)
    if 2 * remainder >= count:
        quotient += 1
    result = Decimal(quotient).scaleb(-scale, context=V.DEC)
    if scaled < 0 and quotient:
        result = -result
    return _fit(result, kind)


def _avg(compiler, node, cx, nulls) -> AggSpec:
    _only(node, "this")
    _reject_nulls(nulls, "AVG")
    value, distinct = _numeric_argument(compiler, cx, node, "AVG")
    fn, t = value.fn, value.type
    kind = t.kind

    def gather(rows, env):
        values = _non_null(_values(fn, rows, env))
        return _distinct(t, values) if distinct else values

    if kind == "INT64":

        def avg_int(rows, env):
            values = gather(rows, env)
            if not values:
                return None
            total = sum(values)
            if abs(total) >= 2**53:
                # the division of an integer too big for a double may round twice in BigQuery
                raise Unsupported("AVG of INT64 with a sum beyond 2**53")
            return total / len(values)

        return AggSpec(T.FLOAT64, avg_int, "AVG")
    if kind in ("NUMERIC", "BIGNUMERIC"):

        def avg_decimal(rows, env):
            values = gather(rows, env)
            if not values:
                return None
            total, over = _decimal_total(values, kind)
            if over:
                raise Unsupported(f"AVG whose sum leaves the range of {kind}")
            return _divide_decimal(total, len(values), kind)

        return AggSpec(t, avg_decimal, "AVG")

    def avg_float(rows, env):
        values = gather(rows, env)
        if not values:
            return None
        total = _naive_sum(values)
        if math.isinf(total) and not any(math.isinf(v) for v in values):
            raise Unsupported("AVG of FLOAT64 whose sum overflows")
        if len(values) > 1 and not _floats_sum_is_exact(values):
            env.ctx.inexact = True
        return total / len(values)

    return AggSpec(T.FLOAT64, avg_float, "AVG")


# ---------------------------------------------------------------------------------------------
# MIN, MAX, ANY_VALUE, MAX_BY, MIN_BY
# ---------------------------------------------------------------------------------------------


def _orderable(t: T.Type, name: str) -> None:
    if t.kind == "STRUCT":
        raise Unsupported(f"{name} of a STRUCT")
    if not T.comparable(t):
        raise AnalysisError(f"{name} does not support values of type {t}")


def _extreme(t: T.Type, values: list, want_max: bool) -> Any:
    """The largest or smallest of non-NULL values; refuses the cases whose answer BigQuery does not pin down."""

    kind = t.kind
    if kind == "FLOAT64":
        nans = [v for v in values if v != v]
        if nans:
            if len(nans) == len(values):
                return nans[0]
            raise Unsupported("MIN/MAX of FLOAT64 values mixing NaN and numbers")
        result = max(values) if want_max else min(values)
        if result == 0.0 and any(v == 0.0 and math.copysign(1.0, v) != math.copysign(1.0, result) for v in values):
            raise Unsupported("MIN/MAX of FLOAT64 mixing 0.0 and -0.0")
        return result
    if kind == "INTERVAL":
        key = V.Interval.total
        result = max(values, key=key) if want_max else min(values, key=key)
        if any(key(v) == key(result) and v != result for v in values):
            raise Unsupported("MIN/MAX of equal INTERVALs written differently")
        return result
    return max(values) if want_max else min(values)


def _min_max(compiler, node, cx, nulls, want_max: bool) -> AggSpec:
    name = "MAX" if want_max else "MIN"
    _only(node, "this", "expressions")
    _reject_nulls(nulls, name)
    if node.args.get("expressions"):
        raise AnalysisError(f"{name} takes one argument")
    if isinstance(node.this, exp.HavingMax):
        raise Unsupported(f"{name} with HAVING MAX/MIN")
    value, _ = _argument(compiler, cx, node.this, name)
    _orderable(value.type, name)
    fn, t = value.fn, value.type

    def compute(rows, env):
        values = _non_null(_values(fn, rows, env))
        if not values:
            return None
        return _extreme(t, values, want_max)

    return AggSpec(t, compute, name)


def _pick_by(t: T.Type, pairs: list, ordering: T.Type, want_max: bool, env: Env, name: str) -> Any:
    """``pairs`` are (value, ordering key) with a non-NULL key: the value of a row with the extreme key."""

    non_null = [(x, y) for x, y in pairs if x is not None]
    if not non_null:
        return None
    if ordering.kind == "FLOAT64" and any(y != y for _, y in pairs):
        raise Unsupported(f"{name} ordered by a FLOAT64 with NaN")
    best = non_null[0][1]
    for _, y in non_null:
        c = V.compare(ordering, y, best)
        if (c > 0) if want_max else (c < 0):
            best = y
    for x, y in pairs:
        if x is None:
            c = V.compare(ordering, y, best)
            if c == 0 or ((c > 0) if want_max else (c < 0)):
                # a NULL value at the extreme key: BigQuery's choice between skipping it and returning it is unknown
                raise Unsupported(f"{name} whose extreme row has a NULL value")
    chosen = [x for x, y in non_null if V.compare(ordering, y, best) == 0]
    if not _all_identical(t, chosen):
        env.ctx.nondet(f"{name} among several rows with the same ordering value")
    return chosen[0]


def _any_value(compiler, node, cx, nulls) -> AggSpec:
    _only(node, "this")
    _reject_nulls(nulls, "ANY_VALUE")
    this = node.this
    having = None
    if isinstance(this, exp.HavingMax):
        _only(this, "this", "expression", "max")
        having = this
        this = this.this
    value, _ = _argument(compiler, cx, this, "ANY_VALUE")
    fn, t = value.fn, value.type
    if having is None:

        def any_value(rows, env):
            values = _non_null(_values(fn, rows, env))
            if not values:
                return None
            if not _all_identical(t, values):
                env.ctx.nondet("ANY_VALUE of a group with different values")
            return values[0]

        return AggSpec(t, any_value, "ANY_VALUE")
    return _by_ordering(compiler, cx, value, having.args["expression"], bool(having.args["max"]), "ANY_VALUE HAVING")


def _by_ordering(compiler, cx, value: E, order_node, want_max: bool, name: str) -> AggSpec:
    ordering = compiler.expr(order_node, cx)
    _check_type(ordering, name)
    _orderable(ordering.type, name)
    fn, t, ofn, ot = value.fn, value.type, ordering.fn, ordering.type

    def compute(rows, env):
        ctx, ctes = env.ctx, env.ctes
        pairs = []
        for row in rows:
            row_env = Env(row, env, ctx, ctes)
            y = ofn(row_env)
            if y is not None:
                pairs.append((fn(row_env), y))
        if not pairs:
            return None
        return _pick_by(t, pairs, ot, want_max, env, name)

    return AggSpec(t, compute, name)


def _arg_by(compiler, node, cx, nulls, want_max: bool) -> AggSpec:
    name = "MAX_BY" if want_max else "MIN_BY"
    _only(node, "this", "expression")
    _reject_nulls(nulls, name)
    value, _ = _argument(compiler, cx, node.this, name)
    return _by_ordering(compiler, cx, value, node.args["expression"], want_max, name)


# ---------------------------------------------------------------------------------------------
# ARRAY_AGG, STRING_AGG, ARRAY_CONCAT_AGG
# ---------------------------------------------------------------------------------------------


class _Parts:
    __slots__ = ("value", "distinct", "order", "limit")

    def __init__(self, value, distinct, order, limit):
        self.value, self.distinct, self.order, self.limit = value, distinct, order, limit


def _peel(node) -> _Parts:
    """Limit(Order(Distinct(x))) -> the pieces; each layer is optional."""

    limit = order = None
    distinct = False
    if isinstance(node, exp.Limit):
        _only(node, "this", "expression")
        limit = node.args["expression"]
        node = node.this
    if isinstance(node, exp.Order):
        _only(node, "this", "expressions")
        order = list(node.expressions)
        node = node.this
    if isinstance(node, exp.Distinct):
        _only(node, "expressions")
        if len(node.expressions) != 1:
            raise AnalysisError("DISTINCT in an aggregate takes exactly one argument")
        distinct = True
        node = node.expressions[0]
    if node is None:
        raise AnalysisError("aggregate needs an argument")
    if isinstance(node, (exp.HavingMax, exp.Star)):
        raise Unsupported(f"aggregate of {type(node).__name__}")
    return _Parts(node, distinct, order, limit)


class _Collector:
    """The part ARRAY_AGG, STRING_AGG and ARRAY_CONCAT_AGG share: NULLs, DISTINCT, ORDER BY, LIMIT.

    ``run`` returns the values in order and whether that order is determined (``ORDER BY`` without undecided ties, or
    a single distinct value), whether ``LIMIT`` kept the same elements however ties are broken, and how many NULLs it
    dropped.
    """

    def __init__(self, compiler, cx, parts: _Parts, value: E, drop_nulls: bool, name: str):
        self.value = value
        self.distinct = parts.distinct
        self.drop_nulls = drop_nulls
        self.limit = None
        if parts.limit is not None:
            self.limit = compiler._constant_int(parts.limit, f"{name} LIMIT")
            if self.limit == 0:
                raise Unsupported(f"{name} with LIMIT 0")
        self.key_fns = []
        self.key_types = []
        self.spec = []
        if parts.order:
            if parts.distinct and len(parts.order) != 1:
                raise AnalysisError(f"{name}(DISTINCT ...) can only be ordered by its argument")
            for item in parts.order:
                if not isinstance(item, exp.Ordered):
                    raise Unsupported(f"{name} ORDER BY item {type(item).__name__}")
                _only(item, "this", "desc", "nulls_first")
                desc = bool(item.args.get("desc"))
                nulls_first = item.args.get("nulls_first")
                nulls_first = (not desc) if nulls_first is None else bool(nulls_first)
                key = compiler.expr(item.this, cx)
                _check_type(key, f"{name} ORDER BY")
                if not T.comparable(key.type):
                    raise AnalysisError(f"ORDER BY does not support expressions of type {key.type}")
                self.key_fns.append(key.fn)
                self.key_types.append(key.type)
                self.spec.append((key.type, desc, nulls_first))
            if parts.distinct:
                same = compiler.signature(parts.value, cx.scope) == compiler.signature(parts.order[0].this, cx.scope)
                if not same:
                    raise AnalysisError(f"{name}(DISTINCT x ORDER BY y) needs y to be x")

    def run(self, rows: list, env: Env) -> tuple[list, bool, bool, int]:
        compiler = _c()
        ctx, ctes = env.ctx, env.ctes
        fn, t = self.value.fn, self.value.type
        key_fns = self.key_fns
        items = []
        dropped = 0
        for row in rows:
            row_env = Env(row, env, ctx, ctes)
            value = fn(row_env)
            if value is None and self.drop_nulls:
                dropped += 1
                continue
            items.append((value, tuple(k(row_env) for k in key_fns)))
        if self.distinct:
            seen = set()
            unique = []
            for item in items:
                key = _key(t, item[0])
                if key not in seen:
                    seen.add(key)
                    unique.append(item)
            items = unique
        sort_keys = None
        determined = True
        if key_fns:
            items = compiler._sort(items, self.spec, ctx)
            sort_keys = [V.row_key(self.key_types, keys) for _, keys in items]
            start = 0
            n = len(items)
            while determined and start < n:
                end = start
                while end + 1 < n and sort_keys[end + 1] == sort_keys[start]:
                    end += 1
                if end > start and not _all_identical(t, [items[i][0] for i in range(start, end + 1)]):
                    determined = False
                start = end + 1
        else:
            determined = _all_identical(t, [item[0] for item in items])
        values = [item[0] for item in items]
        cut_ok = True
        if self.limit is not None:
            if len(values) > self.limit and not compiler._cut_is_determined(values, 0, self.limit, sort_keys):
                cut_ok = False
            values = values[: self.limit]
        return values, determined, cut_ok, dropped


def _array_agg(compiler, node, cx, nulls) -> AggSpec:
    _only(node, "this")
    parts = _peel(node.this)
    value = compiler.expr(parts.value, cx)
    _check_type(value, "ARRAY_AGG")
    t = value.type
    if parts.distinct and not T.groupable(t):
        raise AnalysisError(f"ARRAY_AGG(DISTINCT) does not support {t}")
    array_type = T.array(t)  # an array of arrays is an AnalysisError
    drop_nulls = nulls == "IGNORE"
    collector = _Collector(compiler, cx, parts, value, drop_nulls, "ARRAY_AGG")

    def compute(rows, env):
        if not rows:
            return None
        values, determined, cut_ok, _ = collector.run(rows, env)
        if not cut_ok:
            env.ctx.nondet("ARRAY_AGG LIMIT cuts among rows tied in the ORDER BY")
        if not values and collector.limit is None:
            raise Unsupported("ARRAY_AGG IGNORE NULLS over rows that are all NULL")
        return tuple(values) if determined else V.UnorderedArray(values)

    return AggSpec(array_type, compute, "ARRAY_AGG")


def _string_agg(compiler, node, cx, nulls) -> AggSpec:
    _only(node, "this", "separator")
    _reject_nulls(nulls, "STRING_AGG")
    parts = _peel(node.this)
    value = compiler.expr(parts.value, cx)
    _check_type(value, "STRING_AGG")
    if value.lit == "null":
        value = compiler.coerce(value, T.STRING)
    t = value.type
    if t.kind not in ("STRING", "BYTES"):
        raise AnalysisError(f"No matching signature for aggregate function STRING_AGG for argument type {t}")
    separator_node = node.args.get("separator")
    if separator_node is None:
        default = "," if t.kind == "STRING" else b","
        sep_fn = lambda env: default  # noqa: E731
    else:
        separator = compiler.expr(separator_node, cx)
        if separator.lit == "null":
            raise Unsupported("STRING_AGG with a NULL separator")
        separator = compiler.coerce(separator, t, "STRING_AGG separator")
        if separator.lit is None:
            raise Unsupported("STRING_AGG with a separator that is not a literal or parameter")
        sep_fn = separator.fn
    collector = _Collector(compiler, cx, parts, value, True, "STRING_AGG")

    def compute(rows, env):
        if not rows:
            return None
        values, determined, cut_ok, dropped = collector.run(rows, env)
        if collector.limit is not None and dropped:
            raise Unsupported("STRING_AGG with LIMIT over NULL values")
        if not values:
            return None
        sep = sep_fn(env)
        if sep is None:
            raise Unsupported("STRING_AGG with a NULL separator")
        if not determined:
            env.ctx.nondet("STRING_AGG order of the concatenation is not determined")
        if not cut_ok:
            env.ctx.nondet("STRING_AGG LIMIT cuts among rows tied in the ORDER BY")
        return sep.join(values)

    return AggSpec(t, compute, "STRING_AGG")


def _array_concat_agg(compiler, node, cx, nulls) -> AggSpec:
    _only(node, "this")
    _reject_nulls(nulls, "ARRAY_CONCAT_AGG")
    parts = _peel(node.this)
    if parts.distinct:
        raise Unsupported("ARRAY_CONCAT_AGG(DISTINCT ...)")
    if parts.limit is not None:
        raise Unsupported("ARRAY_CONCAT_AGG with LIMIT")
    value = compiler.expr(parts.value, cx)
    _check_type(value, "ARRAY_CONCAT_AGG")
    if value.lit == "null":
        raise Unsupported("ARRAY_CONCAT_AGG of NULL")
    t = value.type
    if t.kind != "ARRAY":
        raise AnalysisError(f"No matching signature for aggregate function ARRAY_CONCAT_AGG for argument type {t}")
    collector = _Collector(compiler, cx, parts, value, True, "ARRAY_CONCAT_AGG")

    def compute(rows, env):
        if not rows:
            return None
        arrays, determined, _, _ = collector.run(rows, env)
        if not arrays:
            return None
        flat = tuple(x for array in arrays for x in array)
        non_empty = [a for a in arrays if a]
        ordered = determined or len(non_empty) <= 1
        if not all(V.ordered_kind(a) for a in non_empty):
            ordered = False
        return flat if ordered else V.UnorderedArray(flat)

    return AggSpec(t, compute, "ARRAY_CONCAT_AGG")


# ---------------------------------------------------------------------------------------------
# LOGICAL_AND, LOGICAL_OR, BIT_AND, BIT_OR, BIT_XOR
# ---------------------------------------------------------------------------------------------


def _logical(compiler, node, cx, nulls, is_and: bool) -> AggSpec:
    name = "LOGICAL_AND" if is_and else "LOGICAL_OR"
    _only(node, "this")
    _reject_nulls(nulls, name)
    value, _ = _argument(compiler, cx, node.this, name)
    value = compiler.coerce(value, T.BOOL, f"{name} argument")
    fn = value.fn

    def compute(rows, env):
        values = _non_null(_values(fn, rows, env))
        if not values:
            return None
        return all(values) if is_and else any(values)

    return AggSpec(T.BOOL, compute, name)


def _bit(compiler, node, cx, nulls, op: str) -> AggSpec:
    name = f"BIT_{op}"
    _only(node, "this")
    _reject_nulls(nulls, name)
    value, _ = _argument(compiler, cx, node.this, name)
    if value.type != T.INT64:
        raise AnalysisError(f"No matching signature for aggregate function {name} for argument type {value.type}")
    fn = value.fn

    def compute(rows, env):
        values = _non_null(_values(fn, rows, env))
        if not values:
            return None
        result = values[0]
        for v in values[1:]:
            if op == "AND":
                result &= v
            elif op == "OR":
                result |= v
            else:
                result ^= v
        return result

    return AggSpec(T.INT64, compute, name)


# ---------------------------------------------------------------------------------------------
# STDDEV*, VAR*, CORR, COVAR*
# ---------------------------------------------------------------------------------------------


def _stat_argument(compiler, cx, node, name: str) -> E:
    value, _ = _argument(compiler, cx, node, name)
    kind = value.type.kind
    if value.lit == "null":
        return compiler.coerce(value, T.FLOAT64)
    if kind not in ("INT64", "FLOAT64", "NUMERIC", "BIGNUMERIC"):
        raise AnalysisError(f"No matching signature for aggregate function {name} for argument type {value.type}")
    return value


_SCALE = 1074  # a finite double is a whole multiple of 2**-1074


def _exact(values: list, kind: str) -> tuple[list, int]:
    """Whole numbers and the unit they count: the exact values are ``ints / unit``."""

    if kind == "INT64":
        return values, 1
    if kind in ("NUMERIC", "BIGNUMERIC"):
        scale = 9 if kind == "NUMERIC" else 38
        return [int(v.scaleb(scale, context=V.DEC)) for v in values], 10**scale
    out = []
    for v in values:
        num, den = float(v).as_integer_ratio()
        out.append(num << (_SCALE - den.bit_length() + 1))
    return out, 1 << _SCALE


def _to_double(value: Fraction) -> float:
    """The double nearest an exact value; one too large for a double is infinite, as BigQuery's final conversion gives."""

    try:
        return float(value)
    except OverflowError:
        return math.inf if value > 0 else -math.inf


def _sqrt_double(value: Fraction) -> float:
    """The square root of a non-negative exact value as a double (infinite when it does not fit)."""

    if value == 0:
        return 0.0
    num, den = value.numerator, value.denominator
    shift = (120 - (num.bit_length() - den.bit_length())) // 2
    root = math.isqrt((num << (2 * shift)) // den if shift >= 0 else num // (den << (-2 * shift)))
    try:
        return math.ldexp(float(root), -shift)
    except OverflowError:
        return math.inf


def _all_finite(values: list) -> bool:
    return all(not (isinstance(v, float) and (v != v or math.isinf(v))) for v in values)


def _split_having(node):
    """``x HAVING MAX y`` -> ``(x, HavingMax node)``; anything else -> ``(node, None)``."""

    if isinstance(node, exp.HavingMax):
        _only(node, "this", "expression", "max")
        return node.this, node
    return node, None


def _having_rows(compiler, cx, having, name: str):
    """A function keeping the rows whose ordering value is the largest (or smallest) non-NULL one."""

    if having is None:
        return None
    ordering = compiler.expr(having.args["expression"], cx)
    _check_type(ordering, name)
    _orderable(ordering.type, name)
    ofn, ot, want_max = ordering.fn, ordering.type, bool(having.args["max"])

    def keep(rows, env):
        ctx, ctes = env.ctx, env.ctes
        pairs = []
        for row in rows:
            y = ofn(Env(row, env, ctx, ctes))
            if y is not None:
                pairs.append((row, y))
        if not pairs:
            return []
        if ot.kind == "FLOAT64" and any(y != y for _, y in pairs):
            raise Unsupported(f"{name} HAVING over a FLOAT64 with NaN")
        best = pairs[0][1]
        for _, y in pairs:
            c = V.compare(ot, y, best)
            if (c > 0) if want_max else (c < 0):
                best = y
        return [row for row, y in pairs if V.compare(ot, y, best) == 0]

    return keep


def _with_having(spec: AggSpec, keep) -> AggSpec:
    if keep is None:
        return spec
    inner = spec.compute
    return AggSpec(spec.type, lambda rows, env: inner(keep(rows, env), env), spec.name + " HAVING")


def _variance(compiler, node, cx, nulls, name: str, sample: bool, root: bool) -> AggSpec:
    """VAR_*/STDDEV_*: computed exactly, then rounded once (an overflowing variance is infinity, as BigQuery returns).

    A NaN or infinite input makes the result NaN, a single value has variance 0 (NULL for the sample forms).
    """

    _only(node, "this")
    _reject_nulls(nulls, name)
    argument, having = _split_having(node.this)
    value = _stat_argument(compiler, cx, argument, name)
    keep = _having_rows(compiler, cx, having, name)
    fn, kind = value.fn, value.type.kind

    def compute(rows, env):
        values = _non_null(_values(fn, rows, env))
        n = len(values)
        if n == 0 or (sample and n < 2):
            return None
        if not _all_finite(values):
            return math.nan
        if n == 1:
            return 0.0
        xs, unit = _exact(values, kind)
        total = sum(xs)
        squares = sum(x * x for x in xs)
        spread = n * squares - total * total  # n**2 times the population variance, in units of 1 / unit**2
        variance = Fraction(spread, (n * (n - 1) if sample else n * n) * unit * unit)
        env.ctx.inexact = True
        return _sqrt_double(variance) if root else _to_double(variance)

    return _with_having(AggSpec(T.FLOAT64, compute, name), keep)


def _covariance(compiler, node, cx, nulls, name: str) -> AggSpec:
    """COVAR_*/CORR: exact like :func:`_variance`; CORR of a column with no variance is NaN."""

    _only(node, "this", "expression")
    _reject_nulls(nulls, name)
    right_node, having = _split_having(node.args["expression"])
    left = _stat_argument(compiler, cx, node.this, name)
    right = _stat_argument(compiler, cx, right_node, name)
    keep = _having_rows(compiler, cx, having, name)
    lfn, rfn = left.fn, right.fn
    lkind, rkind = left.type.kind, right.type.kind

    def compute(rows, env):
        ctx, ctes = env.ctx, env.ctes
        xs, ys = [], []
        for row in rows:
            row_env = Env(row, env, ctx, ctes)
            x, y = lfn(row_env), rfn(row_env)
            if x is not None and y is not None:
                xs.append(x)
                ys.append(y)
        n = len(xs)
        if n == 0 or (name != "COVAR_POP" and n < 2):
            return None
        if not (_all_finite(xs) and _all_finite(ys)):
            return math.nan
        sx, ux = _exact(xs, lkind)
        sy, uy = _exact(ys, rkind)
        total_x, total_y = sum(sx), sum(sy)
        cxy = n * sum(a * b for a, b in zip(sx, sy)) - total_x * total_y
        if n > 1:
            env.ctx.inexact = True
        if name == "COVAR_POP":
            return _to_double(Fraction(cxy, n * n * ux * uy))
        if name == "COVAR_SAMP":
            return _to_double(Fraction(cxy, n * (n - 1) * ux * uy))
        cxx = n * sum(a * a for a in sx) - total_x * total_x
        cyy = n * sum(b * b for b in sy) - total_y * total_y
        if cxx == 0 or cyy == 0:
            return math.nan
        ratio = _sqrt_double(Fraction(cxy * cxy, cxx * cyy))  # the units cancel
        return -ratio if cxy < 0 else ratio

    return _with_having(AggSpec(T.FLOAT64, compute, name), keep)


# ---------------------------------------------------------------------------------------------
# dispatch
# ---------------------------------------------------------------------------------------------

_BUILDERS: dict = {
    exp.Count: _count,
    exp.CountIf: _countif,
    exp.Sum: _sum,
    exp.Avg: _avg,
    exp.Min: lambda c, n, cx, nulls: _min_max(c, n, cx, nulls, False),
    exp.Max: lambda c, n, cx, nulls: _min_max(c, n, cx, nulls, True),
    exp.AnyValue: _any_value,
    exp.ArgMax: lambda c, n, cx, nulls: _arg_by(c, n, cx, nulls, True),
    exp.ArgMin: lambda c, n, cx, nulls: _arg_by(c, n, cx, nulls, False),
    exp.ArrayAgg: _array_agg,
    exp.GroupConcat: _string_agg,
    exp.ArrayConcatAgg: _array_concat_agg,
    exp.LogicalAnd: lambda c, n, cx, nulls: _logical(c, n, cx, nulls, True),
    exp.LogicalOr: lambda c, n, cx, nulls: _logical(c, n, cx, nulls, False),
    exp.BitwiseAndAgg: lambda c, n, cx, nulls: _bit(c, n, cx, nulls, "AND"),
    exp.BitwiseOrAgg: lambda c, n, cx, nulls: _bit(c, n, cx, nulls, "OR"),
    exp.BitwiseXorAgg: lambda c, n, cx, nulls: _bit(c, n, cx, nulls, "XOR"),
    exp.Stddev: lambda c, n, cx, nulls: _variance(c, n, cx, nulls, "STDDEV", True, True),
    exp.StddevSamp: lambda c, n, cx, nulls: _variance(c, n, cx, nulls, "STDDEV_SAMP", True, True),
    exp.StddevPop: lambda c, n, cx, nulls: _variance(c, n, cx, nulls, "STDDEV_POP", False, True),
    exp.Variance: lambda c, n, cx, nulls: _variance(c, n, cx, nulls, "VAR_SAMP", True, False),
    exp.VariancePop: lambda c, n, cx, nulls: _variance(c, n, cx, nulls, "VAR_POP", False, False),
    exp.Corr: lambda c, n, cx, nulls: _covariance(c, n, cx, nulls, "CORR"),
    exp.CovarPop: lambda c, n, cx, nulls: _covariance(c, n, cx, nulls, "COVAR_POP"),
    exp.CovarSamp: lambda c, n, cx, nulls: _covariance(c, n, cx, nulls, "COVAR_SAMP"),
}


def compile_aggregate(compiler, node, cx) -> AggSpec:
    """Type an aggregate call (possibly wrapped in IGNORE/RESPECT NULLS) and return its :class:`AggSpec`."""

    nulls = None
    if isinstance(node, (exp.IgnoreNulls, exp.RespectNulls)):
        _only(node, "this")
        nulls = "IGNORE" if isinstance(node, exp.IgnoreNulls) else "RESPECT"
        node = node.this
    builder = _BUILDERS.get(type(node))
    if builder is None:
        if is_aggregate(node):
            raise Unsupported(f"aggregate function {type(node).__name__}")
        raise AnalysisError(f"{type(node).__name__} is not an aggregate function")
    if nulls == "RESPECT" and not isinstance(node, exp.ArrayAgg):
        raise Unsupported(f"{type(node).__name__} with RESPECT NULLS")
    return builder(compiler, node, cx, nulls)


# ---------------------------------------------------------------------------------------------
# GROUPING()
# ---------------------------------------------------------------------------------------------


@register_node(exp.Grouping)
def _grouping_node(node):
    _only(node, "expressions")
    if len(node.expressions) != 1:
        raise AnalysisError("GROUPING takes exactly one argument")
    return "GROUPING", list(node.expressions)


@register("GROUPING")
def _grouping(compiler, node, args, cx):
    """``GROUPING(x)``: 0 when ``x`` is a key of the grouping set a row belongs to, 1 when ROLLUP/CUBE rolled it up."""

    info = cx.replace.get("__grouping__")
    if info is None or cx.group is None:
        raise AnalysisError("GROUPING can only be used in a query with GROUP BY")
    signatures, mask_slot = info
    signature = compiler.signature(node.expressions[0], cx.group.base)
    positions = [i for i, s in enumerate(signatures) if s == signature and s[0] != "unresolved"]
    if not positions:
        raise Unsupported("GROUPING of an expression the GROUP BY lists in another form")
    position = positions[0]
    return E(T.INT64, lambda env: 0 if position in env.row[mask_slot] else 1)
