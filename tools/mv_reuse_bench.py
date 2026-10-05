"""Materialized-view and shared-model reuse eval.

Each case gives a model (a materialized view) and a query; the task is to produce a replacement
query that reads only the model and returns the same rows. Deterministic Python only: no LLM runs
at eval time.

    python tools/mv_reuse_bench.py                 # development cases, all sources
    python tools/mv_reuse_bench.py --baseline      # the baseline: the existing prover alone, whole-model matches only
    python tools/mv_reuse_bench.py --held-out      # the reserved split (final evaluation only)
    python tools/mv_reuse_bench.py --source adapted
    python tools/mv_reuse_bench.py --json out.json

Sources (kept separate in every report):

* ``calcite``: Apache Calcite 1.37.0 ``MaterializedViewRelOptRulesTest`` and
  ``MaterializedViewSubstitutionVisitorTest``, copied verbatim by ``tools/extract_calcite_mv.py``.
  Calcite's verdict is ``ok`` (Calcite finds a rewrite) or ``noMat`` (Calcite finds none, which is
  not a proof that none exists).
* ``adapted``: cases written for KumoSQL in ``tests/fixtures/mv_reuse/adapted_cases.json``.
* ``doris``: Apache Doris 3.0.6 outer-join view tests, copied verbatim by ``tools/extract_doris_mv.py``.
  Doris's verdict is ``success`` or ``fail`` (which, like Calcite's ``noMat``, is not a proof that no
  rewrite exists).
* ``outer-union``: outer-join, key-aware, union-compensation and set-operation cases with a checked
  answer each, written by ``tools/make_outer_union_mv_cases.py`` (some adapted from StarRocks 3.3.0).

Every replacement is proven by the algebraic prover and then re-run against random databases that
respect the schema; a proven replacement that differs on any database counts as wrong.
"""

from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import sys
import time
import zlib

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from kumosql.model_reuse import ModelReuse, rewrite_over_model  # noqa: E402
from kumosql.random_check import CheckError, Column, Schema, Table, find_difference  # noqa: E402
from kumosql.smt_equivalence import TableConstraints  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures" / "mv_reuse"


def _t(name, *columns, keys=(), foreign_keys=()):
    return Table(name, [Column(*c) if isinstance(c, tuple) else Column(c) for c in columns], keys, foreign_keys)


_HR_TABLES = (
    ("emps", ("empid", "int", True), ("deptno", "int", True), ("name", "text"), ("salary", "float", True), ("commission", "int")),
    ("depts", ("deptno", "int", True), ("name", "text")),
    ("dependents", ("empid", "int", True), ("name", "text")),
    ("locations", ("empid", "int", True), ("name", "text")),
    ("events", ("eventid", "int", True), ("ts", "date")),
    ("depts2", ("deptno", "int", True), ("inceptiondate", "date")),
)
# Calcite's HR test schema as its materialized-view tests declare it (``MaterializationTest.HrFKUKSchema``):
# reflective Java classes (primitives are NOT NULL, boxed types and strings nullable) and one referential
# constraint, emps.deptno -> depts.deptno, whose target columns are therefore a unique key of depts.
_HR_CONSTRAINTS = {"depts": {"keys": [("deptno",)]}, "emps": {"foreign_keys": [(("deptno",), "depts", ("deptno",))]}}
HR = Schema([_t(name, *columns, **_HR_CONSTRAINTS.get(name, {})) for name, *columns in _HR_TABLES])
# The same tables with no keys: the adapted shared-model cases and the set-operation cases use it.
HR_PLAIN = Schema([_t(name, *columns) for name, *columns in _HR_TABLES])

# The slice of Calcite's foodmart schema these tests read; ids are NOT NULL, time_id and product_id are keys.
FOODMART = Schema(
    [
        _t(
            "sales_fact_1997",
            ("product_id", "int", True),
            ("time_id", "int", True),
            ("customer_id", "int", True),
            ("promotion_id", "int", True),
            ("store_id", "int", True),
            ("store_sales", "float", True),
            ("store_cost", "float", True),
            ("unit_sales", "float", True),
        ),
        _t(
            "time_by_day",
            ("time_id", "int", True),
            ("the_date", "date"),
            ("the_month", "text"),
            ("the_year", "int"),
            ("day_of_month", "int"),
            ("month_of_year", "int"),
            keys=[("time_id",)],
        ),
        _t("product_class", ("product_class_id", "int", True), "product_subcategory", "product_category", "product_department", "product_family", keys=[("product_class_id",)]),
        _t("product", ("product_class_id", "int", True), ("product_id", "int", True), "product_name", keys=[("product_id",)]),
    ]
)
SCHEMAS = {"hr": HR, "hr_plain": HR_PLAIN, "jdbc_foodmart": FOODMART, "foodmart": FOODMART}


def schema_from_json(spec: dict) -> Schema:
    """A fixture's schema: table -> [[column, type, not_null], ...] or {"columns": .., "keys": .., "foreign_keys": ..}.

    Dates are also drawn from the date literals the queries mention, since these fixtures filter on them."""

    tables = []
    for name, table in spec.items():
        if isinstance(table, list):
            table = {"columns": table}
        columns = [Column(c, kind, bool(not_null)) for c, kind, not_null in table["columns"]]
        keys = [tuple(k) for k in table.get("keys", [])]
        foreign = [(tuple(cols), parent, tuple(parent_cols)) for cols, parent, parent_cols in table.get("foreign_keys", [])]
        tables.append(Table(name, columns, keys, foreign))
    return Schema(tables, date_literals=True)


def _fixture_schemas() -> None:
    for name in ("doris_mv_cases.json", "outer_union_cases.json"):
        path = FIXTURES / name
        if path.exists():
            for key, spec in json.loads(path.read_text(encoding="utf-8")).get("schemas", {}).items():
                SCHEMAS.setdefault(key, schema_from_json(spec))


_fixture_schemas()


def constraints_of(schema: Schema) -> dict[str, TableConstraints]:
    return {
        t.name: TableConstraints(
            not_null=frozenset(schema.not_null(t)),
            keys=tuple(tuple(k) for k in t.keys),
            foreign_keys=tuple((tuple(cols), parent, tuple(parent_cols)) for cols, parent, parent_cols in t.foreign_keys),
        )
        for t in schema.tables
    }


def is_held_out(case_id: str) -> bool:
    return zlib.crc32(case_id.encode()) % 4 == 0


def load_cases(source: str | None) -> list[dict]:
    cases = []
    if source in (None, "calcite"):
        data = json.loads((FIXTURES / "calcite_mv_cases.json").read_text(encoding="utf-8"))
        for case in data["cases"]:
            cases.append({**case, "source": "calcite", "expect": "rewrite" if case["calcite"] == "ok" else "none"})
    adapted = FIXTURES / "adapted_cases.json"
    if source in (None, "adapted") and adapted.exists():
        for case in json.loads(adapted.read_text(encoding="utf-8"))["cases"]:
            cases.append({**case, "source": "adapted", "origin": case.get("origin", "adapted"), "schema": case.get("schema", "hr_plain")})
    doris = FIXTURES / "doris_mv_cases.json"
    if source in (None, "doris") and doris.exists():
        for case in json.loads(doris.read_text(encoding="utf-8"))["cases"]:
            cases.append({**case, "source": "doris", "expect": "rewrite" if case["doris"] == "success" else "none"})
    outer_union = FIXTURES / "outer_union_cases.json"
    if source in (None, "outer-union") and outer_union.exists():
        for case in json.loads(outer_union.read_text(encoding="utf-8"))["cases"]:
            cases.append({**case, "source": "outer-union"})
    return cases


def baseline(case: dict, schema: Schema, timeout_ms: int) -> ModelReuse:
    """The existing prover alone: does the model, as a whole, equal the query?"""

    from kumosql.algebraic_equivalence import prove_equivalent_algebraic

    try:
        result = prove_equivalent_algebraic(
            case["query"],
            case["materialization"],
            schema=schema.columns,
            constraints=constraints_of(schema),
            timeout_ms=timeout_ms,
            dialect="postgres",
            compare_names=False,
        )
    except Exception as error:  # noqa: BLE001
        return ModelReuse("unsupported", f"{type(error).__name__}: {error}")
    if result.proven:
        return ModelReuse("rewritten", "the model equals the query", "SELECT * FROM mv0", "whole-model")
    return ModelReuse("no_rewrite", "not the same query")


def run_case(case: dict, use_baseline: bool, timeout_ms: int, trials: int) -> dict:
    schema = SCHEMAS[case["schema"]]
    start = time.time()
    try:
        if use_baseline:
            reuse = baseline(case, schema, timeout_ms)
        else:
            reuse = rewrite_over_model(
                case["query"],
                case["materialization"],
                schema=schema.columns,
                constraints=constraints_of(schema),
                timeout_ms=timeout_ms,
                union_compensation=True,  # the labels allow a replacement that also reads base tables
            )
    except Exception as error:  # noqa: BLE001 - reported as an error, never as a result
        return {"id": case["id"], "status": "error", "reason": f"{type(error).__name__}: {error}", "seconds": time.time() - start}
    record = {
        "id": case["id"],
        "status": reuse.status,
        "reason": reuse.reason,
        "strategy": reuse.strategy,
        "sql": reuse.sql,
        "seconds": round(time.time() - start, 3),
    }
    if reuse.rewritten:
        inlined = reuse.inlined_sql
        if inlined is None:  # the baseline's whole-model rewrite
            inlined = case["materialization"]
        try:
            witness = find_difference(schema, reuse.query_sql or case["query"], inlined, mode="bag", trials=trials)
        except CheckError as error:
            record["check"] = f"unchecked: {error}"
        else:
            if witness is None:
                record["check"] = "verified"
            else:
                record["check"] = "WRONG"
                record["witness"] = {"seed": witness.seed, "only_query": [list(r) for r in witness.only_left[:3]], "only_replacement": [list(r) for r in witness.only_right[:3]]}
    return record


def summarize(cases: list[dict], records: list[dict]) -> dict:
    by_id = {r["id"]: r for r in records}
    summary: dict = {"total": len(cases)}
    groups: dict[str, list[tuple[dict, dict]]] = {}
    for case in cases:
        key = f"{case['source']}:{case['origin']}" if case["source"] in ("calcite", "doris", "outer-union") else case["source"]
        groups.setdefault(key, []).append((case, by_id[case["id"]]))
        if key != case["source"]:
            groups.setdefault(case["source"], []).append((case, by_id[case["id"]]))
    for key, pairs in sorted(groups.items()):
        statuses = Counter(r["status"] for _, r in pairs)
        scored = [(c, r) for c, r in pairs if not c.get("disabled")]
        want = [(c, r) for c, r in scored if c["expect"] == "rewrite"]
        none = [(c, r) for c, r in scored if c["expect"] == "none"]
        summary[key] = {
            "cases": len(pairs),
            "statuses": dict(statuses),
            "expect_rewrite": len(want),
            "rewritten_of_expected": sum(1 for _, r in want if r["status"] == "rewritten"),
            "expect_none": len(none),
            "no_rewrite_of_none": sum(1 for _, r in none if r["status"] != "rewritten"),
            "rewritten_beyond_label": sum(1 for _, r in none if r["status"] == "rewritten"),
            "rewritten_total": sum(1 for _, r in pairs if r["status"] == "rewritten"),
            "verified": sum(1 for _, r in pairs if r.get("check") == "verified"),
            "wrong": sum(1 for _, r in pairs if r.get("check") == "WRONG"),
            "unchecked": sum(1 for _, r in pairs if str(r.get("check", "")).startswith("unchecked")),
            "seconds": round(sum(r["seconds"] for _, r in pairs), 1),
        }
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--baseline", action="store_true")
    parser.add_argument("--held-out", action="store_true", help="run the reserved split instead of the development split")
    parser.add_argument("--all", action="store_true", help="run both splits")
    parser.add_argument("--source", choices=["calcite", "adapted", "doris", "outer-union"])
    parser.add_argument("--json", help="write per-case records here")
    parser.add_argument("--timeout-ms", type=int, default=5000)
    parser.add_argument("--trials", type=int, default=150)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--id", action="append")
    args = parser.parse_args(argv)

    cases = load_cases(args.source)
    if not args.all:
        cases = [c for c in cases if is_held_out(c["id"]) == args.held_out]
    if args.id:
        cases = [c for c in cases if c["id"] in args.id]
    cases = cases[: args.limit]
    records = [run_case(c, args.baseline, args.timeout_ms, args.trials) for c in cases]
    summary = summarize(cases, records)
    split = "all" if args.all else ("held-out" if args.held_out else "development")
    print(f"{'baseline' if args.baseline else 'model_reuse'} on the {split} split: {len(cases)} cases")
    for key, value in summary.items():
        if key != "total":
            print(f"  {key}: {json.dumps(value)}")
    if args.json:
        Path(args.json).write_text(json.dumps({"summary": summary, "records": records}, indent=1), encoding="utf-8")
    return 1 if any(r.get("check") == "WRONG" for r in records) else 0


if __name__ == "__main__":
    raise SystemExit(main())
