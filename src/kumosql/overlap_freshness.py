"""Freshness context for stored relations that overlap another model.

This is advisory context only: it never changes the overlap or equivalence
classification. It uses saved Dataform schedules, loaded job history and cached
BigQuery table metadata; missing evidence remains unknown.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable, Mapping

from . import bigquery_catalog, workflow_configs
from .pipeline import Pipeline
from .pipeline_types import Model, Target

_STORED_KINDS = frozenset({"table", "incremental"})
_MAX_MATCHES = 8


def annotate(
    section: dict,
    pipeline: Pipeline,
    node: str,
    jobs: Iterable[object],
    remote: Mapping[str, object] | None,
) -> dict:
    """Add freshness notes to same-meaning overlaps involving stored models."""

    if section.get("status") != "ok":
        return section

    writes = _last_job_writes(pipeline, jobs)
    schedules, schedules_known = _active_schedules(pipeline, remote)
    metadata: dict[str, dict] = {}
    selected = pipeline.models.get(node)

    for match in section.get("matches", ())[:_MAX_MATCHES]:
        if match.get("kind") != "same_meaning":
            continue
        related = pipeline.models.get(match.get("key", ""))
        participants = []
        for role, key, model in (("selected", node, selected), ("overlap", match.get("key", ""), related)):
            if model is None or model.kind not in _STORED_KINDS:
                continue
            info = _relation_info(key, model, writes, schedules, schedules_known, metadata)
            participants.append({"side": role, **info})
        if participants:
            statuses = {item["status"] for item in participants}
            overall = "may_be_stale" if "may_be_stale" in statuses else (
                "scheduled" if statuses == {"scheduled"} else "unknown"
            )
            match["freshness"] = {
                "status": overall,
                "summary": " ".join(item["summary"] for item in participants),
                "relations": participants,
            }
    return section


def _active_schedules(pipeline: Pipeline, remote: Mapping[str, object] | None) -> tuple[dict[str, list[dict]], bool]:
    url = remote.get("url") if isinstance(remote, Mapping) else None
    if not isinstance(url, str) or not url:
        return {}, False
    try:
        rows = workflow_configs.cached_rows(url)
        if rows is None:
            return {}, False
        mapped = workflow_configs.scheduled_models(pipeline, rows)
        return {key.casefold(): items for key, items in mapped.items()}, True
    except Exception:  # noqa: BLE001 - schedule evidence is optional
        return {}, False


def _last_job_writes(pipeline: Pipeline, jobs: Iterable[object]) -> dict[str, str]:
    from .costs import ObservedJob

    latest: dict[str, tuple[float, str]] = {}
    for record in jobs:
        if not isinstance(record, Mapping):
            continue
        try:
            job = ObservedJob.from_record(record)
        except (TypeError, ValueError):
            continue
        if job.dry_run or job.error_result or not job.creation_time:
            continue
        destination = job.destination_table or record.get("destination")
        if not destination:
            continue
        try:
            resolved = pipeline.resolve_reference(destination)
            key = resolved.identity.key if resolved.matched else ""
        except Exception:  # noqa: BLE001 - an unresolvable job is not write evidence
            continue
        if not key:
            continue
        when = _parse_time(job.creation_time)
        if when is None:
            continue
        folded = key.casefold()
        if folded not in latest or when > latest[folded][0]:
            latest[folded] = (when, job.creation_time)
    return {key: value[1] for key, value in latest.items()}


def _relation_info(
    key: str,
    model: Model,
    writes: Mapping[str, str],
    schedules: Mapping[str, list[dict]],
    schedules_known: bool,
    metadata: dict[str, dict],
) -> dict:
    folded = key.casefold()
    target = model.target
    catalog = _catalog_info(target, metadata)
    job_write = writes.get(folded)
    scheduled = schedules.get(folded, [])
    schedule_state = "active" if scheduled else "none" if schedules_known else "unknown"
    schedule_text = (
        "Active Dataform schedule(s) recorded: " + ", ".join(
            f"{item.get('cron') or 'cadence unknown'}"
            + (f" ({item['time_zone']})" if item.get("time_zone") else "")
            for item in scheduled
        ) + "."
        if scheduled
        else "No active Dataform schedule was found; another scheduler may still refresh it."
        if schedules_known
        else "Dataform schedule status is unknown."
    )
    catalog_text = (
        f"BigQuery catalog last modified: {catalog['last_modified_at']}."
        if catalog.get("last_modified_at")
        else "BigQuery catalog modification time is unavailable."
    )
    job_text = f"Latest destination write in loaded job history: {job_write}." if job_write else "No destination write for this relation appears in the loaded job history."

    if not job_write:
        status = "unknown"
        summary = f"Freshness is unknown. {job_text} {catalog_text} {schedule_text}"
    elif schedules_known and not scheduled:
        status = "may_be_stale"
        summary = f"May be stale. {job_text} {catalog_text} {schedule_text}"
    elif scheduled:
        status = "scheduled"
        summary = f"A Dataform refresh schedule is recorded. {job_text} {catalog_text}"
    else:
        status = "unknown"
        summary = f"Freshness is unknown. {job_text} {catalog_text} {schedule_text}"

    return {
        "kind": model.kind,
        "status": status,
        "summary": summary,
        "last_job_write_at": job_write,
        "last_modified_at": catalog.get("last_modified_at"),
        "catalog_checked_at": catalog.get("checked_at"),
        "catalog_refreshing": bool(catalog.get("refreshing")),
        "schedule_state": schedule_state,
        "schedules": [
            {"cron": item.get("cron"), "time_zone": item.get("time_zone")}
            for item in scheduled
        ],
    }


def _catalog_info(target: Target, memo: dict[str, dict]) -> dict:
    if not (target.database and target.schema and target.name):
        return {}
    key = bigquery_catalog.table_last_modified_key(target.database, target.schema, target.name)
    if key in memo:
        return memo[key]
    try:
        answer = bigquery_catalog.cached(
            key,
            lambda: {"lastModifiedTime": bigquery_catalog.get_table_last_modified(
                target.database, target.schema, target.name
            )},
        )
    except Exception:  # noqa: BLE001 - catalog access is optional metadata
        memo[key] = {}
        return memo[key]
    data = answer.get("data") if isinstance(answer, Mapping) else None
    raw = data.get("lastModifiedTime") if isinstance(data, Mapping) else None
    memo[key] = {
        "last_modified_at": _format_time(raw, milliseconds=True),
        "checked_at": _format_time(answer.get("fetchedAt")) if isinstance(answer, Mapping) else None,
        "refreshing": bool(answer.get("refreshing")) if isinstance(answer, Mapping) else False,
    }
    return memo[key]


def _parse_time(value: object) -> float | None:
    formatted = _format_time(value)
    if not formatted:
        return None
    try:
        return datetime.fromisoformat(formatted.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _format_time(value: object, *, milliseconds: bool = False) -> str | None:
    if value in (None, ""):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        if isinstance(value, str):
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                return value
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        return None
    seconds = number / 1000 if milliseconds or number > 100_000_000_000 else number
    try:
        return datetime.fromtimestamp(seconds, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    except (OverflowError, OSError, ValueError):
        return None
