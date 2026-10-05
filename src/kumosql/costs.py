"""Attribute measured job cost to pipeline graph nodes and edges.

Input is a list of :class:`ObservedJob` records loaded from an exported job
history file (JSON, JSON lines or CSV). Nothing here touches the network.

Measured and estimated cost are separate types of value. A :class:`Cost` has a
``source`` and refuses to be added to a cost of the other source, so planner
(dry run) bytes can never leak into measured totals. Billed bytes, processed
bytes and slot time are separate fields; reports say which one they use.

A job whose billed bytes are missing, or cannot be read as a whole number, is
*unmeasured*: it is left out of every total and counted under
``excluded["unmeasured"]``. A missing measurement is never read as zero cost.

Attribution assigns a job's whole cost to exactly one place and records the
method. A job's cost is never split across the tables it read, because job
history does not report bytes per table. Whatever cannot be assigned goes into
an explicit ``unattributed`` bucket with a reason code, so that
``attributed + unattributed == total`` holds exactly (all sums are integers).

View reads: when a job has no destination and reads several graph nodes, the
cost is placed on the outermost node it read, that is the referenced node that
is not upstream of another referenced node. A query against a view therefore
lands on the view, not on the view's base tables, even though job history lists
both. When there is more than one outermost node the job is unattributed as
``multiple_readers``.
"""

from __future__ import annotations

import csv
import json
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import TYPE_CHECKING, Iterable, Literal, Mapping

from .identity import normalize_table_reference

if TYPE_CHECKING:
    from .pipeline import Pipeline

CostSource = Literal["measured", "estimated"]

TIB = 1024 ** 4

# Reason codes for the unattributed bucket.
REASONS = {
    "temporary_destination": "The job wrote to a temporary or anonymous table.",
    "unmatched_destination": "The destination table is not a node in the graph.",
    "ambiguous_destination": "The destination matches more than one graph node.",
    "no_matching_references": "The job read no table that is a node in the graph.",
    "multiple_readers": "The job read several unrelated graph nodes and its cost cannot be split.",
    "script_without_children": "A script job whose child jobs are not in the input.",
    "no_tables": "The job records neither a destination nor referenced tables.",
}

INVOICE_EXCLUSIONS = (
    "This is billed bytes from the supplied job history times the caller's rate. An invoice also "
    "covers things this does not: BI Engine and reservation (slot capacity) charges, storage, "
    "streaming and other services, discounts, credits and taxes, and jobs outside the supplied "
    "history or window. Jobs without measured bytes billed are left out."
)

# Job label keys that may carry a target table for a job, in priority order.
DEFAULT_LABEL_KEYS = ("target", "action_target")


@dataclass(frozen=True)
class Cost:
    """Summed cost of a set of jobs. Integers only, so sums are exact."""

    source: CostSource = "measured"
    bytes_billed: int = 0
    bytes_processed: int = 0
    slot_ms: int = 0
    job_count: int = 0

    def __add__(self, other: "Cost") -> "Cost":
        if not isinstance(other, Cost):
            return NotImplemented
        if other.source != self.source:
            raise ValueError(f"cannot combine {self.source} cost with {other.source} cost")
        return Cost(
            self.source,
            self.bytes_billed + other.bytes_billed,
            self.bytes_processed + other.bytes_processed,
            self.slot_ms + other.slot_ms,
            self.job_count + other.job_count,
        )

    @classmethod
    def estimated(cls, bytes_processed: int) -> "Cost":
        """A planner (dry run) figure. Only bytes processed exist for it."""

        return cls("estimated", 0, int(bytes_processed), 0, 1)

    def to_json(self) -> dict[str, object]:
        return {
            "source": self.source,
            "bytes_billed": self.bytes_billed,
            "bytes_processed": self.bytes_processed,
            "slot_ms": self.slot_ms,
            "jobs": self.job_count,
        }


@dataclass(frozen=True)
class ObservedJob:
    """One executed job with the fields that were measured for it."""

    job_id: str
    creation_time: str | None = None
    project: str = ""
    location: str = ""
    user_email: str = ""
    statement_type: str = ""
    destination_table: object | None = None
    referenced_tables: tuple[object, ...] = ()
    total_bytes_processed: int | None = None
    total_bytes_billed: int | None = None
    total_slot_ms: int | None = None
    cache_hit: bool = False
    dry_run: bool = False
    labels: Mapping[str, str] = field(default_factory=dict)
    query: str = ""
    error_result: object | None = None
    parent_job_id: str = ""

    @classmethod
    def from_record(cls, record: Mapping[str, object]) -> "ObservedJob":
        """Build from a job-history export row or an API job resource.

        A nested BigQuery Job resource (``jobReference``, ``configuration``,
        ``statistics``, ``status``) is flattened first. Measured fields that are
        absent or unreadable stay ``None``: the job is unmeasured, not free.
        """

        record = flatten_job_resource(record)

        def pick(*names: str, default: object = None) -> object:
            for name in names:
                value = record.get(name)
                if value not in (None, ""):
                    return value
            return default

        refs = pick("referenced_tables", default=())
        if isinstance(refs, str):
            refs = _parse_json_or(refs, [refs])
        if isinstance(refs, Mapping):
            refs = [refs]
        labels = pick("labels", default={})
        if isinstance(labels, str):
            labels = _parse_json_or(labels, {})
        if isinstance(labels, (list, tuple)):
            labels = {
                str(item.get("key")): str(item.get("value"))
                for item in labels
                if isinstance(item, Mapping)
            }
        destination = pick("destination_table")
        if isinstance(destination, str) and destination.startswith("{"):
            destination = _parse_json_or(destination, destination)
        return cls(
            job_id=str(pick("job_id", "jobId", default="")),
            creation_time=_time(pick("creation_time", "creationTime")),
            project=str(pick("project_id", "project", default="")),
            location=str(pick("location", default="")),
            user_email=str(pick("user_email", default="")),
            statement_type=str(pick("statement_type", default="")).upper(),
            destination_table=destination,
            referenced_tables=tuple(refs or ()),  # type: ignore[arg-type]
            total_bytes_processed=_int(pick("total_bytes_processed")),
            total_bytes_billed=_int(pick("total_bytes_billed")),
            total_slot_ms=_int(pick("total_slot_ms")),
            cache_hit=_bool(pick("cache_hit", default=False)),
            dry_run=_bool(pick("dry_run", "dryRun", default=False)),
            labels=dict(labels or {}),  # type: ignore[arg-type]
            query=str(pick("query", default="")),
            error_result=pick("error_result", "errorResult"),
            parent_job_id=str(pick("parent_job_id", default="")),
        )

    @property
    def counted(self) -> bool:
        """Cache hits and dry runs bill nothing, so they add no cost."""

        return not (self.cache_hit or self.dry_run)

    @property
    def measured(self) -> bool:
        """Billed bytes, the basis of every cost figure, were recorded."""

        return self.total_bytes_billed is not None

    @property
    def missing_measurements(self) -> tuple[str, ...]:
        """Names of the measured fields this job does not carry."""

        return tuple(
            name
            for name in ("total_bytes_billed", "total_bytes_processed", "total_slot_ms")
            if getattr(self, name) is None
        )

    def cost(self) -> Cost:
        if not self.counted:
            return Cost()
        if self.total_bytes_billed is None:
            raise ValueError(f"job {self.job_id!r} has no measured bytes billed; it is unmeasured, not free")
        return Cost(
            "measured", self.total_bytes_billed, self.total_bytes_processed or 0, self.total_slot_ms or 0, 1
        )


def _parse_json_or(text: str, fallback: object) -> object:
    try:
        return json.loads(text)
    except ValueError:
        return fallback


def _int(value: object) -> int | None:
    """A whole, non-negative number read exactly, or ``None`` when absent or unreadable.

    Integer strings are parsed as integers, never through a float, so values
    above 2**53 keep every digit. ``None`` means "not measured"; callers must
    not turn it into zero.
    """

    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    text = str(value).strip()
    if not text:
        return None
    try:
        number = int(text)
    except ValueError:
        try:
            decimal = Decimal(text)
        except InvalidOperation:
            return None
        if not decimal.is_finite() or decimal != decimal.to_integral_value():
            return None
        number = int(decimal)
    return number if number >= 0 else None


def flatten_job_resource(record: Mapping[str, object]) -> Mapping[str, object]:
    """Return ``record`` with a nested BigQuery Job resource flattened to export names.

    Reads ``jobReference`` (identity), ``configuration`` (query text, destination,
    labels, dry run), ``statistics`` (times, bytes, slot time, cache hit, referenced
    tables, statement type) and ``status`` (error). Names already present at the
    top level win. A record with none of those sections is returned unchanged.
    """

    def section(source: object, name: str) -> Mapping[str, object]:
        value = source.get(name) if isinstance(source, Mapping) else None
        return value if isinstance(value, Mapping) else {}

    reference = section(record, "jobReference")
    configuration = section(record, "configuration")
    statistics = section(record, "statistics")
    status = section(record, "status")
    if not (reference or configuration or statistics or status):
        return record
    query_config = section(configuration, "query")
    query_stats = section(statistics, "query")

    def first(*candidates: object) -> object:
        for candidate in candidates:
            if candidate not in (None, ""):
                return candidate
        return None

    nested = {
        "job_id": reference.get("jobId"),
        "project_id": reference.get("projectId"),
        "location": reference.get("location"),
        "query": query_config.get("query"),
        "destination_table": query_config.get("destinationTable"),
        "dry_run": configuration.get("dryRun"),
        "labels": configuration.get("labels"),
        "statement_type": query_stats.get("statementType"),
        "referenced_tables": first(query_stats.get("referencedTables"), query_config.get("referencedTables")),
        "total_bytes_billed": query_stats.get("totalBytesBilled"),
        "total_bytes_processed": first(query_stats.get("totalBytesProcessed"), statistics.get("totalBytesProcessed")),
        "total_slot_ms": first(statistics.get("totalSlotMs"), query_stats.get("totalSlotMs")),
        "cache_hit": query_stats.get("cacheHit"),
        "creation_time": statistics.get("creationTime"),
        "parent_job_id": statistics.get("parentJobId"),
        "error_result": status.get("errorResult"),
    }
    flat = dict(record)
    for name, value in nested.items():
        if value not in (None, "") and flat.get(name) in (None, ""):
            flat[name] = value
    return flat


def _bool(value: object) -> bool:
    return value if isinstance(value, bool) else str(value).strip().lower() in {"1", "true", "yes"}


def _time(value: object) -> str | None:
    if value in (None, ""):
        return None
    try:
        if isinstance(value, (int, float)) or str(value).isdigit():
            number = float(value)  # type: ignore[arg-type]
            parsed = datetime.fromtimestamp(number / 1000 if number > 1e11 else number, timezone.utc)
        else:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, OverflowError, OSError):
        return None
    parsed = parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)
    return parsed.isoformat().replace("+00:00", "Z")


def load_jobs(path: str | Path) -> list[ObservedJob]:
    """Load an exported job history: a JSON array, JSON lines, or CSV file."""

    file = Path(path)
    text = file.read_text(encoding="utf-8")
    if file.suffix.lower() == ".csv":
        records: list[Mapping[str, object]] = list(csv.DictReader(text.splitlines()))
    else:
        stripped = text.strip()
        if stripped.startswith("["):
            records = json.loads(stripped)
        else:
            records = [json.loads(line) for line in text.splitlines() if line.strip()]
    return [ObservedJob.from_record(record) for record in records]


@dataclass(frozen=True)
class Attribution:
    """Where one job's cost went, and how that was decided."""

    job_id: str
    node: str | None
    method: str
    reason: str | None = None


@dataclass
class CostAttribution:
    total: Cost
    nodes: dict[str, Cost]
    node_methods: dict[str, dict[str, int]]
    unattributed: Cost
    reasons: dict[str, Cost]
    edges: dict[tuple[str, str], dict[str, object]]
    attributions: list[Attribution]
    skipped: dict[str, int]
    window: dict[str, str | None]
    missing_fields: dict[str, int] = field(default_factory=dict)

    @property
    def attributed(self) -> Cost:
        total = Cost()
        for cost in self.nodes.values():
            total = total + cost
        return total


def _ancestors(upstream: Mapping[str, Iterable[str]], key: str) -> set[str]:
    seen: set[str] = set()
    stack = list(upstream.get(key, ()))
    while stack:
        node = stack.pop()
        if node not in seen:
            seen.add(node)
            stack.extend(upstream.get(node, ()))
    return seen


def _is_temporary(reference: object) -> bool:
    identity = normalize_table_reference(reference)
    return bool(identity and len(identity.parts) >= 2 and identity.parts[-2].startswith("_"))


def attribute_costs(
    pipeline: "Pipeline",
    jobs: Iterable[ObservedJob],
    *,
    label_keys: tuple[str, ...] = DEFAULT_LABEL_KEYS,
    window: Mapping[str, str | None] | None = None,
) -> CostAttribution:
    """Join job cost to graph nodes and record exercised edges."""

    jobs = list(jobs)
    parents_with_children = {job.parent_job_id for job in jobs if job.parent_job_id}
    skipped: dict[str, int] = defaultdict(int)
    missing_fields: dict[str, int] = defaultdict(int)
    upstream = pipeline.upstream

    nodes: dict[str, Cost] = {}
    methods: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    reasons: dict[str, Cost] = {}
    edges: dict[tuple[str, str], dict[str, object]] = {}
    attributions: list[Attribution] = []
    total = Cost()
    unattributed = Cost()
    times: list[str] = []

    for job in jobs:
        if job.creation_time:
            times.append(job.creation_time)
        if not job.counted:
            skipped["cache_hit" if job.cache_hit else "dry_run"] += 1
            continue
        if job.job_id and job.job_id in parents_with_children:
            # The children carry the cost; counting the parent too would double it.
            skipped["script_parent"] += 1
            continue
        if not job.measured:
            # Not zero: nothing was recorded, so nothing is added to any total.
            skipped["unmeasured"] += 1
            attributions.append(Attribution(job.job_id, None, "unmeasured", "no_measured_cost"))
            continue
        for name in job.missing_measurements:
            missing_fields[name] += 1
        cost = job.cost()
        total = total + cost
        matched_refs = sorted(
            {r.identity.key for r in map(pipeline.resolve_reference, job.referenced_tables) if r.matched}
        )

        node: str | None = None
        method = ""
        reason: str | None = None

        if job.destination_table is not None:
            destination = pipeline.resolve_reference(job.destination_table)
            if destination.matched:
                node, method = destination.identity.key, "destination_table"
                for parent in matched_refs:
                    if parent == node:
                        continue
                    edge = edges.setdefault(
                        (parent, node), {"jobs": 0, "declared": parent in upstream.get(node, ())}
                    )
                    edge["jobs"] = int(edge["jobs"]) + 1  # type: ignore[arg-type]
            elif _is_temporary(job.destination_table):
                reason = "temporary_destination"
            elif destination.status == "ambiguous":
                reason = "ambiguous_destination"
            else:
                reason = "unmatched_destination"

        if node is None:
            for key in label_keys:
                value = job.labels.get(key)
                if value:
                    labelled = pipeline.resolve_reference(value)
                    if labelled.matched:
                        node, method, reason = labelled.identity.key, "label", None
                        break

        if node is None and job.destination_table is None:
            outer = [
                n for n in matched_refs
                if not any(n in _ancestors(upstream, other) for other in matched_refs if other != n)
            ]
            if len(outer) == 1:
                node, method = outer[0], "reader"
            elif outer:
                reason = "multiple_readers"
            elif job.referenced_tables:
                reason = "no_matching_references"
            elif job.statement_type == "SCRIPT":
                reason = "script_without_children"
            else:
                reason = "no_tables"

        if node is not None:
            nodes[node] = nodes.get(node, Cost()) + cost
            methods[node][method] += 1
            attributions.append(Attribution(job.job_id, node, method))
        else:
            reason = reason or "no_tables"
            unattributed = unattributed + cost
            reasons[reason] = reasons.get(reason, Cost()) + cost
            attributions.append(Attribution(job.job_id, None, "unattributed", reason))

    win: dict[str, str | None] = {"start": min(times) if times else None, "end": max(times) if times else None}
    if window:
        win.update(window)
    return CostAttribution(
        total, nodes, {k: dict(v) for k, v in methods.items()}, unattributed, reasons,
        edges, attributions, dict(skipped), win, dict(missing_fields),
    )


def build_cost(
    pipeline: "Pipeline",
    jobs: Iterable[ObservedJob],
    *,
    usd_per_tib: float | None = None,
    currency: str = "USD",
    billing_model: str | None = None,
    region: str | None = None,
    label_keys: tuple[str, ...] = DEFAULT_LABEL_KEYS,
    window: Mapping[str, str | None] | None = None,
) -> dict[str, object]:
    """Return the ``/api/cost`` payload for real job history.

    ``measured`` values are billed bytes converted with an explicit
    ``usd_per_tib`` rate. Without a rate no money is invented: values are
    billed bytes (integers), ``currency`` is null and ``unit`` is ``"bytes_billed"``.

    ``basis`` echoes what the figures rest on: the caller's rate, currency,
    ``billing_model`` and ``region`` (``None`` when the caller gave none, never a
    guess), the job locations seen in the history, and, with a rate, what an
    invoice includes that this does not.
    """

    jobs = list(jobs)
    result = attribute_costs(pipeline, jobs, label_keys=label_keys, window=window)

    def value(cost: Cost) -> int | float:
        if usd_per_tib is None:
            return cost.bytes_billed
        return round(cost.bytes_billed / TIB * usd_per_tib, 6)

    attributed = result.attributed
    assert attributed.bytes_billed + result.unattributed.bytes_billed == result.total.bytes_billed
    assert attributed.job_count + result.unattributed.job_count == result.total.job_count
    nodes = [
        {
            "node": key,
            "measured": value(cost),
            "runs": cost.job_count,
            "bytes_processed": cost.bytes_processed,
            "bytes_billed": cost.bytes_billed,
            "slot_ms": cost.slot_ms,
            "source": "measured",
            "methods": result.node_methods.get(key, {}),
        }
        for key, cost in sorted(result.nodes.items(), key=lambda item: (-item[1].bytes_billed, item[0]))
    ]
    basis: dict[str, object] = {
        "measure": "total_bytes_billed",
        "rate_per_tib": usd_per_tib,
        "currency": currency if usd_per_tib is not None else None,
        "billing_model": billing_model,
        "region": region,
        "job_locations": sorted({job.location for job in jobs if job.location}),
        "invoice_exclusions": INVOICE_EXCLUSIONS if usd_per_tib is not None else None,
    }
    return {
        "basis": basis,
        "currency": currency if usd_per_tib is not None else None,
        "unit": "currency" if usd_per_tib is not None else "bytes_billed",
        "window": result.window,
        "totals": {
            "measured": value(result.total),
            "attributed": value(attributed),
            "unattributed": value(result.unattributed),
        },
        "counts": {
            "jobs": result.total.job_count,
            "attributed": attributed.job_count,
            "unattributed": result.unattributed.job_count,
            "excluded": result.skipped,
            "unmeasured": result.skipped.get("unmeasured", 0),
            "missing_fields": result.missing_fields,
        },
        "nodes": nodes,
        "unattributed": [
            {
                "reason": reason,
                "description": REASONS[reason],
                "measured": value(cost),
                "runs": cost.job_count,
                "bytes_billed": cost.bytes_billed,
            }
            for reason, cost in sorted(result.reasons.items())
        ],
        "edges": [
            {
                "upstream": up,
                "downstream": down,
                "jobs": info["jobs"],
                "declared": info["declared"],
                "method": "exercised",
            }
            for (up, down), info in sorted(result.edges.items())
        ],
    }


__all__ = [
    "Attribution",
    "Cost",
    "CostAttribution",
    "INVOICE_EXCLUSIONS",
    "ObservedJob",
    "REASONS",
    "attribute_costs",
    "build_cost",
    "flatten_job_resource",
    "load_jobs",
]
