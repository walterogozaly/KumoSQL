"""Backtest workload-ranked view materialization on STATS-CEB, with measured DuckDB runtimes.

The advisor in :mod:`kumosql.materialization` chooses which joins to store. This module
supplies the evidence a real warehouse would supply: it stores each candidate join view in
DuckDB, runs every query the prover lets read it before and after, and keeps the measured
before/after pairs. Everything else (the runtime model, the advisor, the held-out total) is
computed from those pairs by :mod:`.materialize_model`.

Stages, each cached under ``KUMOSQL_BENCH_DATA`` (nothing is written into the repository):

1. :func:`mine_views`   join views shared by at least two *development* queries, with their
   exact sizes (variable elimination, no join is materialized to count it);
2. :func:`prove_readers` every query a view can answer, rewritten over the view by
   ``model_reuse.rewrite_over_model`` (the proof gate);
3. :func:`measure`      baseline runtimes, then per view its build time and each proven
   reader's runtime and DuckDB plan (estimated and actual cardinalities per operator);

Query names are ``q000``.. in file order. A query is held out when ``sha256("split:" + name)``
is 0 modulo 4. Views are mined and the model fitted on development queries only; held-out
queries are measured after the selection is frozen and only report it.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import statistics
import time
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping, Sequence

from .common import cached_json, data_dir

#: Views with more rows than this are not stored (the same cap the workload prototype used).
ROW_CAP = 30_000_000
THREADS = 4


# ----------------------------------------------------------------------------- workload


def is_held_out(name: str) -> bool:
    """One query in four, chosen by a hash of its name so the split never depends on its contents."""

    return int(hashlib.sha256(("split:" + name).encode()).hexdigest(), 16) % 4 == 0


@dataclass
class Workload:
    """Queries (Postgres dialect, one ``SELECT COUNT(*)`` each), the table schema and, when known, the true counts."""

    queries: dict[str, str]
    schema: dict[str, list[str]]
    cards: dict[str, int] = field(default_factory=dict)

    @property
    def dev(self) -> dict[str, str]:
        return {q: s for q, s in self.queries.items() if not is_held_out(q)}

    @property
    def held_out(self) -> dict[str, str]:
        return {q: s for q, s in self.queries.items() if is_held_out(q)}


def schema_of(ddl: str) -> dict[str, list[str]]:
    """Table name to lower-cased column names from the ``CREATE TABLE`` statements of a DDL script."""

    out: dict[str, list[str]] = {}
    for m in re.finditer(r"CREATE TABLE (\w+)\s*\((.*?)\n\s*\);", ddl, re.S | re.I):
        cols = [line.strip().split()[0].lower() for line in m.group(2).split("\n") if line.strip() and not line.strip().startswith("--")]
        out[m.group(1).lower()] = cols
    return out


def load_workload(repo: str) -> Workload:
    """The 146 STATS-CEB queries of a clone of Han et al.'s benchmark repository."""

    from . import stats_ceb

    with open(os.path.join(repo, "datasets/stats_simplified/stats.sql")) as fh:
        schema = schema_of(fh.read())
    queries, cards = {}, {}
    for i, (card, sql) in enumerate(stats_ceb.workload(repo)):
        queries[f"q{i:03d}"] = sql.rstrip().rstrip(";")
        cards[f"q{i:03d}"] = card
    return Workload(queries, schema, cards)


def duckdb_sql(sql: str) -> str:
    import sqlglot

    return sqlglot.transpile(sql.strip().rstrip(";"), read="postgres", write="duckdb")[0]


# ----------------------------------------------------------------------------- stage 1: views


@dataclass
class View:
    id: str
    sql: str  # Postgres dialect, from view_candidates
    tables: list[str]
    edges: list[list]
    rows: int
    support: list[str]  # development queries that contain the join

    def to_json(self) -> dict:
        return {"id": self.id, "sql": self.sql, "tables": self.tables, "edges": self.edges, "rows": self.rows, "support": self.support}

    @classmethod
    def from_json(cls, d: Mapping) -> "View":
        return cls(d["id"], d["sql"], list(d["tables"]), [list(e) for e in d["edges"]], d["rows"], list(d["support"]))

    @property
    def shape(self):
        from kumosql.view_candidates import Shape

        return Shape(tuple(self.tables), tuple(tuple(e) for e in self.edges))


def mine_views(work: Workload, con: Any, *, row_cap: int = ROW_CAP, max_tables: int = 4, min_support: int = 2) -> list[View]:
    """Candidate join views from the development queries, smaller than ``row_cap`` rows, with exact sizes."""

    from kumosql import view_candidates
    from kumosql.joinorder.query import parse_join_query

    from .truth import exact_count

    views: list[View] = []
    for cand in view_candidates.mine(work.dev, work.schema, min_support=min_support, max_tables=max_tables, dialect="postgres"):
        query = parse_join_query(cand.sql())
        rows = exact_count(con, query, frozenset(query.tables))
        if rows <= row_cap:
            views.append(View(f"v{len(views):03d}", cand.sql(), list(cand.shape.tables),
                              [list(e) for e in cand.shape.edges], int(rows), sorted(cand.queries)))
    return views


# ----------------------------------------------------------------------------- stage 2: proofs


def _prove(task: tuple) -> tuple:
    view_id, query, sql, view_sql, schema, timeout_ms = task
    from kumosql import model_reuse

    try:
        r = model_reuse.rewrite_over_model(sql, view_sql, schema=schema, model_name=f"mv_{view_id}", dialect="postgres", timeout_ms=timeout_ms)
    except Exception as error:  # a prover crash is an unproven reader, not a failed run
        return (view_id, query, "error", str(error)[:120], None)
    return (view_id, query, r.status, r.reason[:120], r.sql if r.rewritten else None)


def placements(work: Workload) -> dict[str, dict]:
    """Per query, the join shapes it contains (shape -> aliases in shape order)."""

    from kumosql import view_candidates

    out = {}
    for q, sql in work.queries.items():
        graph = view_candidates.graph_of(sql, work.schema, "postgres")
        out[q] = view_candidates.shapes_of(graph) if graph else {}
    return out


def prove_readers(work: Workload, views: Sequence[View], *, timeout_ms: int = 10_000, workers: int = 4,
                  queries: Iterable[str] | None = None, log: Callable[[str], None] = print) -> dict[str, dict]:
    """``{"view|query": {"status", "reason", "sql"}}`` for every query that contains a view's join."""

    place = placements(work)
    names = set(work.queries if queries is None else queries)
    tasks = [(v.id, q, work.queries[q], v.sql, work.schema, timeout_ms)
             for v in views for q in sorted(names) if v.shape in place[q]]
    log(f"proving {len(tasks)} reader rewrites")
    if workers > 1 and len(tasks) > 1:
        with ProcessPoolExecutor(workers) as pool:
            results = list(pool.map(_prove, tasks, chunksize=4))
    else:
        results = [_prove(t) for t in tasks]
    return {f"{v}|{q}": {"status": s, "reason": r, "sql": sql} for v, q, s, r, sql in results}


# ----------------------------------------------------------------------------- stage 3: measurement


def run_timed(con: Any, sql: str, timeout: float, reps: int = 3) -> tuple[float | None, list | None]:
    """Median of ``reps`` runs and the first result; ``(None, None)`` when a run exceeds ``timeout``."""

    from .endtoend import run_sql

    times, first = [], None
    for _ in range(reps):
        elapsed, rows = run_sql(con, sql, timeout)
        if elapsed == float("inf"):
            return None, None
        if first is None:
            first = rows
        times.append(elapsed)
    return statistics.median(times), first


def _operator(node: Mapping) -> dict:
    info = node.get("extra_info") or {}
    est = info.get("Estimated Cardinality")
    return {
        "type": node.get("operator_type") or node.get("name"),
        "est": float(est) if est not in (None, "") else None,
        "act": node.get("operator_cardinality"),
        "scanned": node.get("operator_rows_scanned"),
        "time": node.get("operator_timing"),
        "children": [_operator(c) for c in node.get("children", [])],
    }


def plan_of(con: Any, sql: str, *, analyze: bool = True) -> dict | None:
    """DuckDB's physical plan as a tree of ``type, est, act, scanned, time, children``.

    ``est`` is the optimizer's cardinality estimate; ``act`` and ``time`` come from one profiled
    execution (``analyze``), otherwise they are ``None``. Operator times of one run are noisy; use
    the plan's shape and cardinalities, not its seconds, as features.
    """

    try:
        if analyze:
            rows = con.execute("EXPLAIN (ANALYZE, FORMAT JSON) " + sql).fetchall()
            doc = json.loads(rows[-1][-1])
            root = doc["children"][0] if doc.get("children") else doc
            return _operator(root)
        rows = con.execute("EXPLAIN (FORMAT JSON) " + sql).fetchall()
        doc = json.loads(rows[-1][-1])
        return _operator(doc[0] if isinstance(doc, list) else doc)
    except Exception:
        return None


def measure_baselines(con: Any, work: Workload, *, timeout: float = 60.0, reps: int = 3, names: Iterable[str] | None = None,
                      known: Mapping[str, dict] | None = None, log: Callable[[str], None] = print) -> dict[str, dict]:
    """Median runtime, result and plan of each original query (a timeout leaves ``time`` as ``None``)."""

    out = dict(known or {})
    for q in sorted(work.queries if names is None else names):
        if q in out:
            continue
        sql = duckdb_sql(work.queries[q])
        seconds, rows = run_timed(con, sql, timeout, reps)
        out[q] = {"time": seconds, "result": None if rows is None else rows[0][0],
                  "plan": None if seconds is None else plan_of(con, sql),
                  "plan_estimate": plan_of(con, sql, analyze=False)}
    return out


def _reader_timeout(base: float | None) -> float:
    return min(max(5 * (base or 2.0), 2.0), 30.0)


def measure_view(con: Any, view: View, readers: Mapping[str, str], baselines: Mapping[str, dict], work: Workload, *,
                 reps: int = 3, built: dict | None = None) -> dict:
    """Store ``view`` as a table, run its ``readers`` (query -> rewritten SQL), drop it.

    The result of each reader is compared with the original's; a difference is rechecked with
    DuckDB's optimizer off (``duckdb_load.run_unoptimized``) and counts as ``wrong`` only if that
    run also differs. A reader that runs past five times the original (at most 30 s) is recorded
    as censored: its ``time`` is the limit, so its measured saving is an upper bound.
    """

    table = f"mv_{view.id}"
    out = {"id": view.id, "build": None, "rows": None, "readers": {}} if built is None else built
    if out["build"] is None:
        con.execute(f'DROP TABLE IF EXISTS "{table}"')
        start = time.perf_counter()
        con.execute(f'CREATE TABLE "{table}" AS {duckdb_sql(view.sql)}')
        out["build"] = time.perf_counter() - start
        out["rows"] = int(con.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
        con.execute("CHECKPOINT")
    else:
        con.execute(f'DROP TABLE IF EXISTS "{table}"')
        con.execute(f'CREATE TABLE "{table}" AS {duckdb_sql(view.sql)}')
        con.execute("CHECKPOINT")
    try:
        for q, rewritten in readers.items():
            base = baselines.get(q) or {}
            if base.get("time") is None:
                out["readers"][q] = {"skipped": "original timed out"}
                continue
            sql = duckdb_sql(rewritten)
            limit = _reader_timeout(base["time"])
            seconds, rows = run_timed(con, sql, limit, reps)
            if seconds is None:
                out["readers"][q] = {"time": limit, "censored": True, "same": None, "wrong": False, "result": None,
                                     "plan_estimate": plan_of(con, sql, analyze=False)}
                continue
            same = rows[0][0] == base["result"]
            wrong = False
            if not same:
                from kumosql.duckdb_load import run_unoptimized

                original = duckdb_sql(work.queries[q])
                before, after = run_unoptimized(con, original, sql)
                wrong = before != after
            out["readers"][q] = {"time": seconds, "censored": False, "same": same, "wrong": wrong, "result": rows[0][0],
                                 "plan": plan_of(con, sql), "plan_estimate": plan_of(con, sql, analyze=False)}
    finally:
        con.execute(f'DROP TABLE IF EXISTS "{table}"')
        con.execute("CHECKPOINT")
    return out
