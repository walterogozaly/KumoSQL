"""What runs against a project, how often, and what it measured, from job history.

``workload(pipeline, jobs)`` sorts every counted job (cache hits, dry runs and
script parents whose children are present are left out, as in ``costs.py``)
into one of three kinds:

* a **build** of a model: the job writes a model's table (its refresh);
* a **write** to a declared source: a load or ingestion job, kept only for its
  time, because a stored copy of anything reading the source is stale after it;
* a **read**: everything else that reads a graph node, such as a dashboard, an
  export or an ad hoc query. A read belongs to the outermost nodes it read (a
  query against a view is a read of the view, not of the view's tables).

Reads are grouped into templates by their query text with literals kept, so a
dashboard tile run 300 times is one template with 300 runs. Every figure is
measured: bytes processed and billed and slot milliseconds summed per node and
per template, with counts and per-day rates over the window. Query text never
leaves this module: a template carries its text only for the cost model to
read, and its JSON form shows a hash.
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from statistics import median
from typing import TYPE_CHECKING, Iterable

from .costs import ObservedJob, _ancestors, _is_temporary

if TYPE_CHECKING:
    from .pipeline import Pipeline


@dataclass
class Runs:
    """Measured totals over a set of jobs."""

    jobs: int = 0
    bytes_processed: int = 0
    bytes_billed: int = 0
    slot_ms: int = 0
    times: list[str] = field(default_factory=list)

    def add(self, job: ObservedJob) -> None:
        self.jobs += 1
        self.bytes_processed += job.total_bytes_processed
        self.bytes_billed += job.total_bytes_billed
        self.slot_ms += job.total_slot_ms
        if job.creation_time:
            self.times.append(job.creation_time)

    def per_run(self) -> dict[str, float]:
        if not self.jobs:
            return {"bytes_processed": 0.0, "bytes_billed": 0.0, "slot_ms": 0.0}
        return {
            "bytes_processed": self.bytes_processed / self.jobs,
            "bytes_billed": self.bytes_billed / self.jobs,
            "slot_ms": self.slot_ms / self.jobs,
        }

    def to_json(self) -> dict:
        return {
            "jobs": self.jobs,
            "bytes_processed": self.bytes_processed,
            "bytes_billed": self.bytes_billed,
            "slot_ms": self.slot_ms,
        }


@dataclass
class ReadTemplate:
    """Reads that ran the same query text (or, without text, read the same nodes)."""

    id: str
    nodes: tuple[str, ...]
    sql: str | None
    runs: Runs = field(default_factory=Runs)
    readers: set[str] = field(default_factory=set)

    def to_json(self, days: float) -> dict:
        return {
            "id": self.id,
            "nodes": list(self.nodes),
            "has_query_text": self.sql is not None,
            "runs": self.runs.jobs,
            "runs_per_day": self.runs.jobs / days if days else None,
            "readers": len(self.readers),
            "measured": self.runs.to_json(),
        }


@dataclass
class NodeUse:
    node: str
    kind: str
    builds: Runs = field(default_factory=Runs)
    reads: Runs = field(default_factory=Runs)
    writes: list[str] = field(default_factory=list)
    readers: set[str] = field(default_factory=set)

    def builds_per_day(self, days: float) -> float | None:
        """Refreshes per day from the spacing of observed builds; ``None`` with fewer than two."""

        times = sorted(_parse(t) for t in self.builds.times)
        if len(times) < 2:
            return None
        span = (times[-1] - times[0]).total_seconds() / 86400
        if span <= 0:
            return None
        return (len(times) - 1) / span

    def to_json(self, days: float) -> dict:
        per_day = self.builds_per_day(days)
        return {
            "node": self.node,
            "kind": self.kind,
            "builds": self.builds.to_json(),
            "builds_per_day": per_day,
            "reads": self.reads.to_json(),
            "reads_per_day": self.reads.jobs / days if days else None,
            "readers": len(self.readers),
            "source_writes": len(self.writes),
        }


@dataclass
class Workload:
    start: str | None
    end: str | None
    days: float
    nodes: dict[str, NodeUse]
    templates: dict[str, ReadTemplate]
    excluded: dict[str, int]
    unplaced: int = 0

    def build_times(self, node: str) -> list[datetime]:
        use = self.nodes.get(node)
        return sorted(_parse(t) for t in use.builds.times) if use else []

    def to_json(self) -> dict:
        return {
            "window": {"start": self.start, "end": self.end, "days": self.days},
            "nodes": [self.nodes[k].to_json(self.days) for k in sorted(self.nodes)],
            "templates": [
                t.to_json(self.days)
                for t in sorted(self.templates.values(), key=lambda t: (-t.runs.slot_ms, -t.runs.bytes_billed, t.id))
            ],
            "excluded": dict(self.excluded),
            "unplaced_jobs": self.unplaced,
        }


def _parse(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed


def _reader(job: ObservedJob) -> str:
    who = job.user_email or str(job.labels.get("reader", "")) or job.query or job.job_id
    return hashlib.sha256(who.encode("utf-8")).hexdigest()[:16]


def _template_id(sql: str | None, nodes: tuple[str, ...]) -> str:
    text = " ".join(sql.split()) if sql else "nodes:" + ",".join(nodes)
    return "t-" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def workload(pipeline: "Pipeline", jobs: Iterable[ObservedJob], *, days: float | None = None) -> Workload:
    """Sort job history into builds, source writes and read templates per node."""

    jobs = list(jobs)
    parents = {job.parent_job_id for job in jobs if job.parent_job_id}
    upstream = pipeline.upstream
    nodes: dict[str, NodeUse] = {}
    for key, model in pipeline.models.items():
        nodes[key] = NodeUse(key, model.kind)
    for key in pipeline.sources:
        nodes.setdefault(key, NodeUse(key, "source"))
    templates: dict[str, ReadTemplate] = {}
    excluded: dict[str, int] = defaultdict(int)
    unplaced = 0
    times: list[str] = []
    for job in jobs:
        if not job.counted:
            excluded["cache_hit" if job.cache_hit else "dry_run"] += 1
            continue
        if job.job_id and job.job_id in parents:
            excluded["script_parent"] += 1
            continue
        if job.error_result:
            excluded["failed"] += 1
            continue
        if job.creation_time:
            times.append(job.creation_time)
        if job.destination_table is not None and not _is_temporary(job.destination_table):
            destination = pipeline.resolve_reference(job.destination_table)
            if destination.matched and destination.identity.key in nodes:
                use = nodes[destination.identity.key]
                if use.kind == "source":
                    if job.creation_time:
                        use.writes.append(job.creation_time)
                else:
                    use.builds.add(job)
                continue
        refs = sorted({
            r.identity.key
            for r in map(pipeline.resolve_reference, job.referenced_tables)
            if r.matched and r.identity.key in nodes
        })
        outer = tuple(n for n in refs if not any(n in _ancestors(upstream, other) for other in refs if other != n))
        if not outer:
            unplaced += 1
            continue
        reader = _reader(job)
        for node in outer:
            nodes[node].reads.add(job)
            nodes[node].readers.add(reader)
        sql = job.query.strip() or None
        tid = _template_id(sql, outer)
        template = templates.setdefault(tid, ReadTemplate(tid, outer, sql))
        template.runs.add(job)
        template.readers.add(reader)
    start, end = (min(times), max(times)) if times else (None, None)
    if days is None:
        if start and end:
            span = (_parse(end) - _parse(start)).total_seconds() / 86400
            days = max(span, 1.0)
        else:
            days = 1.0
    return Workload(start, end, float(days), nodes, templates, dict(excluded), unplaced)


def runs_per_day(times: Iterable[str]) -> float | None:
    """Rate of a periodic job from its run times; ``None`` with fewer than two runs."""

    parsed = sorted(_parse(t) for t in times)
    if len(parsed) < 2:
        return None
    gaps = [(b - a).total_seconds() for a, b in zip(parsed, parsed[1:])]
    gap = median(gaps)
    return 86400 / gap if gap > 0 else None


__all__ = ["NodeUse", "ReadTemplate", "Runs", "Workload", "runs_per_day", "workload"]
