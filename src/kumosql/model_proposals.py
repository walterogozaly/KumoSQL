"""List "model A can read model B" proposals for a loaded Dataform project, each one proven.

``propose_model_reuse(pipeline)`` asks :func:`kumosql.model_reuse.rewrite_over_model` of every pair of
models whether the query of model A can be answered from the stored rows of model B. The proposer only
proposes: a pair is listed only when the prover proved ``A's query == the replacement with B's query
substituted for the read of B``. Nothing else is ever listed, so a proposal is never a guess.

Each proposal carries the replacement SQL (BigQuery, reading B by its table name, and the same text with
Dataform's ``${ref(...)}`` for B), the strategy that built it, the assumptions the proof rests on and an
estimate of the bytes it would save per run. The estimate uses only metadata supplied with the project:

* ``table_bytes``: the size in bytes of tables (the loaded project carries none of its own), and
* ``jobs``: exported job history (``kumosql.costs.load_jobs``), from which the reader's measured billed
  bytes per run are taken.

With the reader's measured cost and B's size the saving is ``billed bytes per run - size of B`` (the whole
of B is assumed read, the worst case), basis ``estimate``. With sizes only it is the full size of the tables
the reader reads (each reference counted) minus the size of B, also an ``estimate``. ``at_most_bytes`` is what
the reader scans today: the saving cannot exceed it. Anything that is not known is ``None`` (shown as
``unknown``), never a guess. A model that is a view saves nothing, because reading a view runs its query again.

Only proposals the prover could state as a plain replacement are listed. A model is read as its query only
when its stored rows are what that query returns (not incremental, no pre or post operations, no clock or
RAND, no unordered LIMIT), so such a model is neither a reader nor a source. A model that already reads B,
one that B depends on the other way round, and a pair that shares no source table are never offered. A
replacement that keeps other tables of the reader beside B is not listed. Proposals are independent of each
other: ``conflicts`` names the proposals that cannot be applied together (the same reader, or a cycle).

The output order and every figure are deterministic. Nothing here writes a file or calls a cloud service.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Mapping

import sqlglot
from sqlglot import exp

from .ast_utils import binding_cte
from .costs import ObservedJob, attribute_costs
from .model_reuse import ModelReuse, rewrite_over_model
from .pipeline_equivalence import _opaque, nondeterministic, run_dependent, stored_rows_differ
from .prover_schema import ProverSchema, from_pipeline
from .table_roles import _clean_counts

READABLE = ("table", "view")
MODEL_NAME_IN_PROOF = "mv0"  # the name the prover reads B under
MODEL_NAME = "kumosql_model_ref"  # stands for B in the replacement until its table name or ref is written in
UNIT = "bytes_per_run"


class ProposalError(ValueError):
    """The request cannot be answered (an unknown model name)."""


@dataclass(frozen=True)
class Saving:
    """An estimate of bytes saved per run when the reader reads the model instead of what it reads today."""

    bytes_saved: int | None = None  # signed: a negative value says the model is larger than what the reader scans
    at_most_bytes: int | None = None  # what the reader scans today; the saving cannot exceed it
    basis: str = "unknown"  # "estimate" or "unknown"
    method: str = ""
    runs: int | None = None  # the job-history runs behind a measured figure
    reason: str = ""  # why the figure is unknown

    def to_json(self) -> dict:
        return {
            "bytes_saved": self.bytes_saved,
            "at_most_bytes": self.at_most_bytes,
            "basis": self.basis,
            "unit": UNIT,
            "method": self.method or None,
            "runs": self.runs,
            "reason": self.reason or None,
        }


@dataclass
class Proposal:
    reader: str
    model: str
    model_kind: str
    strategy: str
    replacement_sql: str
    replacement_dataform: str
    assumptions: tuple[str, ...]
    saving: Saving
    conflicts: list[str] = field(default_factory=list)

    @property
    def id(self) -> str:
        return f"{self.reader} <- {self.model}"

    def to_json(self) -> dict:
        return {
            "id": self.id,
            "reader": self.reader,
            "model": self.model,
            "model_kind": self.model_kind,
            "strategy": self.strategy,
            "replacement_sql": self.replacement_sql,
            "replacement_sql_dataform": self.replacement_dataform,
            "assumptions": list(self.assumptions),
            "saving": self.saving.to_json(),
            "conflicts": list(self.conflicts),
            "proven": True,
        }


@dataclass
class ProposalReport:
    proposals: list[Proposal]
    pairs_considered: int
    skipped: dict[str, int]  # why pairs were not proposed, by reason (counts only)
    models_read: list[str]  # the models that could be a reader or a source

    def to_json(self) -> dict:
        return {
            "proposals": [p.to_json() for p in self.proposals],
            "counts": {
                "models_read": len(self.models_read),
                "pairs_considered": self.pairs_considered,
                "proposed": len(self.proposals),
                "skipped": dict(sorted(self.skipped.items())),
            },
            "note": "Every proposal was proven by the prover; a pair that is not listed is not shown to be impossible.",
        }


# ------------------------------------------------------------------ eligibility


def _unreadable(model) -> str:
    """Why a model cannot be a reader or a source of a proposal, or ``""``."""

    if model.kind not in READABLE or not model.is_query:
        return f"is a {model.kind}"
    if model.disabled:
        return "is disabled"
    if _opaque(model):
        return "could not be read as a plain query"
    if model.operations_sql:
        return "runs pre or post operations"
    why = stored_rows_differ(model)
    if why:
        return why
    try:
        tree = sqlglot.parse_one(model.sql, read="bigquery")
    except sqlglot.errors.SqlglotError:
        return "could not be parsed"
    return run_dependent(tree) or nondeterministic(tree)


def _bare(mapping: Mapping[str, object]) -> dict:
    """The entries of a prover schema keyed by a bare table name (the spelling ``rewrite_over_model`` reads)."""

    return {name: value for name, value in mapping.items() if "." not in name}


def _depends_on(graph: Mapping[str, Iterable[str]], start: str, goal: str) -> bool:
    """``start`` depends on ``goal`` (directly or through other models) in ``graph``."""

    seen: set[str] = set()
    todo = [start]
    while todo:
        key = todo.pop()
        if key == goal:
            return True
        if key in seen:
            continue
        seen.add(key)
        todo.extend(graph.get(key, ()))
    return False


# ------------------------------------------------------------------ the replacement


def _replacement(reuse: ModelReuse, names: tuple[str, ...], model, ref: str) -> tuple[str, str] | str:
    """``(BigQuery SQL, the same with a Dataform ref)`` for a proven replacement, or why it cannot be offered.

    The prover compares output columns by position, so the outputs are renamed to the reader's own names (a
    reader's consumers read them by name); a replacement that keeps other tables or is not one SELECT is refused.
    """

    try:
        tree = sqlglot.parse_one(reuse.sql or "", read="postgres")
    except sqlglot.errors.SqlglotError:
        return "the replacement could not be read back"
    if not isinstance(tree, exp.Select):
        return "the replacement is not a single SELECT"
    tables = [t for t in tree.find_all(exp.Table)]
    if not tables or any(t.name != MODEL_NAME_IN_PROOF or t.args.get("db") for t in tables):
        return "the replacement keeps reads of other tables"
    if any(isinstance(e, exp.Star) or (isinstance(e, exp.Column) and isinstance(e.this, exp.Star)) for e in tree.expressions):
        return "the replacement selects *"
    if len(tree.expressions) != len(names):
        return "the replacement has a different number of columns"
    renamed: set[str] = set()
    outputs = []
    for item, name in zip(tree.expressions, names):
        old = item.alias_or_name
        if old and old != name:
            renamed.add(old.lower())
        outputs.append(exp.alias_(item.unalias() if isinstance(item, exp.Alias) else item, exp.to_identifier(name, quoted=False)))
    if renamed:
        clauses = [tree.args.get(k) for k in ("group", "having", "qualify", "order")]
        used = {c.name.lower() for part in clauses if part is not None for c in part.find_all(exp.Column) if not c.table}
        if used & renamed:
            return "renaming the output columns would change how the replacement orders or groups"
    tree.set("expressions", outputs)
    for table in tables:
        # the columns read it as ``mv0.x``, so the table keeps that alias unless the replacement gave it another
        table.replace(exp.to_table(MODEL_NAME, dialect="bigquery").as_(table.alias or MODEL_NAME_IN_PROOF))
    text = tree.sql(dialect="bigquery")
    return text.replace(MODEL_NAME, _quoted(model)), text.replace(MODEL_NAME, ref)


def _quoted(model) -> str:
    return model.target.sql()


def _ref_of(pipeline, model) -> str:
    """The Dataform expression that reads the model: ``${ref("dataset", "name")}`` as its config named it."""

    database, schema, name = model.logical if len(model.logical) == 3 else (model.target.database, model.target.schema, model.target.name)
    parts = [name] if not schema else [schema, name]
    if database and database != pipeline.default_project:
        parts.insert(0, database)
    return "${ref(" + ", ".join('"' + p.replace('"', '\\"') + '"' for p in parts) + ")}"


# ------------------------------------------------------------------ the estimate


def _full_scan(pipeline, reader, sizes: Mapping[str, int]) -> tuple[int | None, str]:
    """Bytes the reader scans if it reads every referenced table whole (each reference counted), or why not."""

    try:
        tree = sqlglot.parse_one(reader.sql, read="bigquery")
    except sqlglot.errors.SqlglotError:
        return None, "the reader could not be parsed"
    total = 0
    for table in tree.find_all(exp.Table):
        if binding_cte(table) is not None:
            continue
        key = pipeline.resolve(table)
        if key is None:
            return None, "the reader reads a table the project does not know"
        if key not in sizes:
            return None, f"the size of {key} is not known"
        total += sizes[key]
    return total, ""


def _saving(pipeline, reader_key: str, model_key: str, sizes: Mapping[str, int], measured: Mapping[str, tuple[int, int]]) -> Saving:
    model = pipeline.models[model_key]
    reader = pipeline.models[reader_key]
    if model.kind == "view":
        return Saving(reason=f"{model_key} is a view: reading it runs its query again, so no scan is saved")
    size = sizes.get(model_key)
    if reader_key in measured:
        billed, runs = measured[reader_key]
        per_run, method = billed // runs, "mean billed bytes per run of the reader (job history) minus the size of the model's table"
    else:
        per_run, why = _full_scan(pipeline, reader, sizes)
        runs, method = None, "full size of the tables the reader reads (each reference counted) minus the size of the model's table"
        if per_run is None:
            return Saving(reason=why if size is None else f"the reader's cost is not known: {why}")
    if size is None:
        return Saving(at_most_bytes=per_run, runs=runs, reason=f"the size of {model_key} is not known")
    return Saving(per_run - size, per_run, "estimate", method, runs)


def _measured(pipeline, jobs: Iterable[ObservedJob] | None) -> dict[str, tuple[int, int]]:
    """Per model: ``(billed bytes, runs)`` from job history, for models with at least one measured run."""

    if jobs is None:
        return {}
    nodes = attribute_costs(pipeline, jobs).nodes
    return {key: (cost.bytes_billed, cost.job_count) for key, cost in nodes.items() if cost.job_count > 0}


# ------------------------------------------------------------------ the proposer


def propose_model_reuse(
    pipeline,
    *,
    schema: ProverSchema | None = None,
    table_bytes: Mapping[str, int] | None = None,
    jobs: Iterable[ObservedJob] | None = None,
    readers: Iterable[str] | None = None,
    models: Iterable[str] | None = None,
    timeout_ms: int = 5000,
) -> ProposalReport:
    """Proven "``reader`` can read ``model``" proposals for the models of ``pipeline``.

    ``readers`` and ``models`` limit the models tried as reader and as source (names as for the other
    commands: a full key or a unique trailing part). ``schema`` defaults to what the project declares
    (:func:`kumosql.prover_schema.from_pipeline`); pass the one with BigQuery metadata to prove more.
    """

    facts = schema if schema is not None else from_pipeline(pipeline)
    columns, constraints, types = _bare(facts.columns), _bare(facts.constraints), _bare(facts.types)
    sizes = _clean_counts(pipeline, table_bytes)
    measured = _measured(pipeline, jobs)
    reader_filter = _select(pipeline, readers)
    model_filter = _select(pipeline, models)

    skipped: dict[str, int] = {}

    def skip(reason: str) -> None:
        skipped[reason] = skipped.get(reason, 0) + 1

    eligible = [key for key in sorted(pipeline.models) if not _unreadable(pipeline.models[key])]
    upstream = pipeline.upstream
    reads = pipeline.table_reads()
    proposals: list[Proposal] = []
    considered = 0
    names_of = {key: _output_names(pipeline, key) for key in eligible}
    for reader_key in eligible:
        if reader_filter is not None and reader_key not in reader_filter:
            continue
        reader = pipeline.models[reader_key]
        names = names_of[reader_key]
        for model_key in eligible:
            if model_key == reader_key or (model_filter is not None and model_key not in model_filter):
                continue
            considered += 1
            if model_key in upstream.get(reader_key, ()):
                skip("the reader already reads the model")
                continue
            if _depends_on(upstream, model_key, reader_key):
                skip("the model depends on the reader (reading it would make a cycle)")
                continue
            if not (reads.get(reader_key, frozenset()) & reads.get(model_key, frozenset())):
                skip("no source table in common")
                continue
            if not names:
                skip("the reader's output columns are not known")
                continue
            model = pipeline.models[model_key]
            reuse = rewrite_over_model(
                reader.sql,
                model.sql,
                schema=columns,
                constraints=constraints or None,
                types=types or None,
                model_name=MODEL_NAME_IN_PROOF,
                dialect="bigquery",
                timeout_ms=timeout_ms,
            )
            if not reuse.rewritten:
                skip(f"not proven ({reuse.status})")
                continue
            built = _replacement(reuse, names, model, _ref_of(pipeline, model))
            if isinstance(built, str):
                skip(f"proven but not offered: {built}")
                continue
            proposals.append(
                Proposal(
                    reader_key, model_key, model.kind, reuse.strategy or "", built[0], built[1], tuple(reuse.assumptions),
                    _saving(pipeline, reader_key, model_key, sizes, measured),
                )
            )
    _order(proposals)
    _mark_conflicts(proposals, upstream)
    return ProposalReport(proposals, considered, skipped, eligible)


def _output_names(pipeline, key: str) -> tuple[str, ...]:
    names = tuple(pipeline.output_columns(key))
    if names and len({n.lower() for n in names}) == len(names):
        return names
    return ()


def _select(pipeline, names: Iterable[str] | None) -> set[str] | None:
    if names is None:
        return None
    from .pipeline_equivalence import _resolve

    found = set()
    for name in names:
        try:
            found.add(_resolve(pipeline, name))
        except ValueError as error:
            raise ProposalError(str(error)) from error
    return found


def _order(proposals: list[Proposal]) -> None:
    """Largest known saving first, then those with none known; ties by name."""

    proposals.sort(key=lambda p: (p.saving.bytes_saved is None, -(p.saving.bytes_saved or 0), p.reader, p.model))


def _mark_conflicts(proposals: list[Proposal], upstream: Mapping[str, Iterable[str]]) -> None:
    """Name the proposals that cannot be applied together: one reader twice, or two edits that make a cycle."""

    graph = {key: set(parents) for key, parents in upstream.items()}
    for first in proposals:
        for second in proposals:
            if first is second:
                continue
            if first.reader == second.reader:
                first.conflicts.append(second.id)
                continue
            with_first = {key: set(parents) for key, parents in graph.items()}
            with_first.setdefault(first.reader, set()).add(first.model)
            if _depends_on(with_first, second.model, second.reader):
                first.conflicts.append(second.id)


# ------------------------------------------------------------------ text, CLI


def format_report(report: ProposalReport) -> str:
    """A plain-text list of the proposals with their replacement SQL."""

    lines = [
        f"{len(report.proposals)} proposal(s) from {report.pairs_considered} pair(s) of {len(report.models_read)} readable model(s); "
        "every one is proven."
    ]
    for proposal in report.proposals:
        saving = proposal.saving
        shown = "unknown" if saving.bytes_saved is None else f"{saving.bytes_saved:,} bytes per run ({saving.basis})"
        lines.append("")
        lines.append(f"{proposal.reader} can read {proposal.model} ({proposal.model_kind}, {proposal.strategy}): saves {shown}")
        if saving.reason:
            lines.append(f"  note: {saving.reason}")
        if saving.at_most_bytes is not None:
            lines.append(f"  at most {saving.at_most_bytes:,} bytes per run (what the reader scans today)")
        if proposal.conflicts:
            lines.append("  cannot be applied with: " + "; ".join(proposal.conflicts))
        for assumption in proposal.assumptions:
            lines.append(f"  assumes: {assumption}")
        lines.append("  replacement:")
        lines.extend("    " + line for line in proposal.replacement_sql.splitlines())
    if report.skipped:
        lines.append("")
        lines.append("not proposed: " + "; ".join(f"{count} x {reason}" for reason, count in sorted(report.skipped.items())))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """``python -m kumosql model-proposals DIR [--table-bytes FILE] [--jobs FILE] [--json]``"""

    import argparse
    import json
    import sys
    from pathlib import Path

    from .cli import _load_pipeline
    from .costs import load_jobs
    from .resilience import PipelineLoadError, parse_json_or_raise

    parser = argparse.ArgumentParser(
        prog="python -m kumosql model-proposals",
        description="List 'model A can read model B' proposals for a Dataform project, each proven by the prover, "
        "with the replacement SQL and an estimate of bytes saved. Read-only: no file is written, no cloud call is made.",
    )
    parser.add_argument("project", type=Path, help="Dataform project folder, or a compiled graph JSON file")
    parser.add_argument("--source-schema", type=Path, help="JSON mapping of source tables to columns")
    parser.add_argument("--table-bytes", type=Path, help="JSON mapping of table names to their size in bytes (for the estimate)")
    parser.add_argument("--jobs", type=Path, help="exported query job history (JSON, JSON lines or CSV), for the reader's measured cost")
    parser.add_argument("--reader", action="append", help="only try this model as the reader (repeatable)")
    parser.add_argument("--model", action="append", help="only try this model as the source (repeatable)")
    parser.add_argument("--timeout-ms", type=int, default=5000)
    parser.add_argument("--json", action="store_true", help="print the full report as JSON")
    args = parser.parse_args(argv)
    try:
        pipeline = _load_pipeline(args.project, args.source_schema)
        sizes = parse_json_or_raise(args.table_bytes, "table size file") if args.table_bytes else None
        if sizes is not None and not isinstance(sizes, dict):
            raise PipelineLoadError("table size file must be a JSON object")
        jobs = load_jobs(args.jobs) if args.jobs else None
        report = propose_model_reuse(
            pipeline, table_bytes=sizes, jobs=jobs, readers=args.reader, models=args.model, timeout_ms=args.timeout_ms,
        )
    except (PipelineLoadError, ProposalError, ValueError, OSError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    if args.json:
        json.dump(report.to_json(), sys.stdout, indent=2)
        sys.stdout.write("\n")
    else:
        print(format_report(report))
    return 0
