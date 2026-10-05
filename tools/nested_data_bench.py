"""Score KumoSQL on query pairs over nested data: ARRAY, STRUCT and UNNEST.

``tests/fixtures/nested_data/pairs.json`` holds hand-written pairs in the idioms of GA4 exports (``event_params``
lookups, ``items``), Snowplow events (context arrays, ``unstruct`` structs) and a small shop schema (tags, order
lines, scores), plus literal arrays. Each pair is labelled ``equivalent`` or ``different`` (a trap). The fixture also
holds a few stored databases per schema, and each pair records the databases on which BigQuery returned different
rows (``bigquery_differs``): every trap differs on at least one, no equivalent pair differs on any. ``--bigquery-sql``
prints the queries that check this on BigQuery (inline data, nothing is stored), and ``--check-labels`` replays the
same databases on DuckDB through KumoSQL's BigQuery translation and checks it sees the same differences.

Each pair gets one outcome from :func:`kumosql.algebraic_equivalence.prove_equivalent_algebraic` (BigQuery dialect,
the fixture's types and keys, counterexample search on):

1. **proven**: the prover proved the pair equivalent. Wrong for a trap.
2. **refuted**: the prover, or a database the counterexample search built and ran, shows the two differ. Wrong for an
   equivalent pair.
3. **unknown**: anything else.

Proofs and counterexamples are scored separately (``nested-data-proof`` and ``nested-data-executed``). One pair in
four, by a hash of its id, is held out.

``--scan`` adds an unlabelled coverage scan: the queries in the Spider 2.0 gold SQL (dev split only) and in the
open-source BigQuery projects of ``tests/fixtures/bq_corpora`` that read nested data, each checked against itself, so
a proof only says the prover could encode the query.

    python tools/nested_data_bench.py
    python tools/nested_data_bench.py --show unknown,wrong
    python tools/nested_data_bench.py --check-labels
    python tools/nested_data_bench.py --bigquery-sql > checks.sql
    python tools/nested_data_bench.py --scan
    python tools/nested_data_bench.py --write-results
"""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "tests" / "fixtures" / "nested_data" / "pairs.json"
PROVER_TIMEOUT_MS = 10000
NESTED = re.compile(r"\b(UNNEST|STRUCT|ARRAY|ARRAY_AGG|ARRAY_LENGTH)\b|\[(SAFE_)?(OFFSET|ORDINAL)\(", re.I)


@dataclass(frozen=True)
class Pair:
    id: str
    family: str
    schema: str
    label: str
    left: str
    right: str
    note: str = ""
    keys: dict = field(default_factory=dict)
    nondeterministic: bool = False
    bigquery_differs: tuple = ()

    @property
    def held_out(self) -> bool:
        return int(hashlib.sha1(f"nested-data\n{self.id}".encode()).hexdigest(), 16) % 4 == 0

    @property
    def equivalent(self) -> bool:
        return self.label == "equivalent"


@dataclass(frozen=True)
class Fixture:
    schemas: dict  # schema set -> table -> column -> BigQuery type
    datasets: dict  # schema set -> [table -> [row as {column: JSON value}]]
    pairs: list


def load(path: Path = FIXTURE) -> Fixture:
    data = json.loads(path.read_text())
    pairs = [Pair(**{**p, "bigquery_differs": tuple(p.get("bigquery_differs", ()))}) for p in data["pairs"]]
    return Fixture(schemas=data["schemas"], datasets=data.get("datasets", {}), pairs=pairs)


def load_pairs(path: Path = FIXTURE) -> list[Pair]:
    return load(path).pairs


# ---------------------------------------------------------------- proving


def _constraints(pair: Pair) -> dict:
    from kumosql.smt_equivalence import TableConstraints

    return {
        table: TableConstraints(not_null=frozenset(c for key in keys for c in key), keys=tuple(tuple(key) for key in keys))
        for table, keys in pair.keys.items()
    }


def prove(pair: Pair, schemas: dict, *, search: bool = True) -> tuple[str, str]:
    """The outcome (proven, refuted, unknown) and the prover's reason."""

    from kumosql.algebraic_equivalence import prove_equivalent_algebraic
    from kumosql.smt_equivalence import SmtStatus

    tables = schemas[pair.schema]
    try:
        result = prove_equivalent_algebraic(
            pair.left, pair.right,
            schema={t: list(cols) for t, cols in tables.items()},
            types=tables,
            constraints=_constraints(pair) or None,
            dialect="bigquery",
            timeout_ms=PROVER_TIMEOUT_MS,
            search_counterexample=search,
        )
    except Exception as exc:  # noqa: BLE001 - a crash is a failure to prove, never a proof
        return "unknown", f"error: {type(exc).__name__}: {exc}"[:200]
    if result.status is SmtStatus.PROVEN_EQUIVALENT:
        return "proven", "; ".join(result.assumptions)
    if result.status is SmtStatus.NOT_EQUIVALENT:
        return "refuted", "counterexample search" if result.counterexample is not None and "found by running" in result.reason else "prover"
    return "unknown", (result.reason or "")[:200]


def decide(pair: Pair, schemas: dict) -> dict:
    outcome, reason = prove(pair, schemas)
    wrong = (outcome == "proven" and not pair.equivalent) or (outcome == "refuted" and pair.equivalent)
    return {
        "id": pair.id, "family": pair.family, "label": pair.label, "outcome": outcome, "reason": reason,
        "wrong": wrong, "held_out": pair.held_out,
    }


def run(pairs: list[Pair], schemas: dict) -> list[dict]:
    return [decide(pair, schemas) for pair in pairs]


# ---------------------------------------------------------------- stored databases


def _value(value, bq_type: str):
    from kumosql.nested_values import from_python, is_nested, parse_type

    if value is None:
        return None
    if is_nested(bq_type):
        return from_python(value, parse_type(bq_type))
    if bq_type.upper() in ("TIMESTAMP", "DATETIME"):
        return datetime.fromisoformat(value)
    return value


def dataset(fixture: Fixture, schema: str, index: int):
    """Stored database ``index`` of schema set ``schema`` as a SyntheticDataset."""

    from kumosql.result_equivalence import SyntheticDataset, SyntheticTable

    tables = {}
    stored = fixture.datasets[schema][index]
    for table, columns in fixture.schemas[schema].items():
        rows = stored.get(table, [])
        tables[table] = SyntheticTable(
            columns=tuple(columns.items()),
            rows=tuple(tuple(_value(row.get(name), t) for name, t in columns.items()) for row in rows),
        )
    return SyntheticDataset(seed=index, tables=tables)


def applies(fixture: Fixture, pair: Pair, index: int) -> bool:
    """Stored database ``index`` respects the pair's keys (no NULL in a key column, no repeated key)."""

    stored = fixture.datasets[pair.schema][index]
    for table, keys in pair.keys.items():
        for key in keys:
            values = [tuple(row.get(c) for c in key) for row in stored.get(table, [])]
            if any(v is None for value in values for v in value) or len(set(values)) != len(values):
                return False
    return True


def databases(fixture: Fixture, pair: Pair) -> list[int]:
    """The stored databases a pair is checked on."""

    return [i for i in range(len(fixture.datasets.get(pair.schema, ()))) if applies(fixture, pair, i)]


def _fails_on_bigquery(error: Exception) -> bool:
    """A run-time failure BigQuery shares: a guard fired, or a scalar subquery returned more than one row."""

    from kumosql.result_equivalence import BigQueryWouldFail

    return isinstance(error, BigQueryWouldFail) or "More than one row returned by a subquery" in str(error)


def duckdb_differs(fixture: Fixture, pair: Pair) -> tuple[list[int], list[int], list[int]]:
    """On DuckDB through KumoSQL's translation: the stored databases on which the two return different rows, those on
    which a query fails as it would on BigQuery, and those it cannot run (the translation declines the query)."""

    from kumosql.result_equivalence import DatasetRunner, ExecutionError, compare_outputs

    differs, fails, declined = [], [], []
    with DatasetRunner(fixture.schemas[pair.schema]) as runner:
        for index in databases(fixture, pair):
            data = dataset(fixture, pair.schema, index)
            try:
                left, right = runner.run(pair.left, data, timeout=10), runner.run(pair.right, data, timeout=10)
            except ExecutionError as error:
                (fails if _fails_on_bigquery(error) else declined).append(index)
                continue
            if not compare_outputs(left, right, check_column_names=False, float_digits=9)[0]:
                differs.append(index)
    return differs, fails, declined


def check_labels(fixture: Fixture) -> list[str]:
    """Problems with the labels: a trap no stored database separates on BigQuery, an equivalent pair one does, or
    DuckDB seeing other differences than BigQuery."""

    problems = []
    for pair in fixture.pairs:
        differs, fails, declined = duckdb_differs(fixture, pair)
        if pair.equivalent and pair.bigquery_differs:
            problems.append(f"{pair.id}: labelled equivalent but BigQuery differs on {list(pair.bigquery_differs)}")
        if not pair.equivalent and not pair.bigquery_differs and not pair.nondeterministic:
            problems.append(f"{pair.id}: a trap that no stored database separates on BigQuery")
        expected = [i for i in pair.bigquery_differs if i not in declined]
        if differs != expected:
            problems.append(f"{pair.id}: DuckDB differs on {differs}, BigQuery on {list(pair.bigquery_differs)}")
    return problems


def bigquery_checks(fixture: Fixture, pair: Pair) -> list[int]:
    """The stored databases a pair is checked on in BigQuery: those on which neither query fails."""

    return [i for i in databases(fixture, pair) if i not in duckdb_differs(fixture, pair)[1]]


def _table_literal(fixture: Fixture, schema: str, table: str, index: int) -> str:
    from kumosql.nested_values import NestedType, _untyped, is_nested, literal, parse_type

    columns = fixture.schemas[schema][table]
    row_type = "STRUCT<" + ", ".join(f"{name} {t}" for name, t in columns.items()) + ">"
    data = dataset(fixture, schema, index).tables[table]
    rows = []
    for row in data.rows:
        values = []
        for (name, t), value in zip(columns.items(), row):
            if is_nested(t):  # typed, or a NULL field would make a STRUCT<x INT64> that does not coerce
                values.append(literal(value, parse_type(t), "bigquery"))
            else:
                values.append(_untyped(value, NestedType(t.upper()), "bigquery"))
        rows.append("(" + ", ".join(values) + ")")
    return f"SELECT * FROM UNNEST(ARRAY<{row_type}>[{', '.join(rows)}])"


def bigquery_sql(fixture: Fixture, schema: str, index: int, pairs: list[Pair]) -> str:
    """One BigQuery query that runs ``pairs`` on stored database ``index`` and returns, per pair, whether the two
    results differ as bags (rows written with FORMAT('%T'), so column names do not count)."""

    ctes = ",\n".join(f"{table} AS ({_table_literal(fixture, schema, table, index)})" for table in fixture.schemas[schema])

    def bag(sql: str) -> str:
        return f"(SELECT STRING_AGG(FORMAT('%T', q), '|' ORDER BY FORMAT('%T', q)) FROM ({sql}) AS q)"

    rows = "\nUNION ALL\n".join(f"SELECT '{p.id}' AS id, {bag(p.left)} AS l, {bag(p.right)} AS r" for p in pairs)
    return (
        f"WITH {ctes}\nSELECT COUNT(*) AS checked, STRING_AGG(IF(IFNULL(l, '') != IFNULL(r, ''), id, NULL), ' ' ORDER BY id) "
        f"AS differs FROM (\n{rows}\n)"
    )


# ---------------------------------------------------------------- coverage scan


def scan_queries() -> list[tuple[str, str]]:
    """(name, SQL) for every Spider 2.0 dev-split gold query and every bq_corpora model that reads nested data."""

    sys.path.insert(0, str(ROOT / "tools"))
    out = []
    gold = ROOT / "tests" / "fixtures" / "spider2" / "gold"
    for path in sorted(gold.glob("*.sql")):
        if int(hashlib.sha1(path.stem.encode()).hexdigest(), 16) % 5 == 0:
            continue  # Spider 2.0's held-out split: not read
        text = path.read_text(encoding="utf-8", errors="replace")
        if NESTED.search(text):
            out.append((f"spider2/{path.stem}", text))
    from kumosql.pipeline import load_sqlx_project

    for root in sorted(p for p in (ROOT / "tests" / "fixtures" / "bq_corpora").iterdir() if p.is_dir()):
        try:
            pipeline = load_sqlx_project(root)
        except Exception:  # noqa: BLE001
            continue
        for model in pipeline.models.values():
            if model.is_query and model.sql and NESTED.search(model.sql):
                out.append((f"{root.name}/{model.path}", model.sql))
    return out


def scan() -> dict:
    """How many nested queries the SMT prover encodes, and the reasons it declines the rest."""

    from kumosql.smt_equivalence import SmtStatus, prove_equivalent_smt

    reasons: Counter = Counter()
    encoded = 0
    queries = scan_queries()
    for _, sql in queries:
        try:
            result = prove_equivalent_smt(sql, sql, dialect="bigquery", timeout_ms=5000)
        except Exception as exc:  # noqa: BLE001
            reasons[f"error: {type(exc).__name__}"] += 1
            continue
        if result.status is SmtStatus.PROVEN_EQUIVALENT:
            encoded += 1
        else:
            reasons[re.sub(r"\s+", " ", (result.reason or "").split("(")[0]).strip()[:90]] += 1
    return {"queries": len(queries), "encoded": encoded, "reasons": dict(reasons.most_common())}


# ---------------------------------------------------------------- results


def summary(rows: list[dict]) -> dict:
    eq = [r for r in rows if r["label"] == "equivalent"]
    traps = [r for r in rows if r["label"] == "different"]
    return {
        "equivalent": len(eq),
        "proven": sum(r["outcome"] == "proven" for r in eq),
        "traps": len(traps),
        "refuted": sum(r["outcome"] == "refuted" for r in traps),
        "wrong_proofs": sum(r["wrong"] and r["outcome"] == "proven" for r in rows),
        "wrong_refutations": sum(r["wrong"] and r["outcome"] == "refuted" for r in rows),
    }


def results_rows(rows: list[dict], caveats: str = "") -> dict[str, dict]:
    sys.path.insert(0, str(ROOT / "tools"))
    from bench_common import today

    dev = summary([r for r in rows if not r["held_out"]])
    held = summary([r for r in rows if r["held_out"]])
    eq = [r for r in rows if r["label"] == "equivalent"]
    traps = [r for r in rows if r["label"] == "different"]
    s = summary(rows)
    common = {
        "docs": "docs/evals/nested-data.md",
        "command": "python tools/nested_data_bench.py --write-results",
        "date": today(),
        "caveats": caveats,
    }
    proof = {
        "suite": "Nested data (ARRAY, STRUCT, UNNEST): proofs",
        "order": 64,
        "size": len(eq),
        "score": f"{s['proven']}/{len(eq)}, {s['wrong_proofs']} wrong",
        "metric": "Hand-written equivalent query pairs over ARRAY and STRUCT columns in GA4, Snowplow and shop schemas (event_params lookups, UNNEST joins, IN UNNEST, offsets, struct fields) proved equivalent; the traps among the pairs must never be proved.",
        "evidence": "proof",
        "correctness": f"{s['wrong_proofs']} traps proved (every trap returns different rows on BigQuery on a stored database; every equivalent pair agrees on all of them)",
        "coverage": {k: v for k, v in Counter(r["outcome"] for r in eq).items()},
        "held_out": f"{held['proven']}/{held['equivalent']} proved, {held['wrong_proofs']} wrong (dev {dev['proven']}/{dev['equivalent']})",
        **common,
    }
    executed = {
        "suite": "Nested data (ARRAY, STRUCT, UNNEST): counterexamples",
        "order": 65,
        "size": len(traps),
        "score": f"{s['refuted']}/{len(traps)}, {s['wrong_refutations']} wrong",
        "metric": "Trap pairs (queries over nested data that look alike but differ) refuted, by the prover or by a database the counterexample search built with ARRAY and STRUCT values and ran; an equivalent pair must never be refuted.",
        "evidence": "executed",
        "correctness": f"{s['wrong_refutations']} equivalent pairs refuted (labels checked on BigQuery on the stored databases)",
        "coverage": {k: v for k, v in Counter(r["outcome"] for r in traps).items()},
        "held_out": f"{held['refuted']}/{held['traps']} refuted, {held['wrong_refutations']} wrong (dev {dev['refuted']}/{dev['traps']})",
        **common,
    }
    return {"nested-data-proof": proof, "nested-data-executed": executed}


CAVEATS = (
    "Hand-written for KumoSQL (no outside source), 112 pairs (65 equivalent, 47 traps). Every label was checked on "
    "BigQuery by running both queries on the stored databases (inline data, nothing stored): every trap but one "
    "differs on at least one database and no equivalent pair differs on any. That trap, "
    "shop-array-subquery-unordered, depends on an unspecified order, so its label rests on the argument, not on a "
    "database; four pairs the DuckDB replay declines (the two struct equalities, shop-array-length-filtered and "
    "shop-array-subquery-unordered) are checked on BigQuery only. A label shows two queries differ on a stored database; it cannot show they agree "
    "everywhere. The held-out quarter (26 pairs) is chosen by a hash of each pair id, not by any result. This is the "
    "baseline before any prover rule for nested data: only the data, the translation and the counterexample "
    "search changed, so the scores are a regression and honesty check on one author's pairs, not an independent "
    "benchmark."
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--show", default="", help="comma-separated outcomes to print (proven, refuted, unknown, wrong)")
    parser.add_argument("--include-held-out", action="store_true", help="print held-out pairs too (scores always count them)")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--check-labels", action="store_true", help="replay the stored databases on DuckDB")
    parser.add_argument("--bigquery-sql", action="store_true", help="print the BigQuery label checks")
    parser.add_argument("--scan", action="store_true", help="coverage scan of nested Spider 2.0 and bq_corpora queries")
    parser.add_argument("--write-results", action="store_true")
    args = parser.parse_args(argv)
    sys.path.insert(0, str(ROOT / "tools"))
    from bench_common import quiet, write_results

    quiet()
    fixture = load()
    if args.check_labels:
        problems = check_labels(fixture)
        print("\n".join(problems) or f"all {len(fixture.pairs)} labels agree with the stored databases")
        return 1 if problems else 0
    if args.bigquery_sql:
        for schema, stored in fixture.datasets.items():
            for index in range(len(stored)):
                pairs = [p for p in fixture.pairs if p.schema == schema and index in bigquery_checks(fixture, p)]
                print(f"-- {schema} database {index}\n{bigquery_sql(fixture, schema, index, pairs)};\n")
        return 0
    if args.scan:
        print(json.dumps(scan(), indent=2))
        return 0

    rows = run(fixture.pairs, fixture.schemas)
    if args.json:
        print(json.dumps(rows, indent=2))
        return 0
    shown = set(filter(None, args.show.split(",")))
    for row in rows:
        if row["held_out"] and not args.include_held_out:
            continue
        if row["outcome"] in shown or ("wrong" in shown and row["wrong"]):
            print(f"{row['id']:40} {row['label']:10} {row['outcome']:8} {'WRONG ' if row['wrong'] else ''}{row['reason'][:120]}")
    results = results_rows(rows, CAVEATS)
    for name, row in results.items():
        print(f"{row['suite']}: {row['score']}; held out {row['held_out']}")
    if args.write_results:
        for name, row in results.items():
            write_results(name, row, scoreboard=False)
        import scoreboard

        scoreboard.main([])
        print("wrote " + ", ".join(f"benchmarks/results/{name}.json" for name in results))
    return 0


if __name__ == "__main__":
    sys.exit(main())
