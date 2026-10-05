"""Proven rewrites of KumoSQL's own rewrite pipeline, as the evals that run it on a fixed database count them.

Each of these evals sends a query through ``kumosql.rewrite.apply_rules`` (the canonical rule order, plus
``lift_subqueries``), keeps the rewrites whose verification is ``proven``, and runs original and rewrite once in DuckDB
on one fixed database (after sqlglot's BigQuery to DuckDB translation). The adapters prove each pair the same way and run
the same two DuckDB texts on thousands of random databases over the same tables and column types.

* ``bigquery-edge-cases`` (``tools/bq_behavior_eval.py --corpus edge --pipeline lift``, plus its 662 held-out fuzz cases
  as ``heldout:<id>``): the fixed database is ``t(id, a, b, s, f, arr, st)`` and ``u(id, x)``. Columns are typed as the
  setup creates them (INTEGER, VARCHAR, DOUBLE, ``INTEGER[]``, ``STRUCT(x INTEGER, y VARCHAR)``); array and struct values
  are drawn from a small list that includes the empty array and a NULL field; the float column also gets NaN, infinities
  and -0.0. No keys and no NOT NULL: the proof assumes none. Row order is not compared (the eval compares an
  ``ORDER BY`` query as a list on its one database; with ties on random data that would flag the engine's tie-breaking).
* ``transformation-workloads`` (``tools/transformation_bench.py tpch tpcds job``): the 236 TPC-H, TPC-DS and JOB queries
  through the cleanup pipeline and every rule on its own (``<workload>/<query>#<rule>``); a pair counts when the rule
  changes the query and its verification is ``proven``. The eval runs original and rewrite on the benchmark's data in DuckDB
  through ``transformation_bench._duck`` (sqlglot's BigQuery to DuckDB); the tables are typed by the benchmark's own DDL
  (TPC-H's, DSB's ``tpcds.sql``, JOB's ``schema.sql``), with no keys and no NOT NULL (the proof assumes none). Where the two
  DuckDB texts are identical (a layout-only change, which ``format_sql`` makes of nearly every query) the pair is trivially
  equal and is not run. Needs ``KUMOSQL_BENCH_DIR`` (``benchmark_corpora.py fetch sqlstorm``, plus a DSB checkout for TPC-DS).
* ``job-alternative-forms`` (same script, ``--forms``): each of the five valid alternative forms of a JOB query that the
  eval's ``_prove`` proves (``prove_equivalent_smt`` with the schema's columns, else the structural prover).
* ``sample-databases-rewrites``, ``-sakila-``, ``-pagila-``, ``-oracle_co-``, ``-oracle_hr-rewrites``
  (``tools/sample_db_bench.py``): every workload query through the pipeline stages the eval runs
  (``engine_suites.evaluate_query``: the canonical rule order on the query and on its wrapped and padded variants,
  then ``lift_subqueries``, then the proof-gated ``query_optimizer.optimize`` with the declared keys and NOT NULL
  columns). The eval counts a transformed query "verified" when its real-data result matches, proven or not; a pair
  here needs the rewrite's verification to be ``proven`` (``optimize`` only returns proven rewrites), since an unproven
  rewrite claims nothing. Left is the original query as the eval's control runs it, right the rewrite. The pipeline and
  lift stages run on unconstrained tables (the rules get no catalog); the optimizer stage on the declared constraints.
  A query that reads one of the workload's views is not runnable here (the views are created on the real database).
* ``sqlfluff-refusals`` (``tools/sqlfluff_fixtures_bench.py refusals``): each of the 212 queries sqlfluff leaves alone, through
  each of the seven structural rules (``<case>#<rule>``); a pair counts when the rule changes the query and the verification
  is ``proven``. The eval runs the pair on random DuckDB databases (``rewrite_harms``: the input read as BigQuery, tables read
  off the two queries with a guessed int, text or date type per column); the Case is the same two texts over the same
  inferred tables. The eval counts the 189 "left alone or changed with a proof"; the changed ones are the pairs here.
* ``googlesql-behavior`` (same script, ``--corpus googlesql``): compliance-test queries over no tables, so the only
  database is the empty one and the re-check is the eval's own single run (a queries that reads a table is not executable
  there and is ``unrunnable`` here).
"""

from __future__ import annotations

import gzip
import json
import logging
from pathlib import Path
import sys

import sqlglot  # noqa: E402

TOOLS = Path(__file__).resolve().parent.parent
ROOT = TOOLS.parent
for _path in (str(TOOLS), str(ROOT / "src")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from recheck import dialect_rewrites as dr  # noqa: E402
from recheck import engine  # noqa: E402,F401
from recheck import new_evals_b  # noqa: E402
from recheck.engine import Case, Column, Table  # noqa: E402

logging.getLogger("sqlglot").setLevel(logging.CRITICAL)

Adapter = dr.Adapter


def pipeline_rewrite(sql: str, rules) -> tuple[str, str] | None:
    """``(rewritten text, verification)`` when the pipeline changes ``sql`` (beyond layout) and every step is trusted, else None.
    The eval's own test (``bq_behavior_eval.evaluate``): unchanged or layout-only text is "declined"; an unproven change
    is declined too."""

    from kumosql import rewrite

    try:
        tree = sqlglot.parse_one(sql, read="bigquery")
        if tree is None or isinstance(tree, sqlglot.exp.Command):
            return None
    except Exception:  # noqa: BLE001
        return None
    try:
        result = rewrite.apply_rules(rules, sql)
    except Exception:  # noqa: BLE001
        return None
    after = result.sql
    if after.strip() == sql.strip():
        return None
    try:
        if sqlglot.parse_one(after, read="bigquery") == tree:
            return None
    except Exception:  # noqa: BLE001
        pass
    if not result.verification.trusted:
        return None
    return after, result.verification.status.value


def duck(sql: str) -> str:
    """The eval's translation: ``sqlglot.transpile(sql, read="bigquery", write="duckdb")[0]``."""

    return sqlglot.transpile(sql, read="bigquery", write="duckdb")[0]


# --- BigQuery edge cases and GoogleSQL compliance queries --------------------------------------------------------------

_ARRAYS = ([], [1], [1, 2, 3], [5, 5], [3], [-1, 0])
_STRUCTS = ({"x": 1, "y": "p"}, {"x": None, "y": "p"}, {"x": 4, "y": None}, {"x": 5, "y": "r"}, {"x": None, "y": None}, {"x": 0, "y": ""})


def edge_tables() -> dict[str, Table]:
    t = Table("t", [
        Column("id", "int", sql_type="INTEGER"), Column("a", "int", sql_type="INTEGER"), Column("b", "int", sql_type="INTEGER"),
        Column("s", "text", sql_type="VARCHAR"), Column("f", "float", sql_type="DOUBLE"),
        Column("arr", "text", sql_type="INTEGER[]", values=tuple(_ARRAYS)),
        Column("st", "text", sql_type="STRUCT(x INTEGER, y VARCHAR)", values=tuple(_STRUCTS)),
    ])
    u = Table("u", [Column("id", "int", sql_type="INTEGER"), Column("x", "int", sql_type="INTEGER")])
    return {"t": t, "u": u}


class BqBehavior(Adapter):
    def __init__(self, name: str, corpus: str):
        self.name = name
        self.corpus = corpus

    def _cases(self) -> list[dict]:
        import bq_behavior_eval as bb

        if self.corpus == "googlesql":
            with gzip.open(bb.GOOGLESQL_CORPUS, "rt", encoding="utf-8") as handle:
                return [dict(c, held_out=False, pair=c["id"]) for c in json.load(handle)]
        cases = [dict(c, held_out=False, pair=c["id"]) for c in bb.load_edge()]
        if self.corpus == "edge+heldout":
            cases += [dict(c, held_out=True, pair="heldout:" + c["id"]) for c in bb.load_edge(bb.HELDOUT_CASES)]
        return cases

    def items(self) -> list[dict]:
        return [{"pair": c["pair"], "id": c["id"], "sql": c["sql"], "held_out": c["held_out"]} for c in self._cases()]

    def case(self, item: dict) -> Case | None:
        import bq_behavior_eval as bb

        new_evals_b._install()
        rewritten = pipeline_rewrite(item["sql"], bb.PIPELINES["lift"])
        if rewritten is None:
            return None
        after, status = rewritten
        loose = bb.unordered_aggregates(item["sql"])
        if "GroupConcat" in loose:
            return None  # the eval gives no verdict: the concatenation order is unspecified
        try:
            left, right = duck(item["sql"]), duck(after)
        except Exception:  # noqa: BLE001
            return None
        tables = edge_tables() if self.corpus != "googlesql" else {}
        return Case(self.name, item["pair"], left, right, tables, held_out=item["held_out"], source=(item["sql"], after), dialect="bigquery",
                    meta={"verification": status, "ordered": bb.ordered(item["sql"]), "unordered_elements": bool(loose), "numeric": "special"})


# --- TPC-H, TPC-DS and JOB: cleanup pipeline and alternative forms ---------------------------------------------------------


def _workload_tables(workload: str, names: set[str]) -> dict[str, Table]:
    """Engine tables for the tables ``names`` of a workload, typed by the benchmark's own DDL."""

    import benchmark_corpora as corpora

    if workload == "tpch":
        ddl = corpora.TPCH_DDL
    elif workload == "tpcds":
        ddl = (corpora.BENCH_DIR / "dsb" / "code" / "tools" / "tpcds.sql").read_text()
    else:
        ddl = (corpora.BENCH_DIR / "join-order-benchmark" / "schema.sql").read_text()
    raw = _DDL.get((workload, ddl))
    if raw is None:
        raw = _DDL[(workload, ddl)] = dr.ddl_tables(ddl)
    return {
        name: Table(name, [dr.engine_column(c, t) for c, t, _ in info["columns"]])
        for name, info in raw.items() if name in names
    }


_DDL: dict = {}


def _read_names(*sqls: str) -> set[str]:
    return dr._read_tables(*sqls, dialect="bigquery")


class TransformationWorkloads(Adapter):
    name = "transformation-workloads"

    def items(self) -> list[dict]:
        import benchmark_corpora as corpora
        import transformation_bench as tb

        out = []
        for workload in tb.WORKLOADS:
            for query_id, _ in tb.workload_queries(workload):
                for rule in tb.transformations():
                    out.append({"pair": f"{query_id}#{rule}", "workload": workload, "id": query_id, "rule": rule,
                                "held_out": corpora.held_out(query_id)})
        return out

    def case(self, item: dict) -> Case | None:
        import transformation_bench as tb
        from kumosql.rewrite import VerificationStatus

        dr.install()
        sql = dict(tb.workload_queries(item["workload"]))[item["id"]]
        try:
            result = tb.apply(item["rule"], sql)
        except Exception:  # noqa: BLE001
            return None
        if result.sql == sql or result.verification.status != VerificationStatus.PROVEN:
            return None
        left, right = tb._duck(sql), tb._duck(result.sql)
        if left == right:
            return None  # a layout-only change: the DuckDB text is the same
        tables = _workload_tables(item["workload"], _read_names(sql, result.sql))
        return Case(self.name, item["pair"], left, right, tables, held_out=item["held_out"], source=(sql, result.sql), dialect="bigquery",
                    meta={"workload": item["workload"], "rule": item["rule"]})


class JobForms(Adapter):
    name = "job-alternative-forms"

    def items(self) -> list[dict]:
        import benchmark_corpora as corpora
        import transformation_bench as tb

        out = []
        for query_id, sql in tb.workload_queries("job"):
            schema = corpora.schema("sqlstorm/job")
            for form, build in tb.FORMS.items():
                try:
                    text = build(sql, schema)
                except Exception:  # noqa: BLE001
                    continue
                if text is not None:
                    out.append({"pair": f"{query_id}#{form}", "id": query_id, "form": form, "held_out": corpora.held_out(query_id)})
        return out

    def case(self, item: dict) -> Case | None:
        import benchmark_corpora as corpora
        import transformation_bench as tb

        dr.install()
        schema = corpora.schema("sqlstorm/job")
        sql = dict(tb.workload_queries("job"))[item["id"]]
        form = tb.FORMS[item["form"]](sql, schema)
        if form is None:
            return None
        try:
            status, _ = tb._prove(sql, form, schema)
        except Exception:  # noqa: BLE001
            return None
        if status != "proven_equivalent":
            return None
        tables = _workload_tables("job", _read_names(sql, form))
        return Case(self.name, item["pair"], tb._duck(sql), tb._duck(form), tables, held_out=item["held_out"], source=(sql, form),
                    dialect="bigquery", meta={"form": item["form"]})


# --- sample databases: the rewrite stages -----------------------------------------------------------------------------------


class SampleRewrites(Adapter):
    def __init__(self, name: str, databases: tuple[str, ...]):
        self.name = name
        self.databases = databases

    def items(self) -> list[dict]:
        import engine_suites as es
        import sample_db_bench as sb

        out = []
        for database in self.databases:
            adapter = sb.ADAPTERS[database]
            for query in adapter.workload():
                held = sb.held_out(f"{database}:{query['id']}")
                tree = es._single_query(query["sql"], "bigquery")
                variants = ["plain"] + ([label for label, _ in es._variants(tree)] if tree is not None else [])
                for variant in variants:
                    out.append({"pair": f"{database}:{query['id']}#{variant}#pipeline", "database": database, "id": query["id"], "variant": variant, "stage": "pipeline", "held_out": held})
                for stage in ("lift_subqueries", "optimizer"):
                    out.append({"pair": f"{database}:{query['id']}#plain#{stage}", "database": database, "id": query["id"], "variant": "plain", "stage": stage, "held_out": held})
        return out

    def case(self, item: dict) -> Case | None:
        import engine_suites as es
        import sample_db_bench as sb
        from kumosql import query_optimizer as qo
        from kumosql.rewrite import VerificationStatus, apply_rules, canonical_rule_order

        dr.install()
        adapter = sb.ADAPTERS[item["database"]]
        query = next(q for q in adapter.workload() if q["id"] == item["id"])
        sql = query["sql"]
        left = right = None
        if item["stage"] == "optimizer":
            try:
                outcome = qo.optimize(sql, sb.optimizer_catalog(adapter), dialect="bigquery", timeout_ms=5000, deletion_budget_s=10.0,
                                      budget_s=sb.OPTIMIZER_BUDGET_S)
            except Exception:  # noqa: BLE001
                return None
            if not outcome.sql:
                return None
            left, right, treated = sb.to_duckdb(sql), sb.to_duckdb(outcome.sql), outcome.sql
        else:
            tree = es._single_query(sql, "bigquery")
            if tree is None or es.NONDETERMINISTIC.search(sql):
                return None
            bigquery_sql = tree.sql(dialect="bigquery")
            try:
                control = sqlglot.transpile(bigquery_sql, read="bigquery", write="duckdb")[0]
            except Exception:  # noqa: BLE001
                return None
            if item["stage"] == "pipeline":
                text = bigquery_sql if item["variant"] == "plain" else dict(es._variants(tree))[item["variant"]]
                rules, source = canonical_rule_order(), text
            else:
                rules, source = ("lift_subqueries",), sql
            try:
                result = apply_rules(rules, source)
            except Exception:  # noqa: BLE001
                return None
            try:
                if sqlglot.parse_one(result.sql, read="bigquery") == sqlglot.parse_one(source, read="bigquery"):
                    return None
            except Exception:  # noqa: BLE001
                pass
            if result.verification.status != VerificationStatus.PROVEN:
                return None
            try:
                left, right, treated = control, sqlglot.transpile(result.sql, read="bigquery", write="duckdb")[0], result.sql
            except Exception:  # noqa: BLE001
                return None
        constrained = item["stage"] == "optimizer"
        tables = _tables_for(adapter, constrained)
        return Case(self.name, item["pair"], left, right, tables, held_out=item["held_out"], source=(sql, treated), dialect="bigquery",
                    meta={"stage": item["stage"], "variant": item["variant"], "origin": query.get("origin", "")})


def _tables_for(adapter, constrained: bool) -> dict[str, Table]:
    tables = new_evals_b.SampleDatabasePairs.tables(adapter, ())
    if constrained:
        return tables
    return {name: Table(name, [Column(c.name, c.kind, False, c.sql_type, c.values) for c in t.columns]) for name, t in tables.items()}


# --- sqlfluff refusals ---------------------------------------------------------------------------------------------------------


class SqlfluffRefusals(Adapter):
    name = "sqlfluff-refusals"

    def items(self) -> list[dict]:
        import sqlfluff_fixtures_bench as sf

        return [{"pair": f"{case.id}#{rule}", "id": case.id, "rule": rule, "held_out": case.held_out} for case in sf.load_refusals() for rule in sf.STRUCTURAL_RULES]

    def case(self, item: dict) -> Case | None:
        import sqlfluff_fixtures_bench as sf
        from kumosql.rewrite import apply_rule

        new_evals_b._install()
        case = next(c for c in sf.load_refusals() if c.id == item["id"])
        try:
            result = apply_rule(item["rule"], case.sql)
        except Exception:  # noqa: BLE001
            return None
        if result.sql.strip() == case.sql.strip() or result.verification.status.value not in sf._TRUSTED:
            return None
        left = None
        for reading in (case.dialect, "bigquery"):
            try:
                left = sf.parse_all(case.sql, reading)
                break
            except sf.Unsupported:
                continue
        try:
            right = sf.parse_all(result.sql, "bigquery")
        except sf.Unsupported:
            return None
        if left is None or len(left) != 1 or len(right) != 1 or not isinstance(right[0], sqlglot.exp.Query):
            return None
        schema, kinds = sf.infer_schema(left + right)
        from recheck import pipelines_refactors as pr

        try:
            sqls = [pr.to_duck(sql, "bigquery") for sql in (case.sql, result.sql)]
        except pr.NotRechecked:
            return None
        names = {"int": ("int", "BIGINT"), "text": ("text", "VARCHAR"), "date": ("date", "DATE")}
        tables = {}
        for name, columns in schema.items():
            cols = []
            for c in columns:
                kind, sql_type = names.get(kinds.get(f"{name}.{c}", "int"), names["int"])
                cols.append(Column(c, kind, sql_type=sql_type))
            tables[name] = Table(name, cols)
        return Case(self.name, item["pair"], sqls[0], sqls[1], tables, setup=pr.bq_setup(), held_out=item["held_out"],
                    source=(case.sql, result.sql), dialect="bigquery", meta={"rule": item["rule"], "status": result.verification.status.value})


ADAPTERS = {
    a.name: a
    for a in [
        BqBehavior("bigquery-edge-cases", "edge+heldout"),
        BqBehavior("googlesql-behavior", "googlesql"),
        SampleRewrites("sample-databases-rewrites", ("chinook", "northwind")),
        SampleRewrites("sample-databases-sakila-rewrites", ("sakila",)),
        SampleRewrites("sample-databases-pagila-rewrites", ("pagila",)),
        SampleRewrites("sample-databases-oracle_co-rewrites", ("oracle_co",)),
        SampleRewrites("sample-databases-oracle_hr-rewrites", ("oracle_hr",)),
        SqlfluffRefusals(),
        TransformationWorkloads(),
        JobForms(),
    ]
}
