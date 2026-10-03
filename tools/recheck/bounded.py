"""Bounded-verified pairs of the ``bounded-*`` evals (``tools/bounded_bench.py run <suite> --rows 3``).

Those evals count a pair as "bounded at 3 rows" when ``kumosql.bounded_equivalence.check_bounded``
finds no difference on any database with at most 3 rows per table. Each pair is checked here exactly
as ``bounded_bench`` checks it (same entry point, schema, constraints, options, timeouts), and a
pair whose status is ``bounded`` (the whole 3-row bound finished) becomes a :class:`Case` whose
DuckDB SQL is the eval's own replay translation (``DuckDBReplay.left`` / ``.right``) on the replay's
column types. The search then looks only at databases the bounded claim covers:

* at most 3 rows in every table (the ``legal`` callback, plus a cap on the random generator's row
  counts so the budget is not spent on databases the callback rejects);
* the bounded schema's declared facts: NOT NULL columns, keys, foreign keys and ENUM values become
  engine ``Table`` facts; the bounded eval's extra assertions (VeriEQL's cross-row predicates and
  consecutive-id columns) are evaluated with the eval's own z3 builders on the concrete rows; values of
  NUMERIC-like columns stay inside the domain the encoding gives them.

The pair suites (SQLSolver, QED, R-Bot, Cosette, SPES) declare keys and NOT NULLs but no foreign keys
in their bounded schema (``bounded_bench.schema_from_tables``), so witnesses here may break a foreign
key the full eval declares; triage notes that.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
import datetime as dt
import itertools
import os
from pathlib import Path
import sys

TOOLS = Path(__file__).resolve().parent.parent
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))
if str(TOOLS.parent / "src") not in sys.path:
    sys.path.insert(0, str(TOOLS.parent / "src"))

from recheck import engine  # noqa: E402
from recheck.engine import Case, Column, Table  # noqa: E402

ROWS = 3  # the bound the evals report
# bounded_bench.py defaults (--timeout-ms, --budget); KUMOSQL_BOUNDED_SCALE stretches them on a loaded machine
_SCALE = float(os.environ.get("KUMOSQL_BOUNDED_SCALE", "1") or 1)
TIMEOUT_MS = int(20000 * _SCALE)
BUDGET_S = 60.0 * _SCALE


def _log(eval_name: str, pair: str, result, **extra) -> None:
    """One line per bounded check in ``$KUMOSQL_BOUNDED_LOG`` (status, bound, reason, seconds), so the
    counts can be compared with the results file; nothing when the variable is unset."""

    path = os.environ.get("KUMOSQL_BOUNDED_LOG")
    if not path:
        return
    import json

    if result is None:
        line = {"eval": eval_name, "pair": pair, "status": "crash", **extra}
    else:
        line = {"eval": eval_name, "pair": pair, "status": result.status.value, "bound": result.bound, "reason": result.reason[:200],
                "seconds": round(result.seconds, 2), **extra}
    with open(path, "a", encoding="utf-8") as sink:
        sink.write(json.dumps(line, default=str) + "\n")

_KIND = {"int": "int", "real": "float", "str": "text", "bool": "bool", "date": "date", "time": "time", "datetime": "timestamp", None: "int"}


# --- engine extensions, installed only in processes that build bounded cases ----------------------


def _install() -> None:
    _install_row_cap()
    _install_watchdog()
    _install_lazy_rows()


class _LazyProduct:
    """``itertools.product(*choices)`` as a sequence that is never materialized (a 16-column table with
    NULL and two values per column has 3**16 row options; the engine builds that list just to count it)."""

    def __init__(self, choices: list[list]):
        self.choices = choices
        self.size = 1
        for options in choices:
            self.size *= len(options)

    def __len__(self) -> int:
        return self.size

    def __bool__(self) -> bool:
        return self.size > 0

    def __getitem__(self, index: int) -> tuple:
        if index < 0:
            index += self.size
        if not 0 <= index < self.size:
            raise IndexError(index)
        out = []
        for options in reversed(self.choices):
            index, digit = divmod(index, len(options))
            out.append(options[digit])
        return tuple(reversed(out))

    def __iter__(self):
        return itertools.product(*self.choices)


def _install_lazy_rows() -> None:
    """``Generator._row_options`` returning a lazy product for bounded cases (same rows, same order)."""

    if getattr(engine.Generator, "_bounded_lazy_rows", False):
        return
    original = engine.Generator._row_options

    def _row_options(self, name, values, data):
        if not (isinstance(self.case.meta, dict) and self.case.meta.get("max_rows")):
            return original(self, name, values, data)
        case = self.case
        table = case.tables[name]
        key_columns = {k.lower() for key in table.keys for k in key}
        parent_values: dict[int, list] = {}
        for columns, parent, parent_columns in table.foreign_keys:
            if len(columns) == 1 and parent in data and parent != name:
                ptable = case.tables[parent]
                position = ptable.index(parent_columns[0])
                parent_values[table.index(columns[0])] = sorted({r[position] for r in data[parent] if r[position] is not None}, key=repr)
        choices = []
        for position, column in enumerate(table.columns):
            used = self.star or column.name.lower() in self.columns
            if position in parent_values:
                options = list(parent_values[position])
            elif column.values:
                options = list(column.values)[:3]
            elif used:
                options = [v for v in values.get(column.kind, []) if engine.fits(column, v)] or [engine._default(column.kind)]
            else:
                options = ["__fill__"] if column.name.lower() in key_columns else [engine._default(column.kind)]
            if not column.not_null and (used or position in parent_values):
                options = [None] + options
            choices.append(options)
        product = _LazyProduct(choices)
        return list(product) if len(product) <= 5000 else product

    engine.Generator._row_options = _row_options
    engine.Generator._bounded_lazy_rows = True


def _install_watchdog() -> None:
    """One watchdog thread per runner instead of a ``threading.Timer`` per query (under load, starting a
    thread per query took half of the search time); same interrupt after ``query_seconds``, checked
    every 0.2 s. Only for bounded cases (``meta["max_rows"]``); other cases keep the engine's own."""

    if getattr(engine.Runner, "_bounded_watchdog", False):
        return
    import threading
    import time

    original_run, original_close = engine.Runner.run, engine.Runner.close

    def _watch(runner) -> None:
        while not runner._watch_stop:
            time.sleep(0.2)
            deadline = runner._watch_deadline
            if deadline is not None and time.time() > deadline:
                runner._watch_deadline = None
                try:
                    runner.db.interrupt()
                except Exception:
                    pass

    def run(self, sql: str):
        if not (isinstance(self.case.meta, dict) and self.case.meta.get("max_rows")):
            return original_run(self, sql)
        if not hasattr(self, "_watch_stop"):
            self._watch_stop, self._watch_deadline = False, None
            threading.Thread(target=_watch, args=(self,), daemon=True).start()
        self._watch_deadline = time.time() + self.query_seconds
        try:
            return self.db.execute(sql).fetchall()
        except self.duckdb.Error as error:
            raise engine.QueryError(f"{type(error).__name__}: {str(error).splitlines()[0][:300]}") from None
        finally:
            self._watch_deadline = None

    def close(self) -> None:
        self._watch_stop = True
        original_close(self)

    engine.Runner.run, engine.Runner.close = run, close
    engine.Runner._bounded_watchdog = True


def _install_row_cap() -> None:
    """Cap the engine's random row counts for cases that carry ``meta["max_rows"]`` (installed only in
    processes that build bounded cases; every other case keeps the engine's own distribution)."""

    if getattr(engine.Generator, "_bounded_row_cap", False):
        return
    original = engine.Generator._row_count

    def _row_count(self) -> int:
        count = original(self)
        cap = self.case.meta.get("max_rows") if isinstance(self.case.meta, dict) else None
        if cap is None or count <= cap:
            return count
        return max(0, cap - self.rng.choice([0, 0, 0, 1]))  # mostly full tables: bigger is likelier to separate

    engine.Generator._row_count = _row_count
    engine.Generator._bounded_row_cap = True


# --- the bounded schema as engine tables and a legality check ------------------------------------


def engine_tables(schema, consecutive=()) -> dict[str, Table]:
    """Engine tables on the replay's DuckDB types; a consecutive-id column (``1..n``) draws from ``1..ROWS``
    and is a key (its values are distinct by definition)."""

    from kumosql import bounded_equivalence as be

    sequential = {(t, c.lower()) for t, c in consecutive}
    out = {}
    names = {n.lower(): n for n in schema.tables}
    for name, table in schema.tables.items():
        columns = []
        for c in table.columns:
            kind = be.kind_of(c.type)
            values = tuple(c.values) if (c.values and kind == "str") else ()
            if (name, c.name.lower()) in sequential:
                values = tuple(range(1, ROWS + 1))
            columns.append(Column(c.name, _KIND[kind], not_null=c.not_null, sql_type=be._duck_type(c), values=values))
        foreign = []
        for cols, parent, pcols in table.foreign_keys:
            resolved = parent if parent in schema.tables else names.get(parent.lower())
            if resolved is not None:
                foreign.append((tuple(cols), resolved, tuple(pcols)))
        keys = [tuple(k) for k in table.keys] + [(c,) for t, c in consecutive if t == name]
        out[name] = Table(name, columns, keys, foreign)
    return out


class _Concrete:
    """A stand-in for ``SymbolicDatabase``: the concrete rows as symbolic constants, for the eval's own
    extra-constraint builders (``bounded_bench._predicate``, ``bounded_bench._consecutive``)."""

    def __init__(self, schema, tables):
        self.schema = schema
        self.tables = tables


def _value(be, value, kind: str):
    if value is None:
        return be.null_value(kind)
    if kind == "real":
        from fractions import Fraction

        return be.const(Fraction(Decimal(repr(value)) if isinstance(value, float) else value), "real")
    if kind == "date" and isinstance(value, dt.datetime):
        value = value.date()
    return be.const(value, kind)


class Legal:
    """At most ``rows`` rows per table, real values inside the encoding's domain, and the bounded
    schema's extra assertions (picklable: rebuilt from the item in each worker)."""

    def __init__(self, schema, rows: int, consecutive: list[tuple[str, str]] = ()):
        from kumosql import bounded_equivalence as be

        self.schema = schema
        self.rows = rows
        self.consecutive = list(consecutive)
        self.domains = {}
        for name, table in schema.tables.items():
            for position, column in enumerate(table.columns):
                if be.kind_of(column.type) == "real":
                    domain = be._REAL_DOMAINS.get(be._base_type(column.type))
                    if domain is not None:
                        self.domains[(name, position)] = domain

    def __call__(self, data) -> bool:
        for name, rows in data.items():
            if len(rows) > self.rows:
                return False
        for (name, position), (digits, bound) in self.domains.items():
            for row in data.get(name, ()):
                value = row[position]
                if value is None:
                    continue
                try:
                    number = Decimal(repr(value)) if isinstance(value, float) else Decimal(value)
                except (InvalidOperation, TypeError, ValueError):
                    return False
                if not number.is_finite() or abs(number) >= bound:
                    return False
                if digits is not None and number != round(number, digits):
                    return False
        ordered = {}
        for name, column in self.consecutive:
            if name not in data:
                continue
            index = [c.name.lower() for c in self.schema.tables[name].columns].index(column.lower())
            values = [row[index] for row in data[name]]
            if any(v is None or isinstance(v, bool) or not isinstance(v, int) for v in values):
                return False
            if sorted(values) != list(range(1, len(values) + 1)):
                return False
            ordered[name] = index  # the encoding numbers rows in slot order: compare in id order
        if not self.schema.extra:
            return True
        return self._extras(data, ordered)

    def _extras(self, data, ordered) -> bool:
        import z3

        from kumosql import bounded_equivalence as be

        tables = {}
        for name, rows in data.items():
            table = self.schema.tables[name]
            kinds = [be.kind_of(c.type) for c in table.columns]
            if name in ordered:
                rows = sorted(rows, key=lambda r: r[ordered[name]])
            built = []
            for row in rows:
                cells = []
                for value, kind, column in zip(row, kinds, table.columns):
                    cells.append(be.V("unsupported", z3.IntVal(0), z3.BoolVal(False), column.type) if kind is None else _value(be, value, kind))
                built.append(be.Row(z3.BoolVal(True), cells))
            tables[name] = built
        concrete = _Concrete(self.schema, tables)
        for build in self.schema.extra:
            try:
                formulas = build(concrete)
            except KeyError:  # a table the partial database does not hold yet (empty: the assertion holds)
                continue
            for formula in formulas:
                reduced = z3.simplify(formula)
                if z3.is_true(reduced):
                    continue
                if z3.is_false(reduced):
                    return False
                solver = z3.Solver()
                solver.set("timeout", 2000)
                solver.add(z3.Not(formula))
                if solver.check() != z3.unsat:
                    return False
        return True


def _bounded_case(name: str, pair: str, result, replay, schema, *, source, meta, consecutive=(), held_out=False) -> Case | None:
    import bounded_bench as bb

    status = bb._status(result, ROWS)
    if status != "bounded":
        return None
    _install()
    meta = dict(meta)
    meta.update(max_rows=ROWS, bounded_seconds=round(result.seconds, 2), bounded_reason=result.reason)
    return Case(name, pair, replay.left, replay.right, engine_tables(schema, consecutive), legal=Legal(schema, ROWS, consecutive),
                held_out=held_out, source=tuple(source), dialect="mysql", meta=meta)


class Adapter:
    name = ""

    def items(self) -> list[dict]:
        raise NotImplementedError

    def case(self, item: dict) -> Case | None:
        raise NotImplementedError


# --- SQLSolver, QED, R-Bot, Cosette, SPES (bounded_bench.pair_work) ----------------------------------

_PAIRS: dict[str, list[dict]] = {}


def _pair_cases(suite: str) -> list[dict]:
    import bounded_bench as bb

    if suite not in _PAIRS:
        _PAIRS[suite] = bb.pair_cases(suite)
    return _PAIRS[suite]


class PairSuite(Adapter):
    def __init__(self, suite: str):
        self.suite = suite
        self.name = f"bounded-{suite}"

    def items(self) -> list[dict]:
        return [{"pair": c["name"], "position": i} for i, c in enumerate(_pair_cases(self.suite))]

    def case(self, item: dict) -> Case | None:
        import bounded_bench as bb
        import sqlsolver_bench as sb
        from kumosql import bounded_equivalence as be

        found = _pair_cases(self.suite)[item["position"]]
        assert found["name"] == item["pair"]
        tables, left, right, constants = found["tables"], found["left"], found["right"], found["constants"]
        schema = bb.schema_from_tables(tables)

        def prepare(sql):
            sql = sb.spark_days(sql)
            return sb.name_values(sql) if constants else sql

        def translate(sql):
            text = sb.to_dialect(prepare(sql), "duckdb")
            return sb.constant_groupings(text) if constants else text

        try:
            replay = be.DuckDBReplay(schema, left, right, "mysql", translate=translate)
            result = be.check_bounded(left, right, schema, rows=ROWS, dialect="mysql", timeout_ms=TIMEOUT_MS, budget_s=BUDGET_S,
                                      replay=replay, prepare=prepare, group_constants=constants)
        except Exception:  # the eval reports a crash as unknown
            _log(self.name, item["pair"], None)
            return None
        _log(self.name, item["pair"], result)
        return _bounded_case(self.name, item["pair"], result, replay, schema, source=(left, right),
                             meta={"label": found["label"], "constants": constants})


# --- VeriEQL literature, calcite, leetcode (bounded_bench.run_case) ----------------------------------

_VERIEQL: dict[str, list[dict]] = {}


def _verieql_cases(suite: str) -> list[dict]:
    import verieql_bench as vb

    if suite not in _VERIEQL:
        cases = vb.load_cases(suite)
        _VERIEQL[suite] = cases[::24] if suite == "leetcode" else cases  # the eval's 1,000-case sample: every 24th
    return _VERIEQL[suite]


def _consecutive_columns(case: dict) -> list[tuple[str, str]]:
    tables = case["schema"]
    out = []
    for constraint in case.get("constraint") or []:
        (kind, body), = constraint.items()
        if kind in ("inc", "consec"):
            ref = body["value"]
            for table in tables:
                if ref.startswith(table + "__") and ref[len(table) + 2:] in tables[table]:
                    out.append((table, ref[len(table) + 2:]))
                    break
    return out


class VeriEql(Adapter):
    def __init__(self, suite: str):
        self.suite = suite
        self.name = f"bounded-{suite}"

    def items(self) -> list[dict]:
        return [{"pair": str(c["index"]), "position": i} for i, c in enumerate(_verieql_cases(self.suite))]

    def case(self, item: dict) -> Case | None:
        import bounded_bench as bb
        from kumosql import bounded_equivalence as be

        found = _verieql_cases(self.suite)[item["position"]]
        assert str(found["index"]) == item["pair"]
        try:
            schema = bb.schema_for_case(found)
        except Exception:
            return None
        left, right = found["pair"]
        try:
            replay = be.DuckDBReplay(schema, left, right, "mysql")
            result = be.check_bounded(left, right, schema, rows=ROWS, dialect="mysql", timeout_ms=TIMEOUT_MS, budget_s=BUDGET_S, replay=replay)
        except Exception:
            _log(self.name, item["pair"], None)
            return None
        _log(self.name, item["pair"], result)
        return _bounded_case(self.name, item["pair"], result, replay, schema, source=(left, right), consecutive=_consecutive_columns(found),
                             meta={"constraints": [list(c)[0] for c in found.get("constraint") or []]})


# --- Singh & Bedathur (bounded_bench.singh_work) -------------------------------------------------------

_SINGH: list | None = None


def _singh_pairs() -> list:
    global _SINGH
    import singh_bedathur_bench as sbb

    if _SINGH is None:
        _SINGH = sbb.load_pairs()
    return _SINGH


class Singh(Adapter):
    """The eval runs the bounded check on the pairs its own decision (``singh_bedathur_bench.decide``)
    does not refute. Here the bounded check runs first; a bounded pair is then decided, and one the suite
    refutes is still searched but marked ``meta.counted = False`` (the eval skips it)."""

    name = "bounded-singh"

    def items(self) -> list[dict]:
        return [{"pair": p.key, "position": i} for i, p in enumerate(_singh_pairs())]

    def case(self, item: dict) -> Case | None:
        import sqlglot

        import singh_bedathur_bench as sbb
        from kumosql import bounded_equivalence as be
        from kumosql.set_operations import positional_sql_pair

        pair = _singh_pairs()[item["position"]]
        assert pair.key == item["pair"]
        try:
            trees = [sqlglot.parse_one(pair.left, read="mysql"), sqlglot.parse_one(pair.right, read="mysql")]
            kinds = sbb.column_kinds(trees, pair.tables)
        except Exception:
            return None
        names = {"VARCHAR": "VARCHAR", "DATE": "DATE"}
        schema = be.BoundedSchema({
            table: be.BTable(table, [be.BColumn(c, names.get(kinds.get((table, c), "BIGINT"), "DECIMAL" if str(kinds.get((table, c), "")).startswith("DECIMAL") else "BIGINT")) for c in columns])
            for table, columns in pair.tables.items()
        })
        try:
            result = be.check_bounded(pair.left, pair.right, schema, rows=ROWS, dialect="mysql", timeout_ms=TIMEOUT_MS, budget_s=BUDGET_S)
        except Exception:
            _log(self.name, item["pair"], None)
            return None
        if result.status is not be.BoundedStatus.BOUNDED_EQUIVALENT or result.bound < ROWS:
            _log(self.name, item["pair"], result)
            return None
        left_sql, right_sql, problem = positional_sql_pair(pair.left, pair.right, "mysql")
        if problem:
            return None
        replay = be.DuckDBReplay(schema, left_sql, right_sql, "mysql")  # what _check_bounded builds when no replay is given
        try:
            verdict = sbb.decide(pair, 300)
            suite = verdict.kind
        except Exception as error:
            suite = f"crash: {type(error).__name__}"
        _log(self.name, item["pair"], result, suite_verdict=suite)
        return _bounded_case(self.name, item["pair"], result, replay, schema, source=(pair.left, pair.right),
                             meta={"suite_verdict": suite, "counted": suite != "different", "gold": pair.gold,
                                   "main_eval_held_out": pair.held_out, "index": pair.index})


ADAPTERS = {
    a.name: a
    for a in [
        *(PairSuite(s) for s in ("sqlsolver-calcite", "sqlsolver-spark", "sqlsolver-tpch", "sqlsolver-tpcc", "qed", "rbot", "cosette", "spes")),
        VeriEql("literature"), VeriEql("calcite"), VeriEql("leetcode"),
        Singh(),
    ]
}
