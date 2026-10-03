"""Score KumoSQL on paired queries from other engines' test suites and bug fixes, plus authored guards.

Every case in ``tests/fixtures/engine_pairs/cases.jsonl`` is two queries and a hand-checked label:

* **Trino** ``AbstractTestJoinQueries`` (Apache-2.0): every ``assertQuery(sql, expectedSql)`` written
  as two literal strings. Trino runs the first query, H2 runs the second over the same TPC-H tables,
  and the test asserts the same multiset of rows.
* **Spark** ``SubquerySuite`` (Apache-2.0): the EXISTS / IN / NOT EXISTS / NOT IN tests over the
  ``l`` and ``r`` tables (NULLs and duplicates). Queries that expect the same rows are paired; NOT IN
  against NOT EXISTS is the NULL-sensitive negative.
* **PostgreSQL** commit f4c00d1 (PostgreSQL licence, bug 17976): the regression query it added to
  ``join.sql``, against the join-removed form its plan shows and against the dropped-qual reading.
* **DuckDB** ``test_window_order_collate.test`` (MIT): the collation fixture added for issue 20608.
* **Authored** guards: the JoinEquiv projection counterexample and the documented jOOQ COUNT-to-EXISTS
  transforms with negative siblings (our own SQL; the pages are cited, not copied).

Labels: ``equivalent`` (same rows on every database the declared keys and NOT NULL columns allow),
``fixture`` (same rows on the source's own data only) and ``not_equivalent``. Every pair is
translated to BigQuery SQL (the original text and dialect are kept beside it) except the DuckDB
collation pair, which stays in DuckDB SQL.

Each pair gets one outcome:

1. **proven**: ``kumosql.prove_equivalent`` (structural) or ``prove_equivalent_algebraic`` (with the
   fixture's types, keys and NOT NULL columns) proves it. Right only for ``equivalent``.
2. **refuted**: the algebraic prover's executed counterexample search or the targeted refuter
   (``kumosql.refute.find_targeted_difference``) finds a database where the results differ, and a
   replay of that database in DuckDB with the optimizer off (``run_unoptimized``) confirms it. Right
   only for ``fixture`` and ``not_equivalent``.
3. **unknown**: anything else, including a prover claim whose database does not load as declared or
   does not replay (an SMT model may put a placeholder string in an INT64 column).

**Wrong** is a proof of a pair not labelled ``equivalent``, or any claim (replayed or not) that an
``equivalent`` pair differs.

Both queries also run on the source's own data (TPC-H at scale 0.01, Trino's ``tiny`` schema, made by
``tools/benchmark_corpora.py``; the inline Spark, PostgreSQL, DuckDB and authored tables otherwise):
an ``equivalent`` or ``fixture`` pair must return the same rows and a ``not_equivalent`` pair different
rows. One case in five (by a hash of its id) is held out.

    python tools/engine_pairs_bench.py                 # every case (a few minutes)
    python tools/engine_pairs_bench.py --show unknown
    python tools/engine_pairs_bench.py --write-results
    python tools/engine_pairs_bench.py --check-sources # download the pinned files and re-extract
"""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass, field
import hashlib
import json
import logging
from pathlib import Path
import re
import sys
import time
import urllib.request

import sqlglot

logging.getLogger("sqlglot").setLevel(logging.CRITICAL)

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures" / "engine_pairs"
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

PROVER_TIMEOUT_MS = 10000
REFUTE_BUDGET_S = 20.0
NAME = "engine-paired-tests"

# Pinned upstream files: the commit, the path and the file's SHA-256
SOURCES = {
    "trino": {
        "repo": "trinodb/trino",
        "commit": "2cf6ed7e29ee36a67002a6a5373f81cecdd06f7a",
        "path": "testing/trino-testing/src/main/java/io/trino/testing/AbstractTestJoinQueries.java",
        "sha256": "5d11f8436c4d12315199bd08c7ab5338e192324288e71c9a7116901b91c76b78",
        "licence": "Apache-2.0",
    },
    "spark": {
        "repo": "apache/spark",
        "commit": "099c3ee2e2e71ef8c826ee06d35081cb6d82b29c",
        "path": "sql/core/src/test/scala/org/apache/spark/sql/SubquerySuite.scala",
        "sha256": "b88511d3be4c2d25acb838541748b0c5ccc84659674c2283e180fed3767de367",
        "licence": "Apache-2.0",
    },
    "postgres": {
        "repo": "postgres/postgres",
        "commit": "f4c00d138f6dea4c9d8af8ec280b7edc9b0a29e1",
        "path": "src/test/regress/sql/join.sql",
        "sha256": "b7e0166efab99eec1e8d5d45bad5cd227d70a51b3c4d6997c1d0ff886e75b53e",
        "licence": "PostgreSQL",
    },
    "duckdb": {
        "repo": "duckdb/duckdb",
        "commit": "25a52e4149d331ccf1bf2ab02235549a37b0e71b",
        "path": "test/sql/window/test_window_order_collate.test",
        "sha256": "5c4c8321d8b51fd7ebbaf3156c172da41ccac56765d47a0b622ace0c38438478",
        "licence": "MIT",
    },
}

# Trino's tpch connector names columns without the TPC-H prefix and reads decimals as DOUBLE
TPCH = {
    "nation": [("nationkey", "INT64"), ("name", "STRING"), ("regionkey", "INT64"), ("comment", "STRING")],
    "region": [("regionkey", "INT64"), ("name", "STRING"), ("comment", "STRING")],
    "part": [("partkey", "INT64"), ("name", "STRING"), ("mfgr", "STRING"), ("brand", "STRING"), ("type", "STRING"),
             ("size", "INT64"), ("container", "STRING"), ("retailprice", "FLOAT64"), ("comment", "STRING")],
    "supplier": [("suppkey", "INT64"), ("name", "STRING"), ("address", "STRING"), ("nationkey", "INT64"),
                 ("phone", "STRING"), ("acctbal", "FLOAT64"), ("comment", "STRING")],
    "customer": [("custkey", "INT64"), ("name", "STRING"), ("address", "STRING"), ("nationkey", "INT64"),
                 ("phone", "STRING"), ("acctbal", "FLOAT64"), ("mktsegment", "STRING"), ("comment", "STRING")],
    "orders": [("orderkey", "INT64"), ("custkey", "INT64"), ("orderstatus", "STRING"), ("totalprice", "FLOAT64"),
               ("orderdate", "DATE"), ("orderpriority", "STRING"), ("clerk", "STRING"), ("shippriority", "INT64"),
               ("comment", "STRING")],
    "lineitem": [("orderkey", "INT64"), ("partkey", "INT64"), ("suppkey", "INT64"), ("linenumber", "INT64"),
                 ("quantity", "FLOAT64"), ("extendedprice", "FLOAT64"), ("discount", "FLOAT64"), ("tax", "FLOAT64"),
                 ("returnflag", "STRING"), ("linestatus", "STRING"), ("shipdate", "DATE"), ("commitdate", "DATE"),
                 ("receiptdate", "DATE"), ("shipinstruct", "STRING"), ("shipmode", "STRING"), ("comment", "STRING")],
}
TPCH_PREFIX = {"nation": "n_", "region": "r_", "part": "p_", "supplier": "s_", "customer": "c_", "orders": "o_", "lineitem": "l_"}
# The TPC-H specification's primary keys; every TPC-H column is NOT NULL
TPCH_KEYS = {"nation": [["nationkey"]], "region": [["regionkey"]], "part": [["partkey"]], "supplier": [["suppkey"]],
             "customer": [["custkey"]], "orders": [["orderkey"]], "lineitem": [["orderkey", "linenumber"]]}
TPCH_SCALE = 0.01  # Trino's "tiny" schema


@dataclass(frozen=True)
class Case:
    id: str
    source: str
    inventory: str
    origin: str  # original (an upstream pair, translated) | adapted (changed or paired by us) | authored
    kind: str  # alternative | expected-result | witness | guard
    fixture: str
    label: str | None  # equivalent | fixture | not_equivalent; None when the pair is not scored
    why: str
    left: str | None
    right: str | None
    dialect: str = "bigquery"
    translation: str = ""
    original: dict | None = None
    test: str = ""
    line: int = 0
    excluded: str = ""

    @property
    def held_out(self) -> bool:
        return int(hashlib.sha1(f"{NAME}\n{self.id}".encode()).hexdigest(), 16) % 5 == 0

    @property
    def scored(self) -> bool:
        return self.label is not None and not self.excluded


def load_cases(fixtures: Path = FIXTURES) -> list[Case]:
    return [Case(**json.loads(line)) for line in (fixtures / "cases.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]


def load_fixtures(fixtures: Path = FIXTURES) -> dict:
    data = json.loads((fixtures / "fixtures.json").read_text(encoding="utf-8"))
    data["tpch"] = {
        "tables": {t: {"columns": cols} for t, cols in TPCH.items()},
        "constraints": {t: {"not_null": [c for c, _ in cols], "keys": TPCH_KEYS[t]} for t, cols in TPCH.items()},
    }
    return data


# ------------------------------------------------------------------ declared facts


def types_of(fixture: dict) -> dict[str, dict[str, str]]:
    return {t: {c: k for c, k in spec["columns"]} for t, spec in fixture["tables"].items()}


def constraints_of(fixture: dict):
    from kumosql.smt_equivalence import TableConstraints

    out = {}
    for table, spec in (fixture.get("constraints") or {}).items():
        out[table] = TableConstraints(not_null=frozenset(spec.get("not_null", ())), keys=tuple(tuple(k) for k in spec.get("keys", ())))
    return out


def rules_of(fixture: dict):
    from kumosql.result_equivalence import DataRules

    return {
        table: DataRules(not_null=frozenset(spec.get("not_null", ())), keys=tuple(tuple(k) for k in spec.get("keys", ())))
        for table, spec in (fixture.get("constraints") or {}).items()
    }


# ------------------------------------------------------------------ execution


def tpch_file() -> Path | None:
    """The TPC-H tiny database, made once by tools/benchmark_corpora.py; None without tpchgen-cli."""

    import shutil

    import benchmark_corpora

    target = benchmark_corpora.BENCH_DIR / f"tpch-sf{TPCH_SCALE:g}.duckdb"
    if target.exists():
        return target
    if shutil.which("tpchgen-cli") is None:
        return None
    return benchmark_corpora.fetch_tpch_data(TPCH_SCALE)


def inline_struct_rows(sql: str) -> str:
    """For execution only: ``UNNEST([STRUCT(1 AS a), ...]) AS t`` (sqlglot's GoogleSQL for Trino's
    ``VALUES``) becomes ``(SELECT 1 AS a UNION ALL ...) AS t``, the same rows in a GoogleSQL form that
    KumoSQL's DuckDB reading accepts (it refuses a STRUCT outside a select list). The provers get the
    pair as written."""

    from sqlglot import exp

    tree = sqlglot.parse_one(sql, read="bigquery")
    for unnest in list(tree.find_all(exp.Unnest)):
        arrays = unnest.expressions
        if len(arrays) != 1 or not isinstance(arrays[0], exp.Array) or not arrays[0].expressions:
            continue
        items = arrays[0].expressions
        if not all(isinstance(i, exp.Struct) and i.expressions and all(isinstance(f, exp.PropertyEQ) for f in i.expressions) for i in items):
            continue
        selects = [exp.select(*[exp.alias_(f.expression.copy(), f.this.name) for f in item.expressions]) for item in items]
        body = selects[0]
        for select in selects[1:]:
            body = exp.union(body, select, distinct=False)
        alias = unnest.args.get("alias")
        # GoogleSQL's UNNEST(...) AS t names the element (here the struct), which sqlglot keeps as a column alias
        name = (alias.name or (alias.columns[0].name if alias.columns else "")) if alias else ""
        unnest.replace(exp.Subquery(this=body, alias=exp.TableAlias(this=exp.to_identifier(name)) if name else None))
    return tree.sql(dialect="bigquery")


class Engine:
    """A DuckDB connection holding one fixture's tables, running BigQuery SQL the way BigQuery would."""

    def __init__(self, name: str, fixture: dict, rows: dict[str, list] | None = None):
        import duckdb

        from kumosql.bigquery_on_duckdb import configure
        from kumosql.duckdb_load import insert_rows
        from kumosql.result_equivalence import _DUCKDB_TYPES, _local_name

        self.fixture = fixture
        self.dialect = fixture.get("dialect", "bigquery")
        self.schema = types_of(fixture)
        self.db = duckdb.connect()
        if self.dialect == "bigquery":
            configure(self.db)
        data = rows if rows is not None else {t: spec.get("rows", []) for t, spec in fixture["tables"].items()}
        if fixture.get("setup"):  # a native fixture keeps its own DDL (collations)
            self.db.execute(fixture["setup"])
            for table, values in data.items():
                insert_rows(self.db, table, values)
            return
        if name == "tpch" and rows is None:
            path = tpch_file()
            if path is None:
                raise RuntimeError("TPC-H data needs tpchgen-cli (pip install tpchgen-cli)")
            self.db.execute(f"ATTACH '{path}' AS tpch (READ_ONLY)")
            for table, columns in TPCH.items():
                parts = []
                for column, kind in columns:
                    source = TPCH_PREFIX[table] + column
                    parts.append(f"CAST({source} AS {_DUCKDB_TYPES[kind]}) AS {column}")
                self.db.execute(f'CREATE VIEW "{_local_name(table)}" AS SELECT {", ".join(parts)} FROM tpch.{table}')
            return
        for table, spec in fixture["tables"].items():
            columns = ", ".join(f'"{c}" {_DUCKDB_TYPES[k]}' for c, k in spec["columns"])
            self.db.execute(f'CREATE TABLE "{_local_name(table)}" ({columns})')
            insert_rows(self.db, f'"{_local_name(table)}"', data.get(table, []))

    def close(self) -> None:
        self.db.close()

    def __enter__(self) -> "Engine":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def text(self, sql: str) -> str:
        if self.dialect != "bigquery":
            return sql
        from kumosql.result_equivalence import prepare_statements

        statements, target = prepare_statements(inline_struct_rows(sql), self.schema, run_tag="pairs")
        if len(statements) != 1 or target is not None:
            raise ValueError("not one query")
        return statements[0]

    def rows(self, raw: list[tuple]) -> tuple:
        if self.dialect != "bigquery":
            return tuple(tuple(r) for r in raw)
        from kumosql.bigquery_on_duckdb import bigquery_rows

        return tuple(bigquery_rows(raw))

    def differ(self, left: str, right: str) -> tuple[bool, int, int]:
        """Whether the two queries return different bags here (a difference must survive the
        optimizer-off rerun), and each side's row count."""

        from kumosql.duckdb_load import run_unoptimized
        from kumosql.result_equivalence import QueryOutput, compare_outputs

        a, b = self.text(left), self.text(right)

        def same(x, y) -> bool:
            out_x, out_y = (QueryOutput(columns=tuple(f"c{i}" for i in range(len(r[0]) if r else 0)) or ("c",), rows=self.rows(r)) for r in (x, y))
            if x and y and len(x[0]) != len(y[0]):
                return False
            if not x or not y:
                return len(x) == len(y)
            return compare_outputs(out_x, out_y, check_column_names=False)[0]

        x, y = self.db.execute(a).fetchall(), self.db.execute(b).fetchall()
        if same(x, y):
            return False, len(x), len(y)
        x, y = run_unoptimized(self.db, a, b)
        return not same(x, y), len(x), len(y)


def execute(case: Case, fixtures: dict) -> dict:
    """Run both queries on the source's data: {"same": bool | None, "rows": [n, m], "error": str}."""

    try:
        with Engine(case.fixture, fixtures[case.fixture]) as engine:
            differs, n, m = engine.differ(case.left, case.right)
    except Exception as exc:  # a pair that cannot run on its data is reported, not scored as agreeing
        return {"same": None, "rows": None, "error": f"{type(exc).__name__}: {str(exc).splitlines()[0][:200]}"}
    return {"same": not differs, "rows": [n, m], "error": ""}


# ------------------------------------------------------------------ provers and refuter


def _rows_of(case: Case, fixtures: dict, tables: dict) -> dict[str, list]:
    """A counterexample's tables as lists of rows in the fixture's column order."""

    fixture = fixtures[case.fixture]
    out = {}
    for table, spec in fixture["tables"].items():
        names = [c for c, _ in spec["columns"]]
        got = None
        for key, value in tables.items():
            if key.lower().split(".")[-1] == table.lower():
                got = value
        if got is None:
            out[table] = []
        elif hasattr(got, "rows"):  # SyntheticTable
            index = {n.lower(): i for i, (n, _) in enumerate(got.columns)}
            out[table] = [[row[index[n.lower()]] if n.lower() in index else None for n in names] for row in got.rows]
        else:  # list of dicts
            out[table] = [[{k.lower(): v for k, v in row.items()}.get(n.lower()) for n in names] for row in got]
    return out


def replays(case: Case, fixtures: dict, tables: dict) -> bool:
    """The counterexample separates the two queries in a fresh DuckDB (optimizer off agrees)."""

    try:
        with Engine(case.fixture, fixtures[case.fixture], rows=_rows_of(case, fixtures, tables)) as engine:
            return engine.differ(case.left, case.right)[0]
    except Exception:
        return False


def decide(case: Case, fixtures: dict) -> dict:
    from kumosql import prove_equivalent
    from kumosql.algebraic_equivalence import prove_equivalent_algebraic
    from kumosql.refute import find_targeted_difference
    from kumosql.smt_equivalence import SmtStatus

    fixture = fixtures[case.fixture]
    types = types_of(fixture)
    schema = {t: list(cols) for t, cols in types.items()}
    started = time.perf_counter()
    outcome, method = "unknown", ""
    if case.dialect == "bigquery":
        try:
            if prove_equivalent(case.left, case.right).proven:
                outcome, method = "proven", "structural"
        except Exception:
            pass
    claimed = False  # the prover said NOT_EQUIVALENT
    if outcome == "unknown":
        try:
            result = prove_equivalent_algebraic(
                case.left, case.right, schema=schema, types=types if case.dialect == "bigquery" else None,
                constraints=constraints_of(fixture) or None, compare_names=False, dialect=case.dialect,
                timeout_ms=PROVER_TIMEOUT_MS, search_counterexample=True,
            )
        except Exception:  # a crash is a failure to prove, never a proof
            result = None
        if result is not None and result.status is SmtStatus.PROVEN_EQUIVALENT:
            outcome, method = "proven", "algebraic"
        elif result is not None and result.status is SmtStatus.NOT_EQUIVALENT:
            claimed = True
            # An SMT model may use placeholder values (a string in an INT64 column, NULL in a NOT NULL
            # column it does not read); it counts only when it loads and replays as declared.
            if result.counterexample is not None and replays(case, fixtures, result.counterexample.tables):
                outcome, method = "refuted", "algebraic"
    if outcome == "unknown":
        try:
            found = find_targeted_difference(
                case.left, case.right, types, rules_of(fixture) or None, dialect=case.dialect, budget=REFUTE_BUDGET_S,
            )
        except Exception:
            found = None
        if found is not None:
            claimed = True
            if replays(case, fixtures, dict(found.dataset.tables)):
                outcome, method = "refuted", "targeted"
    # Wrong: a proof of a pair that is not equivalent, or any claim that an equivalent pair differs
    wrong = (outcome == "proven" and case.label != "equivalent") or (claimed and case.label == "equivalent")
    correct = not wrong and outcome != "unknown"
    return {
        "id": case.id, "source": case.source, "origin": case.origin, "kind": case.kind, "label": case.label,
        "outcome": outcome, "method": method, "unreplayed_claim": claimed and outcome != "refuted",
        "correct": correct, "wrong": wrong, "held_out": case.held_out, "seconds": round(time.perf_counter() - started, 1),
    }


def run(cases: list[Case], fixtures: dict | None = None, *, execute_pairs: bool = True, skip_tpch: bool = False) -> list[dict]:
    fixtures = fixtures or load_fixtures()
    rows = []
    for case in cases:
        if not case.scored:
            continue
        row = decide(case, fixtures)
        if execute_pairs and not (skip_tpch and case.fixture == "tpch"):
            row["execution"] = execute(case, fixtures)
            same = row["execution"]["same"]
            row["fixture_agrees"] = None if same is None else (same == (case.label != "not_equivalent"))
        rows.append(row)
    return rows


# ------------------------------------------------------------------ results


def summary(rows: list[dict]) -> dict:
    counts = Counter(r["outcome"] for r in rows)
    return {
        "size": len(rows),
        "correct": sum(r["correct"] for r in rows),
        "wrong": sum(r["wrong"] for r in rows),
        "proven": counts["proven"],
        "refuted": counts["refuted"],
        "unknown": counts["unknown"],
    }


def results_row(rows: list[dict], cases: list[Case]) -> dict:
    from bench_common import today

    s = summary(rows)
    held = summary([r for r in rows if r["held_out"]])
    labels = Counter(r["label"] for r in rows)
    by_source = Counter(r["source"] for r in rows)
    executed = [r for r in rows if "execution" in r]
    agreeing = sum(bool(r.get("fixture_agrees")) for r in executed)
    excluded = sum(1 for c in cases if not c.scored)
    unreplayed = sum(bool(r.get("unreplayed_claim")) for r in rows)
    return {
        "suite": "Paired engine tests (Trino, Spark, PostgreSQL, DuckDB) and authored guards",
        "order": 37,
        "size": s["size"],
        "score": f"{s['correct']}/{s['size']} decided correctly, {s['wrong']} wrong",
        "metric": (
            "Query pairs from Trino's two-query join assertions, Spark's predicate-subquery tests, a PostgreSQL "
            "join-removal regression and a DuckDB collation fixture, plus authored JoinEquiv and jOOQ guards, each "
            "labelled equivalent, same rows on the source data only, or not equivalent: correct is a proof of an "
            "equivalent pair or a replayed refutation of any other."
        ),
        "evidence": "proof",
        "correctness": (
            "A proof of a pair not labelled equivalent, or any claim that an equivalent pair differs, counts as wrong; a "
            "refutation counts only when its database loads as declared and replays in DuckDB with the optimizer off "
            f"(prover claims left unknown because they did not: {unreplayed}). Labels were checked by hand and by running both "
            f"queries on the source's own data ({agreeing}/{len(executed)} agree with their label there)."
        ),
        "coverage": {k: s[k] for k in ("proven", "refuted", "unknown") if s[k]},
        "held_out": f"{held['correct']}/{held['size']}, {held['wrong']} wrong",
        "docs": "docs/evals/engine-paired-tests.md",
        "command": "python tools/engine_pairs_bench.py --write-results",
        "date": today(),
        "caveats": (
            f"{labels['equivalent']} equivalent, {labels['fixture']} same-on-fixture-only and {labels['not_equivalent']} "
            f"not-equivalent pairs ({', '.join(f'{k} {v}' for k, v in sorted(by_source.items()))}); {excluded} more extracted "
            "pairs are kept unscored (two Trino clock reads, Trino array_intersect, two Spark multi-column NOT IN, a DuckDB "
            "row_number without ORDER BY). Trino pairs are translated by sqlglot plus recorded hand edits, and the "
            "translations were run on DuckDB through KumoSQL's BigQuery reading, not on BigQuery; the Spark pairing, the "
            "PostgreSQL and DuckDB counterparts and the JoinEquiv and jOOQ guards are ours. Most unknowns are Trino's "
            "inline VALUES, which sqlglot writes as UNNEST of STRUCT arrays and the prover does not read. Every case, "
            "held-out ones included, was seen while building the harness; no prover change was made for this eval."
        ),
    }


# ------------------------------------------------------------------ source pins


def _download(source: str) -> bytes:
    spec = SOURCES[source]
    url = f"https://raw.githubusercontent.com/{spec['repo']}/{spec['commit']}/{spec['path']}"
    with urllib.request.urlopen(url, timeout=60) as response:  # noqa: S310 - pinned https URL
        return response.read()


def _java_literal_pairs(text: str) -> list[dict]:
    """Every assertQuery call whose two SQL arguments are string literals (an optional session first)."""

    out, method = [], None
    for match in re.finditer(r"public void (test\w+)\(|\bassertQuery\(", text):
        if match.group(1):
            method = match.group(1)
            continue
        depth, j, args, current = 1, match.end(), [], []
        while depth:
            c = text[j]
            if text.startswith('"""', j):
                k = text.index('"""', j + 3)
                lines = text[j + 3:k].split("\n")[1:]
                indent = min((len(x) - len(x.lstrip()) for x in lines if x.strip()), default=0)
                current.append(("s", "\n".join(x[indent:] for x in lines).replace("\\\n", "")))
                j = k + 3
                continue
            if text.startswith("//", j):
                j = text.index("\n", j)
                continue
            if c == '"':
                k = j + 1
                while text[k] != '"':
                    k += 2 if text[k] == "\\" else 1
                current.append(("s", text[j + 1:k].encode().decode("unicode_escape")))
                j = k + 1
                continue
            if c in "([{":
                depth += 1
            elif c in ")]}":
                depth -= 1
                if not depth:
                    break
            elif c == "," and depth == 1:
                args.append(current)
                current = []
                j += 1
                continue
            if not c.isspace():
                current.append(("c", c))
            j += 1
        args.append(current)

        def literal(arg):
            if not arg or any(kind == "c" and value != "+" for kind, value in arg):
                return None
            return "".join(value for kind, value in arg if kind == "s")

        values = [literal(a) for a in args]
        line = text.count("\n", 0, match.start()) + 1
        if len(args) == 2 and None not in values:
            out.append({"test": method, "line": line, "left": values[0], "right": values[1]})
        elif len(args) == 3 and values[0] is None and None not in values[1:]:
            out.append({"test": method, "line": line, "left": values[1], "right": values[2]})
    return out


def _squash(text: str) -> str:
    return " ".join(text.split())


def check_sources(cases: list[Case]) -> list[str]:
    """Download every pinned file, check its SHA-256 and that each original query is in it."""

    problems = []
    for source, spec in SOURCES.items():
        body = _download(source)
        digest = hashlib.sha256(body).hexdigest()
        if spec["sha256"] and digest != spec["sha256"]:
            problems.append(f"{source}: SHA-256 {digest} does not match the pin")
        text = body.decode("utf-8")
        mine = [c for c in cases if c.source == source and c.original]
        if source == "trino":
            extracted = {(p["line"], p["left"], p["right"]) for p in _java_literal_pairs(text)}
            recorded = {(c.line, c.original["left"], c.original["right"]) for c in mine}
            if extracted != recorded:
                problems.append(f"trino: {len(extracted - recorded)} extracted pairs missing from the fixture, {len(recorded - extracted)} not in the file")
            continue
        squashed = _squash(re.sub(r'"\s*\+\s*"', "", text))  # Scala's "a" + "b" is one string
        for case in mine:
            for side in ("left", "right"):
                query = case.original.get(side)
                if query and case.original.get(f"{side}_from_source", True) and _squash(query) not in squashed:
                    problems.append(f"{case.id}: original {side} query not found in {spec['path']}")
    return problems


def main(argv: list[str] | None = None) -> int:
    from bench_common import quiet, write_results

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--show", default="", help="comma-separated outcomes to print, e.g. unknown,refuted")
    parser.add_argument("--only", default="", help="comma-separated case ids or id prefixes")
    parser.add_argument("--json", metavar="PATH", help="write every row to PATH")
    parser.add_argument("--write-results", action="store_true")
    parser.add_argument("--check-sources", action="store_true", help="download the pinned files and re-extract")
    args = parser.parse_args(argv)
    quiet()

    cases = load_cases()
    if args.check_sources:
        problems = check_sources(cases)
        print("\n".join(problems) or "every pinned file and original query matches")
        return 1 if problems else 0
    if args.only:
        wanted = tuple(args.only.split(","))
        cases = [c for c in cases if c.id.startswith(wanted)]
    rows = run(cases)
    shown = set(filter(None, args.show.split(",")))
    for row in rows:
        execution = row.get("execution") or {}
        if row["outcome"] in shown or row["wrong"] or row.get("fixture_agrees") is False or execution.get("error"):
            print(f"{row['id']:22} {row['label']:15} {row['outcome']:8} {row['method']:10} unreplayed={row['unreplayed_claim']} "
                  f"fixture={execution.get('same')} {execution.get('rows')} {execution.get('error', '')}")
    s = summary(rows)
    held = summary([r for r in rows if r["held_out"]])
    by = Counter((r["source"], r["outcome"]) for r in rows)
    print("by source:", dict(sorted(by.items())))
    print(f"{s['correct']}/{s['size']} correct, {s['wrong']} wrong (proven {s['proven']}, refuted {s['refuted']}, unknown {s['unknown']}); "
          f"held out {held['correct']}/{held['size']}, {held['wrong']} wrong; "
          f"fixture agrees {sum(bool(r.get('fixture_agrees')) for r in rows)}/{sum('execution' in r for r in rows)}")
    if args.json:
        Path(args.json).write_text(json.dumps(rows, indent=1) + "\n", encoding="utf-8")
    if args.write_results:
        if args.only:
            print("--write-results needs every case")
            return 1
        path = write_results(NAME, results_row(rows, cases))
        print(f"wrote {path.relative_to(ROOT)}")
    return 1 if s["wrong"] else 0


if __name__ == "__main__":
    sys.exit(main())
