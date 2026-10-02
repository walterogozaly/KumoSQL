"""Apply KumoSQL's transformations to TPC-H, TPC-DS and the Join Order Benchmark and measure them.

Four things are reported separately, per workload:

* **correctness**: transformed queries that return different rows from the original (on the
  benchmark's real data where it is available here, and always on generated tables), counted
  separately for rewrites KumoSQL marked proven. A proven rewrite that changes results is wrong.
* **coverage**: how many transformations changed the query, and how many of those were proven.
* **usefulness**: proven rewrites that DuckDB plans differently and that run measurably faster
  on real data (at least 5%). Requiring a changed plan keeps timer noise out of the count.
* **performance**: transformation time per query, prover time, and DuckDB runtime of the
  original and the transformed query on real data.

The transformations are every registered rewrite rule on its own and the whole cleanup
pipeline in its canonical order. For the Join Order Benchmark (JOB) the tool also writes
*alternative forms* of each query (explicit ``JOIN ... ON`` instead of comma joins, the
FROM list reversed, columns left unqualified where the schema makes them unambiguous,
single-table filters moved into CTEs, aliases renamed) and checks that:

* the prover proves each alternative equal to the original (with timing by join count),
* each alternative returns the same rows on generated tables (a generator self-check), and
* column lineage (dependency analysis) gives every output column the same sources in every
  form, and the sources sqlglot's own lineage finds.

It also writes two *broken* forms per query as negative controls, which the prover must never
prove: one join predicate dropped (only one the remaining equalities do not imply) and one
single-table filter negated.

Workloads come from ``tools/benchmark_corpora.py``: the official TPC-H and TPC-DS queries
(SQLStorm v0.0, one instance of each template) and the 113 JOB queries. Real data, when
fetched (``python tools/benchmark_corpora.py fetch tpch-data tpcds-data job-data``), is TPC-H at
scale 0.1 (``tpchgen-cli``), TPC-DS at scale 1 (``dsdgen``) and JOB's IMDB snapshot (from
jo-bench) in DuckDB files. On JOB, every alternative and broken form also runs on IMDB.

    python tools/transformation_bench.py tpch tpcds job
    python tools/transformation_bench.py job --json out.json
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import statistics
import sys
import threading
import time
from collections import Counter, defaultdict
from multiprocessing import Pool
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))

import sqlglot  # noqa: E402
from sqlglot import exp  # noqa: E402

import benchmark_corpora as corpora  # noqa: E402
from kumosql import engine, rewrite  # noqa: E402
from kumosql.result_equivalence import ResultEquivalenceStatus, check_result_equivalence  # noqa: E402
from kumosql.smt_equivalence import SmtStatus, prove_equivalent_smt  # noqa: E402
from kumosql.equivalence import prove_equivalent  # noqa: E402

WORKLOADS = ("tpch", "tpcds", "job")
SEEDS = (1, 2, 3)
SPEEDUP = 0.95  # a proven rewrite is useful when DuckDB plans it differently and it runs in at most 95% of the original's time
RUNS = 3
QUERY_TIMEOUT_S = 60


# ---------------------------------------------------------------- workloads


def workload_queries(name: str) -> list[tuple[str, str]]:
    """``(id, BigQuery SQL)`` for each query of a workload; unconvertible queries are skipped."""

    if name == "job":
        root = corpora.BENCH_DIR / "join-order-benchmark"
        items = [(f"job/{p.stem}", p.read_text()) for p in sorted(root.glob("[0-9]*.sql"), key=corpora._natural)]
    else:
        dataset = "tpch" if name == "tpch" else "tpcds"
        count = 22 if name == "tpch" else 103  # TPC-DS stream 0 lists its 99 templates in 103 files
        root = corpora.BENCH_DIR / "SQLStorm" / "v0.0" / dataset / "queries"
        items = []
        for index in range(1, count + 1):
            text = (root / f"{index}.sql").read_text()
            template = re.search(r"using template (query\d+)", text)
            label = template.group(1) if template else f"q{index}"
            if any(i.split("_")[0] == f"{name}/{label}" for i, _ in items):
                label = f"{label}_file{index}"  # two files can instantiate the same template
            body = re.sub(r"(?m)^--.*$", "", text)
            for part, statement in enumerate(s for s in sqlglot.parse(body, read="postgres") if s is not None):
                suffix = f"_{part + 1}" if len(re.findall(";", body)) > 1 else ""
                items.append((f"{name}/{label}{suffix}", statement.sql(dialect="postgres")))
    out = []
    for query_id, sql in items:
        try:
            out.append((query_id, corpora.to_bigquery(sql)))
        except Exception:  # noqa: BLE001
            continue
    return out


def real_database(name: str) -> Path | None:
    path = corpora.BENCH_DIR / f"{name}.duckdb"
    return path if path.exists() else None


# ---------------------------------------------------------------- real-data execution


def _run_duckdb(con, sql: str, timeout: float = QUERY_TIMEOUT_S):
    timer = threading.Timer(timeout, con.interrupt)
    timer.start()
    try:
        started = time.perf_counter()
        rows = con.execute(sql).fetchall()
        return rows, time.perf_counter() - started
    finally:
        timer.cancel()


def _duck(sql: str) -> str:
    return sqlglot.transpile(sql, read="bigquery", write="duckdb")[0]


def _bag(rows) -> Counter:
    def norm(v):
        if isinstance(v, float):
            return "NaN" if math.isnan(v) else round(v, 6)
        return v
    return Counter(tuple(norm(v) for v in row) for row in rows)


def real_check(db: Path, original: str, transformed: str, timings: bool = True, memory: str = "3GB") -> dict:
    """Run both on real data: same rows (as bags; with a shared ORDER BY .. LIMIT, same rows before it) and timings."""

    import duckdb

    con = duckdb.connect(str(db), read_only=True, config={"memory_limit": memory, "threads": 2})
    try:
        try:
            base_rows, _ = _run_duckdb(con, _duck(original))
        except Exception as exc:  # noqa: BLE001
            return {"status": "original_fails", "detail": str(exc)[:120]}
        try:
            new_rows, _ = _run_duckdb(con, _duck(transformed))
        except Exception as exc:  # noqa: BLE001
            return {"status": "timeout" if "INTERRUPT" in str(exc).upper() else "error", "detail": str(exc)[:120]}
        same = _bag(base_rows) == _bag(new_rows)
        if not same and _shares_root_limit(original, transformed):
            try:
                a, _ = _run_duckdb(con, _duck(_strip_root_limit(original)))
                b, _ = _run_duckdb(con, _duck(_strip_root_limit(transformed)))
                same = _bag(a) == _bag(b)
            except Exception:  # noqa: BLE001
                pass
        if not timings:
            return {"status": "same" if same else "different"}
        times = {"original": [], "transformed": []}
        for _ in range(RUNS):
            for key, sql in (("original", original), ("transformed", transformed)):
                times[key].append(_run_duckdb(con, _duck(sql))[1])
        return {
            "status": "same" if same else "different",
            "original_s": statistics.median(times["original"]),
            "transformed_s": statistics.median(times["transformed"]),
            "plan_changed": _plan(con, original) != _plan(con, transformed),
        }
    finally:
        con.close()


def _plan(con, sql: str) -> str:
    """DuckDB's physical plan, so a speed-up from timer noise (an unchanged plan) is not counted as useful."""

    return "\n".join(str(row[-1]) for row in con.execute("EXPLAIN " + _duck(sql)).fetchall())


def _strip_root_limit(sql: str) -> str:
    query = sqlglot.parse_one(sql, read="bigquery")
    for key in ("order", "limit", "offset"):
        query.set(key, None)
    return query.sql(dialect="bigquery")


def _shares_root_limit(left: str, right: str) -> bool:
    a, b = sqlglot.parse_one(left, read="bigquery"), sqlglot.parse_one(right, read="bigquery")
    tail = lambda q: [q.args.get(k) and q.args[k].sql(dialect="bigquery") for k in ("order", "limit", "offset")]
    return a.args.get("limit") is not None and tail(a) == tail(b)


# ---------------------------------------------------------------- transformations


def transformations() -> list[str]:
    return [*engine.available_rules(), "pipeline"]


def apply(name: str, sql: str):
    if name == "pipeline":
        return rewrite.apply_rules(rewrite.canonical_rule_order(), sql)
    return rewrite.apply_rule(name, sql)


def synthetic_check(original: str, transformed: str, schema) -> str:
    result = check_result_equivalence(original, transformed, schema, seeds=SEEDS, rows_per_table=12)
    if result.status is ResultEquivalenceStatus.ERROR:
        return "original_fails" if result.reason.startswith("left side failed") else "error"
    if result.status is ResultEquivalenceStatus.DIFFERENT and _shares_root_limit(original, transformed):
        again = check_result_equivalence(_strip_root_limit(original), _strip_root_limit(transformed), schema, seeds=SEEDS, rows_per_table=12)
        return "same" if again.equivalent else "different"
    return {ResultEquivalenceStatus.EQUIVALENT: "same", ResultEquivalenceStatus.DIFFERENT: "different"}.get(result.status, "inconclusive")


def run_transformations(item: tuple[str, str, str]) -> tuple[str, dict]:
    workload, query_id, sql = item
    schema = corpora.schema(f"sqlstorm/{workload}")
    db = real_database(workload)
    out: dict[str, dict] = {}
    for name in transformations():
        started = time.perf_counter()
        try:
            result = apply(name, sql)
        except Exception as exc:  # noqa: BLE001
            out[name] = {"status": "crash", "detail": str(exc)[:160], "seconds": time.perf_counter() - started}
            continue
        record = {
            "seconds": round(time.perf_counter() - started, 4),
            "changed": result.sql != sql,
            "verification": result.verification.status.value,
        }
        if record["changed"]:
            try:
                record["synthetic"] = synthetic_check(sql, result.sql, schema)
            except Exception as exc:  # noqa: BLE001
                record["synthetic"] = f"check_crashed: {str(exc)[:80]}"
            if db is not None:
                try:
                    record["real"] = real_check(db, sql, result.sql)
                except Exception as exc:  # noqa: BLE001
                    record["real"] = {"status": "check_crashed", "detail": str(exc)[:80]}
        out[name] = record
    return query_id, out


# ---------------------------------------------------------------- JOB alternative forms


_FROM = "from_" if "from_" in exp.Select.arg_types else "from"
_WITH = "with_" if "with_" in exp.Select.arg_types else "with"


def _tables(select: exp.Select) -> list[exp.Table]:
    tables = [select.args[_FROM].this]
    tables += [join.this for join in select.args.get("joins") or []]
    return tables


def _conjuncts(where: exp.Expression | None) -> list[exp.Expression]:
    if where is None:
        return []
    return list(where.this.flatten()) if isinstance(where.this, exp.And) else [where.this]


def _aliases(node: exp.Expression) -> set[str]:
    return {c.table for c in node.find_all(exp.Column) if c.table}


def _comma_join_query(sql: str) -> exp.Select | None:
    query = sqlglot.parse_one(sql, read="bigquery")
    if not isinstance(query, exp.Select) or query.args.get(_WITH) or query.args.get(_FROM) is None:
        return None
    joins = query.args.get("joins") or []
    if any(j.args.get("on") or j.args.get("using") or (j.args.get("kind") or "CROSS").upper() != "CROSS" or j.args.get("side") for j in joins):
        return None
    if not all(isinstance(t, exp.Table) for t in _tables(query)):
        return None
    return query


def form_explicit_joins(sql: str) -> str | None:
    """``FROM a, b, c WHERE a.x = b.y AND ...`` as ``FROM a JOIN b ON ... JOIN c ON ...``."""

    query = _comma_join_query(sql)
    if query is None:
        return None
    tables = _tables(query)
    remaining = _conjuncts(query.args.get("where"))
    seen = {tables[0].alias_or_name}
    joined = []
    for table in tables[1:]:
        alias = table.alias_or_name
        on = [c for c in remaining if alias in _aliases(c) and _aliases(c) <= seen | {alias} and len(_aliases(c)) > 1]
        remaining = [c for c in remaining if c not in on]
        condition = exp.and_(*on) if on else exp.true()
        joined.append(exp.Join(this=table.copy(), on=condition.copy(), kind="INNER"))
        seen.add(alias)
    new = query.copy()
    new.set("joins", joined)
    new.set("where", exp.Where(this=exp.and_(*remaining)) if remaining else None)
    return new.sql(dialect="bigquery")


def form_reversed(sql: str) -> str | None:
    query = _comma_join_query(sql)
    if query is None:
        return None
    tables = list(reversed(_tables(query)))
    new = query.copy()
    new.set(_FROM, exp.From(this=tables[0].copy()))
    new.set("joins", [exp.Join(this=t.copy()) for t in tables[1:]])
    conjuncts = list(reversed(_conjuncts(query.args.get("where"))))
    if conjuncts:
        new.set("where", exp.Where(this=exp.and_(*[c.copy() for c in conjuncts])))
    return new.sql(dialect="bigquery")


def form_unqualified(sql: str, schema) -> str | None:
    """Drop the table alias from every column whose name belongs to exactly one table in the query."""

    query = _comma_join_query(sql)
    if query is None:
        return None
    by_alias = {t.alias_or_name: t.name for t in _tables(query)}
    owners: dict[str, set[str]] = defaultdict(set)
    for alias, table in by_alias.items():
        for column in (schema.get(table) or {}):
            owners[column.lower()].add(alias)
    if any(not schema.get(t) for t in by_alias.values()):
        return None
    outputs = {e.alias_or_name.lower() for e in query.expressions}
    new = query.copy()
    changed = False
    for column in new.find_all(exp.Column):
        if column.table and owners.get(column.name.lower()) == {column.table} and column.name.lower() not in outputs:
            column.set("table", None)
            changed = True
    return new.sql(dialect="bigquery") if changed else None


def form_filter_ctes(sql: str) -> str | None:
    """Move each table's single-table filters into a CTE that the query reads instead."""

    query = _comma_join_query(sql)
    if query is None:
        return None
    conjuncts = _conjuncts(query.args.get("where"))
    tables = _tables(query)
    ctes, rest = [], list(conjuncts)
    new = query.copy()
    replacements = {}
    for table in tables:
        alias = table.alias_or_name
        local = [c for c in rest if _aliases(c) == {alias} and not c.find(exp.Subquery)]
        if not local:
            continue
        rest = [c for c in rest if c not in local]
        name = f"f_{alias}"
        body = sqlglot.select("*").from_(table.copy()).where(exp.and_(*[c.copy() for c in local]))
        ctes.append(exp.CTE(this=body, alias=exp.TableAlias(this=exp.to_identifier(name))))
        replacements[alias] = name
    if not ctes:
        return None
    for table in [new.args[_FROM].this, *[j.this for j in new.args.get("joins") or []]]:
        if table.alias_or_name in replacements:
            table.set("this", exp.to_identifier(replacements[table.alias_or_name]))
            table.set("db", None)
            table.set("catalog", None)
    new.set("where", exp.Where(this=exp.and_(*[c.copy() for c in rest])) if rest else None)
    new.set(_WITH, exp.With(expressions=ctes))
    return new.sql(dialect="bigquery")


def form_renamed_aliases(sql: str) -> str | None:
    query = _comma_join_query(sql)
    if query is None:
        return None
    new = query.copy()
    mapping = {t.alias_or_name: f"r{i}_{t.alias_or_name}" for i, t in enumerate(_tables(new))}
    for table in _tables(new):
        table.set("alias", exp.TableAlias(this=exp.to_identifier(mapping[table.alias_or_name])))
    for column in new.find_all(exp.Column):
        if column.table in mapping:
            column.set("table", exp.to_identifier(mapping[column.table]))
    return new.sql(dialect="bigquery")


FORMS = {
    "explicit_joins": lambda sql, schema: form_explicit_joins(sql),
    "reversed_from": lambda sql, schema: form_reversed(sql),
    "unqualified": form_unqualified,
    "filter_ctes": lambda sql, schema: form_filter_ctes(sql),
    "renamed_aliases": lambda sql, schema: form_renamed_aliases(sql),
}


# Broken forms: negative controls the prover must never prove (a prover that proves everything would
# otherwise score 100% on the forms above).


def _column_classes(conjuncts: list[exp.Expression]) -> dict[str, str]:
    """Union-find over ``a.x = b.y`` conjuncts: column -> representative of its equality class."""

    parent: dict[str, str] = {}

    def find(x: str) -> str:
        parent.setdefault(x, x)
        while parent[x] != x:
            x = parent[x]
        return x

    for c in conjuncts:
        if isinstance(c, exp.EQ) and isinstance(c.left, exp.Column) and isinstance(c.right, exp.Column):
            parent[find(c.left.sql())] = find(c.right.sql())
    return {k: find(k) for k in parent}


def broken_dropped_join(sql: str) -> str | None:
    """Drop one join predicate that the remaining equalities do not imply."""

    query = _comma_join_query(sql)
    if query is None:
        return None
    conjuncts = _conjuncts(query.args.get("where"))
    for i, c in enumerate(conjuncts):
        if not (isinstance(c, exp.EQ) and isinstance(c.left, exp.Column) and isinstance(c.right, exp.Column) and c.left.table != c.right.table):
            continue
        rest = conjuncts[:i] + conjuncts[i + 1:]
        classes = _column_classes(rest)
        left, right = c.left.sql(), c.right.sql()
        if left in classes and classes.get(left) == classes.get(right):
            continue  # implied by the others, so dropping it is a valid rewrite
        new = query.copy()
        new.set("where", exp.Where(this=exp.and_(*[x.copy() for x in rest])) if rest else None)
        return new.sql(dialect="bigquery")
    return None


def broken_negated_filter(sql: str) -> str | None:
    """Negate one single-table filter on a constant."""

    query = _comma_join_query(sql)
    if query is None:
        return None
    conjuncts = _conjuncts(query.args.get("where"))
    for i, c in enumerate(conjuncts):
        if len(_aliases(c)) == 1 and c.find(exp.Literal) and not c.find(exp.Subquery):
            new = query.copy()
            changed = [x.copy() for x in conjuncts]
            changed[i] = exp.Not(this=exp.Paren(this=changed[i]))
            new.set("where", exp.Where(this=exp.and_(*changed)))
            return new.sql(dialect="bigquery")
    return None


BROKEN_FORMS = {
    "dropped_join_predicate": lambda sql, schema: broken_dropped_join(sql),
    "negated_filter": lambda sql, schema: broken_negated_filter(sql),
}


def _lineage(sql: str, schema=None) -> dict[str, set[tuple[str, str]]] | None:
    """KumoSQL's column lineage of a one-query project: output column -> {(table, column)}.

    ``schema`` is the source tables' columns, as a saved BigQuery catalog would supply.
    """

    import tempfile

    from kumosql.pipeline import load_sqlx_project

    root = Path(tempfile.mkdtemp(prefix="kumosql_bench_"))
    (root / "definitions").mkdir()
    (root / "definitions" / "q.sql").write_text(sql)
    pipeline = load_sqlx_project(root, source_schema=schema)
    out: dict[str, set[tuple[str, str]]] = {}
    for row in pipeline.lineage_report():
        out[row["column"].lower()] = {(s["node"].split(".")[-1].lower(), s["column"].lower()) for s in row["sources"]}
    return out


def _truth_lineage(sql: str, schema) -> dict[str, set[tuple[str, str]]]:
    """Output column -> base (table, column) pairs, from sqlglot's qualifier and scope walk."""

    from sqlglot.optimizer.qualify import qualify
    from sqlglot.optimizer.scope import build_scope

    mapping = {t: {c: "STRING" for c in cols} for t, cols in schema.items()}
    query = qualify(sqlglot.parse_one(sql, read="bigquery"), schema=mapping, dialect="bigquery", quote_identifiers=False)
    root = build_scope(query)
    out: dict[str, set[tuple[str, str]]] = {}

    def resolve(scope, column: exp.Column, depth=0) -> set[tuple[str, str]]:
        source = scope.sources.get(column.table)
        if isinstance(source, exp.Table):
            return {(source.name.lower(), column.name.lower())}
        if source is not None and depth < 20:
            inner = next((e for e in source.expression.selects if e.alias_or_name.lower() == column.name.lower()), None)
            if inner is not None:
                return {pair for c in inner.find_all(exp.Column) for pair in resolve(source, c, depth + 1)}
        return set()

    for projection in root.expression.selects:
        out[projection.alias_or_name.lower()] = {pair for c in projection.find_all(exp.Column) for pair in resolve(root, c)}
    return out


def _prove(left: str, right: str, schema) -> tuple[str, float]:
    columns = {t: list(c) for t, c in schema.items()}
    started = time.perf_counter()
    smt = prove_equivalent_smt(left, right, timeout_ms=10000, schema=columns)
    status = smt.status.value
    if smt.status is not SmtStatus.PROVEN_EQUIVALENT and prove_equivalent(left, right).proven:
        status = SmtStatus.PROVEN_EQUIVALENT.value
    return status, time.perf_counter() - started


def run_forms(item: tuple[str, str, str]) -> tuple[str, dict]:
    workload, query_id, sql = item
    schema = corpora.schema(f"sqlstorm/{workload}")
    query = sqlglot.parse_one(sql, read="bigquery")
    db = real_database(workload)
    record: dict = {"tables": len(list(query.find_all(exp.Table))), "forms": {}}
    try:
        base_lineage = _lineage(sql, schema)
        truth = _truth_lineage(sql, schema)
        record["lineage_matches_truth"] = base_lineage == truth
        if base_lineage != truth:
            record["lineage_diff"] = {k: [sorted(base_lineage.get(k, ())), sorted(truth.get(k, ()))] for k in set(base_lineage) | set(truth) if base_lineage.get(k) != truth.get(k)}
    except Exception as exc:  # noqa: BLE001
        base_lineage = None
        record["lineage_error"] = str(exc)[:160]
    for name, build in FORMS.items():
        try:
            form = build(sql, schema)
        except Exception as exc:  # noqa: BLE001
            record["forms"][name] = {"status": "generator_error", "detail": str(exc)[:120]}
            continue
        if form is None:
            continue
        entry: dict = {}
        try:
            entry["prover"], entry["prover_s"] = _prove(sql, form, schema)
        except Exception as exc:  # noqa: BLE001
            entry["prover"], entry["prover_s"] = f"crash: {str(exc)[:100]}", None
        try:
            entry["synthetic"] = synthetic_check(sql, form, schema)
        except Exception as exc:  # noqa: BLE001
            entry["synthetic"] = f"check_crashed: {str(exc)[:80]}"
        if db is not None:
            try:
                entry["real"] = real_check(db, sql, form, timings=False)["status"]
            except Exception as exc:  # noqa: BLE001
                entry["real"] = f"check_crashed: {str(exc)[:80]}"
        try:
            lineage = _lineage(form, schema)
            entry["lineage_same"] = base_lineage is not None and lineage == base_lineage
        except Exception as exc:  # noqa: BLE001
            entry["lineage_same"] = False
            entry["lineage_error"] = str(exc)[:120]
        record["forms"][name] = entry
    record["broken"] = {}
    for name, build in BROKEN_FORMS.items():
        form = build(sql, schema)
        if form is None:
            continue
        entry = {}
        try:
            entry["prover"], entry["prover_s"] = _prove(sql, form, schema)
        except Exception as exc:  # noqa: BLE001
            entry["prover"], entry["prover_s"] = f"crash: {str(exc)[:100]}", None
        try:
            entry["synthetic"] = synthetic_check(sql, form, schema)
        except Exception as exc:  # noqa: BLE001
            entry["synthetic"] = f"check_crashed: {str(exc)[:80]}"
        if db is not None:
            try:
                entry["real"] = real_check(db, sql, form, timings=False)["status"]
            except Exception as exc:  # noqa: BLE001
                entry["real"] = f"check_crashed: {str(exc)[:80]}"
        record["broken"][name] = entry
    return query_id, record


# ---------------------------------------------------------------- report


def summarise(workload: str, results: dict, forms: dict | None) -> dict:
    per_rule: dict[str, Counter] = defaultdict(Counter)
    seconds: dict[str, list[float]] = defaultdict(list)
    speedups = []
    for query in results.values():
        for name, rec in query.items():
            c = per_rule[name]
            c["queries"] += 1
            seconds[name].append(rec.get("seconds") or 0)
            if rec.get("status") == "crash":
                c["crash"] += 1
                continue
            if not rec.get("changed"):
                continue
            c["changed"] += 1
            proven = rec["verification"] == "proven"
            c["proven" if proven else "unproven"] += 1
            synthetic = rec.get("synthetic")
            if synthetic == "different":
                c["wrong_synthetic" if proven else "caught_synthetic"] += 1
            real = rec.get("real") or {}
            if real.get("status") == "different":
                c["wrong_real" if proven else "caught_real"] += 1
            if real.get("status") == "same":
                c["real_same"] += 1
                if real.get("plan_changed"):
                    c["plan_changed"] += 1
                    if proven and real["transformed_s"] <= SPEEDUP * real["original_s"]:
                        c["useful"] += 1
                if proven:
                    speedups.append(real["original_s"] / max(real["transformed_s"], 1e-9))
    summary = {
        "workload": workload,
        "queries": len(results),
        "rules": {name: dict(c) for name, c in per_rule.items()},
        "transform_ms": {name: round(1000 * statistics.median(v), 1) for name, v in seconds.items() if v},
        "transform_ms_max": {name: round(1000 * max(v), 1) for name, v in seconds.items() if v},
        "speedup_median": round(statistics.median(speedups), 3) if speedups else None,
    }
    if forms:
        summary["forms"] = summarise_forms(forms)
    return summary


def summarise_forms(forms: dict) -> dict:
    out: dict[str, Counter] = defaultdict(Counter)
    by_tables: dict[str, list[float]] = defaultdict(list)
    lineage = Counter()
    for record in forms.values():
        lineage["queries"] += 1
        lineage["matches_truth"] += bool(record.get("lineage_matches_truth"))
        lineage["error"] += "lineage_error" in record
        bucket = "≤5 tables" if record["tables"] <= 5 else "6-9 tables" if record["tables"] <= 9 else "10+ tables"
        for name, entry in record["forms"].items():
            c = out[name]
            c["generated"] += 1
            if "status" in entry:
                c[entry["status"]] += 1
                continue
            c[f"prover_{entry['prover'].split(':')[0]}"] += 1
            c[f"synthetic_{entry['synthetic'].split(':')[0]}"] += 1
            c["lineage_same"] += bool(entry.get("lineage_same"))
            if "real" in entry:
                c[f"real_{entry['real'].split(':')[0]}"] += 1
            if entry["prover"] == "proven_equivalent" and "different" in (entry["synthetic"], entry.get("real")):
                c["wrong"] += 1
            if entry.get("prover_s") is not None:
                by_tables[bucket].append(entry["prover_s"])
    broken: dict[str, Counter] = defaultdict(Counter)
    for record in forms.values():
        for name, entry in (record.get("broken") or {}).items():
            c = broken[name]
            c["generated"] += 1
            c[f"prover_{entry['prover'].split(':')[0]}"] += 1
            c[f"synthetic_{entry['synthetic'].split(':')[0]}"] += 1
            if "real" in entry:
                c[f"real_{entry['real'].split(':')[0]}"] += 1
            if entry["prover"] == "proven_equivalent":
                c["wrong"] += 1
    return {
        "lineage": dict(lineage),
        "forms": {k: dict(v) for k, v in out.items()},
        "broken": {k: dict(v) for k, v in broken.items()},
        "prover_seconds": {
            k: {"median": round(statistics.median(v), 3), "max": round(max(v), 3), "n": len(v)} for k, v in sorted(by_tables.items())
        },
    }


def main(argv: list[str] | None = None) -> int:
    os.environ.setdefault("KUMOSQL_TIMING", "0")  # worker processes inherit it
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("workloads", nargs="*", default=list(WORKLOADS), choices=WORKLOADS)
    parser.add_argument("--jobs", type=int, default=None)
    parser.add_argument("--limit", type=int, default=0, help="first N queries per workload (0 for all)")
    parser.add_argument("--json", type=Path, help="write per-query results and summaries here")
    parser.add_argument("--no-forms", action="store_true", help="skip JOB alternative forms")
    args = parser.parse_args(argv)
    report = {}
    with Pool(args.jobs or os.cpu_count()) as pool:
        for workload in args.workloads:
            items = [(workload, qid, sql) for qid, sql in workload_queries(workload)]
            if args.limit:
                items = items[: args.limit]
            results = dict(pool.imap_unordered(run_transformations, items, chunksize=1))
            forms = None
            if workload == "job" and not args.no_forms:
                forms = dict(pool.imap_unordered(run_forms, items, chunksize=1))
            report[workload] = {"summary": summarise(workload, results, forms), "queries": results, "forms": forms}
            print(json.dumps(report[workload]["summary"], indent=1, ensure_ascii=False))
    if args.json:
        args.json.write_text(json.dumps(report, indent=1, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
