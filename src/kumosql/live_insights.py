"""The cost and change-report views, built from what the UI server has loaded.

``/api/cost`` and ``/api/changes`` answer from the loaded project, its job
history and (for change reports) a comparison against another git branch. When
an input is missing the payload is an empty state naming what to load. Nothing
here makes up numbers: a figure appears only when the data behind it was
supplied, and anything that cannot be derived is left out of the payload.
"""

from __future__ import annotations

from datetime import datetime
from typing import Iterable

from . import live_graph
from .live_graph import ProjectError, empty_payload, source_info
from .timing import stage

MAX_OPPORTUNITIES = 50


def _filtered(current: dict, scope_name: str | None):
    """The scope plan, the job history it keeps and the model keys it keeps (``None`` = all)."""

    plan = live_graph._plan(scope_name, current["observed_reads"])
    reads = list(current["observed_reads"])
    if plan and plan.jobs is not None:
        from .scopes import job_record

        reads = [row for row in reads if plan.jobs.matches(job_record(row))]
    keep = current["pipeline"].scope_keys(plan.models) if plan and plan.models is not None else None
    return plan, reads, keep


def _window_days(window: dict | None) -> float | None:
    try:
        start = datetime.fromisoformat(str(window["start"]).replace("Z", "+00:00"))
        end = datetime.fromisoformat(str(window["end"]).replace("Z", "+00:00"))
    except (KeyError, TypeError, ValueError):
        return None
    return max((end - start).total_seconds() / 86400, 1.0)


def _model_names(pipeline) -> dict[str, str]:
    """``{name a finding uses: model key}`` for ``key`` and ``schema.name`` spellings."""

    names: dict[str, str] = {}
    for key, model in pipeline.models.items():
        names[key] = key
        target = model.target
        short = ".".join(part for part in (target.schema, target.name) if part)
        if short:
            names.setdefault(short, key)
    return names


def _reach(graph, keys: Iterable[str]) -> int:
    children: dict[str, set[str]] = {}
    for edge in graph.edges:
        children.setdefault(edge.upstream.stable_key, set()).add(edge.downstream.stable_key)
    start = set(keys)
    seen: set[str] = set()
    stack = list(start)
    while stack:
        for child in children.get(stack.pop(), ()):
            if child not in seen and child not in start:
                seen.add(child)
                stack.append(child)
    return len(seen)


def _opportunities(pipeline, reads: list, nodes: dict[str, dict], window: dict | None, keep, unit_value) -> list[dict]:
    """Repeated work found in the models, with the measured cost of the models that repeat it.

    No saving is estimated: that needs a proposed change, which this view does
    not generate, so ``savings`` is ``None`` and the list is ranked by measured cost.
    """

    from .graph import build_query_graph
    from .opportunities import OpportunityInput, rank_opportunities
    from .repeated_work import repeated_work_report

    with stage("repeated work"):
        found = pipeline._remembered(("repeated_work",), lambda: repeated_work_report(pipeline))["opportunities"]
    names = _model_names(pipeline)
    graph = build_query_graph(pipeline, reads) if found else None
    days = _window_days(window)
    items = []
    for item in found:
        keys = sorted({names[m] for m in item["models"] if m in names})
        if keep is not None and not set(keys) & keep:
            continue
        priced = [nodes[key] for key in keys if key in nodes]
        items.append(OpportunityInput(
            id=item["id"],
            title=item["title"],
            measured_cost=unit_value(sum(node["bytes_billed"] for node in priced)) if nodes else None,
            runs=sum(node["runs"] for node in priced) if nodes else None,
            window_days=days if nodes else None,
            downstream_reach=_reach(graph, keys) if graph is not None else 0,
            extra={"repeats": item["repeats"], "occurrences": item["occurrences"], "certainty": item["certainty"],
                   "kind": item["kind"], "consumers": []},
        ))
    ranked = rank_opportunities(items)
    ranked.sort(key=lambda row: (-(row["measured_cost"] or 0), row["rank"]))
    for index, row in enumerate(ranked, start=1):
        row["rank"] = index
    return ranked[:MAX_OPPORTUNITIES]


def cost_payload(scope_name: str | None = None, rate: float | None = None) -> dict:
    """The ``/api/cost`` payload.

    Needs a loaded project. With job history it adds measured cost per asset and
    what could not be attributed; without it, repeated work is still listed (with
    no cost). ``rate`` is a price per TiB billed; without one, values are bytes billed.
    """

    from .cost_rules import rule_catalog
    from .costs import ObservedJob, build_cost
    from .savings import load as load_ledger

    current = live_graph.loaded()
    if current is None:
        return empty_payload("project", "Load a Dataform project to see where the same work repeats.", scope_name)
    waiting = live_graph.pending_payload(current, scope_name)
    if waiting:
        return waiting
    pipeline = current["pipeline"]
    plan, reads, keep = _filtered(current, scope_name)
    if rate is not None and not 0 < rate < 1e6:
        raise ValueError("price per TiB must be a positive number")

    payload: dict = {
        "source": source_info(), "scope": plan.to_json() if plan else None,
        "has_jobs": bool(reads), "needs_jobs": not current["observed_reads"],
        "unit": "bytes_billed", "currency": None, "window": {"start": None, "end": None},
        "totals": None, "nodes": [], "unattributed": [], "counts": None,
    }
    nodes: dict[str, dict] = {}

    def unit_value(billed: int) -> float:
        return round(billed / 1024 ** 4 * rate, 6) if rate is not None else float(billed)

    if reads:
        built = build_cost(pipeline, [ObservedJob.from_record(row) for row in reads], usd_per_tib=rate)
        if keep is not None:
            built["nodes"] = [node for node in built["nodes"] if node["node"] in keep]
        payload.update({key: built[key] for key in ("unit", "currency", "window", "totals", "nodes", "unattributed", "counts")})
        nodes = {node["node"]: node for node in built["nodes"]}
    payload["opportunities"] = _opportunities(pipeline, reads, nodes, payload["window"], keep, unit_value)
    payload["rules"] = rule_catalog()
    ledger = load_ledger().summary(unit=payload["unit"])
    payload["validated"] = ledger
    return payload


# ------------------------------------------------------------------ change reports


def _evidence_coverage(changes: list[dict]) -> dict:
    """Label counts over the changed models (proof, planner and synthetic agreement kept apart)."""

    counts = {"proven": 0, "planner_checked": 0, "unproven": 0, "failed": 0}
    agreed = useful = 0
    for change in changes:
        label = change["verification"]["label"]
        counts[label if label in counts else "unproven"] += 1
        passed = label != "failed" and any(
            check.get("kind") == "synthetic_results" and check.get("outcome") == "passed"
            for check in change["verification"]["checks"])
        agreed += passed
        useful += passed or label == "proven"
    return {"changed": len(changes), **counts, "synthetic_agreed": agreed, "useful_evidence": useful}


def _sources(current: dict) -> list[dict]:
    from .query_sources import ObservedReadsSource, SourceRegistry

    pipeline = current["pipeline"]
    git = current.get("remote")
    sources = [{
        "name": "Dataform project", "kind": "compiled models", "state": "connected", "matched": None,
        "detail": f"{len(pipeline.models)} models from {current['label']}" + ("" if git else " (not loaded from git)"),
    }]
    reads = current["observed_reads"]
    if reads:
        registry = SourceRegistry()
        registry.register(ObservedReadsSource(reads, name="Job history"))
        sources.extend(registry.to_json(pipeline))
    else:
        sources.append({"name": "Job history", "kind": "observed reads", "state": "not_enabled", "matched": None,
                        "detail": "Load a job-history export to compare against what actually ran."})
    return sources


def _proposals(current: dict) -> list[dict]:
    from .shared_logic import propose_shared_logic

    try:
        pipeline = current["pipeline"]
        reads = current["observed_reads"]
        with stage("shared logic proposals"):
            return pipeline._remembered(
                ("shared_logic", id(reads), len(reads)),
                lambda: [p.to_json() for p in propose_shared_logic(pipeline, reads)])
    except Exception:  # noqa: BLE001 - proposals are advisory; the rest of the page must still load
        return []


def changes_payload(scope_name: str | None = None) -> dict:
    """The ``/api/changes`` payload.

    Needs a loaded project. Guided refactors and query sources come from the
    project alone. The change report itself appears after a comparison with
    another branch (``compare_branch``); until then ``report`` is ``None``.
    """

    from .ci_check import build_check

    current = live_graph.loaded()
    if current is None:
        return empty_payload("project", "Load a Dataform git repository to compare branches.", scope_name)
    waiting = live_graph.pending_payload(current, scope_name)
    if waiting:
        return waiting
    plan = live_graph._plan(scope_name, current["observed_reads"])
    report = current.get("report")
    payload: dict = {
        "source": source_info(), "scope": plan.to_json() if plan else None,
        "can_compare": bool(current.get("remote")),
        "remote_branch": (current.get("remote") or {}).get("actual"),
        "report": report, "proposals": _proposals(current), "sources": _sources(current),
    }
    if report is not None:
        changed = [c for c in report["changes"] if c["kind"] != "unchanged"]
        payload["evidence_coverage"] = _evidence_coverage(changed)
        payload["ci"] = build_check(report)
    return payload


def compare_branch(base_branch: object, refresh: bool = False, scope_name: str | None = None) -> dict:
    """Compare the loaded project (head) with another branch of the same git remote.

    The report is kept with the project and returned by ``changes_payload``.
    """

    from .change_report import build_change_report
    from .git_repo import GitRepoError, fetch_project
    from .live_graph import _Checkout, _write_files
    from .pipeline import load_sqlx_project
    from .scopes import get_scope

    current = live_graph.loaded()
    if current is None:
        raise ProjectError(live_graph.NOT_LOADED)
    remote = current.get("remote")
    if not remote:
        raise ProjectError("change reports compare git branches; load the project from a git repository first")
    if not isinstance(base_branch, str) or not base_branch.strip():
        raise ValueError("enter the branch to compare against")
    scope = None
    if scope_name:
        scope = get_scope(scope_name)
        if scope is None:
            raise ValueError(f"no saved scope named {scope_name!r}")
    try:
        base = fetch_project(remote["url"], base_branch, refresh)
        head = fetch_project(remote["url"], remote["branch"], refresh)
    except GitRepoError as exc:
        raise ProjectError(str(exc)) from exc

    from pathlib import Path

    with _Checkout() as base_dir, _Checkout() as head_dir:
        _write_files(base["files"], base_dir)
        _write_files(head["files"], head_dir)
        try:
            base_pipeline = load_sqlx_project(base_dir)
            head_pipeline = load_sqlx_project(head_dir)
        except Exception as exc:  # loader errors are user-facing
            raise ProjectError(str(exc) or "project could not be loaded") from exc
        report = build_change_report(
            base_pipeline, head_pipeline, base_root=Path(base_dir), head_root=Path(head_dir),
            title=f"{base['branch']} → {head['branch']}",
            base_label=f"{base['branch']} @ {base['commit']}", head_label=f"{head['branch']} @ {head['commit']}",
            scope=scope,
        )
    live_graph.set_report(report)
    return changes_payload(scope_name)
