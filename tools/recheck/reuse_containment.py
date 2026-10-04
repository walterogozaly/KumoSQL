"""Proven rewrites and containments of the model-reuse evals (``docs/model-reuse.md``).

* ``mv-reuse-calcite`` (``tools/mv_reuse_bench.py --all``): a query rewritten over a materialized view (Calcite
  1.37.0 cases and the adapted shared-model cases), proven equal to the query by ``model_reuse.rewrite_over_model``.
  Every case the eval counts as rewritten is a pair, disabled cases included (the eval verifies them too).
* ``containment`` (``tools/containment_bench.py --all``): ``q1`` proven contained in ``q2`` by
  ``containment.check_containment``, checked as a sub-bag (bag semantics) or a subset (set semantics), the two
  modes the eval's own replay and labelling use.
* ``aggregate-decomposition`` (``tools/decomposition_bench.py --all``): a target rebuilt from a summary table
  (``<id>#synth``), and any trap replacement ``check_replacement`` would prove (``<id>#trap<n>``; the eval says
  there are none).
* ``mv-benchmark`` (``tools/mv_workload_bench.py --all --sample 100``): workload queries (JOB, SCALE, STATS, TPC-DS)
  rewritten over views mined from the development queries (track ``mined``), and JOB over the benchmark's own ten
  views (track ``given``), exactly as ``try_rewrite`` picks them (largest applicable view first, first proof wins).
  The eval's ``poisoned`` control and its baseline are not pairs. The workload files are downloaded into
  ``KUMOSQL_BENCH_DATA`` (default ``~/.cache/kumosql-bench``) as ``mv_workload_bench`` does.

Each Case runs the DuckDB SQL of the eval's own executed check: ``random_check.find_difference`` (or ``replay``)
on ``reuse.query_sql or <the query>`` against ``reuse.inlined_sql``, the replacement with ``model_reuse``'s own
restatement of the view substituted, both through ``random_check._duck`` (Postgres reading; ``FLOOR(x TO unit)``
as ``DATE_TRUNC``, reserved words quoted). The aggregate-decomposition eval compares the raw target query, not
``query_sql``, and runs a trap through ``_inline_trap``; the Cases do the same. The view as written (``WITH
mv0(<column names>) AS (<the view's SQL>) <replacement>``) is kept in ``meta["as_written"]`` for triage: a pair
that differs on the inlined form but not on that one is a flaw in ``model_reuse``'s restatement, and a pair that
differs only on the as-written form is a gap between the restatement and the view.

Table schemas are the evals' own (a key's columns are NOT NULL, as ``random_check.Schema.not_null`` and the prover's
constraints have them).
"""

from __future__ import annotations

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


def reuse_case(eval_name: str, item: dict, reuse, model_sql: str, schema, *, left: str, held_out: bool, meta: dict | None = None) -> Case:
    """The Case for a proven ``rewrite_over_model`` answer: ``left`` against the replacement with the view substituted,
    both as the eval's ``random_check.find_difference`` reads them."""

    try:
        as_written = with_model(reuse.sql, model_sql, list(reuse.model_columns), schema.columns)
    except Exception:  # noqa: BLE001 - only a triage aid
        as_written = None
    if reuse.inlined_sql:
        right = duck(reuse.inlined_sql)
    elif as_written is not None:  # mv_workload_bench falls back to the view itself; never reached for a rewrite
        right = duck(as_written)
    else:
        raise ValueError("a rewrite with neither an inlined form nor a readable view")
    extra = {
        "strategy": reuse.strategy,
        "replacement": reuse.sql,
        "model": model_sql,
        "as_written": duck(as_written) if as_written else None,
        "assumptions": list(reuse.assumptions),
    }
    extra.update(meta or {})
    return Case(
        eval_name, item["pair"], duck(left), right, rc_tables(schema),
        held_out=held_out, source=(reuse.query_sql or left, reuse.inlined_sql or ""), dialect="postgres", meta=extra,
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
        return reuse_case(self.name, item, reuse, item["model"], schema, left=reuse.query_sql or item["query"], held_out=item["held_out"],
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
        import decomposition_bench as db
        from kumosql import model_reuse as mr
        from kumosql.random_check import prover_constraints

        sales = db.SALES
        meta = {"kind": item["kind"], "expect": item["expect"], "family": item["family"]}
        if item["kind"] == "synth":
            try:
                reuse = mr.rewrite_over_model(item["query"], item["summary"], schema=sales.columns, constraints=prover_constraints(sales), timeout_ms=self.timeout_ms)
            except Exception:  # noqa: BLE001
                return None
            if not reuse.rewritten:
                return None
            # the eval re-runs the raw target query, not ``reuse.query_sql``
            return reuse_case(self.name, item, reuse, item["summary"], sales, left=item["query"], held_out=item["held_out"], meta=meta)
        # a trap the prover proves (the eval says none is): run it as the eval's replay does, the raw target
        # query against the replacement with the summary's restatement substituted
        try:
            check = mr.check_replacement(item["query"], item["summary"], item["sql"], schema=sales.columns, constraints=prover_constraints(sales), database=sales, timeout_ms=self.timeout_ms, trials=150)
        except Exception:  # noqa: BLE001
            return None
        if check.status != "proven":
            return None
        inlined = db._inline_trap({"query": item["query"], "summary": item["summary"]}, item["sql"])
        if inlined is None:
            raise ValueError("the proven trap cannot be inlined")
        return Case(self.name, item["pair"], duck(item["query"]), duck(inlined), rc_tables(sales), held_out=item["held_out"],
                    source=(item["query"], item["sql"]), dialect="postgres", meta={**meta, "model": item["summary"], "replacement": item["sql"]})


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
                return reuse_case(self.name, item, reuse, view["sql"], schema, left=reuse.query_sql or item["sql"], held_out=item["held_out"],
                                  meta={"workload": item["workload"], "track": item["track"], "view": view["name"]})
        return None


ADAPTERS = {a.name: a for a in [MvReuse(), Containment(), Decomposition(), MvBenchmark()]}
