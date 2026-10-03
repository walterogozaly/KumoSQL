"""Proven rewrites and containments of the model-reuse evals (``docs/model-reuse.md``) and JOB's alternative forms.

* ``mv-reuse-calcite`` (``tools/mv_reuse_bench.py --all``): a query rewritten over a materialized view (Calcite
  1.37.0 cases and the adapted shared-model cases), proven equal to the query by ``model_reuse.rewrite_over_model``.
* ``containment`` (``tools/containment_bench.py --all``): ``q1`` proven contained in ``q2`` by
  ``containment.check_containment``, checked as a sub-bag (bag semantics) or a subset (set semantics).
* ``containment-steps``: every equivalence the prover proved while ``check_containment`` answered those questions
  (``equal``, ``pre-filter``, ``distinct-collapse``, ``union``, the branch steps of set operations, and the
  ``post-filter`` rewrite over ``q2``), checked as a bag equality; a wrong step is a false proof even when the
  containment it supports happens to hold.
* ``aggregate-decomposition`` (``tools/decomposition_bench.py --all``): a target rebuilt from a summary table, and
  any trap replacement the prover would prove.
* ``mv-benchmark`` (``tools/mv_workload_bench.py --all --sample 100``): workload queries (JOB, SCALE, STATS, TPC-DS)
  rewritten over views mined from the development queries (track ``mined``), and JOB over the benchmark's own ten
  views (track ``given``), exactly as ``try_rewrite`` picks them (largest applicable view first, first proof wins).
* ``job-alternative-forms`` (``tools/transformation_bench.py job``): five valid rewrites of each JOB query proven equal
  to the original by ``transformation_bench._prove`` (BigQuery dialect).

A rewrite over a view reads the view **as written**: the rewritten side runs as ``WITH mv0(<column names>) AS
(<the view's own SQL>) <replacement>``, not as the eval's ``inlined_sql``, which substitutes ``model_reuse``'s own
restatement of the view (``_named_model_sql``). The check then covers that restatement as well as the proof, and the
search sees every table and column the view reads. The eval's inlined form is kept in ``meta["inlined"]``.

Postgres-dialect pairs run on DuckDB through ``kumosql.random_check._duck`` (the translation the evals' own random
check uses: ``FLOOR(x TO unit)`` as ``DATE_TRUNC``, reserved words quoted); BigQuery pairs through the same function
with ``kumosql.bigquery_on_duckdb``'s settings and macros. Table schemas are the evals' own (a key's columns are NOT
NULL, as ``random_check.Schema.not_null`` and the prover's constraints have them).

``containment-steps`` lists its items by running every containment question once; set ``RECHECK_CACHE`` to a folder to
keep that list between runs.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sys

TOOLS = Path(__file__).resolve().parent.parent
ROOT = TOOLS.parent
for _path in (str(TOOLS), str(ROOT / "src")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import sqlglot  # noqa: E402
from sqlglot import exp  # noqa: E402

from recheck.engine import Case, Column, Table  # noqa: E402

_WITH = "with_" if "with_" in exp.Select.arg_types else "with"
_RC_KIND = {"int": ("int", "BIGINT"), "float": ("float", "DOUBLE"), "text": ("text", "VARCHAR"), "date": ("date", "DATE"), "bool": ("bool", "BOOLEAN")}
_BQ_KIND = {
    "INT64": ("int", "BIGINT"),
    "STRING": ("text", "VARCHAR"),
    "FLOAT64": ("float", "DOUBLE"),
    "NUMERIC": ("decimal", "DECIMAL(38,9)"),
    "BOOL": ("bool", "BOOLEAN"),
    "DATE": ("date", "DATE"),
    "TIMESTAMP": ("timestamp", "TIMESTAMP"),
}


# --- shared helpers ------------------------------------------------------------------------------


def rc_tables(schema) -> dict[str, Table]:
    """A ``kumosql.random_check.Schema`` (what these evals declare to the prover and their random check) as engine tables."""

    out = {}
    for table in schema.tables:
        not_null = schema.not_null(table)
        columns = [Column(c.name, _RC_KIND[c.type][0], not_null=c.name in not_null, sql_type=_RC_KIND[c.type][1]) for c in table.columns]
        out[table.name] = Table(table.name, columns, [tuple(k) for k in table.keys])
    return out


def duck(sql: str, dialect: str = "postgres") -> str:
    """DuckDB SQL for ``sql`` exactly as ``kumosql.random_check`` (the evals' random check) runs it."""

    from kumosql.random_check import _duck

    return _duck(sql, dialect)


def with_model(replacement_sql: str, model_sql: str, names, schema_columns, *, dialect: str = "postgres", model_name: str = "mv0") -> str:
    """``replacement_sql`` (Postgres, reading ``model_name``) with the model's own SQL as a leading
    ``WITH model_name(names) AS (...)``; the model is taken as written (names lower-cased, schema prefixes dropped,
    as ``model_reuse._plain`` reads it), and its outputs are named by position."""

    from kumosql import model_reuse as mr

    model = mr._plain(model_sql, schema_columns, dialect)
    tree = sqlglot.parse_one(replacement_sql, read="postgres")
    cte = exp.CTE(this=model, alias=exp.TableAlias(this=exp.to_identifier(model_name), columns=[exp.to_identifier(n) for n in names]))
    existing = tree.args.get(_WITH)
    if existing is not None:
        existing.set("expressions", [cte, *existing.expressions])
    else:
        tree.set(_WITH, exp.With(expressions=[cte]))
    return tree.sql(dialect="postgres")


def reuse_case(eval_name: str, item: dict, reuse, model_sql: str, schema, *, held_out: bool, meta: dict | None = None) -> Case:
    """The Case for a proven ``rewrite_over_model`` answer: the query as the prover read it, against the replacement over the view."""

    right = with_model(reuse.sql, model_sql, list(reuse.model_columns), schema.columns)
    extra = {
        "strategy": reuse.strategy,
        "replacement": reuse.sql,
        "model": model_sql,
        "inlined": duck(reuse.inlined_sql) if reuse.inlined_sql else None,
        "assumptions": list(reuse.assumptions),
    }
    extra.update(meta or {})
    return Case(
        eval_name, item["pair"], duck(reuse.query_sql), duck(right), rc_tables(schema),
        held_out=held_out, source=(reuse.query_sql, reuse.inlined_sql or ""), dialect="postgres", meta=extra,
    )


class Adapter:
    """``items()`` lists every pair (picklable); ``case(item)`` proves it as the eval does and returns a Case, or None."""

    name = ""

    def items(self) -> list[dict]:
        raise NotImplementedError

    def case(self, item: dict) -> Case | None:
        raise NotImplementedError


# --- mv-reuse-calcite ----------------------------------------------------------------------------


class MvReuse(Adapter):
    name = "mv-reuse-calcite"
    timeout_ms = 5000  # mv_reuse_bench's default --timeout-ms

    def items(self) -> list[dict]:
        import mv_reuse_bench as mb

        return [
            {
                "pair": c["id"], "query": c["query"], "model": c["materialization"], "schema": c["schema"], "source": c["source"],
                "expect": c["expect"], "disabled": bool(c.get("disabled")), "held_out": mb.is_held_out(c["id"]),
            }
            for c in mb.load_cases(None)
        ]

    def case(self, item: dict) -> Case | None:
        import mv_reuse_bench as mb
        from kumosql.model_reuse import rewrite_over_model

        schema = mb.SCHEMAS[item["schema"]]
        try:
            reuse = rewrite_over_model(item["query"], item["model"], schema=schema.columns, constraints=mb.constraints_of(schema), timeout_ms=self.timeout_ms)
        except Exception:  # noqa: BLE001 - the eval scores a crash as an error, never a rewrite
            return None
        if not reuse.rewritten:
            return None
        return reuse_case(self.name, item, reuse, item["model"], schema, held_out=item["held_out"],
                          meta={"source": item["source"], "expect": item["expect"], "disabled": item["disabled"]})


# --- containment ---------------------------------------------------------------------------------


def _shop():
    import containment_bench as cb

    return cb.SHOP


def containment_answer(q1: str, q2: str, semantics: str, *, steps: list | None = None):
    """``check_containment`` as ``containment_bench.run`` calls it. ``database`` is left out: it only feeds the
    counterexample search, which runs after every proof attempt, so a ``contained`` answer is the same.

    With ``steps``, every equivalence the prover proves on the way (and every ``post-filter`` rewrite) is appended.
    """

    import kumosql.containment as cm
    from kumosql.random_check import prover_constraints
    from kumosql.smt_equivalence import SmtStatus

    shop = _shop()
    if steps is None:
        return cm.check_containment(q1, q2, schema=shop.columns, constraints=prover_constraints(shop), semantics=semantics, database=None, timeout_ms=5000)
    original_prove, original_post = cm._prove, cm._post_filter

    def prove(a, b, **options):
        result = original_prove(a, b, **options)
        if result is not None and result.status is SmtStatus.PROVEN_EQUIVALENT:
            steps.append({"kind": "equivalence", "a": a, "b": b, "reason": str(result.reason)[:300]})
        return result

    def post(q1_sql, q2_sql, *args):
        reuse = original_post(q1_sql, q2_sql, *args)
        if reuse is not None:
            steps.append({"kind": "post-filter", "a": q1_sql, "b": q2_sql, "replacement": reuse.sql})
        return reuse

    cm._prove, cm._post_filter = prove, post
    try:
        return cm.check_containment(q1, q2, schema=shop.columns, constraints=prover_constraints(shop), semantics=semantics, database=None, timeout_ms=5000)
    finally:
        cm._prove, cm._post_filter = original_prove, original_post


class Containment(Adapter):
    name = "containment"

    def items(self) -> list[dict]:
        import containment_bench as cb

        cases, held = cb.load()
        return [
            {"pair": f"{c['id']}|{s}", "id": c["id"], "q1": c["q1"], "q2": c["q2"], "semantics": s, "label": c[s], "family": c["family"], "held_out": c["family"] in held}
            for c in cases
            for s in ("set", "bag")
        ]

    def case(self, item: dict) -> Case | None:
        try:
            answer = containment_answer(item["q1"], item["q2"], item["semantics"])
        except Exception:  # noqa: BLE001 - scored as an error by the eval
            return None
        if answer.status != "contained":
            return None
        return Case(
            self.name, item["pair"], duck(item["q1"]), duck(item["q2"]), rc_tables(_shop()),
            held_out=item["held_out"], source=(item["q1"], item["q2"]), dialect="postgres",
            mode="contained" if item["semantics"] == "bag" else "contained-set",
            meta={"method": answer.method, "label": item["label"], "family": item["family"], "assumptions": list(answer.assumptions)},
        )


def _cache_path(name: str, *inputs: Path) -> Path | None:
    folder = os.environ.get("RECHECK_CACHE")
    if not folder:
        return None
    digest = hashlib.sha256()
    for path in inputs:
        digest.update(path.read_bytes())
    for module in ("containment.py", "model_reuse.py", "algebraic_equivalence.py", "smt_equivalence.py"):
        digest.update((ROOT / "src" / "kumosql" / module).read_bytes())
    return Path(folder) / f"{name}-{digest.hexdigest()[:16]}.json"


class ContainmentSteps(Adapter):
    name = "containment-steps"

    def items(self) -> list[dict]:
        import containment_bench as cb

        cache = _cache_path(self.name, cb.FIXTURE)
        if cache is not None and cache.exists():
            return json.loads(cache.read_text(encoding="utf-8"))
        out = []
        for item in Containment().items():
            steps: list = []
            try:
                answer = containment_answer(item["q1"], item["q2"], item["semantics"], steps=steps)
                status = answer.status
            except Exception:  # noqa: BLE001
                status = "error"
            seen = set()
            for step in steps:
                key = (step["kind"], step["a"], step["b"])
                if key in seen:
                    continue
                seen.add(key)
                out.append({**step, "pair": f"{item['pair']}|{len(seen) - 1}", "answer": status, "held_out": item["held_out"], "family": item["family"], "semantics": item["semantics"], "label": item["label"]})
        if cache is not None:
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps(out), encoding="utf-8")
        return out

    def case(self, item: dict) -> Case | None:
        import kumosql.containment as cm
        from kumosql import model_reuse as mr
        from kumosql.random_check import prover_constraints
        from kumosql.smt_equivalence import SmtStatus

        shop = _shop()
        base = {t.lower(): [c.lower() for c in cols] for t, cols in shop.columns.items()}
        meta = {"answer": item["answer"], "semantics": item["semantics"], "label": item["label"], "kind": item["kind"]}
        if item["kind"] == "equivalence":
            result = cm._prove(item["a"], item["b"], schema=base, constraints=prover_constraints(shop), types=None, timeout_ms=5000, dialect="postgres")
            if result is None or result.status is not SmtStatus.PROVEN_EQUIVALENT:
                return None
            return Case(self.name, item["pair"], duck(item["a"]), duck(item["b"]), rc_tables(shop), held_out=item["held_out"],
                        source=(item["a"], item["b"]), dialect="postgres", meta={**meta, "reason": str(result.reason)[:300]})
        reuse = mr.rewrite_over_model(item["a"], item["b"], schema=base, constraints=prover_constraints(shop), model_name="mv0", dialect="postgres", timeout_ms=5000, identity=True)
        if not reuse.rewritten:
            return None
        return reuse_case(self.name, item, reuse, item["b"], shop, held_out=item["held_out"], meta=meta)


# --- aggregate-decomposition ---------------------------------------------------------------------


class Decomposition(Adapter):
    name = "aggregate-decomposition"
    timeout_ms = 5000

    def items(self) -> list[dict]:
        import decomposition_bench as db

        cases, held = db.load()
        out = []
        for c in cases:
            base = {"query": c["query"], "summary": c["summary"], "family": c["family"], "expect": c["expect"], "held_out": c["family"] in held}
            out.append({**base, "pair": f"{c['id']}#synth", "kind": "synth"})
            for index, trap in enumerate(c["traps"]):
                out.append({**base, "pair": f"{c['id']}#trap{index}", "kind": "trap", "sql": trap["sql"]})
        return out

    def case(self, item: dict) -> Case | None:
        from kumosql import model_reuse as mr
        from kumosql.random_check import prover_constraints
        from make_decomposition_cases import SALES

        meta = {"kind": item["kind"], "expect": item["expect"], "family": item["family"]}
        if item["kind"] == "synth":
            try:
                reuse = mr.rewrite_over_model(item["query"], item["summary"], schema=SALES.columns, constraints=prover_constraints(SALES), timeout_ms=self.timeout_ms)
            except Exception:  # noqa: BLE001
                return None
            if not reuse.rewritten:
                return None
            return reuse_case(self.name, item, reuse, item["summary"], SALES, held_out=item["held_out"], meta=meta)
        # a trap the prover proves (the eval says none is): check it the same way
        check = mr.check_replacement(item["query"], item["summary"], item["sql"], schema=SALES.columns, constraints=prover_constraints(SALES), timeout_ms=self.timeout_ms)
        if check.status != "proven":
            return None
        tree = mr._prepare(item["summary"], SALES.columns, "postgres")
        names = mr._output_names(mr._block(tree, mr.not_null_columns(prover_constraints(SALES))))
        plain = mr._plain(item["query"], SALES.columns, "postgres").sql(dialect="postgres")
        right = with_model(item["sql"], item["summary"], names, SALES.columns)
        return Case(self.name, item["pair"], duck(plain), duck(right), rc_tables(SALES), held_out=item["held_out"],
                    source=(plain, item["sql"]), dialect="postgres", meta={**meta, "model": item["summary"]})


# --- mv-benchmark --------------------------------------------------------------------------------

_SCHEMAS: dict = {}


def _workload_schema(workload: str):
    import mv_workload_bench as mw

    if workload not in _SCHEMAS:
        _SCHEMAS[workload] = mw.parse_schema(mw.fetch(mw.WORKLOADS[workload][2], None))
    return _SCHEMAS[workload]


class MvBenchmark(Adapter):
    name = "mv-benchmark"

    def items(self) -> list[dict]:
        import mv_workload_bench as mw
        from kumosql import view_candidates as vc

        out = []
        for workload in mw.WORKLOADS:
            sample = None if workload == "job" else 100
            queries, schema = mw.load_workload(workload, None, sample)
            dev = {q: r["sql"] for q, r in queries.items() if not r["held_out"]}
            chosen = vc.select(vc.mine(dev, schema.columns), mw.BUDGET)
            plans = {"mined": [mw.view_record(c, f"view{i}") for i, c in enumerate(chosen)]}
            if workload == "job":
                plans["given"] = [g for g in (mw.given_record(n, s, schema) for n, s in mw.load_given(None).items()) if g]
            for track, views in plans.items():
                for ident, record in queries.items():
                    graph = vc.graph_of(record["sql"], schema.columns)
                    usable = [v for v in views if mw.applicable(graph, v["tables"])]
                    usable.sort(key=lambda v: (-len(v["tables"]), v["name"]))
                    pair = f"{track}/{ident}" if workload == "job" else f"{track}/{workload}/{ident}"
                    out.append({"pair": pair, "workload": workload, "track": track, "sql": record["sql"], "views": usable, "held_out": record["held_out"]})
        return out

    def case(self, item: dict) -> Case | None:
        import mv_workload_bench as mw
        from kumosql import model_reuse as mr

        schema = _workload_schema(item["workload"])
        constraints = mw.constraints_of(schema)
        for view in item["views"]:  # try_rewrite's order: largest view first, first proven rewrite wins
            try:
                reuse = mr.rewrite_over_model(item["sql"], view["sql"], schema=schema.columns, constraints=constraints, timeout_ms=mw.TIMEOUT_MS)
            except Exception:  # noqa: BLE001
                continue
            if reuse.rewritten:
                return reuse_case(self.name, item, reuse, view["sql"], schema, held_out=item["held_out"],
                                  meta={"workload": item["workload"], "track": item["track"], "view": view["name"]})
        return None


# --- job-alternative-forms -----------------------------------------------------------------------


def job_tables() -> dict[str, Table]:
    import benchmark_corpora as corpora

    schema = corpora.schema("sqlstorm/job")
    return {
        name: Table(name, [Column(c, _BQ_KIND[t][0], sql_type=_BQ_KIND[t][1]) for c, t in columns.items()])
        for name, columns in schema.items()
    }


def bigquery_setup() -> tuple[str, ...]:
    from kumosql.bigquery_on_duckdb import MACROS, SETTINGS

    return tuple(SETTINGS) + tuple(MACROS)


class JobForms(Adapter):
    name = "job-alternative-forms"

    def items(self) -> list[dict]:
        import benchmark_corpora as corpora
        import transformation_bench as tb

        schema = corpora.schema("sqlstorm/job")
        out = []
        for query_id, sql in tb.workload_queries("job"):
            for form_name, build in tb.FORMS.items():
                try:
                    form = build(sql, schema)
                except Exception:  # noqa: BLE001 - a generator error is not a form
                    continue
                if form is None:
                    continue
                out.append({"pair": f"{query_id}/{form_name}", "sql": sql, "form": form, "held_out": corpora.held_out(query_id)})
        return out

    def case(self, item: dict) -> Case | None:
        import benchmark_corpora as corpora
        import transformation_bench as tb

        try:
            status, _ = tb._prove(item["sql"], item["form"], corpora.schema("sqlstorm/job"))
        except Exception:  # noqa: BLE001
            return None
        if status != "proven_equivalent":
            return None
        return Case(self.name, item["pair"], duck(item["sql"], "bigquery"), duck(item["form"], "bigquery"), job_tables(),
                    setup=bigquery_setup(), held_out=item["held_out"], source=(item["sql"], item["form"]), dialect="bigquery")


ADAPTERS = {a.name: a for a in [MvReuse(), Containment(), ContainmentSteps(), Decomposition(), MvBenchmark(), JobForms()]}
