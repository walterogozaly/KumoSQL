"""Window (analytic) functions: ``fn(...) OVER (PARTITION BY ... ORDER BY ... frame)``.

``compile_window`` turns a sqlglot ``exp.Window`` into a :class:`WindowFn` (with its static ``type``), and
``apply_windows`` runs several of them over the rows of a SELECT, appending one value per spec to every row.

Semantics follow BigQuery:

* Partitions are formed with ``values.group_key`` (NULLs and NaNs group together); inside a partition the rows
  are sorted with the compiler's own ``_sort`` (NULLS FIRST for ASC, LAST for DESC unless stated).
* Without ORDER BY every row of a partition is a peer of every other. The default frame is ``RANGE BETWEEN
  UNBOUNDED PRECEDING AND CURRENT ROW`` with ORDER BY and the whole partition without.
* A ``RANGE`` frame with an offset needs exactly one numeric ORDER BY key; rows whose key is NULL see only their
  NULL peers through an offset boundary and rows with a non-NULL key never see NULL rows through one.
* When ties in the ORDER BY make an answer depend on the order of the tied rows, ``ctx.nondet`` is called.
  This is decided exactly, by checking that swapping any two adjacent tied (and different) rows leaves the
  multiset of ``(row, result)`` pairs of the partition unchanged; past a work budget it assumes the worst.
"""

from __future__ import annotations

import bisect
import math
from collections import Counter
from decimal import Decimal
from fractions import Fraction

from sqlglot import exp

from . import types as T
from . import values as V
from .errors import AnalysisError, EvalError, Unsupported
from .runtime import E, Env

_BUDGET = 400  # recomputations of one partition allowed when proving that ties do not matter

# frame boundary kinds, in the order a frame must respect
UNB_PREC, PREC, CUR, FOLL, UNB_FOLL = range(5)


# ---------------------------------------------------------------------------------------------
# public entry points
# ---------------------------------------------------------------------------------------------


def compile_window(compiler, node: exp.Window, cx, named_windows: dict) -> "WindowFn":
    from .compiler import _only

    _only(node, "this", "partition_by", "order", "spec", "alias", "over")
    if node.args.get("over") not in ("OVER", True) and node.args.get("over") is not None:
        raise Unsupported("window clause form")
    definition = _resolve(node, named_windows, ())
    function = node.this
    if isinstance(function, exp.Identifier):
        raise Unsupported("window reference without a function")
    if _contains_window(function):
        raise AnalysisError("Analytic functions cannot be nested")
    for part in definition.partition + ([definition.order] if definition.order is not None else []):
        if _contains_window(part):
            raise AnalysisError("Analytic functions are not allowed in a window specification")
    nulls, inner = _unwrap(function)

    partition = []
    for p in definition.partition:
        value = compiler.expr(p, cx)
        if not T.groupable(value.type):
            raise AnalysisError(f"Partitioning by expressions of type {value.type} is not allowed")
        partition.append(value)
    order_keys: list = []  # (E, desc, nulls_first)
    if definition.order is not None:
        _only(definition.order, "expressions")
        for item in definition.order.expressions:
            ordered = item if isinstance(item, exp.Ordered) else exp.Ordered(this=item)
            _only(ordered, "this", "desc", "nulls_first")
            target = ordered.this
            if isinstance(target, exp.Literal) and not target.is_string:
                raise Unsupported("window ORDER BY a numeric literal")
            key = compiler.expr(target, cx)
            if not T.comparable(key.type):
                raise AnalysisError(f"ORDER BY does not support expressions of type {key.type}")
            desc = bool(ordered.args.get("desc"))
            nulls_first = ordered.args.get("nulls_first")
            order_keys.append((key, desc, (not desc) if nulls_first is None else bool(nulls_first)))
    layout = _Shape(partition, order_keys)

    maker = _MAKERS.get(type(inner))
    if maker is not None:
        func = maker(compiler, inner, nulls, cx, definition, layout)
        return func
    from . import aggregates

    if aggregates.is_aggregate(inner):
        return _make_aggregate(compiler, function, inner, cx, definition, layout)
    raise Unsupported(f"window function {type(inner).__name__}")


def apply_windows(specs: list, rows: list, env: Env) -> list:
    if not specs:
        return [tuple(r) for r in rows]
    columns = [spec.compute(rows, env) for spec in specs]
    return [tuple(row) + tuple(col[i] for col in columns) for i, row in enumerate(rows)]


# ---------------------------------------------------------------------------------------------
# window definitions
# ---------------------------------------------------------------------------------------------


class _Def:
    __slots__ = ("partition", "order", "frame")

    def __init__(self, partition, order, frame):
        self.partition = partition
        self.order = order
        self.frame = frame


def _resolve(node: exp.Window, named: dict, chain: tuple) -> _Def:
    """The PARTITION BY / ORDER BY / frame of a window specification, with its named base window merged in."""

    from .compiler import _only

    _only(node, "this", "partition_by", "order", "spec", "alias", "over", "comments")
    own_partition = list(node.args.get("partition_by") or [])
    own_order = node.args.get("order")
    own_frame = node.args.get("spec")
    base_name = node.args.get("alias")
    if base_name is None:
        return _Def(own_partition, own_order, own_frame)
    name = base_name.name.lower()
    if name in chain:
        raise AnalysisError(f"Window {name} refers to itself")
    if name not in named:
        raise AnalysisError(f"Window {base_name.name} is not defined")
    names = list(named)
    own_name = chain[-1] if chain else None
    if own_name is not None and names.index(name) > names.index(own_name):
        raise Unsupported("named window referring to a later window")
    base = _resolve(named[name], named, chain + (name,))
    if own_partition:
        raise AnalysisError("PARTITION BY is not allowed in a window that refers to a named window")
    if own_order is not None and base.order is not None:
        raise AnalysisError("ORDER BY is not allowed in a window that refers to a named window with ORDER BY")
    if own_frame is not None and base.frame is not None:
        raise AnalysisError("A window frame is not allowed in a window that refers to a named window with a frame")
    if base.frame is not None and own_order is not None:
        raise Unsupported("ORDER BY added to a named window that has a frame")
    return _Def(base.partition, own_order if own_order is not None else base.order, own_frame if own_frame is not None else base.frame)


def _unwrap(function: exp.Expression):
    nulls = None
    inner = function
    while isinstance(inner, (exp.IgnoreNulls, exp.RespectNulls)):
        kind = "ignore" if isinstance(inner, exp.IgnoreNulls) else "respect"
        if nulls is not None:
            raise Unsupported("both IGNORE NULLS and RESPECT NULLS")
        nulls = kind
        inner = inner.this
    return nulls, inner


def _contains_window(node: exp.Expression) -> bool:
    from .compiler import is_query

    if isinstance(node, exp.Window):
        return True
    if is_query(node):
        return False
    return any(_contains_window(child) for child in node.iter_expressions())


# ---------------------------------------------------------------------------------------------
# partitions
# ---------------------------------------------------------------------------------------------


class _Shape:
    """PARTITION BY and ORDER BY of one window, compiled."""

    def __init__(self, partition: list, order: list):
        self.partition = partition
        self.order = order
        self.sort_spec = [(e.type, desc, nf) for e, desc, nf in order]


class _Layout:
    """One partition after sorting: positions, peer groups, order keys and (lazily) the frame of every position."""

    __slots__ = ("n", "peer_start", "peer_end", "group_no", "keys", "desc", "key_type", "_bounds")

    def __init__(self, n, peer_start, peer_end, group_no, keys, desc, key_type):
        self.n, self.peer_start, self.peer_end, self.group_no = n, peer_start, peer_end, group_no
        self.keys, self.desc, self.key_type = keys, desc, key_type
        self._bounds = None

    def bounds(self, frame: "_Frame") -> list:
        if self._bounds is None:
            self._bounds = frame.compute(self)
        return self._bounds


class _Arr:
    """A layout plus an arrangement of the original rows over its positions (tied rows may be permuted)."""

    __slots__ = ("layout", "idx", "rows")

    def __init__(self, layout, idx, rows):
        self.layout, self.idx, self.rows = layout, idx, rows


def _partitions(shape: _Shape, rows: list, env: Env):
    """[(sorted original indices, _Layout)] for every partition, in first-appearance order."""

    ctx = env.ctx
    groups: dict = {}
    order_values: list = [()] * len(rows)
    for i, row in enumerate(rows):
        row_env = Env(row, env, ctx, env.ctes)
        key = tuple(V.group_key(e.type, e.fn(row_env)) for e in shape.partition)
        groups.setdefault(key, []).append(i)
        if shape.order:
            order_values[i] = tuple(e.fn(row_env) for e, _, _ in shape.order)
    from .compiler import _sort

    out = []
    for members in groups.values():
        if shape.order:
            items = _sort([(i, order_values[i]) for i in members], shape.sort_spec, ctx)
            idx = [i for i, _ in items]
        else:
            idx = list(members)
        n = len(idx)
        peer_start = [0] * n
        peer_end = [n] * n
        group_no = [0] * n
        if shape.order:
            kinds = [e.type for e, _, _ in shape.order]
            gkeys = [tuple(V.group_key(t, v) for t, v in zip(kinds, order_values[i])) for i in idx]
        else:
            gkeys = [()] * n
        start = 0
        group = 0
        for p in range(1, n + 1):
            if p == n or gkeys[p] != gkeys[start]:
                for q in range(start, p):
                    peer_start[q], peer_end[q], group_no[q] = start, p, group
                start, group = p, group + 1
        first_key = [order_values[i][0] for i in idx] if shape.order else [None] * n
        desc = shape.order[0][1] if shape.order else False
        key_type = shape.order[0][0].type if shape.order else None
        out.append((idx, _Layout(n, peer_start, peer_end, group_no, first_key, desc, key_type)))
    return out


# ---------------------------------------------------------------------------------------------
# frames
# ---------------------------------------------------------------------------------------------


class _Frame:
    def __init__(self, kind: str, start: tuple, end: tuple, key_numeric: bool):
        self.kind, self.start, self.end = kind, start, end
        self.offsets = kind == "RANGE" and (start[0] in (PREC, FOLL) or end[0] in (PREC, FOLL))

    def compute(self, layout: _Layout) -> list:
        n = layout.n
        out = []
        if self.kind == "ROWS":
            for i in range(n):
                lo = self._rows_lo(i)
                hi = self._rows_hi(i, n)
                lo, hi = max(lo, 0), min(hi, n)
                out.append((lo, hi) if hi > lo else (0, 0))
            return out
        if not self.offsets:
            for i in range(n):
                lo = 0 if self.start[0] == UNB_PREC else layout.peer_start[i]
                hi = n if self.end[0] == UNB_FOLL else layout.peer_end[i]
                out.append((lo, hi) if hi > lo else (0, 0))
            return out
        keys = layout.keys
        non_null = [p for p in range(n) if keys[p] is not None]
        a = non_null[0] if non_null else 0
        b = non_null[-1] + 1 if non_null else 0
        if any(isinstance(keys[p], float) and keys[p] != keys[p] for p in non_null):
            raise Unsupported("RANGE offset over a NaN key")
        s = [None] * n
        for p in non_null:
            s[p] = -keys[p] if layout.desc else keys[p]
        for i in range(n):
            if keys[i] is None:  # NULL keys only reach their peers through an offset boundary
                lo = self._null_bound(self.start, layout.peer_start[i], i, layout, True)
                hi = self._null_bound(self.end, layout.peer_end[i], i, layout, False)
            else:
                lo = self._range_bound(self.start, s, i, a, b, layout, True)
                hi = self._range_bound(self.end, s, i, a, b, layout, False)
            out.append((lo, hi) if hi > lo else (0, 0))
        return out

    def _rows_lo(self, i):
        kind, k = self.start[0], self.start[1] if len(self.start) > 1 else 0
        return 0 if kind == UNB_PREC else i - k if kind == PREC else i if kind == CUR else i + k

    def _rows_hi(self, i, n):
        kind, k = self.end[0], self.end[1] if len(self.end) > 1 else 0
        return n if kind == UNB_FOLL else i + 1 if kind == CUR else i - k + 1 if kind == PREC else i + k + 1

    @staticmethod
    def _null_bound(boundary, peer_edge, i, layout, is_start):
        kind = boundary[0]
        if kind == UNB_PREC:
            return 0
        if kind == UNB_FOLL:
            return layout.n
        return peer_edge  # CURRENT ROW, and an offset on a NULL key

    @staticmethod
    def _range_bound(boundary, s, i, a, b, layout, is_start):
        kind = boundary[0]
        if kind == UNB_PREC:
            return 0
        if kind == UNB_FOLL:
            return layout.n
        if kind == CUR:
            return layout.peer_start[i] if is_start else layout.peer_end[i]
        target = s[i] - boundary[1] if kind == PREC else s[i] + boundary[1]
        if is_start:
            return bisect.bisect_left(s, target, a, b)
        return bisect.bisect_right(s, target, a, b)


def _boundary(compiler, side: str, offset, which: str, kind: str, key_type, one_key: bool) -> tuple:
    """A frame boundary node of a ``WindowSpec`` as ``(kind[, offset])``."""

    if offset is None:
        raise AnalysisError("Window frame boundary missing")
    if isinstance(offset, str):
        text = offset.upper()
        if text == "CURRENT ROW":
            return (CUR,)
        if text == "UNBOUNDED":
            side = (side or "").upper()
            if side == "PRECEDING":
                if which == "end":
                    raise AnalysisError("Ending window frame boundary cannot be UNBOUNDED PRECEDING")
                return (UNB_PREC,)
            if side == "FOLLOWING":
                if which == "start":
                    raise AnalysisError("Starting window frame boundary cannot be UNBOUNDED FOLLOWING")
                return (UNB_FOLL,)
        raise Unsupported(f"window frame boundary {offset}")
    side = (side or "").upper()
    if side not in ("PRECEDING", "FOLLOWING"):
        raise Unsupported("window frame boundary side")
    code = PREC if side == "PRECEDING" else FOLL
    if isinstance(offset, exp.Interval):
        raise Unsupported("window frame offset of type INTERVAL")
    if kind == "ROWS":
        count = compiler._constant_int(offset, "Window frame offset")
        return (code, count)
    return (code, _range_offset(compiler, offset, key_type, one_key))


def _range_offset(compiler, node: exp.Expression, key_type, one_key: bool):
    from .compiler import Cx, EmptyScope

    if not one_key:
        raise AnalysisError("A RANGE window frame with an offset needs exactly one ORDER BY key")
    if key_type is None or not key_type.is_numeric:
        if key_type is not None and key_type.kind in ("DATE", "DATETIME", "TIMESTAMP", "TIME"):
            raise Unsupported(f"RANGE offset over an ORDER BY key of type {key_type}")
        raise AnalysisError("A RANGE window frame with an offset needs a numeric ORDER BY key")
    value = compiler.expr(node, Cx(EmptyScope()))
    if value.lit is None:
        raise Unsupported("RANGE window frame offset that is not a literal or parameter")
    value = compiler.coerce(value, key_type, "Window frame offset")
    payload = value.fn(None)
    if payload is None:
        raise AnalysisError("Window frame offset must not be NULL")
    if isinstance(payload, float) and payload != payload:
        raise Unsupported("NaN window frame offset")
    if payload < 0:
        raise AnalysisError("Window frame offset must not be negative")
    return payload


def _build_frame(compiler, definition: _Def, layout: _Shape, name: str, allowed: bool) -> _Frame:
    """The frame of a window function that accepts one (the default frame when the query names none)."""

    frame = definition.frame
    has_order = bool(layout.order)
    if frame is None:
        return _Frame("RANGE" if has_order else "ROWS", (UNB_PREC,), (CUR,) if has_order else (UNB_FOLL,), True)
    if not allowed:
        raise AnalysisError(f"Window framing clause is not allowed for analytic function {name}")
    from .compiler import _only

    _only(frame, "kind", "start", "start_side", "end", "end_side")
    kind = str(frame.args.get("kind") or "").upper()
    if kind not in ("ROWS", "RANGE"):
        raise Unsupported(f"window frame unit {kind or 'missing'}")
    key_type = layout.order[0][0].type if has_order else None
    one_key = len(layout.order) == 1
    start = _boundary(compiler, frame.args.get("start_side"), frame.args.get("start"), "start", kind, key_type, one_key)
    end_node = frame.args.get("end")
    if end_node is None:
        if start[0] == FOLL:
            raise AnalysisError("A window frame without BETWEEN cannot start at n FOLLOWING")
        if start[0] == UNB_FOLL:
            raise AnalysisError("Starting window frame boundary cannot be UNBOUNDED FOLLOWING")
        end = (CUR,)
    else:
        end = _boundary(compiler, frame.args.get("end_side"), end_node, "end", kind, key_type, one_key)
    if start[0] > end[0]:
        raise Unsupported("window frame whose start comes after its end")
    return _Frame(kind, start, end, True)


# ---------------------------------------------------------------------------------------------
# the compiled window function
# ---------------------------------------------------------------------------------------------


class WindowFn:
    """A compiled window function. ``type`` is its static type; ``compute(rows, env)`` returns one value per row."""

    peer_invariant = False  # every peer gets the same answer whatever the order of tied rows

    def __init__(self, type: T.Type, shape: _Shape, name: str):
        self.type = type
        self.shape = shape
        self.name = name

    # subclasses
    def prepare(self, rows: list, env: Env):
        return None

    def run(self, arr: _Arr, state) -> list:
        raise NotImplementedError

    def compute(self, rows: list, env: Env) -> list:
        results: list = [None] * len(rows)
        if not rows:
            return results
        state = self.prepare(rows, env)
        for idx, layout in _partitions(self.shape, rows, env):
            arr = _Arr(layout, idx, [rows[i] for i in idx])
            values = self.run(arr, state)
            for i, v in zip(idx, values):
                results[i] = v
            if not self.peer_invariant and not self._tie_free(arr, values, state):
                env.ctx.nondet(f"window function {self.name} over ties in its ORDER BY")
        return results

    def _tie_free(self, arr: _Arr, values: list, state) -> bool:
        """Whether swapping tied rows cannot change the partition's multiset of (row, result) pairs."""

        layout = arr.layout
        swaps = []
        for p in range(layout.n - 1):
            if layout.group_no[p] == layout.group_no[p + 1] and repr(arr.rows[p]) != repr(arr.rows[p + 1]):
                swaps.append(p)
        if not swaps:
            return True
        if len(swaps) > _BUDGET:
            return False
        rtype = self.type
        base = Counter((repr(r), V.group_key(rtype, v)) for r, v in zip(arr.rows, values))
        for p in swaps:
            idx = list(arr.idx)
            rows = list(arr.rows)
            idx[p], idx[p + 1] = idx[p + 1], idx[p]
            rows[p], rows[p + 1] = rows[p + 1], rows[p]
            other = self.run(_Arr(layout, idx, rows), state)
            if Counter((repr(r), V.group_key(rtype, v)) for r, v in zip(rows, other)) != base:
                return False
        return True


def _require_order(layout: _Shape, name: str) -> None:
    if not layout.order:
        raise AnalysisError(f"Window ORDER BY is required for analytic function {name}")


def _no_frame(definition: _Def, name: str) -> None:
    if definition.frame is not None:
        raise AnalysisError(f"Window framing clause is not allowed for analytic function {name}")


def _no_nulls(nulls, name: str) -> None:
    if nulls is not None:
        raise Unsupported(f"{'IGNORE' if nulls == 'ignore' else 'RESPECT'} NULLS on {name}")


# --- numbering functions ------------------------------------------------------------------------------


class _Numbering(WindowFn):
    def __init__(self, shape, name, kind, buckets=None):
        super().__init__(T.FLOAT64 if kind in ("percent_rank", "cume_dist") else T.INT64, shape, name)
        self.kind = kind
        self.buckets = buckets
        self.peer_invariant = kind != "row_number" and kind != "ntile"

    def run(self, arr, state):
        layout, n, kind = arr.layout, arr.layout.n, self.kind
        if kind == "row_number":
            return list(range(1, n + 1))
        if kind == "rank":
            return [p + 1 for p in layout.peer_start]
        if kind == "dense_rank":
            return [g + 1 for g in layout.group_no]
        if kind == "percent_rank":
            return [0.0 if n == 1 else (layout.peer_start[i]) / (n - 1) for i in range(n)]
        if kind == "cume_dist":
            return [layout.peer_end[i] / n for i in range(n)]
        buckets = self.buckets
        base, extra = divmod(n, buckets)
        cut = extra * (base + 1)
        out = []
        for i in range(n):
            if i < cut:
                out.append(i // (base + 1) + 1)
            else:
                out.append(extra + (i - cut) // base + 1)
        return out


def _make_numbering(name: str, kind: str, order_required: bool):
    def make(compiler, node, nulls, cx, definition, layout):
        from .compiler import _only

        _only(node, *(("this",) if kind in ("row_number", "ntile") else ()))
        _no_nulls(nulls, name)
        _no_frame(definition, name)
        if order_required:
            _require_order(layout, name)
        buckets = None
        if kind == "ntile":
            if node.args.get("this") is None:
                raise AnalysisError("NTILE needs the number of buckets")
            buckets = compiler._constant_int(node.this, "NTILE")
            if buckets <= 0:
                raise AnalysisError("NTILE buckets must be positive")
        elif node.args.get("this") is not None:
            raise AnalysisError(f"{name} takes no arguments")
        return _Numbering(layout, name, kind, buckets)

    return make


# --- navigation functions -----------------------------------------------------------------------------


class _Lag(WindowFn):
    def __init__(self, shape, name, value: E, default: E | None, offset: int, sign: int, ignore: bool):
        super().__init__(value.type, shape, name)
        self.value, self.default, self.offset, self.sign, self.ignore = value, default, offset, sign, ignore

    def prepare(self, rows, env):
        envs = [Env(r, env, env.ctx, env.ctes) for r in rows]
        values = [self.value.fn(e) for e in envs]
        defaults = [self.default.fn(e) for e in envs] if self.default is not None else None
        return values, defaults

    def run(self, arr, state):
        values, defaults = state
        n, idx, sign, offset = arr.layout.n, arr.idx, self.sign, self.offset
        out = []
        for i in range(n):
            if not self.ignore:
                j = i + sign * offset
                if 0 <= j < n:
                    out.append(values[idx[j]])
                else:
                    out.append(defaults[idx[i]] if defaults is not None else None)
                continue
            remaining, j, found = offset, i, False
            while True:
                j += sign
                if not 0 <= j < n:
                    break
                if values[idx[j]] is not None:
                    remaining -= 1
                    if remaining == 0:
                        found = True
                        break
            out.append(values[idx[j]] if found else (defaults[idx[i]] if defaults is not None else None))
        return out


def _make_lag(name: str, sign: int):
    def make(compiler, node, nulls, cx, definition, layout):
        from .compiler import _only

        _only(node, "this", "offset", "default")
        _no_frame(definition, name)
        _require_order(layout, name)
        value = compiler.expr(node.this, cx)
        offset = 1
        if node.args.get("offset") is not None:
            offset = compiler._constant_int(node.args["offset"], f"{name} offset")
        default = None
        if node.args.get("default") is not None:
            default = compiler.expr(node.args["default"], cx)
            if default.lit is None:
                raise Unsupported(f"{name} default that is not a literal or parameter")
            if value.lit == "null":
                target, (value, default) = compiler.unify([value, default], name)
            else:
                default = compiler.coerce(default, value.type, f"{name} default")
        ignore = nulls == "ignore"
        if ignore and offset == 0:
            raise Unsupported(f"{name} with offset 0 and IGNORE NULLS")
        return _Lag(layout, name, value, default, offset, sign, ignore)

    return make


class _Nth(WindowFn):
    """FIRST_VALUE, LAST_VALUE and NTH_VALUE over the frame."""

    def __init__(self, shape, name, value: E, frame: _Frame, which: str, n: int, ignore: bool):
        super().__init__(value.type, shape, name)
        self.value, self.frame, self.which, self.n, self.ignore = value, frame, which, n, ignore

    def prepare(self, rows, env):
        return [self.value.fn(Env(r, env, env.ctx, env.ctes)) for r in rows]

    def run(self, arr, values):
        idx = arr.idx
        bounds = arr.layout.bounds(self.frame)
        cache: dict = {}
        out = []
        for lo, hi in bounds:
            if (lo, hi) not in cache:
                cache[(lo, hi)] = self._pick(values, idx, lo, hi)
            out.append(cache[(lo, hi)])
        return out

    def _pick(self, values, idx, lo, hi):
        if hi <= lo:
            return None
        positions = range(lo, hi) if self.which != "last" else range(hi - 1, lo - 1, -1)
        count = 0
        target = self.n if self.which == "nth" else 1
        for p in positions:
            v = values[idx[p]]
            if self.ignore and v is None:
                continue
            count += 1
            if count == target:
                return v
        return None


def _make_nth(name: str, which: str):
    def make(compiler, node, nulls, cx, definition, layout):
        from .compiler import _only

        _only(node, "this", "offset") if which == "nth" else _only(node, "this")
        if which == "nth" and node.args.get("from_first") is not None:
            raise Unsupported("NTH_VALUE FROM LAST")
        value = compiler.expr(node.this, cx)
        n = 1
        if which == "nth":
            n = compiler._constant_int(node.args["offset"], "NTH_VALUE offset")
            if n <= 0:
                raise AnalysisError("NTH_VALUE offset must be positive")
        frame = _build_frame(compiler, definition, layout, name, True)
        return _Nth(layout, name, value, frame, which, n, nulls == "ignore")

    return make


# --- percentiles --------------------------------------------------------------------------------------


class _Percentile(WindowFn):
    peer_invariant = True

    def __init__(self, shape, name, value: E, percentile: E, disc: bool, result_type):
        super().__init__(result_type, shape, name)
        self.value, self.percentile, self.disc = value, percentile, disc

    def prepare(self, rows, env):
        values = [self.value.fn(Env(r, env, env.ctx, env.ctes)) for r in rows]
        return values, env

    def run(self, arr, state):
        values, env = state
        taken = [values[i] for i in arr.idx if values[i] is not None]
        p = self.percentile.fn(env)
        if p is None:
            raise EvalError("The percentile argument must not be NULL")
        fraction = Fraction(p)
        if not 0 <= fraction <= 1:
            raise EvalError("The percentile argument must be in [0, 1]")
        vtype = self.value.type
        taken.sort(key=lambda v: V.sort_key(vtype, v))
        n = len(taken)
        if n == 0:
            result = None
        elif self.disc:
            result = self._disc(taken, p, fraction, vtype, env)
        else:
            result = self._cont(taken, p, fraction, vtype, env)
        return [result] * arr.layout.n

    def _disc(self, taken, p, fraction, vtype, env):
        n = len(taken)
        exact = math.ceil(fraction * n)
        if isinstance(p, float) and math.ceil(p * n) != exact:
            raise Unsupported("PERCENTILE_DISC at a rank that float arithmetic places differently")
        position = max(exact - 1, 0)
        chosen = taken[position]
        key = V.sort_key(vtype, chosen)
        if any(V.sort_key(vtype, v) == key and repr(v) != repr(chosen) for v in taken):
            env.ctx.nondet("PERCENTILE_DISC chose between equal values that print differently")
        return chosen

    def _cont(self, taken, p, fraction, vtype, env):
        n = len(taken)
        exact = fraction * (n - 1)
        low = math.floor(exact)
        frac = exact - low
        if vtype.kind in ("INT64", "FLOAT64"):
            def as_float(v):
                if isinstance(v, int) and abs(v) > 2**53:
                    raise Unsupported("PERCENTILE_CONT over integers beyond 2**53")
                return float(v)

            if vtype.kind == "FLOAT64" and any(v != v for v in taken):
                raise Unsupported("PERCENTILE_CONT over NaN")
            lower = as_float(taken[low])
            if frac == 0:
                return lower
            upper = as_float(taken[low + 1])
            if not (math.isfinite(lower) and math.isfinite(upper)):
                raise Unsupported("PERCENTILE_CONT interpolating an infinite value")
            env.ctx.inexact = True
            return lower + float(frac) * (upper - lower)
        lower, upper = taken[low], taken[min(low + 1, n - 1)]
        if frac == 0:
            return lower
        value = Fraction(lower) + frac * (Fraction(upper) - Fraction(lower))
        scale = 9 if vtype.kind == "NUMERIC" else 38
        scaled = value * 10**scale
        if scaled.denominator != 1:
            raise Unsupported("PERCENTILE_CONT result that needs rounding in a decimal type")
        return Decimal(scaled.numerator).scaleb(-scale)


def _make_percentile(name: str, disc: bool):
    def make(compiler, node, nulls, cx, definition, layout):
        from .compiler import Cx, EmptyScope, _only

        _only(node, "this", "expression")
        if nulls == "respect":
            raise Unsupported(f"RESPECT NULLS on {name}")
        if layout.order:
            raise AnalysisError(f"Window ORDER BY is not allowed for analytic function {name}")
        _no_frame(definition, name)
        if node.args.get("expression") is None:
            raise AnalysisError(f"{name} needs a percentile argument")
        value = compiler.expr(node.this, cx)
        if disc:
            if not T.comparable(value.type):
                raise AnalysisError(f"{name} does not support values of type {value.type}")
            result = value.type
        else:
            if value.type.kind in ("INT64", "FLOAT64"):
                result = T.FLOAT64
            elif value.type.kind in ("NUMERIC", "BIGNUMERIC"):
                result = value.type
            elif value.lit == "null":
                result = T.FLOAT64
            else:
                raise AnalysisError(f"{name} needs a numeric argument, not {value.type}")
        percentile = compiler.expr(node.args["expression"], Cx(EmptyScope()))
        if percentile.lit is None:
            raise Unsupported(f"{name} percentile that is not a literal or parameter")
        if not percentile.type.is_numeric and percentile.lit != "null":
            raise AnalysisError(f"{name} percentile must be numeric")
        return _Percentile(layout, name, value, percentile, disc, result)

    return make


# --- aggregates as window functions --------------------------------------------------------------------


class _AggregateWindow(WindowFn):
    def __init__(self, shape, name, spec, frame: _Frame):
        super().__init__(spec.type, shape, name)
        self.spec, self.frame = spec, frame
        self.peer_invariant = False

    def prepare(self, rows, env):
        return env

    def run(self, arr, env):
        bounds = arr.layout.bounds(self.frame)
        cache: dict = {}
        out = []
        rows = arr.rows
        for lo, hi in bounds:
            if (lo, hi) not in cache:
                cache[(lo, hi)] = self.spec.compute(rows[lo:hi], env)
            out.append(cache[(lo, hi)])
        return out


def _make_aggregate(compiler, function, inner, cx, definition, layout) -> WindowFn:
    from . import aggregates

    name = type(inner).__name__.upper()
    if inner.find(exp.Distinct) is not None:
        if layout.order:
            raise AnalysisError("Window ORDER BY is not allowed if DISTINCT is specified")
        if definition.frame is not None:
            raise AnalysisError("Window framing clause is not allowed if DISTINCT is specified")
    frame = _build_frame(compiler, definition, layout, name, True)
    spec = aggregates.compile_aggregate(compiler, function, cx)
    return _AggregateWindow(layout, name, spec, frame)


# ---------------------------------------------------------------------------------------------

_MAKERS = {
    exp.RowNumber: _make_numbering("ROW_NUMBER", "row_number", False),
    exp.Rank: _make_numbering("RANK", "rank", True),
    exp.DenseRank: _make_numbering("DENSE_RANK", "dense_rank", True),
    exp.PercentRank: _make_numbering("PERCENT_RANK", "percent_rank", True),
    exp.CumeDist: _make_numbering("CUME_DIST", "cume_dist", True),
    exp.Ntile: _make_numbering("NTILE", "ntile", True),
    exp.Lag: _make_lag("LAG", -1),
    exp.Lead: _make_lag("LEAD", 1),
    exp.FirstValue: _make_nth("FIRST_VALUE", "first"),
    exp.LastValue: _make_nth("LAST_VALUE", "last"),
    exp.NthValue: _make_nth("NTH_VALUE", "nth"),
    exp.PercentileCont: _make_percentile("PERCENTILE_CONT", False),
    exp.PercentileDisc: _make_percentile("PERCENTILE_DISC", True),
}
