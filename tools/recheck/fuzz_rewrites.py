"""Proven pairs of the fuzzing and rewrite evals: SQLancer-style TLP/NoREC fuzzing, unsafe-rewrite
detection, rewrite composition, join rewrites and constraint-dependent rewrites.

Each pair is proved exactly as its eval proves it (same entry point, schema, types, declared
constraints and options) and a proven pair is turned into the DuckDB SQL the eval's oracle runs:
BigQuery SQL through ``kumosql.bigquery_on_duckdb.faithful`` on a connection prepared with its
``SETTINGS`` and ``MACROS`` (a guard error means BigQuery would fail on that database, which the
engine counts as a one-side error, never as a difference), except ``join-rewrites``, whose own check
is a plain transpile on a bare DuckDB.

* ``sqlancer-tlp-norec``: ``unsafe_fuzz.build_cases("fuzz", 60, 2)``; a pair is scored only when the
  eval's 40-database oracle can run both sides, then ``unsafe_fuzz.prove``.
* ``unsafe-rewrite-detection``: ``unsafe_fuzz.build_cases("unsafe", 20, 1)``, the same way.
* ``rewrite-composition``: ``compose_fuzz.run(60, 21, trials=40)`` replayed step by step (the same
  random chains, the same skipped queries); every changed step the oracle can run is a pair
  (step input, step output) proved with ``unsafe_fuzz.prove``.
* ``join-rewrites``: ``tests/fixtures/join_rewrites/pairs.jsonl`` and ``held_out.jsonl`` (held out),
  proved as ``join_rewrite_bench.run`` does and run as its executed check runs them: a plain sqlglot
  transpile (``join_rewrite_bench._duckdb``) on a bare DuckDB. The BigQuery-faithful translation of a
  pair that reads differently is kept in ``meta["faithful"]`` for triage of a dialect gap.
* ``constraint-rewrites``: ``tests/fixtures/constraint_rewrites/cases.json`` and ``held_out.json``
  (held out). Three kinds of pair per case, each a proof the eval makes: ``<id>`` with every offered
  fact (the main proof), ``<id>@needed`` with only the facts ``needed_guarantees`` reported (the
  proof behind "exactly the needed guarantees"), and ``<id>@without:<fact>`` for each ablation the
  prover proves (none should be).
"""

from __future__ import annotations

from collections import Counter
import json
import os
from pathlib import Path
import sys

TOOLS = Path(__file__).resolve().parent.parent
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import sqlglot  # noqa: E402

import unsafe_fuzz as uf  # noqa: E402

from recheck.engine import Case, Column, Table  # noqa: E402

def _setup() -> tuple[str, ...]:
    from kumosql.bigquery_on_duckdb import MACROS, SETTINGS

    return tuple(SETTINGS) + tuple(MACROS)


def bq_to_duckdb(sql: str) -> str:
    """The DuckDB SQL ``unsafe_fuzz.Oracle.run`` and ``constraint_rewrite_bench.to_duck`` run."""

    from kumosql.bigquery_on_duckdb import faithful

    return faithful(sqlglot.parse_one(sql, read="bigquery")).sql(dialect="duckdb")


# --- the eval's own 40-database oracle, one per process --------------------------------------

_ORACLE: dict[int, object] = {}


def _oracle():
    pid = os.getpid()
    if pid not in _ORACLE:
        _ORACLE.clear()
        _ORACLE[pid] = uf.Oracle(trials=40)
    return _ORACLE[pid]


def _oracle_verdict(oracle, left: str, right: str) -> str:
    """``invalid`` (the eval skips the pair), ``separated`` or ``agrees`` on the eval's oracle."""

    try:
        return "separated" if oracle.search(left, right) is not None else "agrees"
    except Exception:
        return "invalid"


def _proved_fuzz(left: str, right: str) -> tuple[bool, str]:
    try:
        result = uf.prove(left, right)
    except Exception as error:  # a crash is a failure to prove, never a proof
        return False, f"crash: {type(error).__name__}"
    return uf.classify(result) == "proved", result.reason[:300]


def fuzz_tables() -> dict[str, Table]:
    return {t: Table(t, [Column(c, "int", sql_type="BIGINT") for c in uf.COLUMNS]) for t in uf.TABLES}


class Adapter:
    """``items()`` lists every pair (picklable); ``case(item)`` proves it as the eval does and returns a Case, or None."""

    name = ""

    def items(self) -> list[dict]:
        raise NotImplementedError

    def case(self, item: dict) -> Case | None:
        raise NotImplementedError


class FuzzSuite(Adapter):
    """``unsafe_fuzz.py fuzz|unsafe``: a pair counts as proven when the oracle runs it and the prover proves it."""

    def __init__(self, name: str, suite: str, count: int, seed: int):
        self.name, self.suite, self.count, self.seed = name, suite, count, seed

    def items(self) -> list[dict]:
        return [
            {"pair": c.id, "family": c.family, "left": c.left, "right": c.right, "expect": c.expect, "held_out": c.heldout}
            for c in uf.build_cases(self.suite, self.count, self.seed)
        ]

    def case(self, item: dict) -> Case | None:
        oracle = _oracle_verdict(_oracle(), item["left"], item["right"])
        if oracle == "invalid":  # evaluate() returns "unsupported" before proving
            return None
        proved, reason = _proved_fuzz(item["left"], item["right"])
        if not proved:
            return None
        return Case(
            self.name, item["pair"], bq_to_duckdb(item["left"]), bq_to_duckdb(item["right"]), fuzz_tables(),
            setup=_setup(), held_out=item["held_out"], source=(item["left"], item["right"]), dialect="bigquery",
            meta={"family": item["family"], "expect": item["expect"], "eval_oracle": oracle, "reason": reason},
        )


class Compose(Adapter):
    """``compose_fuzz.run(60, 21, trials=40)``: every changed rule step the oracle runs, proved with ``unsafe_fuzz.prove``."""

    name = "rewrite-composition"

    def __init__(self, count: int = 60, seed: int = 21, trials: int = 40):
        self.count, self.seed, self.trials = count, seed, trials

    def items(self) -> list[dict]:
        import random

        import compose_fuzz as cf

        rng = random.Random(self.seed)
        oracle = uf.Oracle(trials=self.trials)
        out: list[dict] = []
        try:
            for index in range(self.count):
                query = uf.gen_query(rng)
                try:
                    oracle.search(query, query)
                except Exception:
                    continue  # invalid generated query: no chain is drawn
                chain = cf.random_chain(rng)
                try:
                    steps = cf._apply(chain, query)
                except Exception:
                    continue
                for number, (rule, before, after, _) in enumerate(steps):
                    if after == before:
                        continue
                    verdict = _oracle_verdict(oracle, before, after)
                    if verdict == "invalid":
                        continue  # the eval records an error and does not prove the step
                    out.append({"pair": f"q{index}-s{number}-{rule}", "rule": rule, "left": before, "right": after, "eval_oracle": verdict})
        finally:
            oracle.db.close()
        return out

    def case(self, item: dict) -> Case | None:
        proved, reason = _proved_fuzz(item["left"], item["right"])
        if not proved:
            return None
        return Case(
            self.name, item["pair"], bq_to_duckdb(item["left"]), bq_to_duckdb(item["right"]), fuzz_tables(),
            setup=_setup(), source=(item["left"], item["right"]), dialect="bigquery",
            meta={"rule": item["rule"], "eval_oracle": item["eval_oracle"], "reason": reason},
        )


# --- join rewrites ---------------------------------------------------------------------------


def _constraint_tables(columns: dict[str, dict[str, str]], constraints: dict | None) -> dict[str, Table]:
    """Engine tables for ``{table: {column: INT64|STRING}}`` under ``TableConstraints`` (or none)."""

    kinds = {"INT64": ("int", "BIGINT"), "STRING": ("text", "VARCHAR")}
    out = {}
    constraints = {k.lower(): v for k, v in (constraints or {}).items()}
    for table, spec in columns.items():
        facts = constraints.get(table.lower())
        not_null = {c.lower() for c in facts.not_null} if facts else set()
        cols = [Column(c, kinds[t][0], not_null=c.lower() in not_null, sql_type=kinds[t][1]) for c, t in spec.items()]
        keys = [tuple(k) for k in facts.keys] if facts else []
        fks = [(tuple(fk[0]), fk[1], tuple(fk[2])) for fk in getattr(facts, "foreign_keys", ())] if facts else []
        out[table] = Table(table, cols, keys, fks)
    return out


class JoinRewrites(Adapter):
    name = "join-rewrites"

    def items(self) -> list[dict]:
        import join_rewrite_bench as jb

        out = []
        for held_out in (False, True):
            for p in jb.load_pairs(held_out):
                out.append({"pair": p["name"], "left": p["left"], "right": p["right"], "constraints": p["constraints"],
                            "equivalent": p["equivalent"], "held_out": held_out})
        return out

    def case(self, item: dict) -> Case | None:
        import join_rewrite_bench as jb
        from kumosql.algebraic_equivalence import prove_equivalent_algebraic
        from kumosql.smt_equivalence import SmtStatus

        try:
            result = prove_equivalent_algebraic(
                item["left"], item["right"], schema=jb.SCHEMA, types=jb.TYPES, constraints=jb.CONSTRAINTS[item["constraints"]],
                dialect="bigquery", search_counterexample=True,
            )
        except Exception:
            return None
        if result.status is not SmtStatus.PROVEN_EQUIVALENT:
            return None
        columns = {t: {c: "INT64" for c in cols} for t, cols in jb.SCHEMA.items()}
        # the eval's executed check: a plain sqlglot transpile on a bare DuckDB (``join_rewrite_bench._duckdb``)
        left, right = jb._duckdb(item["left"]), jb._duckdb(item["right"])
        meta = {"constraints": item["constraints"], "label_equivalent": item["equivalent"], "reason": result.reason[:300]}
        try:  # BigQuery's reading of the same pair, for triage of a dialect gap
            faithful_left, faithful_right = bq_to_duckdb(item["left"]), bq_to_duckdb(item["right"])
        except Exception:  # no faithful reading exists
            faithful_left = faithful_right = None
        if (faithful_left, faithful_right) != (left, right):
            meta["faithful"] = [faithful_left, faithful_right]
        return Case(
            self.name, item["pair"], left, right, _constraint_tables(columns, jb.CONSTRAINTS[item["constraints"]]),
            held_out=item["held_out"], source=(item["left"], item["right"]), dialect="bigquery", meta=meta,
        )


# --- constraint rewrites ---------------------------------------------------------------------


class ConstraintRewrites(Adapter):
    name = "constraint-rewrites"

    def items(self) -> list[dict]:
        import constraint_rewrite_bench as cb

        out = []
        for filename, held_out in (("cases.json", False), ("held_out.json", True)):
            data = cb.load_cases(filename)
            for case in data["cases"]:
                base = {"left": case["original"], "right": case["rewritten"], "file": filename, "id": case["id"], "held_out": held_out}
                out.append({**base, "pair": case["id"], "facts": "offered"})
                out.append({**base, "pair": case["id"] + "@needed", "facts": "needed"})
                if case.get("valid", True):
                    for requirement in case["requires"]:
                        label = cb._label(requirement)
                        out.append({**base, "pair": f"{case['id']}@without:{label}", "facts": "without", "requirement": requirement})
        return out

    def case(self, item: dict) -> Case | None:
        import constraint_rewrite_bench as cb
        from kumosql.algebraic_equivalence import prove_equivalent_algebraic
        from kumosql.constraint_dependence import constraints_with, needed_guarantees

        data = cb.load_cases(item["file"])
        case = next(c for c in data["cases"] if c["id"] == item["id"])
        schema = data["schemas"][case["schema"]]
        columns, constraints = cb.schema_parts(schema)
        meta = {"valid": case.get("valid", True), "requires": [cb._label(r) for r in case.get("requires", [])], "kind": item["facts"]}
        try:
            report = needed_guarantees(case["original"], case["rewritten"], schema=columns, constraints=constraints)
        except Exception:
            return None
        offered = list(report.offered)
        if item["facts"] in ("offered", "needed"):
            if report.status != "proven":
                return None
            facts = offered if item["facts"] == "offered" else list(report.needed)
            reason = report.result.reason
            expected = {json.dumps(r, sort_keys=True) for r in case.get("requires", [])}
            got = {json.dumps(cb._as_requirement(g), sort_keys=True) for g in report.needed}
            # the eval's score counts the main proof only, when it needs exactly the expected facts
            meta["counted"] = bool(item["facts"] == "offered" and meta["valid"] and expected == got)
        else:
            fact = cb._fact(item["requirement"])
            facts = [g for g in offered if g != fact]
            try:
                result = prove_equivalent_algebraic(case["original"], case["rewritten"], schema=columns,
                                                    constraints=constraints_with(facts) or None, dialect="bigquery")
            except Exception:
                return None
            if not result.proven:
                return None
            reason = result.reason
            meta["counted"] = False
        meta["facts"] = sorted(g.label for g in facts)
        meta["needed"] = sorted(g.label for g in report.needed)
        meta["reason"] = (reason or "")[:300]
        typed = {t: dict(spec["columns"]) for t, spec in schema["tables"].items()}
        return Case(
            self.name, item["pair"], cb.to_duck(case["original"]), cb.to_duck(case["rewritten"]),
            _constraint_tables(typed, constraints_with(facts)), setup=_setup(), held_out=item["held_out"],
            source=(case["original"], case["rewritten"]), dialect="bigquery", meta=meta,
        )


ADAPTERS = {
    a.name: a
    for a in [
        FuzzSuite("sqlancer-tlp-norec", "fuzz", 60, 2),
        FuzzSuite("unsafe-rewrite-detection", "unsafe", 20, 1),
        Compose(),
        JoinRewrites(),
        ConstraintRewrites(),
    ]
}


def tally(path: str) -> Counter:
    """Verdict counts of a raw ``<eval>.jsonl``."""

    return Counter(json.loads(line)["verdict"] for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip())
