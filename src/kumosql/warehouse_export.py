"""Read-only export of the job history and table sizes ``python -m kumosql advise`` reads.

``python -m kumosql export-warehouse --project P --region us --jobs-out jobs.json --sizes-out sizes.json``
writes two local JSON files from the warehouse's own metadata:

* **jobs**: completed query jobs of the last ``--days`` days from
  ``region-R.INFORMATION_SCHEMA.JOBS_BY_PROJECT`` (the fields
  :class:`kumosql.costs.ObservedJob` reads);
* **sizes**: rows and logical bytes per table from
  ``region-R.INFORMATION_SCHEMA.TABLE_STORAGE``, and bytes per column from free
  dry runs: one ``SELECT * FROM t`` for the column names and types, then one
  ``SELECT col FROM t`` per column (a dry run reads no data and costs nothing).

What it will not do. It never writes to the warehouse: every statement is checked
to be one read-only SELECT before it is sent. The only statements that bill are
the two INFORMATION_SCHEMA reads; each is dry-run first and refused when its
estimate is over ``--max-bytes-billed`` (and carries that cap as BigQuery's
``maximumBytesBilled``). The project is always given with ``--project`` (the project
that is billed and whose metadata is read); nothing is discovered from the
environment. ``--dry-run`` prints the SQL and the plan and sends nothing, and needs
no credentials.

The jobs file holds query text and user emails from your warehouse. Keep it private;
``--omit-query-text`` and ``--omit-user-email`` leave those fields out.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable
from urllib.parse import quote, urlencode

from . import dryrun, scope_queries
from .scope_queries import QueryError

DEFAULT_DAYS = 14
DEFAULT_MAX_JOBS = 100_000
DEFAULT_MAX_TABLES = 200
DEFAULT_MAX_COLUMNS = 100
_REGION = re.compile(r"^[a-z][a-z0-9]*(-[a-z0-9]+)*$")
_PROJECT = re.compile(r"^[a-z][a-z0-9\-:.]{3,}$")
_NAME = re.compile(r"^[A-Za-z0-9_\-]+$")
_POLL_SECONDS = 120

Post = Callable[[str, dict, bytes], tuple[int, dict]]
Get = Callable[[str, dict], tuple[int, dict]]
Planner = Callable[[str], "dryrun.DryRunResult"]


# ----------------------------------------------------------------------- SQL


def region_qualifier(region: str) -> str:
    """``us`` or ``region-us`` as the ``region-us`` INFORMATION_SCHEMA qualifier."""

    name = region.strip().lower()
    name = name if name.startswith("region-") else f"region-{name}"
    if not _REGION.match(name):
        raise ValueError(f"not a BigQuery region name: {region!r} (for example us, eu, us-east1)")
    return name


def _literal(value: str) -> str:
    if not _NAME.match(value) and not _PROJECT.match(value):
        raise ValueError(f"unsafe name in a query: {value!r}")
    return "'" + value + "'"


def check_project(project: str) -> str:
    if not _PROJECT.match(project or ""):
        raise ValueError(f"not a project id: {project!r}")
    return project


def jobs_sql(project: str, region: str, *, days: int = DEFAULT_DAYS, max_jobs: int = DEFAULT_MAX_JOBS,
             query_text: bool = True, user_email: bool = True) -> str:
    """The INFORMATION_SCHEMA read for completed query jobs; times are text, tables are JSON text."""

    if not 1 <= days <= 180:
        raise ValueError("--days must be between 1 and 180")
    if not 1 <= max_jobs <= 1_000_000:
        raise ValueError("--max-jobs must be between 1 and 1,000,000")
    query = "query" if query_text else "CAST(NULL AS STRING) AS query"
    email = "user_email" if user_email else "CAST(NULL AS STRING) AS user_email"
    return f"""SELECT
  job_id,
  FORMAT_TIMESTAMP('%Y-%m-%dT%H:%M:%SZ', creation_time) AS creation_time,
  project_id,
  {email},
  statement_type,
  TO_JSON_STRING(destination_table) AS destination_table,
  TO_JSON_STRING(referenced_tables) AS referenced_tables,
  total_bytes_processed,
  total_bytes_billed,
  total_slot_ms,
  cache_hit,
  parent_job_id,
  {query}
FROM `{region_qualifier(region)}`.INFORMATION_SCHEMA.JOBS_BY_PROJECT
WHERE project_id = {_literal(check_project(project))}
  AND job_type = 'QUERY'
  AND state = 'DONE'
  AND error_result IS NULL
  AND creation_time >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL {int(days)} DAY)
ORDER BY creation_time DESC
LIMIT {int(max_jobs)}"""


def storage_sql(project: str, region: str, *, datasets: tuple[str, ...] = (), tables: tuple[str, ...] = ()) -> str:
    """The INFORMATION_SCHEMA read for table sizes, optionally limited to datasets or ``dataset.table`` names."""

    where = [f"project_id = {_literal(check_project(project))}", "deleted = FALSE"]
    if datasets:
        where.append("table_schema IN (" + ", ".join(_literal(d) for d in datasets) + ")")
    if tables:
        pairs = []
        for table in tables:
            dataset, _, name = table.partition(".")
            if not dataset or not name or "." in name:
                raise ValueError(f"--table must be dataset.table, got {table!r}")
            pairs.append(f"(table_schema = {_literal(dataset)} AND table_name = {_literal(name)})")
        where.append("(" + " OR ".join(pairs) + ")")
    return f"""SELECT
  table_schema,
  table_name,
  total_rows,
  total_logical_bytes
FROM `{region_qualifier(region)}`.INFORMATION_SCHEMA.TABLE_STORAGE
WHERE {' AND '.join(where)}
ORDER BY table_schema, table_name"""


def column_probe_sql(project: str, dataset: str, table: str, column: str | None = None) -> str:
    """``SELECT * FROM t`` (names and types) or ``SELECT col FROM t`` (that column's bytes); dry run only."""

    path = f"`{check_project(project)}.{_check_name(dataset)}.{_check_name(table)}`"
    if column is None:
        return f"SELECT * FROM {path}"
    if "`" in column or "\\" in column or not column:
        raise ValueError(f"unsafe column name: {column!r}")
    return f"SELECT `{column}` FROM {path}"


def _check_name(value: str) -> str:
    if not _NAME.match(value or ""):
        raise ValueError(f"unsafe dataset or table name: {value!r}")
    return value


# --------------------------------------------------------------- running reads


@dataclass
class Rows:
    columns: list[str]
    rows: list[dict]
    estimated_bytes: int | None = None
    bytes_billed: int | None = None


def _cell(value: object, kind: str) -> object:
    if value is None:
        return None
    if kind in ("INTEGER", "INT64"):
        return int(value)
    if kind in ("FLOAT", "FLOAT64"):
        return float(value)
    if kind in ("BOOLEAN", "BOOL"):
        return str(value).lower() == "true"
    return value


def read_rows(
    sql: str,
    project: str,
    *,
    location: str | None = None,
    max_bytes: int = scope_queries.DEFAULT_MAX_BYTES_BILLED,
    token: str | None = None,
    transport: dryrun.Transport | None = None,
    post: Post | None = None,
    get: Get | None = None,
) -> Rows:
    """Run one read-only INFORMATION_SCHEMA SELECT: dry run, refuse over the cap, then run with the cap."""

    scope_queries.validate_query(sql)
    token = token or dryrun.access_token()
    plan = dryrun.dry_run(sql, project, location=location, token=token, transport=transport)
    if not plan.ok:
        raise QueryError(f"BigQuery rejected the query: {plan.error_message}")
    estimate = plan.total_bytes_processed
    if estimate is not None and estimate > max_bytes:
        raise QueryError(
            f"the query would process about {scope_queries._size(estimate)}, over the {scope_queries._size(max_bytes)} cap; "
            "narrow it (fewer --days, --dataset or --table) or raise --max-bytes-billed"
        )
    post = post or scope_queries._post
    get = get or scope_queries._get
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    body: dict = {
        "query": sql,
        "useLegacySql": False,
        "useQueryCache": True,
        "maximumBytesBilled": str(max_bytes),
        "maxResults": 10000,
        "timeoutMs": 30000,
        "labels": {"kumosql": "warehouse_export"},
    }
    if location:
        body["location"] = location
    base = f"{scope_queries._API}/projects/{quote(project, safe='')}"
    status, payload = post(f"{base}/queries", headers, json.dumps(body).encode("utf-8"))
    if status >= 400 or "error" in payload:
        raise scope_queries._api_error(status, payload)
    job = payload.get("jobReference", {})
    where = {"location": job["location"]} if job.get("location") else {}
    deadline = time.time() + _POLL_SECONDS
    while not payload.get("jobComplete", True):
        if time.time() > deadline:
            raise QueryError("the query did not finish in time; try again with a narrower query")
        params = {"timeoutMs": "30000", **where}
        status, payload = get(f"{base}/queries/{quote(job.get('jobId', ''), safe='')}?{urlencode(params)}", headers)
        if status >= 400 or "error" in payload:
            raise scope_queries._api_error(status, payload)
    fields = payload.get("schema", {}).get("fields", [])
    names = [f.get("name", "") for f in fields]
    kinds = [f.get("type", "STRING") for f in fields]
    rows: list[dict] = []
    while True:
        for raw in payload.get("rows", []):
            cells = raw.get("f", [])
            rows.append({name: _cell(cells[i].get("v") if i < len(cells) else None, kinds[i]) for i, name in enumerate(names)})
        token_page = payload.get("pageToken")
        if not token_page:
            break
        params = {"pageToken": token_page, "maxResults": "10000", **where}
        status, payload = get(f"{base}/queries/{quote(job.get('jobId', ''), safe='')}?{urlencode(params)}", headers)
        if status >= 400 or "error" in payload:
            raise scope_queries._api_error(status, payload)
    billed = payload.get("totalBytesBilled")
    return Rows(names, rows, estimate, int(billed) if billed is not None else None)


# ------------------------------------------------------------------ the export


def job_records(rows: list[dict]) -> list[dict]:
    """Rows of the jobs read as job-history records: JSON text becomes JSON, empty text is dropped."""

    out = []
    for row in rows:
        record = {k: v for k, v in row.items() if v is not None}
        for key in ("destination_table", "referenced_tables"):
            text = record.get(key)
            if isinstance(text, str):
                try:
                    value = json.loads(text)
                except ValueError:
                    value = None
                if value in (None, [], {}):
                    record.pop(key)
                else:
                    record[key] = value
        out.append(record)
    return out


@dataclass
class SizeReport:
    sizes: dict[str, dict] = field(default_factory=dict)
    skipped: list[str] = field(default_factory=list)
    dry_runs: int = 0


def _top_level_columns(schema: tuple["dryrun.Field", ...]) -> list[tuple[str, str]]:
    return [(f.name, f.type) for f in schema if f.name]


def table_sizes(
    project: str,
    storage: list[dict],
    plan: Planner | None,
    *,
    max_columns: int = DEFAULT_MAX_COLUMNS,
) -> SizeReport:
    """Sizes of each stored table; ``plan(sql)`` dry-runs one probe (``None`` skips per-column bytes).

    A column whose probe fails (for example a table that requires a partition filter) is left out of
    ``columns`` and listed under ``skipped``; its bytes are then estimated from its type, never guessed here.
    """

    report = SizeReport()
    for row in storage:
        dataset, name = str(row["table_schema"]), str(row["table_name"])
        key = f"{project}.{dataset}.{name}".lower()
        entry: dict = {
            "rows": int(row.get("total_rows") or 0),
            "bytes": int(row["total_logical_bytes"]) if row.get("total_logical_bytes") is not None else None,
        }
        if entry["bytes"] is None:
            del entry["bytes"]
        try:
            probe = column_probe_sql(project, dataset, name) if plan is not None else None
        except ValueError as exc:
            probe = None
            report.skipped.append(f"{key}: columns unknown ({exc})")
        if plan is not None and probe is not None:
            schema = plan(probe)
            report.dry_runs += 1
            if not schema.ok or not schema.schema_observed:
                report.skipped.append(f"{key}: columns unknown ({schema.error_message or 'no schema returned'})")
            else:
                columns = _top_level_columns(schema.schema)
                entry["types"] = {c.lower(): t for c, t in columns}
                if len(columns) > max_columns:
                    report.skipped.append(f"{key}: {len(columns)} columns, per-column bytes only for the first {max_columns}")
                measured: dict[str, int] = {}
                for column, _ in columns[:max_columns]:
                    result = plan(column_probe_sql(project, dataset, name, column))
                    report.dry_runs += 1
                    if result.ok and result.total_bytes_processed is not None:
                        measured[column.lower()] = int(result.total_bytes_processed)
                    else:
                        report.skipped.append(f"{key}.{column}: bytes unknown ({result.error_message or 'no estimate'})")
                if measured:
                    entry["columns"] = measured
        report.sizes[key] = entry
    return report


def plan_description(args: argparse.Namespace) -> list[str]:
    both = not (args.jobs_out or args.sizes_out)  # nothing chosen yet: show both reads
    lines = [f"-- jobs ({'written to ' + str(args.jobs_out) if args.jobs_out else 'not requested' if not both else 'give --jobs-out to write it'})"]
    if args.jobs_out or both:
        lines.append(jobs_sql(args.project, args.region, days=args.days, max_jobs=args.max_jobs,
                              query_text=not args.omit_query_text, user_email=not args.omit_user_email))
    lines.append(f"-- table sizes ({'written to ' + str(args.sizes_out) if args.sizes_out else 'not requested' if not both else 'give --sizes-out to write it'})")
    if args.sizes_out or both:
        lines.append(storage_sql(args.project, args.region, datasets=tuple(args.dataset), tables=tuple(args.table)))
        if args.skip_columns:
            lines.append("-- per-column bytes: skipped (--skip-columns)")
        else:
            lines += [
                "-- per-column bytes: free BigQuery dry runs, one per table and column, nothing is read or billed:",
                "--   " + column_probe_sql(args.project, "DATASET", "TABLE"),
                "--   " + column_probe_sql(args.project, "DATASET", "TABLE", "COLUMN"),
            ]
    return lines


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m kumosql export-warehouse",
        description="Read-only export of job history and table sizes from BigQuery INFORMATION_SCHEMA, "
        "for `python -m kumosql advise`. Nothing is written to the warehouse.",
    )
    parser.add_argument("--project", required=True, help="Project to bill and whose metadata is read (never discovered)")
    parser.add_argument("--region", required=True, help="INFORMATION_SCHEMA region, for example us, eu or us-east1")
    parser.add_argument("--location", help="Job location for the reads (default: the region's multi-region or location)")
    parser.add_argument("--jobs-out", type=Path, help="Write the job history here (JSON)")
    parser.add_argument("--sizes-out", type=Path, help="Write the table sizes here (JSON)")
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS, help=f"Days of job history (default {DEFAULT_DAYS})")
    parser.add_argument("--max-jobs", type=int, default=DEFAULT_MAX_JOBS, help=f"Newest jobs to keep (default {DEFAULT_MAX_JOBS})")
    parser.add_argument("--dataset", action="append", default=[], help="Only tables of this dataset (repeatable)")
    parser.add_argument("--table", action="append", default=[], help="Only this dataset.table (repeatable)")
    parser.add_argument("--max-tables", type=int, default=DEFAULT_MAX_TABLES,
                        help=f"Refuse rather than dry-run more tables than this (default {DEFAULT_MAX_TABLES})")
    parser.add_argument("--max-columns", type=int, default=DEFAULT_MAX_COLUMNS,
                        help=f"Per-column bytes for at most this many columns of a table (default {DEFAULT_MAX_COLUMNS})")
    parser.add_argument("--skip-columns", action="store_true", help="Table sizes only: no per-column dry runs")
    parser.add_argument("--max-bytes-billed", type=int, default=scope_queries.DEFAULT_MAX_BYTES_BILLED,
                        help="Refuse an INFORMATION_SCHEMA read estimated over this many bytes (default 1 GiB)")
    parser.add_argument("--omit-query-text", action="store_true", help="Leave query text out of the jobs file")
    parser.add_argument("--omit-user-email", action="store_true", help="Leave user emails out of the jobs file")
    parser.add_argument("--overwrite", action="store_true", help="Replace an output file that already exists")
    parser.add_argument("--dry-run", action="store_true", help="Print the SQL and the plan; send nothing, need no credentials")
    return parser


def _write(path: Path, data: object, overwrite: bool) -> None:
    if path.exists() and not overwrite:
        raise QueryError(f"{path} exists; use --overwrite to replace it")
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def run(
    args: argparse.Namespace,
    *,
    token: str | None = None,
    transport: dryrun.Transport | None = None,
    post: Post | None = None,
    get: Get | None = None,
    out=None,
    err=None,
) -> int:
    """The export for parsed arguments; ``transport``/``post``/``get``/``token`` replace the network in tests."""

    out = out or sys.stdout
    err = err or sys.stderr
    if not (args.jobs_out or args.sizes_out) and not args.dry_run:
        raise QueryError("give --jobs-out, --sizes-out or both")
    location = args.location
    if args.dry_run:
        print("\n".join(plan_description(args)), file=out)
        print("-- dry run: nothing was sent, no credentials were read", file=out)
        return 0
    for path in (args.jobs_out, args.sizes_out):
        if path and path.exists() and not args.overwrite:
            raise QueryError(f"{path} exists; use --overwrite to replace it")
    token = token or dryrun.access_token()
    reads = dict(location=location, max_bytes=args.max_bytes_billed, token=token, transport=transport, post=post, get=get)
    if args.jobs_out:
        sql = jobs_sql(args.project, args.region, days=args.days, max_jobs=args.max_jobs,
                       query_text=not args.omit_query_text, user_email=not args.omit_user_email)
        result = read_rows(sql, args.project, **reads)
        records = job_records(result.rows)
        _write(args.jobs_out, records, args.overwrite)
        print(f"jobs: {len(records)} written to {args.jobs_out} (estimate {result.estimated_bytes} bytes, billed {result.bytes_billed})", file=err)
        if len(records) >= args.max_jobs:
            print(f"warning: the {args.max_jobs}-job limit was reached; older jobs are missing (raise --max-jobs or lower --days)", file=err)
    if args.sizes_out:
        sql = storage_sql(args.project, args.region, datasets=tuple(args.dataset), tables=tuple(args.table))
        result = read_rows(sql, args.project, **reads)
        if len(result.rows) > args.max_tables and not args.skip_columns:
            raise QueryError(
                f"{len(result.rows)} tables match; per-column dry runs would send about {len(result.rows) * 8}. "
                "Narrow with --dataset/--table, raise --max-tables, or use --skip-columns"
            )

        def plan(probe: str) -> "dryrun.DryRunResult":
            return dryrun.dry_run(probe, args.project, location=location, token=token, transport=transport)

        report = table_sizes(args.project, result.rows, None if args.skip_columns else plan, max_columns=args.max_columns)
        _write(args.sizes_out, report.sizes, args.overwrite)
        print(f"sizes: {len(report.sizes)} tables written to {args.sizes_out} ({report.dry_runs} free dry runs)", file=err)
        for line in report.skipped:
            print(f"skipped: {line}", file=err)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        for name, value in (("--days", args.days), ("--max-jobs", args.max_jobs), ("--max-tables", args.max_tables),
                            ("--max-columns", args.max_columns), ("--max-bytes-billed", args.max_bytes_billed)):
            if value < 1:
                parser.error(f"{name} must be at least 1")
        region_qualifier(args.region)
        check_project(args.project)
        return run(args)
    except (QueryError, ValueError, RuntimeError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
