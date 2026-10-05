"""The 0-wrong sweep: run counterexample synthesis over every pair the prover evals ask about.

The claim to establish is that :func:`kumosql.refutation_synthesis.synthesize` never refutes a pair
that an eval labels equivalent. This module is a pytest plugin (and a small runner around it) that
wraps :func:`kumosql.algebraic_equivalence.prove_equivalent_algebraic`. After the prover returns,
the plugin runs ``synthesize`` on the same pair when the prover said ``proven_equivalent``,
``proven_conditionally`` or ``not_proven``, and appends one JSON line for each refutation. The
prover's own result is returned untouched, so the eval being run scores exactly what it would score
without the plugin.

What the plugin does and does not do:

* **top level only**: a call made while another wrapped call is running (the prover calls itself
  for pipelines, conditions and containment) is never synthesized, and synthesis itself runs behind
  a guard, so nothing recurses;
* **no schema, no search**: a pair called without a schema is skipped (counted, not logged);
* **types**: ``--mode real`` synthesizes only when the eval declared a type for every column the
  queries read (the same rule the prover's own hook follows). ``--mode guessed`` handles the rest:
  every table and column of the schema that has no declared type is typed INT, and each record says
  so (``types_guessed``). Pairs whose types are complete are left to the real run. ``--mode all``
  does both in one pass: the two sets of pairs are disjoint, so it finds what a real run and a
  guessed run would find together, each record carrying ``types_guessed``;
* **held-out rows are skipped**: the SQL of the held-out split of every eval that has one is
  loaded best-effort from the eval's own loader, and a pair that mentions it is neither run nor
  logged (counted as ``held_out``);
* **repeats**: the same pair, schema, types, constraints and dialect is synthesized once per process.

Usage (from the repository root)::

    python tools/refutation_sweep.py --mode real --out /tmp/real.jsonl tests/test_cosette_benchmarks.py
    python tools/refutation_sweep.py --mode guessed --out /tmp/guessed.jsonl tests/test_qed_benchmarks.py -x
    python tools/refutation_sweep.py --mode all --out /tmp/all.jsonl tests/test_pipeline_bench.py   # slow tests run too

or as a plugin of any pytest run::

    PYTHONPATH=tools KUMOSQL_SWEEP_OUT=/tmp/x.jsonl KUMOSQL_SWEEP_MODE=guessed pytest -p refutation_sweep tests/test_x.py

Each process also writes ``<out>.summary.json`` (counters per eval file). Records are appended with
a single write, so worker processes can share one output file.
"""

from __future__ import annotations

import argparse
import functools
import hashlib
import json
import os
import re
import sys
import threading
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "src") not in sys.path and (ROOT / "src").exists():
    sys.path.insert(0, str(ROOT / "src"))
TOOLS = Path(__file__).resolve().parent
for _path in (str(ROOT), str(ROOT / "tests")):  # some eval tests import ``tools.<bench>``
    if _path not in sys.path:
        sys.path.insert(0, _path)

MODES = ("real", "guessed", "all")
STATUSES_RUN = ("proven_equivalent", "proven_conditionally", "not_proven")
GUESS = {"bigquery": "INT64"}  # every other dialect: INTEGER


def _guess_type(dialect: str) -> str:
    return GUESS.get(dialect, "INTEGER")


def normalise(sql: str) -> str:
    """Whitespace and case folded, so a pair read from a fixture matches the text a test passes on."""

    return re.sub(r"\s+", " ", sql).strip().rstrip(";").strip().lower()


# --- held-out rows ------------------------------------------------------------------------------------


def _sql_strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value] if re.search(r"\bselect\b", value, re.I) else []
    if isinstance(value, dict):
        return [s for v in value.values() for s in _sql_strings(v)]
    if isinstance(value, (list, tuple, set)):
        return [s for v in value for s in _sql_strings(v)]
    if hasattr(value, "__dict__"):
        return _sql_strings(vars(value))
    return []


class HeldOut:
    """The SQL of the held-out rows: a row of exactly two queries is a pair, any other row's queries stand alone."""

    def __init__(self, singles: set[str] | None = None, pairs: set[frozenset] | None = None):
        self.singles = singles if singles is not None else set()
        self.pairs = pairs if pairs is not None else set()

    def __bool__(self) -> bool:
        return bool(self.singles or self.pairs)

    def add_row(self, row) -> None:
        texts = [normalise(s) for s in _sql_strings(row)]
        if len(texts) == 2:
            self.pairs.add(frozenset(texts))
        else:
            self.singles.update(texts)

    def contains(self, left: str, right: str) -> bool:
        a, b = normalise(left), normalise(right)
        return a in self.singles or b in self.singles or frozenset((a, b)) in self.pairs


def held_out_texts() -> tuple[HeldOut, list[str]]:
    """``(the held-out rows, notes on loaders that could not run)``.

    Each loader is the eval's own split function, so the sweep never disagrees with the eval about
    which rows are held out. A loader that needs a download that is not cached is skipped and named.
    """

    texts = HeldOut()
    notes: list[str] = []
    if str(TOOLS) not in sys.path:
        sys.path.insert(0, str(TOOLS))

    def add(label: str, rows) -> None:
        for row in rows:
            texts.add_row(row)

    def attempt(label: str, load) -> None:
        try:
            add(label, load())
        except Exception as error:  # noqa: BLE001 - a missing download must not stop the sweep
            notes.append(f"{label}: not loaded ({type(error).__name__})")

    def singh():
        import singh_bedathur_bench as module

        from kumosql.canonical_rules import canonicalize

        rows = []
        for pair in module.load_pairs(None):
            if pair.held_out:
                rows.append(pair)
                try:  # the bench proves the canonicalized text a second time
                    rows.append([canonicalize(pair.left, "mysql", pair.tables), canonicalize(pair.right, "mysql", pair.tables)])
                except Exception:  # noqa: BLE001
                    pass
        return rows

    def llm_sql_solver():
        import llm_sql_solver_bench as module

        rows = []
        for case in module.load_cases():
            if case.held_out:
                rows.append(case)
                adapted = [module.adapt(case.sql1, case.tables), module.adapt(case.sql2, case.tables)]
                rows.append(adapted)
                rows.append([module.for_prover(sql) for sql in adapted])
        return rows

    def calcite_mined():
        import calcite_mined_bench as module

        return module.split_pairs(module.load_pairs(), "held-out")

    def join_rewrite():
        import join_rewrite_bench as module

        return module.load_pairs(True)

    def cosette():
        import cosette_bench as module

        return [row for suite in ("cosette", "cosette-adapted", "spes-only") for row in _cosette(module, suite) if module.held_out(row["name"])]

    def _cosette(module, suite):
        try:
            return module.load(suite)
        except Exception:  # noqa: BLE001
            return []

    def pipeline():
        import pipeline_bench as module

        return module.all_cases(True)

    def mv_workload():
        import mv_workload_bench as module

        rows = []
        for name in ("mined", "job", "calcite"):
            try:
                queries, _ = module.load_workload(name, None, None)
            except Exception:  # noqa: BLE001
                continue
            rows.extend(r for r in queries.values() if r.get("held_out"))
        return rows

    for label, load in (
        ("singh_bedathur", singh), ("llm_sql_solver", llm_sql_solver), ("calcite_mined", calcite_mined),
        ("join_rewrite", join_rewrite), ("cosette", cosette), ("pipeline", pipeline), ("mv_workload", mv_workload),
    ):
        attempt(label, load)
    return texts, notes


# --- the sweep ----------------------------------------------------------------------------------------


class Sweep:
    """Wraps the prover, synthesizes on what it leaves unrefuted, and logs the refutations."""

    def __init__(self, mode: str = "real", out: str | os.PathLike | None = None, seconds: float = 3.0, held_out: HeldOut | None = None):
        if mode not in MODES:
            raise ValueError(f"mode must be one of {', '.join(MODES)}")
        self.mode = mode
        self.out = Path(out) if out else None
        self.seconds = seconds
        self.held_out = held_out if held_out is not None else HeldOut()
        self.local = threading.local()
        self.lock = threading.Lock()
        self.seen: set[str] = set()
        self.counts: dict[str, dict[str, int]] = {}
        self.original = None
        self.wrapper = None

    # state ----------------------------------------------------------------------------------------

    def _count(self, name: str) -> None:
        eval_file = self.eval_file()
        with self.lock:
            bucket = self.counts.setdefault(eval_file, {})
            bucket[name] = bucket.get(name, 0) + 1

    @staticmethod
    def test_id() -> str:
        current = os.environ.get("PYTEST_CURRENT_TEST", "")
        return current.rsplit(" (", 1)[0] if current else ""

    def eval_file(self) -> str:
        return self.test_id().split("::", 1)[0] or "(outside a test)"

    # wrapping -------------------------------------------------------------------------------------

    def wrap(self, original):
        sweep = self

        @functools.wraps(original)
        def prove_equivalent_algebraic(left_sql, right_sql, **kwargs):
            local = sweep.local
            depth = getattr(local, "depth", 0)
            local.depth = depth + 1
            try:
                result = original(left_sql, right_sql, **kwargs)
            finally:
                local.depth = depth
            if depth == 0 and not getattr(local, "busy", False):
                local.busy = True
                try:
                    sweep.observe(left_sql, right_sql, kwargs, result)
                except Exception as error:  # noqa: BLE001 - the sweep must never change what an eval sees
                    sweep._count(f"sweep_error:{type(error).__name__}")
                finally:
                    local.busy = False
            return result

        prove_equivalent_algebraic.__sweep_wrapped__ = original
        return prove_equivalent_algebraic

    def install(self) -> None:
        from kumosql import algebraic_equivalence

        current = algebraic_equivalence.prove_equivalent_algebraic
        if getattr(current, "__sweep_wrapped__", None) is None:
            self.original = current
            self.wrapper = self.wrap(current)
            algebraic_equivalence.prove_equivalent_algebraic = self.wrapper
        self.rebind()

    def rebind(self) -> None:
        """Point every module that imported the prover by name at the wrapper."""

        if self.original is None:
            return
        for module in list(sys.modules.values()):
            namespace = getattr(module, "__dict__", None)
            if namespace is not None and namespace.get("prove_equivalent_algebraic") is self.original:
                namespace["prove_equivalent_algebraic"] = self.wrapper

    def uninstall(self) -> None:
        if self.original is None:
            return
        from kumosql import algebraic_equivalence

        for module in list(sys.modules.values()):
            namespace = getattr(module, "__dict__", None)
            if namespace is not None and namespace.get("prove_equivalent_algebraic") is self.wrapper:
                namespace["prove_equivalent_algebraic"] = self.original
        algebraic_equivalence.prove_equivalent_algebraic = self.original
        self.original = self.wrapper = None

    # one pair -------------------------------------------------------------------------------------

    def is_held_out(self, left: str, right: str) -> bool:
        return bool(self.held_out) and self.held_out.contains(left, right)

    def types_for(self, left: str, right: str, kwargs: dict) -> tuple[dict | None, bool, str]:
        """``(types to synthesize with, guessed, why not)`` for one pair."""

        from kumosql import refutation_synthesis as rs

        schema = kwargs.get("schema") or {}
        dialect = kwargs.get("dialect", "bigquery") or "bigquery"
        types = kwargs.get("types") or {}
        constraints = kwargs.get("constraints")
        read = rs.tables_read(left, dialect) | rs.tables_read(right, dialect)
        if rs.typed_schema(read, schema, types, constraints) is not None:
            return types, False, ""
        if self.mode == "real":
            return None, False, "untyped"  # guessed and all go on to fill the gaps
        have = {t.lower(): {c.lower(): ty for c, ty in cols.items()} for t, cols in types.items()}
        guess = _guess_type(dialect)
        filled = {}
        for table, columns in schema.items():
            known = have.get(table.lower(), {})
            filled[table] = {c: known.get(c.lower(), guess) for c in columns}
        return filled, True, ""

    def observe(self, left: str, right: str, kwargs: dict, result) -> None:
        from kumosql import refutation_synthesis as rs

        self._count("calls")
        status = getattr(result.status, "value", str(result.status))
        if status == "not_equivalent":
            if rs.ASSUMPTION in tuple(getattr(result, "assumptions", ()) or ()) and getattr(result, "counterexample", None) is not None:
                self._count("refuted_in_prover")
                self.log_prover_refutation(left, right, kwargs, result)
            return
        if status not in STATUSES_RUN:
            return
        if not isinstance(left, str) or not isinstance(right, str) or not kwargs.get("schema"):
            self._count("skipped_no_schema")
            return
        if self.is_held_out(left, right):
            self._count("held_out")
            return
        dialect = kwargs.get("dialect", "bigquery") or "bigquery"
        key = hashlib.sha1(
            json.dumps([left, right, dialect, kwargs["schema"], kwargs.get("types"), _constraints(kwargs.get("constraints"), kwargs["schema"])], default=str, sort_keys=True).encode()
        ).hexdigest()
        with self.lock:
            repeat = key in self.seen
            self.seen.add(key)
        if repeat:
            self._count("repeat")
            return
        try:
            types, guessed, why = self.types_for(left, right, kwargs)
        except Exception:  # noqa: BLE001 - unparseable SQL: nothing to synthesize
            self._count("skipped_unparsed")
            return
        if types is None:
            self._count(f"skipped_{why}")
            return
        if self.mode == "guessed" and not guessed:
            self._count("skipped_typed")
            return
        if not guessed and kwargs.get("search_counterexample") and status == "not_proven":
            self._count("skipped_hook_ran")  # the prover's own hook already ran the same synthesis on real types
            return
        began = time.monotonic()
        found = rs.synthesize(
            left, right, schema=kwargs["schema"], types=types, constraints=kwargs.get("constraints"), dialect=dialect, time_limit=self.seconds
        )
        self._count("synthesized")
        self._count(f"synthesized_{status}")
        if found is None:
            return
        self._count("refuted")
        self._count(f"refuted_{status}")
        self._count("refuted_guessed" if guessed else "refuted_real")
        self.log(self.record(left, right, kwargs, status, result, types, guessed, found, time.monotonic() - began))

    def record(self, left, right, kwargs, status, result, types, guessed, found, seconds) -> dict:
        from kumosql import refutation_synthesis as rs

        schema = kwargs.get("schema") or {}
        by_lower = {k.lower(): k for k in schema}
        used = {t: list(schema[by_lower[t.lower()]]) for t in found.data if t.lower() in by_lower}
        return {
            "eval_file": self.eval_file(), "test": self.test_id(), "prover_status": status,
            "conditions": [str(c) for c in (getattr(result, "conditions", ()) or ())][:8],
            "dialect": kwargs.get("dialect", "bigquery") or "bigquery",
            "left": left, "right": right,
            "types_guessed": guessed, "types": {t: dict(c) for t, c in types.items() if t in used or t.lower() in {u.lower() for u in used}},
            "guessed_columns": sum(len(c) for c in types.values()) - sum(len(c) for c in (kwargs.get("types") or {}).values()) if guessed else 0,
            "schema": used,
            "constraints": _constraints(kwargs.get("constraints"), used),
            "witness": {t: [[rs._export(v) for v in row] for row in rows] for t, rows in found.data.items()},
            "left_rows": [[rs._export(v) for v in row] for row in found.left_rows],
            "right_rows": [[rs._export(v) for v in row] for row in found.right_rows],
            "method": found.method, "rows": found.rows, "seconds": round(seconds, 3), "pid": os.getpid(),
        }

    def log_prover_refutation(self, left, right, kwargs, result) -> None:
        """A refutation the prover's own hook made: logged, never re-run."""

        if self.is_held_out(left, right):
            self._count("held_out")
            return
        tables = {t: rows for t, rows in result.counterexample.tables.items()}
        self.log({
            "eval_file": self.eval_file(), "test": self.test_id(), "prover_status": "not_equivalent", "in_prover": True,
            "dialect": kwargs.get("dialect", "bigquery") or "bigquery", "left": left, "right": right,
            "types_guessed": False, "types": kwargs.get("types") or {}, "schema": {t: list((kwargs.get("schema") or {}).get(t, ())) for t in tables},
            "constraints": _constraints(kwargs.get("constraints"), tables),
            "witness": {t: [list(r.values()) if isinstance(r, dict) else list(r) for r in rows] for t, rows in tables.items()},
            "left_rows": [list(r) for r in result.counterexample.left_rows], "right_rows": [list(r) for r in result.counterexample.right_rows],
            "method": "prover", "pid": os.getpid(),
        })

    def log(self, record: dict) -> None:
        if self.out is None:
            return
        line = json.dumps(record, default=str, ensure_ascii=False) + "\n"
        with self.lock:
            self.out.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(self.out, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
            try:
                os.write(fd, line.encode())
            finally:
                os.close(fd)

    def write_summary(self) -> None:
        if self.out is None:
            return
        path = self.out.with_name(self.out.name + f".summary.{os.getpid()}.json")
        path.write_text(json.dumps({"mode": self.mode, "seconds": self.seconds, "counts": self.counts}, indent=1, sort_keys=True), encoding="utf-8")


def _constraints(constraints, tables) -> dict:
    wanted = {t.lower() for t in tables}
    out = {}
    for name, declared in (constraints or {}).items():
        if name.lower() in wanted:
            out[name] = {
                "not_null": sorted(getattr(declared, "not_null", ()) or ()),
                "keys": [list(k) for k in getattr(declared, "keys", ()) or ()],
                "foreign_keys": [[list(c) if not isinstance(c, str) else c, p, list(pc) if not isinstance(pc, str) else pc] for c, p, pc in getattr(declared, "foreign_keys", ()) or ()],
            }
    return out


# --- the pytest plugin --------------------------------------------------------------------------------

_sweep: Sweep | None = None


def pytest_configure(config) -> None:
    global _sweep
    mode = os.environ.get("KUMOSQL_SWEEP_MODE", "real")
    if os.environ.get("KUMOSQL_SWEEP_MODE") == "off":
        return
    texts, notes = held_out_texts()
    _sweep = Sweep(mode, os.environ.get("KUMOSQL_SWEEP_OUT"), float(os.environ.get("KUMOSQL_SWEEP_SECONDS", "3")), texts)
    _sweep.install()
    for note in notes:
        print(f"refutation_sweep: held-out {note}", file=sys.stderr)


def pytest_runtest_setup(item) -> None:
    if _sweep is not None:
        _sweep.rebind()  # tools imported by the test module itself bind the prover by name


def pytest_unconfigure(config) -> None:
    global _sweep
    if _sweep is not None:
        _sweep.write_summary()
        _sweep.uninstall()
        _sweep = None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--mode", choices=MODES, default="real")
    parser.add_argument("--out", required=True, help="JSONL file the refutations are appended to")
    parser.add_argument("--seconds", type=float, default=3.0, help="time limit of one synthesis")
    args, pytest_args = parser.parse_known_args(argv)
    os.environ.update(KUMOSQL_SWEEP_MODE=args.mode, KUMOSQL_SWEEP_OUT=str(Path(args.out).resolve()), KUMOSQL_SWEEP_SECONDS=str(args.seconds))
    import pytest

    # the repository's default deselects tests marked slow, and the long evals are the ones worth sweeping
    marker = [] if any(a == "-m" or a.startswith("-m") for a in pytest_args) else ["-m", "slow or not slow"]
    return int(pytest.main(["-p", "no:cacheprovider", "-q", *marker, *pytest_args], plugins=[sys.modules[__name__]]))


if __name__ == "__main__":
    raise SystemExit(main())
