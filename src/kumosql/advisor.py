"""Recommend materializations for a Dataform project from its job history.

``advise(pipeline, jobs, sizes=..., pricing=...)`` answers: which views should
be stored as tables, which tables could be views again, and what each change
saves a day, using only what was measured plus a column-level estimate of how
each measured job would change.

How a change is priced
----------------------

BigQuery on-demand bills the bytes of the columns a query reads from each
stored table (a view is expanded into the query that reads it). The advisor
therefore estimates, for every read template and every refresh, the stored
columns it reads under a given set of stored models, following column lineage
through views (``Pipeline.column_lineage`` and ``consumed_columns``), and
prices them with table sizes (``sizes``: rows, bytes and per-column bytes, for
example from ``INFORMATION_SCHEMA.TABLE_STORAGE`` and ``COLUMN_FIELD_PATHS``).
A job that was measured keeps its measured cost; a change scales it by the
estimated ratio (estimate after / estimate before), so the estimator only has
to get ratios right. Slot time is scaled the same way: a stand-in, reported as
such, because dry runs have no stage statistics and only completed jobs say
what slot time a plan takes. Every figure's basis is kept: ``measured`` for
the work as it ran, ``estimate`` for the work after a change.

When results stay the same
--------------------------

Storing a view changes when its rows are computed, so it is safe only when no
read can tell the difference (the evidence on every candidate):

* ``changes_results`` when the view reads a clock, ``RAND()`` or a UUID: a
  table freezes the value at refresh time;
* ``proven`` when every stored input (after expanding views) is a model
  refreshed by the same schedules, so a table refreshed in those runs after its
  inputs equals the view at every read outside the run;
* ``conditional`` when an input is a declared source or refreshed on another
  schedule: the stored copy is stale between the source's change and the next
  refresh. The condition is listed; the change is never counted as a saving.

Turning a table back into a view is judged the same way in reverse.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Iterable, Mapping

import sqlglot
from sqlglot import exp

from .cost_model import MIN_BYTES_PER_TABLE, Pricing, error_summary
from .costs import ObservedJob
from .materialization import Candidate, Evidence, Problem, Selection, select
from .pipeline_types import ColumnRef
from .workload import Workload, runs_per_day, workload

if TYPE_CHECKING:
    from .pipeline import Pipeline

STORED_KINDS = ("table", "incremental")
#: Logical bytes per value of fixed-width GoogleSQL types (BigQuery's data size rules).
TYPE_BYTES = {
    "INT64": 8, "INTEGER": 8, "INT": 8, "BIGINT": 8, "SMALLINT": 8, "TINYINT": 8, "BYTEINT": 8,
    "FLOAT64": 8, "FLOAT": 8, "DOUBLE": 8, "NUMERIC": 16, "DECIMAL": 16, "BIGNUMERIC": 32, "BIGDECIMAL": 32,
    "BOOL": 1, "BOOLEAN": 1, "DATE": 8, "DATETIME": 8, "TIME": 8, "TIMESTAMP": 8, "INTERVAL": 16,
}
#: Bytes assumed per value of a column whose type and size are both unknown.
UNKNOWN_WIDTH = 8.0


@dataclass(frozen=True)
class TableSize:
    """Stored size of one table: rows, logical bytes and, when known, bytes per column."""

    rows: float
    bytes: float | None = None
    columns: Mapping[str, float] = field(default_factory=dict)
    types: Mapping[str, str] = field(default_factory=dict)

    def column_bytes(self, column: str, all_columns: Iterable[str]) -> float:
        name = column.lower()
        if name in self.columns:
            return float(self.columns[name])
        names = [c.lower() for c in all_columns] or [name]
        fixed = {c: TYPE_BYTES.get(self.types.get(c, "").upper().split("<")[0]) for c in names}
        if fixed.get(name) is not None:
            return self.rows * fixed[name]
        variable = [c for c, width in fixed.items() if width is None]
        if self.bytes is not None and variable:
            rest = self.bytes - sum(self.rows * w for w in fixed.values() if w is not None)
            return max(rest, 0.0) / len(variable)
        return self.rows * UNKNOWN_WIDTH


def load_sizes(path: str | Path) -> dict[str, TableSize]:
    """Table sizes from JSON: ``{"project.dataset.table": {"rows": n, "bytes": b, "columns": {...}, "types": {...}}}``."""

    data = json.loads(Path(path).read_text(encoding="utf-8"))
    out: dict[str, TableSize] = {}
    for key, row in data.items():
        out[key.lower()] = TableSize(
            rows=float(row.get("rows", 0)),
            bytes=float(row["bytes"]) if row.get("bytes") is not None else None,
            columns={k.lower(): float(v) for k, v in (row.get("columns") or {}).items()},
            types={k.lower(): str(v) for k, v in (row.get("types") or {}).items()},
        )
    return out


# ------------------------------------------------------------ column bytes


class BytesModel:
    """Stored columns a read or refresh scans under a set of stored models, and their bytes."""

    def __init__(self, pipeline: "Pipeline", sizes: Mapping[str, TableSize]):
        self.pipeline = pipeline
        self.sizes = {k.lower(): v for k, v in sizes.items()}
        self.lineage = pipeline.column_lineage()
        self.consumed = pipeline.consumed_columns()
        self.kinds = {key: model.kind for key, model in pipeline.models.items()}
        self._outputs: dict[str, tuple[str, ...]] = {}
        self._memo: dict = {}

    def outputs(self, node: str) -> tuple[str, ...]:
        if node not in self._outputs:
            columns = self.pipeline.output_columns(node)
            if not columns and node in self.pipeline.source_schema:
                columns = tuple(self.pipeline.source_schema[node])
            size = self.sizes.get(node.lower())
            if not columns and size is not None:
                columns = tuple(size.columns or size.types)
            self._outputs[node] = tuple(c.lower() for c in columns)
        return self._outputs[node]

    def stored(self, node: str, stored: frozenset[str]) -> bool:
        kind = self.kinds.get(node)
        return kind is None or node in stored

    def needed_inputs(self, model: str, columns: frozenset[str] | None) -> frozenset[ColumnRef]:
        """Input columns ``model`` reads to produce ``columns`` (``None``: all of them)."""

        consumed = self.consumed.get(model, frozenset())
        if columns is None:
            return consumed
        projected: set[ColumnRef] = set()
        wanted: set[ColumnRef] = set()
        for name in self.outputs(model):
            sources = self.lineage.get(ColumnRef(model, name), frozenset())
            projected |= sources
            if name in columns:
                wanted |= sources
        # Columns read by filters, joins, grouping, windows or DISTINCT are needed whichever outputs are read.
        return frozenset((consumed - projected) | wanted | self._structural(model, consumed))

    def _structural(self, model: str, consumed: frozenset[ColumnRef]) -> frozenset[ColumnRef]:
        key = ("structural", model)
        if key not in self._memo:
            names = _structural_names(self.pipeline.models[model].sql) if model in self.pipeline.models else None
            # A name read by a filter of any scope counts for every input with that column: more bytes, never fewer.
            self._memo[key] = consumed if names is None else frozenset(r for r in consumed if r.column.lower() in names)
        return self._memo[key]

    def scan(self, node: str, columns: frozenset[str] | None, stored: frozenset[str]) -> frozenset[ColumnRef]:
        """Stored columns read when ``columns`` of ``node`` are read."""

        if self.stored(node, stored):
            names = columns if columns is not None else frozenset(self.outputs(node))
            return frozenset(ColumnRef(node, c) for c in names)
        key = (node, columns, stored & self._upstream_views(node))
        hit = self._memo.get(key)
        if hit is not None:
            return hit
        by_table: dict[str, set[str]] = {}
        for ref in self.needed_inputs(node, columns):
            by_table.setdefault(ref.table, set()).add(ref.column.lower())
        out: set[ColumnRef] = set()
        for table, names in by_table.items():
            out |= self.scan(table, frozenset(names), stored)
        result = frozenset(out)
        self._memo[key] = result
        return result

    def _upstream_views(self, node: str) -> frozenset[str]:
        key = ("up", node)
        if key not in self._memo:
            seen: set[str] = set()
            stack = [node]
            while stack:
                for parent in self.pipeline.upstream.get(stack.pop(), ()):
                    if parent not in seen and self.kinds.get(parent) is not None:
                        seen.add(parent)
                        stack.append(parent)
            self._memo[key] = frozenset(seen | {node})
        return self._memo[key]

    def bytes(self, refs: Iterable[ColumnRef]) -> tuple[float, float, bool]:
        """(bytes processed, bytes billed, every table sized) for a set of stored columns."""

        per_table: dict[str, float] = {}
        known = True
        for ref in refs:
            size = self.sizes.get(ref.table.lower())
            if size is None:
                known = False
                per_table.setdefault(ref.table, 0.0)
                continue
            per_table[ref.table] = per_table.get(ref.table, 0.0) + size.column_bytes(ref.column, self.outputs(ref.table))
        processed = sum(per_table.values())
        billed = sum(max(b, MIN_BYTES_PER_TABLE) for b in per_table.values())
        return processed, billed, known


_STRUCTURAL = (exp.Where, exp.Group, exp.Having, exp.Qualify, exp.Window)


def _structural_names(sql: str) -> frozenset[str] | None:
    """Column names a query reads outside its projections; ``None`` when every column counts (DISTINCT) or it does not parse."""

    try:
        tree = sqlglot.parse_one(sql, read="bigquery")
    except sqlglot.errors.SqlglotError:
        return None
    if tree is None:
        return None
    if any(True for _ in tree.find_all(exp.Distinct)) or any(u.args.get("distinct") for u in tree.find_all(exp.Union)):
        return None
    names: set[str] = set()
    for node in tree.find_all(*_STRUCTURAL):
        names |= {c.name.lower() for c in node.find_all(exp.Column)}
    for join in tree.find_all(exp.Join):
        for part in (join.args.get("on"), *(join.args.get("using") or ())):
            if part is not None:
                names |= {c.name.lower() for c in part.find_all(exp.Column)} | ({part.name.lower()} if isinstance(part, exp.Identifier) else set())
    for order in tree.find_all(exp.Order):
        names |= {c.name.lower() for c in order.find_all(exp.Column)}
    return frozenset(names)


def columns_read(pipeline: "Pipeline", sql: str, outputs: Callable[[str], tuple[str, ...]]) -> dict[str, frozenset[str] | None] | None:
    """Columns a query reads from each graph node; ``None`` for a node read with ``*``. ``None`` if it does not parse."""

    try:
        trees = [t for t in sqlglot.parse(sql, read="bigquery") if t is not None]
    except sqlglot.errors.SqlglotError:
        return None
    out: dict[str, frozenset[str] | None] = {}
    for tree in trees:
        names = {c.name.lower() for c in tree.find_all(exp.Column) if not isinstance(c.this, exp.Star)}
        star = any(isinstance(s, exp.Star) or (isinstance(s, exp.Column) and isinstance(s.this, exp.Star)) for s in tree.find_all(exp.Star, exp.Column))
        for table in tree.find_all(exp.Table):
            key = pipeline.resolve(table)
            if not key:
                continue
            if star:
                out[key] = None
                continue
            have = set(outputs(key))
            mine = frozenset(names & have) if have else None
            previous = out.get(key, frozenset())
            out[key] = None if mine is None or previous is None else frozenset(previous | mine)
    return out


# --------------------------------------------------------------- evidence


def _run_dependent(sql: str) -> str:
    from .pipeline_equivalence import run_dependent

    try:
        tree = sqlglot.parse_one(sql, read="bigquery")
    except sqlglot.errors.SqlglotError:
        return "the model does not parse"
    return run_dependent(tree) if tree is not None else "the model does not parse"


class Freshness:
    """Whether storing (or un-storing) a model keeps every read's results."""

    def __init__(self, pipeline: "Pipeline", use: Workload, schedules: Mapping[str, Iterable[str]] | None):
        self.pipeline = pipeline
        self.use = use
        self.schedules = {k: frozenset(v) for k, v in (schedules or {}).items()}
        self.kinds = {key: model.kind for key, model in pipeline.models.items()}

    def stored_inputs(self, node: str, stored: frozenset[str]) -> tuple[set[str], list[str]]:
        """Stored inputs of ``node`` after expanding views, and every view expanded on the way."""

        inputs: set[str] = set()
        views: list[str] = []
        stack = list(self.pipeline.upstream.get(node, ()))
        seen: set[str] = set()
        while stack:
            parent = stack.pop()
            if parent in seen:
                continue
            seen.add(parent)
            kind = self.kinds.get(parent)
            if kind is None or kind in STORED_KINDS or parent in stored:
                inputs.add(parent)
            else:
                views.append(parent)
                stack.extend(self.pipeline.upstream.get(parent, ()))
        return inputs, views

    def _same_runs(self, nodes: set[str]) -> tuple[bool, str]:
        if all(n in self.schedules for n in nodes):
            sets = {self.schedules[n] for n in nodes}
            if len(sets) == 1 and next(iter(sets)):
                return True, "refreshed by the same schedules (" + ", ".join(sorted(next(iter(sets)))) + ")"
            return False, "its inputs are refreshed by different schedules"
        times = {n: self.use.build_times(n) for n in nodes}
        if all(len(t) >= 2 for t in times.values()):
            anchor = min(nodes, key=lambda n: (len(times[n]), n))
            slack = timedelta(hours=1)
            aligned = all(
                all(any(abs(t - u) <= slack for u in times[n]) for t in times[anchor])
                and len(times[n]) == len(times[anchor])
                for n in nodes
            )
            if aligned:
                return True, f"its inputs were built in the same runs ({len(times[anchor])} runs observed)"
            return False, "its inputs were built at different times"
        return False, "no schedule or build history shows when its inputs are refreshed"

    def evidence(self, node: str, store: bool, stored: frozenset[str]) -> Evidence:
        model = self.pipeline.models.get(node)
        if model is None:
            return Evidence("unknown", "not a model of the project")
        why = _run_dependent(model.sql)
        expanded_text = [self.pipeline.models[v].sql for v in self.stored_inputs(node, stored)[1] if v in self.pipeline.models]
        for text in expanded_text:
            why = why or _run_dependent(text)
        if why == "the model does not parse":
            return Evidence("unknown", why)
        if why:
            return Evidence(
                "changes_results",
                f"{why.split(': ', 1)[-1]} is evaluated at read time by a view and at refresh time by a table",
            )
        inputs, _ = self.stored_inputs(node, stored)
        sources = sorted(n for n in inputs if self.kinds.get(n) is None)
        if sources:
            action = "a stored copy is stale" if store else "the table is stale"
            return Evidence(
                "conditional",
                f"reads {len(sources)} declared source(s); {action} between a source change and the next refresh",
                tuple(f"{s} changes only before the scheduled refresh, and is never read in between" for s in sources),
            )
        if not inputs:
            return Evidence("proven", "reads no stored table, so every evaluation returns the same rows")
        same, how = self._same_runs(inputs)
        if same:
            verb = "a table refreshed in those runs after them equals the view" if store else "the view equals the table"
            return Evidence("proven", f"every input is {how.removeprefix('refreshed by ')}; {verb} at every read outside a run" if how.startswith("refreshed") else f"{how}; {verb} at every read outside a run")
        return Evidence(
            "conditional",
            how,
            (f"{node} is refreshed whenever any of {', '.join(sorted(inputs))} is",),
        )


# ----------------------------------------------------------------- advisor


@dataclass
class Advice:
    unit: str
    pricing: Pricing
    selection: Selection
    candidates: list[dict]
    calibration: dict
    workload: dict
    notes: list[str]

    def to_json(self) -> dict:
        return {
            "unit": self.unit,
            "pricing": self.pricing.to_json(),
            "selection": self.selection.to_json(),
            "recommendations": [c for c in self.candidates if c["evidence"]["label"] == "proven" and (c["saving_per_day"] or 0) > 0],
            "needs_proof": [c for c in self.candidates if c["evidence"]["label"] in ("conditional", "unknown")],
            "changes_results": [c for c in self.candidates if c["evidence"]["label"] == "changes_results"],
            "not_worth_it": [c for c in self.candidates if c["evidence"]["label"] == "proven" and (c["saving_per_day"] or 0) <= 0],
            "calibration": self.calibration,
            "workload": self.workload,
            "notes": self.notes,
        }


@dataclass
class _JobTemplate:
    id: str
    runs_per_day: float
    measured_bytes: float
    measured_billed: float
    measured_slot: float
    reads: dict[str, frozenset[str] | None]  # node -> columns
    estimate: Callable[[frozenset[str]], tuple[float, float, bool]]

    def cost(self, stored: frozenset[str], baseline: frozenset[str], pricing: Pricing) -> float:
        now = self.estimate(baseline)
        then = self.estimate(stored)
        ratio = then[0] / now[0] if now[0] > 0 else 1.0
        billed_ratio = then[1] / now[1] if now[1] > 0 else 1.0
        return pricing.compute_cost(self.measured_billed * billed_ratio, self.measured_slot * ratio)


class _FnTemplate:
    """A template whose cost under a stored set comes from a function (no choice of plan)."""

    def __init__(self, tid: str, runs_per_day: float, fn: Callable[[frozenset[str]], float], relevant: frozenset[str]):
        self.id = tid
        self.runs_per_day = runs_per_day
        self._fn = fn
        self._relevant = relevant
        self._memo: dict[frozenset[str], float] = {}
        self.options = ()

    def daily(self, stored: frozenset[str]) -> float:
        key = stored & self._relevant
        if key not in self._memo:
            self._memo[key] = self.runs_per_day * self._fn(stored)
        return self._memo[key]


def advise(
    pipeline: "Pipeline",
    jobs: Iterable[ObservedJob],
    *,
    sizes: Mapping[str, TableSize] | None = None,
    pricing: Pricing | None = None,
    schedules: Mapping[str, Iterable[str]] | None = None,
    refresh_per_day: float | None = None,
    days: float | None = None,
    budget_bytes: float | None = None,
    exact_limit: int = 12,
) -> Advice:
    """Price every store/un-store change for the project's models and pick the best proven set."""

    pricing = pricing or Pricing()
    sizes = {k.lower(): v for k, v in (sizes or {}).items()}
    use = workload(pipeline, jobs, days=days)
    fresh = Freshness(pipeline, use, schedules)
    baseline = frozenset(k for k, m in pipeline.models.items() if m.kind in STORED_KINDS)
    notes: list[str] = []
    model = BytesModel(pipeline, sizes)

    # A view that is not stored has no measured size; bound it by its widest input.
    size_basis: dict[str, str] = {}
    for key, m in sorted(pipeline.models.items()):
        if m.kind != "view":
            continue
        if key.lower() in sizes:
            size_basis[key] = "measured"
            continue
        inputs, _ = fresh.stored_inputs(key, frozenset())
        bound = _stored_size(model, key, inputs)
        if bound is not None:
            rows, per_column = bound
            model.sizes[key.lower()] = TableSize(rows=rows, bytes=sum(per_column.values()), columns=per_column)
            size_basis[key] = "upper_bound"

    def estimator(reads: Mapping[str, frozenset[str] | None]) -> Callable[[frozenset[str]], tuple[float, float, bool]]:
        def run(stored: frozenset[str]) -> tuple[float, float, bool]:
            refs: set[ColumnRef] = set()
            for node, columns in reads.items():
                refs |= model.scan(node, columns, stored)
            return model.bytes(refs)
        return run

    templates: list[_JobTemplate] = []
    for t in use.templates.values():
        reads: dict[str, frozenset[str] | None] = {n: None for n in t.nodes}
        if t.sql:
            parsed = columns_read(pipeline, t.sql, model.outputs)
            if parsed:
                reads = {n: parsed.get(n) for n in t.nodes}
        per = t.runs.per_run()
        templates.append(_JobTemplate(t.id, t.runs.jobs / use.days, per["bytes_processed"], per["bytes_billed"],
                                      per["slot_ms"], reads, estimator(reads)))
    for key in sorted(baseline):
        node = use.nodes.get(key)
        if node is None or not node.builds.jobs:
            continue
        needed: dict[str, set[str]] = {}
        for ref in model.needed_inputs(key, None):
            needed.setdefault(ref.table, set()).add(ref.column.lower())
        reads = {k: frozenset(v) for k, v in needed.items()}
        per = node.builds.per_run()
        rate = runs_per_day(node.builds.times) or node.builds.jobs / use.days
        templates.append(_JobTemplate("build:" + key, rate, per["bytes_processed"], per["bytes_billed"],
                                      per["slot_ms"], reads, estimator(reads)))

    # Calibration: how far the column estimate is from what each measured job processed.
    estimated, measured, slot_ratio = [], [], []
    for jt in templates:
        processed, _, known = jt.estimate(baseline)
        if known and processed > 0 and jt.measured_bytes > 0:
            estimated.append(processed)
            measured.append(jt.measured_bytes)
        if jt.measured_bytes > 0 and jt.measured_slot > 0:
            slot_ratio.append(jt.measured_slot / jt.measured_bytes)
    ratios = sorted(m / e for e, m in zip(estimated, measured))
    scale = ratios[len(ratios) // 2] if ratios else 1.0
    slot_per_byte = sorted(slot_ratio)[len(slot_ratio) // 2] if slot_ratio else None
    calibration = {
        "bytes_estimate_vs_measured": error_summary([e * scale for e in estimated], measured),
        "bytes_scale": scale,
        "slot_ms_per_byte": slot_per_byte,
    }
    if pricing.compute == "editions":
        notes.append("Slot time after a change is the measured slot time scaled by the estimated bytes ratio; "
                     "a new refresh's slot time is its estimated bytes times the median measured slot time per byte.")

    read_views: set[str] = set()
    for jt in templates:
        for node in jt.reads:
            if node in pipeline.models and pipeline.models[node].kind == "view":
                read_views.add(node)
            read_views.update(fresh.stored_inputs(node, baseline)[1])
    candidates: list[Candidate] = []
    for view in sorted(read_views):
        inputs, _ = fresh.stored_inputs(view, baseline)
        rates = [r for r in (runs_per_day(use.nodes[i].builds.times) for i in inputs if i in use.nodes) if r]
        rate = refresh_per_day if refresh_per_day is not None else (max(rates) if rates else 1.0)
        processed, billed, known = model.bytes(model.scan(view, None, baseline))
        slot = processed * scale * slot_per_byte if slot_per_byte is not None else None
        if pricing.compute == "editions" and slot is None:
            refresh, refresh_basis = 0.0, "unknown: no measured slot time"
        else:
            refresh = pricing.compute_cost(billed * scale, slot or 0.0)
            refresh_basis = "estimate" if known else "estimate with unsized tables"
        stored = model.sizes.get(view.lower())
        candidates.append(Candidate(
            id="store:" + view,
            title=f"Store {view} as a table",
            kind="store_view",
            node=view,
            refresh_per_day=rate,
            refresh_cost=refresh,
            storage_bytes=stored.bytes if stored is not None else None,
            evidence=fresh.evidence(view, True, baseline),
            detail={"refresh_basis": refresh_basis, "storage_basis": size_basis.get(view, "unknown"),
                    "refresh_bytes_estimate": processed * scale},
        ))
    for table in sorted(baseline):
        node = use.nodes.get(table)
        if node is None or not node.builds.jobs:
            continue
        per = node.builds.per_run()
        size = sizes.get(table.lower())
        candidates.append(Candidate(
            id="unstore:" + table,
            title=f"Make {table} a view",
            kind="unstore_table",
            node=table,
            refresh_per_day=runs_per_day(node.builds.times) or node.builds.jobs / use.days,
            refresh_cost=pricing.compute_cost(per["bytes_billed"], per["slot_ms"]),
            storage_bytes=size.bytes if size is not None else None,
            evidence=fresh.evidence(table, False, baseline),
            detail={"refresh_basis": "measured", "storage_basis": "measured" if size is not None else "unknown"},
            store=False,
        ))

    unstorable = {"build:" + c.node for c in candidates if not c.store}
    fn_templates = []
    for jt in templates:
        if jt.id in unstorable:
            continue  # a table's own refresh is priced by its un-store candidate
        relevant = frozenset().union(*(model._upstream_views(n) for n in jt.reads)) if jt.reads else frozenset()
        fn_templates.append(_FnTemplate(jt.id, jt.runs_per_day, _cost_of(jt, baseline, pricing), relevant))
    problem = Problem(fn_templates, candidates, pricing.unit, pricing.storage_per_byte_day(), baseline)
    selection = select(problem, budget_bytes=budget_bytes, exact_limit=exact_limit)
    rows = []
    for c in candidates:
        rows.append({
            "id": c.id,
            "kind": c.kind,
            "title": c.title,
            "node": c.node,
            "saving_per_day": problem.saving([c.id]),
            "saving_basis": "estimate",
            "refresh_per_day": c.refresh_per_day,
            "refresh_cost": c.refresh_cost,
            "storage_bytes": c.storage_bytes,
            "evidence": c.evidence.to_json(),
            "chosen": c.id in selection.chosen,
            **dict(c.detail),
        })
    rows.sort(key=lambda r: (-(r["saving_per_day"] or 0), r["id"]))
    if pricing.storage_per_byte_day() is None:
        notes.append("Storage is not priced: give a storage rate (and a compute rate for on-demand) to weigh it.")
    return Advice(pricing.unit, pricing, selection, rows, calibration, use.to_json(), notes)


def _cost_of(jt: _JobTemplate, baseline: frozenset[str], pricing: Pricing) -> Callable[[frozenset[str]], float]:
    return lambda stored: jt.cost(stored, baseline, pricing)


def _stored_size(model: BytesModel, view: str, inputs: Iterable[str]) -> tuple[float, dict[str, float]] | None:
    """Upper bound on a view's stored size: its widest input's rows, and per-column bytes at that many rows."""

    rows = [model.sizes[i.lower()].rows for i in inputs if i.lower() in model.sizes]
    if not rows:
        return None
    most = max(rows)
    columns: dict[str, float] = {}
    for column in model.outputs(view) or ():
        widths = []
        for ref in model.lineage.get(ColumnRef(view, column), frozenset()):
            size = model.sizes.get(ref.table.lower())
            if size is not None and size.rows:
                widths.append(size.column_bytes(ref.column, model.outputs(ref.table)) / size.rows)
        columns[column] = most * (max(widths) if widths else UNKNOWN_WIDTH)
    return most, columns


__all__ = ["Advice", "BytesModel", "Freshness", "TableSize", "advise", "columns_read", "load_sizes"]
