"""Semantic change report: behavior, cost and blast radius side by side.

Two project snapshots (base and head) are compared model by model. Each
changed model carries

* ``verification``: the existing evidence label from :func:`verify_rewrite`,
  computed on the raw file text where it exists,
* ``cost``: caller-supplied figures only; ``unavailable`` when none are given,
* ``consumers``: transitive readers taken from the query graph, with
  ``complete`` false whenever the graph has gaps.

The shape matches ``report`` in ``docs/ui-roadmap.md``. Nothing here needs
credentials or network access.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping

from .graph import build_query_graph
from .pipeline import Model, Pipeline, load_compiled_graph, load_sqlx_project
from .rewrite import verify_rewrite

COST_BASES = ("measured", "estimate", "upper_bound")
_PARSE_CODES = {"parse_error", "no_query", "qualify_error", "sqlx_parse_error", "unsupported_ref"}


def _display(model: Model) -> str:
    t = model.target
    return ".".join(p for p in (t.schema, t.name) if p) or model.key or (model.path or "")


def _raw_text(root: Path | None, model: Model) -> str:
    """Raw file text when available, so SQLX block checks apply."""

    if root is not None and model.path:
        try:
            return (root / model.path).read_text(encoding="utf-8")
        except OSError:
            pass
    return model.sql


def normalize_cost(entry: Mapping[str, object] | None) -> dict[str, object]:
    """Validate one caller-supplied cost entry; absent input reads as unknown."""

    if not entry:
        return {"basis": "unavailable"}
    basis = entry.get("basis")
    if basis not in COST_BASES:
        raise ValueError(f"cost basis must be one of {', '.join(COST_BASES)}, got {basis!r}")
    out: dict[str, object] = {"basis": basis}
    for key in ("before", "after"):
        value = entry.get(key)
        if value is not None:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"cost {key} must be a number")
            out[key] = value
    if "before" not in out and "after" not in out:
        return {"basis": "unavailable"}
    return out


class _Consumers:
    """Reader closure over one pipeline's query graph."""

    def __init__(self, pipeline: Pipeline):
        graph = build_query_graph(pipeline)
        self.names: dict[str, str] = {}
        self.ids: dict[str, str] = {}
        for key, model in pipeline.models.items():
            if model.identity is not None:
                self.ids[key] = model.identity.stable_key
                self.names[model.identity.stable_key] = _display(model)
        self.readers: dict[str, set[str]] = {}
        self.low: set[str] = set()
        for edge in graph.edges:
            up, down = edge.upstream.stable_key, edge.downstream.stable_key
            self.readers.setdefault(up, set()).add(down)
            if edge.confidence == "low":
                self.low.add(down)
        self.gap = (
            pipeline._analyse().blind
            or any(d.code in _PARSE_CODES for d in pipeline.all_diagnostics())
            or bool(graph.unresolved_observation_count or graph.unattributed_observation_count)
        )

    def of(self, key: str) -> dict[str, object]:
        start = self.ids.get(key)
        seen: set[str] = set()
        stack = [start] if start else []
        while stack:
            for nxt in self.readers.get(stack.pop(), ()):
                if nxt not in seen:
                    seen.add(nxt)
                    stack.append(nxt)
        complete = start is not None and not self.gap and not (seen & self.low)
        return {"models": sorted(self.names.get(s, s) for s in seen), "complete": complete}


def load_snapshot(root: Path, source_schema=None) -> tuple[Pipeline, Path | None]:
    """Load a project folder or compiled graph file; also return the raw-text root."""

    if root.is_file():
        return load_compiled_graph(root, source_schema=source_schema), None
    return load_sqlx_project(root, source_schema=source_schema), root


def build_change_report(
    base: Pipeline,
    head: Pipeline,
    *,
    base_root: Path | None = None,
    head_root: Path | None = None,
    costs: Mapping[str, Mapping[str, object]] | None = None,
    title: str = "Change report",
    base_label: str = "base",
    head_label: str = "head",
    generated_at: str | None = None,
) -> dict[str, object]:
    """Compare two pipelines and return the documented ``report`` payload.

    ``costs`` maps a model's ``schema.name`` to ``{basis, before?, after?}``.
    Models are matched by target key, so a rename shows as a removal plus an
    addition. Per-model failures become diagnostics, not a failed report.
    """

    costs = costs or {}
    diagnostics: list[dict[str, str]] = []
    changes: list[dict[str, object]] = []
    cache: dict[str, _Consumers] = {}

    def consumers(side: str) -> _Consumers:
        if side not in cache:
            cache[side] = _Consumers(head if side == "head" else base)
        return cache[side]

    for key in sorted(set(base.models) | set(head.models)):
        old, new = base.models.get(key), head.models.get(key)
        name = _display(new or old)
        kind = "modified"
        try:
            if old and new:
                before, after = _raw_text(base_root, old), _raw_text(head_root, new)
                if before == after:
                    continue
                if old.is_query and new.is_query:
                    v = verify_rewrite(before, after)
                    verification = {
                        "label": v.status.value,
                        "reason": v.reason,
                        "checks": [c.to_json() for c in v.checks],
                    }
                else:
                    verification = {"label": "unproven", "reason": "not analyzed: not a query model", "checks": []}
                readers = consumers("head").of(key)
            elif new:
                kind = "added"
                verification = {"label": "unproven", "reason": "new model; no baseline to compare", "checks": []}
                readers = consumers("head").of(key)
            else:
                kind = "removed"
                verification = {"label": "unproven", "reason": "model removed; its readers may break", "checks": []}
                readers = consumers("base").of(key)
            cost = normalize_cost(costs.get(name))
        except Exception as exc:  # one model must not sink the report
            diagnostics.append({"asset": name, "message": f"{type(exc).__name__}: {exc}"})
            verification = {"label": "failed", "reason": "analysis of this model failed", "checks": []}
            readers = {"models": [], "complete": False}
            cost = {"basis": "unavailable"}
        changes.append(
            {"model": name, "kind": kind, "verification": verification, "cost": cost, "consumers": readers}
        )

    for side, pipe in (("base", base), ("head", head)):
        for d in pipe.all_diagnostics():
            if d.code in _PARSE_CODES:
                diagnostics.append({"asset": d.model, "message": f"{side}: {d.message}"})
    return {
        "title": title,
        "base": base_label,
        "head": head_label,
        "generated_at": generated_at or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "changes": changes,
        "diagnostics": diagnostics,
    }


def change_report_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Report behavior, cost and consumer changes between two project snapshots"
    )
    parser.add_argument("base", type=Path, help="Base project root or compiled graph JSON")
    parser.add_argument("head", type=Path, help="Head project root or compiled graph JSON")
    parser.add_argument(
        "--cost", type=Path,
        help='JSON mapping "schema.name" to {"basis", "before", "after"}; omitted means unknown',
    )
    parser.add_argument("--title", default="Change report")
    parser.add_argument("-o", "--output", type=Path, help="Write JSON here; stdout if omitted")
    args = parser.parse_args(argv)
    try:
        costs = json.loads(args.cost.read_text(encoding="utf-8")) if args.cost else {}
        for entry in costs.values():
            normalize_cost(entry)
        base, base_root = load_snapshot(args.base)
        head, head_root = load_snapshot(args.head)
    except (OSError, ValueError, AttributeError) as exc:
        parser.error(str(exc))
    report = build_change_report(
        base, head, base_root=base_root, head_root=head_root, costs=costs,
        title=args.title, base_label=str(args.base), head_label=str(args.head),
    )
    text = json.dumps({"report": report}, indent=2)
    if args.output:
        args.output.write_text(text + "\n", encoding="utf-8")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(change_report_main())
