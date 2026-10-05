"""Proven pairs of evals that landed or grew after round one of the re-check (family ``new-evals-a``).

Each adapter proves a pair exactly as its eval's harness does and returns the DuckDB SQL that harness's own
executed check runs, over the tables the eval declares.

* ``quite-rewrites`` and ``quite-negatives`` (``tools/quite_bench.py``): the flagged-equal and flagged-unequal
  pairs, proved with ``quite_bench.prove`` (PostgreSQL, exact arithmetic, 60 s) and replayed as
  ``quite_bench.replay_sql`` reads them (PostgreSQL integer division and NULL order, ``now()`` pinned).
* ``logos-core-proof`` (``tools/logos_bench.py``): one item per statement pair; a case counts when every
  statement is proved (BigQuery route, then MySQL route), as ``logos_bench.outcome`` requires.
* ``querybooster`` (``tools/querybooster_bench.py``): ``prove`` with the case's schema, replayed through
  ``DatasetRunner.prepare`` with the dialect's settings; an ORDER BY .. LIMIT pair on both sides is compared as a list.
* ``dbgpt-rules`` and ``documented-rewrites`` (``tools/dbgpt_rules_bench.py``): ``prove`` of every valid case,
  replayed with ``to_duckdb`` on the case's schema.
* ``optimizer-bugs`` (``tools/optimizer_bugs_bench.py``): every pair must stay unproved; a proof would be a case.
* ``jaffle-shop-refactors`` (``tools/jaffle_shop_bench.py``): one item per changed output of each authored refactor;
  a refactor counts when ``prove_models`` proves every changed output, and each output becomes a pair of
  queries with the upstream models inlined as CTEs, translated as the eval's ``duckdb_sql`` translates a model.
* ``cosette-adapted`` (``tools/cosette_bench.py cosette-adapted``): the adapted Cosette pairs, as ``cosette``.
* ``arcwise-corrections`` (``tools/arcwise_bench.py``): BIRD gold SQL against its repair, decided as
  ``llm_sql_solver_bench.decide`` decides a Spider pair; the eval expects no proof (a proof is cosmetic or wrong).
* ``analytical-sql-coverage`` (``tools/analytical_coverage.py``): the eval's execution stage runs every cleanup and
  formatter rewrite KumoSQL marks proven against the original on generated tables; each such rewrite of the
  sampled queries (60 spread through each corpus, ``--split all``) is a pair.
"""

from __future__ import annotations

from pathlib import Path
import signal
import sys

TOOLS = Path(__file__).resolve().parent.parent
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import sqlglot  # noqa: E402

from recheck.engine import Case, Column, Table  # noqa: E402

_KIND = {"BIGINT": "int", "DOUBLE": "float", "DATE": "date", "TIMESTAMP": "timestamp", "BOOLEAN": "bool", "VARCHAR": "text"}


class _Deadline(Exception):
    pass


def _raise_deadline(_signum, _frame):
    raise _Deadline


def run_capped(seconds: float, function, *args):
    """``function(*args)``, abandoned after ``seconds`` (``_Deadline``); the caller's own timer keeps running.

    ``tools/proof_recheck.py`` keeps an interval timer on SIGALRM for the whole pair, so this one saves it,
    runs its own, and puts the caller's back with the time that passed taken off.
    """

    import time

    previous_handler = signal.signal(signal.SIGALRM, _raise_deadline)
    remaining, interval = signal.getitimer(signal.ITIMER_REAL)
    start = time.time()
    signal.setitimer(signal.ITIMER_REAL, seconds, 0)
    try:
        return function(*args)
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)
        if remaining:
            signal.setitimer(signal.ITIMER_REAL, max(0.05, remaining - (time.time() - start)), interval)


class Adapter:
    """``items()`` lists every pair (picklable); ``case(item)`` proves it as the eval does and returns a Case, or None."""

    name = ""

    def items(self) -> list[dict]:
        raise NotImplementedError

    def case(self, item: dict) -> Case | None:
        raise NotImplementedError


# --- QuITE ---------------------------------------------------------------------------------------------


class Quite(Adapter):
    """``tools/quite_bench.py``: ``decide`` proves with ``prove`` (postgres, exact arithmetic, bag equivalence)
    inside ``PROVE_SECONDS`` and replays on DuckDB with PostgreSQL's integer division and NULL ordering."""

    def __init__(self, name: str, label: str):
        self.name = name
        self.label = label
        self._tables: dict = {}
        self._schemas: dict = {}

    def items(self) -> list[dict]:
        import quite_bench as q

        items = [
            {"pair": p.key, "benchmark": p.benchmark, "original": p.original, "rewritten": p.rewritten, "held_out": p.held_out,
             "reason": p.reason, "systems": sum(p.systems.values())}
            for p in q.load_pairs()
            if p.label == self.label
        ]
        # shortest first: the cheapest proofs and searches come first, and ``--every n`` samples every length
        return sorted(items, key=lambda i: len(i["original"]) + len(i["rewritten"]))

    def schema(self, benchmark: str) -> dict:
        import quite_bench as q

        if benchmark not in self._schemas:
            self._schemas[benchmark] = q.schema_for(benchmark)
        return self._schemas[benchmark]

    def engine_tables(self, benchmark: str) -> dict[str, Table]:
        import quite_bench as q

        if benchmark not in self._tables:
            out = {}
            schema = self.schema(benchmark)
            for name, table in schema.items():
                columns = []
                for column, pg_type in table.columns.items():
                    duck = q._duck_type(pg_type)
                    kind = "decimal" if duck.startswith("DECIMAL") else _KIND[duck]
                    columns.append(Column(column, kind, not_null=column in table.not_null, sql_type=duck))
                out[name] = Table(name, columns, list(table.keys), [(c, p, pc) for c, p, pc in table.foreign if p in schema])
            self._tables[benchmark] = out
        return self._tables[benchmark]

    def case(self, item: dict) -> Case | None:
        import quite_bench as q

        tables = self.schema(item["benchmark"])
        try:
            left, _ = q.adapt(item["original"])
            right, _ = q.adapt(item["rewritten"])
            sqlglot.parse_one(left, read="postgres"), sqlglot.parse_one(right, read="postgres")
        except (sqlglot.errors.SqlglotError, ValueError):
            return None  # unsupported
        try:
            result = run_capped(q.PROVE_SECONDS, q.prove, left, right, tables)
        except _Deadline:
            return None
        except Exception:  # a crash is a failure to prove, never a proof
            return None
        if not result.proven:
            return None
        return Case(
            self.name, item["pair"], q.replay_sql(left), q.replay_sql(right), self.engine_tables(item["benchmark"]),
            setup=q.DUCKDB_SETTINGS, held_out=item["held_out"], source=(left, right), dialect="postgres",
            meta={"benchmark": item["benchmark"], "reason": item["reason"], "systems": item["systems"]},
        )




# --- Logos ----------------------------------------------------------------------------------------------

_SQL_KIND = {"INT": "int", "DECIMAL": "decimal", "DOUBLE": "float", "DATE": "date", "TIME": "time"}


def _declared_column(name: str, declared: str, not_null: bool) -> Column:
    base = declared.split("(")[0].strip().upper()
    if base in ("INT", "INTEGER", "BIGINT", "SMALLINT"):
        return Column(name, "int", not_null, "BIGINT")
    if base in ("DECIMAL", "NUMERIC"):
        return Column(name, "decimal", not_null, declared.replace(" ", "") if "(" in declared else "DECIMAL(18,3)")
    if base in ("FLOAT", "DOUBLE", "REAL"):
        return Column(name, "float", not_null, "DOUBLE")
    if base == "DATE":
        return Column(name, "date", not_null, "DATE")
    if base == "TIME":
        return Column(name, "time", not_null, "TIME")
    return Column(name, "text", not_null, "VARCHAR")


class Logos(Adapter):
    name = "logos-core-proof"

    def items(self) -> list[dict]:
        import logos_bench as lb

        out = []
        for case in lb.load_cases():
            for index, (left, right) in enumerate(case.statements):
                name = case.id if len(case.statements) == 1 else f"{case.id}#{index}"
                out.append({"pair": name, "case": case.id, "index": index, "family": case.family, "rule": case.rule,
                            "held_out": case.held_out, "statements": case.statements})
        return out

    def case(self, item: dict) -> Case | None:
        import logos_bench as lb

        folder, database = lb.FAMILIES[item["family"]]
        workload = lb.load_workload(lb.core_root() / folder / "create_tables.sql")
        # a case is proven only when every one of its statement pairs is (``outcome``)
        for left, right in item["statements"]:
            if lb.prove(left, right, workload)["status"] != "proven":
                return None
        left, right = item["statements"][item["index"]]
        tables = {}
        for table, columns in workload.columns.items():
            keys = workload.keys[table]
            tables[table] = Table(
                table, [_declared_column(c, workload.types[table][c], c in workload.not_null[table]) for c in columns],
                [tuple(k) for k in keys],
            )
        return Case(self.name, item["pair"], lb._duck(left, database), lb._duck(right, database), tables, held_out=item["held_out"],
                    source=(left, right), dialect="postgres", meta={"family": item["family"], "rule": item["rule"]})


# --- QueryBooster ---------------------------------------------------------------------------------------


class QueryBooster(Adapter):
    name = "querybooster"

    def items(self) -> list[dict]:
        import querybooster_bench as qb

        cases, _ = qb.load_cases()
        return [{"pair": c.id, "family": c.family, "left": c.left, "right": c.right, "claim": c.claim, "schema_name": c.schema_name,
                 "source": c.source, "held_out": c.held_out} for c in cases]

    def case(self, item: dict) -> Case | None:
        import querybooster_bench as qb
        from kumosql.result_equivalence import DatasetRunner, _local_name

        if not hasattr(self, "_schemas"):
            self._schemas = qb.load_cases()[1]
        case = qb.Case(item["pair"], item["family"], item["source"], item["left"], item["right"], item["claim"], item["schema_name"])
        schema = qb.schema_for(case, self._schemas)
        proven, _, _ = qb.prove(case.left, case.right, schema)
        if not proven:
            return None
        used = qb._used(case, schema)
        kinds = {t: schema.kinds[t] for t in used}
        runner = DatasetRunner(kinds, schema.dialect, qb._settings(schema))
        try:
            left, right = runner.prepare(case.left), runner.prepare(case.right)
        finally:
            runner.close()
        duck = {"INT64": ("int", "BIGINT"), "FLOAT64": ("float", "DOUBLE"), "NUMERIC": ("decimal", "DECIMAL(38,9)"), "STRING": ("text", "VARCHAR"),
                "BOOL": ("bool", "BOOLEAN"), "DATE": ("date", "DATE"), "TIMESTAMP": ("timestamp", "TIMESTAMP")}
        tables = {}
        for table in used:
            not_null = schema.not_null.get(table, set())
            columns = [Column(c, duck[k][0], c in not_null, duck[k][1]) for c, k in kinds[table].items()]
            foreign = [((c,), _local_name(p), (pc,)) for child, c, p, pc in schema.foreign if child == table and p in used]
            keys = [tuple(k) for k in schema.unique.get(table, [])]
            tables[_local_name(table)] = Table(_local_name(table), columns, keys, foreign)
        ordered = qb.ordered(case.left, schema.dialect) and qb.ordered(case.right, schema.dialect)
        return Case(self.name, item["pair"], left, right, tables, setup=qb._settings(schema), held_out=item["held_out"],
                    source=(case.left, case.right), dialect=schema.dialect, mode="list" if ordered else "bag",
                    meta={"family": item["family"], "claim": item["claim"], "schema": schema.origin})


# --- DB-GPT and documented rewrites ---------------------------------------------------------------------

_DOMAIN_KIND = {"INTEGER": "int", "VARCHAR": "text", "BOOLEAN": "bool", "TIMESTAMP": "timestamp"}


class DbgptStyle(Adapter):
    """``dbgpt_rules_bench.decide``'s proof (``prove``) and ``differs_on``'s DuckDB run, over the case's own tables."""

    def __init__(self, name: str, fixtures: str):
        self.name = name
        self.fixtures = fixtures

    def _cases(self) -> list:
        import dbgpt_rules_bench as shared

        return shared.load_cases(shared.FIXTURES.parent / self.fixtures)

    def items(self) -> list[dict]:
        return [{"pair": c.id, "label": c.label} for c in self._cases()]

    def case(self, item: dict) -> Case | None:
        import dbgpt_rules_bench as shared

        case = next(c for c in self._cases() if c.id == item["pair"])
        if case.label == "invalid" or shared.prove(case) != "proven":
            return None  # an invalid case is never scored (and does not run)
        tables = {}
        for table, columns in case.schema.items():
            rules = case.constraints.get(table, {})
            keys = [tuple(k) for k in rules.get("keys", ())]
            not_null = set(rules.get("not_null", ())) | {c for key in keys for c in key}
            tables[table] = Table(table, [Column(c, _DOMAIN_KIND[k], c in not_null, k) for c, k in columns.items()], keys)
        return Case(self.name, item["pair"], shared.to_duckdb(case.left, case.dialect), shared.to_duckdb(case.right, case.dialect), tables,
                    source=(case.left, case.right), dialect=case.dialect, meta={"label": case.label, "source": case.source})


# --- optimizer bugs -------------------------------------------------------------------------------------


class OptimizerBugs(Adapter):
    """Pairs that must never be proved; a proof becomes a Case over the setup's tables (DuckDB cases only)."""

    name = "optimizer-bugs"

    def items(self) -> list[dict]:
        import optimizer_bugs_bench as ob

        return [{"pair": c.id, "held_out": c.held_out} for c in ob.load_cases()]

    def case(self, item: dict) -> Case | None:
        import optimizer_bugs_bench as ob

        case = next(c for c in ob.load_cases() if c.id == item["pair"])
        if ob.prove(case) != "proven":
            return None
        if case.engine != "duckdb":
            raise LookupError(f"proved a pair whose bug runs on {case.engine}: re-check it by hand")
        tables = {}
        for statement in sqlglot.parse(case.setup, read="duckdb") if case.setup else ():
            if not (isinstance(statement, sqlglot.exp.Create) and statement.kind == "TABLE"):
                continue
            name = statement.this.this.name
            columns, keys, not_null = [], [], set()
            for column in statement.this.expressions:
                if isinstance(column, sqlglot.exp.ColumnDef):
                    declared = column.args["kind"].sql(dialect="duckdb")
                    base = declared.split("(")[0].upper()
                    kind = ("int" if base in ("INTEGER", "INT", "BIGINT", "SMALLINT", "TINYINT") else "decimal" if base in ("DECIMAL", "NUMERIC")
                            else "float" if base in ("DOUBLE", "FLOAT", "REAL") else "date" if base == "DATE" else "timestamp" if base == "TIMESTAMP"
                            else "bool" if base == "BOOLEAN" else "text")
                    columns.append(Column(column.name, kind, sql_type=declared))
                    for constraint in column.args.get("constraints") or ():
                        what = constraint.args.get("kind")
                        if isinstance(what, sqlglot.exp.PrimaryKeyColumnConstraint):
                            keys.append((column.name,))
                            not_null.add(column.name)
                        elif isinstance(what, sqlglot.exp.NotNullColumnConstraint):
                            not_null.add(column.name)
                        elif isinstance(what, sqlglot.exp.UniqueColumnConstraint):
                            keys.append((column.name,))
            for column in columns:
                column.not_null = column.name in not_null
            tables[name] = Table(name, columns, keys)
        return Case(self.name, item["pair"], case.left, case.right, tables, held_out=item["held_out"], source=(case.left, case.right), dialect="duckdb",
                    meta={"tracker": case.tracker, "family": case.family})


# --- Jaffle Shop refactors ------------------------------------------------------------------------------


class JaffleRefactors(Adapter):
    name = "jaffle-shop-refactors"

    def items(self) -> list[dict]:
        import jaffle_shop_bench as jb

        models = jb.dbt_models()
        out = []
        for refactor in jb.refactors(models):
            if refactor.label != "equivalent":
                continue
            _, rename = jb.world(models, refactor)
            for output in models:
                if output in rename:
                    out.append({"pair": f"{refactor.id}::{output}", "refactor": refactor.id, "output": output, "held_out": refactor.held_out})
        return out

    def case(self, item: dict) -> Case | None:
        import jaffle_shop_bench as jb
        from kumosql.pipeline_equivalence import prove_models

        models = jb.dbt_models()
        refactor = next(r for r in jb.refactors(models) if r.id == item["refactor"])
        files, rename = jb.world(models, refactor)
        combined = jb.load(files)
        schema = jb.prover_schema(combined)
        # ``run_refactor``: the case is proved when every changed output is
        for output in models:
            if output in rename and not prove_models(combined, jb.key(output), jb.key(rename[output]), declared=[], schema=schema, timeout_ms=5000).proven:
                return None
        left = self.compose(jb, combined, jb.key(item["output"]))
        right = self.compose(jb, combined, jb.key(rename[item["output"]]))
        tables = {
            table: Table(table, [Column(c, {"INT64": "int", "STRING": "text", "DATE": "date"}[t], sql_type=jb.DUCK_TYPES[t]) for c, t in columns.items()])
            for table, columns in jb.SEEDS.items()
        }
        return Case(self.name, item["pair"], left, right, tables, setup=("CREATE MACRO farm_fp(x) AS CAST(hash(x) AS HUGEINT)",),
                    held_out=item["held_out"], dialect="bigquery",
                    source=(combined.models[jb.key(item["output"])].sql, combined.models[jb.key(rename[item["output"]])].sql),
                    meta={"label": refactor.label, "note": refactor.note})

    @staticmethod
    def compose(jb, pipeline, target: str) -> str:
        """``target`` with every model it reads a CTE, each translated as ``jaffle_shop_bench.duckdb_sql`` translates a model
        (the eval builds each as a table, which reads the same rows); seeds are the seed tables."""

        needed, todo = set(), [target]
        while todo:
            name = todo.pop()
            if name in needed or name not in pipeline.models:
                continue
            needed.add(name)
            todo.extend(pipeline.upstream.get(name, ()))
        ctes = []
        for name in pipeline.topological_order():
            if name not in needed:
                continue
            tree = sqlglot.parse_one(pipeline.models[name].sql, read="bigquery")
            local = {c.alias_or_name.lower() for c in tree.find_all(sqlglot.exp.CTE)}
            for table in list(tree.find_all(sqlglot.exp.Table)):
                if not table.args.get("db") and table.name.lower() in local:
                    continue
                resolved = pipeline.resolve(table)
                if resolved is None:
                    continue
                short = resolved.split(".")[-1]
                table.set("catalog", None)
                table.set("db", None)
                table.set("this", sqlglot.exp.to_identifier(f"an__{short}" if resolved in pipeline.models else short))
            ctes.append(f'"an__{name.split(".")[-1]}" AS ({tree.sql(dialect="duckdb")})')
        return f'WITH {", ".join(ctes)} SELECT * FROM "an__{target.split(".")[-1]}"'


# --- Cosette (adapted) ----------------------------------------------------------------------------------


class CosetteAdapted(Adapter):
    """The adapted Cosette pairs: ``cosette_bench.run`` proves with ``prove_result`` (no constants) and replays on DuckDB."""

    name = "cosette-adapted"

    def items(self) -> list[dict]:
        import cosette_bench

        return [{"pair": r["name"], "left": r["sql_a"], "right": r["sql_b"], "ddl": r["ddl"], "label": r.get("label", "equivalent"),
                 "held_out": cosette_bench.held_out(r["name"])} for r in cosette_bench.load("cosette-adapted")]

    def case(self, item: dict) -> Case | None:
        import cosette_bench
        import sqlsolver_bench as sb
        from recheck.calcite_family import duckdb_pair, engine_tables

        tables = cosette_bench._tables(item["ddl"])
        left, right = cosette_bench.repaired(item["left"], item["right"], tables)
        try:
            result = sb.prove_result(left, right, tables)
        except Exception:  # a crash is a failure to prove, never a proof
            return None
        if not (result if isinstance(result, bool) else result.proven):
            return None
        left_sql, right_sql = duckdb_pair(left, right, False)
        return Case(self.name, item["pair"], left_sql, right_sql, engine_tables(tables), held_out=item["held_out"], source=(left, right),
                    dialect="mysql", meta={"label": item["label"], "uninterpreted": "__" in item["ddl"]})


# --- Arcwise corrections --------------------------------------------------------------------------------


class Arcwise(Adapter):
    """Every pair ``arcwise_bench.decide`` could prove: the repair plays Spider's gold query, so the proof is
    ``llm_sql_solver_bench``'s (adapt, prove, the mixed-type rule) and the Case runs both adapted queries in SQLite
    over the repair's declared types, as ``recheck.dialect_rewrites.LlmSqlSolver`` does."""

    name = "arcwise-corrections"

    def items(self) -> list[dict]:
        import arcwise_bench as ab

        cases, _ = ab.load_cases()
        return [{"pair": c.id, "held_out": c.held_out} for c in cases]

    def case(self, item: dict) -> Case | None:
        import arcwise_bench as ab
        import llm_sql_solver_bench as L
        from recheck.dialect_rewrites import _quiet, install

        _quiet()
        install()
        arcwise = next(c for c in ab.load_cases()[0] if c.id == item["pair"])
        case = L.Case(arcwise.suite, int(arcwise.question_id), arcwise.database, arcwise.corrected, arcwise.original, "inequivalent",
                      arcwise.tables, arcwise.keys, arcwise.foreign)
        sql1, sql2 = L.adapt(case.sql1, case.tables), L.adapt(case.sql2, case.tables)

        def prove():
            if not L.ordered(sql1):
                return L.prove(sql1, sql2, case.tables) == "proven"
            return L.ordered(sql2) and L.prove(L.for_prover(sql1), L.for_prover(sql2), case.tables) == "proven"

        try:
            proved = run_capped(ab.PAIR_TIMEOUT, prove)
        except _Deadline:
            return None
        if not proved or L.mixed_type_comparison(sql1, case.tables) or L.mixed_type_comparison(sql2, case.tables):
            return None
        tables = {
            t: Table(t, [Column(c, "int" if k == "INTEGER" else "text", sql_type="BIGINT" if k == "INTEGER" else "VARCHAR") for c, k in cols.items()])
            for t, cols in case.tables.items()
        }
        return Case(self.name, item["pair"], sql1, sql2, tables, held_out=item["held_out"], source=(sql1, sql2), dialect="sqlite",
                    mode="list" if L.ordered(sql1) else "bag",
                    meta={"engines": ("sqlite", "sqlite"), "sqlite_tables": {t: list(cols.items()) for t, cols in case.tables.items()},
                          "listed_keys": {t: list(k) for t, k in case.keys.items()}, "foreign_keys": list(case.foreign)})


# --- Analytical SQL coverage ----------------------------------------------------------------------------

CORPORA = ("sqlstorm/stackoverflow", "sqlstorm/tpch", "sqlstorm/tpcds", "sqlstorm/job", "sqlstorm-v0/tpch", "sqlstorm-v0/tpcds",
           "sqlstorm-v0/job", "dsb")
SAMPLE = 60  # ``analytical_coverage.py --split all --limit 60``, the run the results page records
_BQ_KINDS = {"INT64": ("int", "BIGINT"), "FLOAT64": ("float", "DOUBLE"), "NUMERIC": ("decimal", "DECIMAL(38,9)"), "STRING": ("text", "VARCHAR"),
             "BOOL": ("bool", "BOOLEAN"), "DATE": ("date", "DATE"), "TIMESTAMP": ("timestamp", "TIMESTAMP")}


class AnalyticalCoverage(Adapter):
    """One item per sampled query and rewrite (``cleanup`` is the six non-formatting rules in sequence, ``format`` is
    sqlfluff's formatter). A pair is proven when the rewrite changed the query and its verification is ``PROVEN``
    (what ``_execution`` calls a trusted rewrite); the Case runs both through the eval's own BigQuery-on-DuckDB
    translation (``DatasetRunner.prepare``) over the corpus's tables, with no keys or NOT NULL columns."""

    name = "analytical-sql-coverage"

    def items(self) -> list[dict]:
        import analytical_coverage as ac

        out = []
        for corpus in CORPORA:
            for qid, _ in ac.sample(corpus, SAMPLE, "all"):
                for kind in ("cleanup", "format"):
                    out.append({"pair": f"{qid}::{kind}", "query": qid, "corpus": corpus, "kind": kind})
        return out

    def case(self, item: dict) -> Case | None:
        import analytical_coverage as ac
        import benchmark_corpora as corpora
        from kumosql import rewrite
        from kumosql.result_equivalence import DatasetRunner, _local_name

        text = dict(ac.corpora_mod.queries(item["corpus"]))[item["query"]]
        try:
            sql = corpora.to_bigquery(text)
        except Exception:  # noqa: BLE001 - the eval leaves a query sqlglot cannot convert out of its score
            return None
        schema = corpora.schema(item["corpus"])
        if schema is None:
            return None
        try:
            if item["kind"] == "cleanup":
                result = rewrite.apply_rules(ac.cov.CLEANUP_RULES, sql)
            else:
                result = rewrite.apply_rule("format_sql", sql)
        except Exception:  # noqa: BLE001 - a crash is a failure to prove, never a proof
            return None
        if result.sql == sql or result.verification.status is not rewrite.VerificationStatus.PROVEN:
            return None
        runner = DatasetRunner(schema, "bigquery")
        try:
            left, right = runner.prepare(sql), runner.prepare(result.sql)
        finally:
            runner.close()
        tables = {
            _local_name(t): Table(_local_name(t), [Column(c, _BQ_KINDS[k.upper()][0], sql_type=_BQ_KINDS[k.upper()][1]) for c, k in cols.items()])
            for t, cols in schema.items()
        }
        return Case(self.name, item["pair"], left, right, tables, setup=bigquery_setup(), source=(sql, result.sql), dialect="bigquery",
                    meta={"corpus": item["corpus"], "rewrite": item["kind"]})


def bigquery_setup() -> tuple[str, ...]:
    from kumosql import bigquery_on_duckdb as bq

    return tuple(bq.SETTINGS) + tuple(bq.MACROS)


ADAPTERS = {
    a.name: a
    for a in [
        AnalyticalCoverage(), Arcwise(), Quite("quite-rewrites", "equal"), Quite("quite-negatives", "unequal"), Logos(), QueryBooster(),
        DbgptStyle("dbgpt-rules", "dbgpt_rules"), DbgptStyle("documented-rewrites", "documented_rewrites"),
        OptimizerBugs(), JaffleRefactors(), CosetteAdapted(),
    ]
}
