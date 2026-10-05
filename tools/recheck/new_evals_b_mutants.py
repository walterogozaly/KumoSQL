"""Mutants the equivalence prover calls equivalent in the targeted-test-data eval (``tools/targeted_data_bench.py``).

The eval builds faulty variants (``kumosql.query_mutants``) of each original query (SQLSolver's Calcite, Spark, TPC-H and
TPC-C pairs and a university schema) and asks which checking strategy tells each mutant from its original. A mutant that
no strategy kills is classified by the algebraic prover with the schema's keys and NOT NULL columns (3 s, exact
arithmetic): *proven equivalent* mutants leave the denominator, so a false proof there hides a real fault from every
score. The eval never asks the prover about a mutant a database killed, so a false proof on such a mutant is invisible to it.

``targeted-test-data`` proves every mutant of every original the way the eval does (``_prove_equivalent``) and keeps the
proven ones, killed or not; a difference found here is a false proof the eval cannot see. The Case runs original and
mutant as the eval's ``DatasetRunner`` runs them: ``prepare_statements`` over the suite's schema, the BigQuery settings and
macros, results read as BigQuery returns them, over tables with the suite's primary and unique keys and NOT NULL columns.
Pairs are named ``<suite>:<index>#<mutant number>:<operator>``; TPC-H and TPC-C are held out in the eval.
"""

from __future__ import annotations

import logging
from pathlib import Path
import sys

import sqlglot

TOOLS = Path(__file__).resolve().parent.parent
ROOT = TOOLS.parent
for _path in (str(TOOLS), str(ROOT / "src")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from recheck import dialect_rewrites as dr  # noqa: E402
from recheck import new_evals_b  # noqa: E402
from recheck.engine import Case, Table  # noqa: E402

logging.getLogger("sqlglot").setLevel(logging.CRITICAL)

Adapter = dr.Adapter

_CORPUS: dict = {}


def corpus() -> tuple[list[dict], dict]:
    if not _CORPUS:
        import targeted_data_bench as tdb

        _CORPUS["all"] = tdb.build_corpus("all")
    return _CORPUS["all"]


def original_of(item: dict) -> str:
    import sqlsolver_bench as ssb

    if item["suite"] == "university":
        return sqlglot.transpile(item["source"], read="mysql", write="bigquery")[0]
    return ssb.to_dialect(item["source"], "bigquery")


class TargetedTestData(Adapter):
    name = "targeted-test-data"

    def items(self) -> list[dict]:
        from kumosql.query_mutants import mutate

        items, _ = corpus()
        out = []
        for item in items:
            try:
                mutants = mutate(original_of(item))
            except Exception:  # noqa: BLE001 - the eval counts the original unsupported
                continue
            for number, mutant in enumerate(mutants):
                out.append({"pair": f"{item['suite']}:{item['index']}#{number}:{mutant.operator}", "suite": item["suite"], "index": item["index"],
                            "number": number, "held_out": item["split"] == "heldout"})
        return out

    def case(self, item: dict) -> Case | None:
        import targeted_data_bench as tdb
        from kumosql.query_mutants import mutate
        from kumosql.result_equivalence import _local_name, prepare_statements

        new_evals_b._install()
        items, suites = corpus()
        entry = next(i for i in items if i["suite"] == item["suite"] and i["index"] == item["index"])
        suite = suites[item["suite"]]
        original = original_of(entry)
        mutant = mutate(original)[item["number"]]
        if not tdb._prove_equivalent(original, mutant.sql, suite):
            return None
        schema, rules = suite["schema"], suite["rules"]
        sides = []
        for sql in (original, mutant.sql):
            statements, target = prepare_statements(sql, schema, run_tag="runner")
            if len(statements) != 1 or target is not None:
                return None
            sides.append(statements[0])
        tables = {}
        for name, columns in schema.items():
            rule = rules[name]
            cols = [new_evals_b._bq_column(c, kind, not_null=c in rule.not_null) for c, kind in columns.items()]
            tables[_local_name(name)] = Table(_local_name(name), cols, [tuple(k) for k in rule.keys])
        return Case(self.name, item["pair"], sides[0], sides[1], tables, setup=dr.bigquery_setup(), held_out=item["held_out"],
                    source=(original, mutant.sql), dialect="bigquery", meta={"operator": mutant.operator, "results": "bigquery"})


ADAPTERS = {a.name: a for a in [TargetedTestData()]}
