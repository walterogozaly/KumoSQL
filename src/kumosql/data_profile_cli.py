"""``python -m kumosql profile-table``: profile a table's values and save the summary for agents.

BigQuery tables are named ``project.dataset.table`` and run in the billing project (``--project``, else
the one chosen in Settings) under a byte cap. ``--dry-run`` only plans the queries and, for BigQuery,
prints the bytes each would process. DuckDB tables come from a database file (``--duckdb``) or a CSV,
Parquet or JSON file (``--file``).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from . import data_profile, data_profile_store
from .data_profile import ByteCapExceeded, DuckDBExecutor, ProfileError

READERS = {".csv": "read_csv_auto", ".tsv": "read_csv_auto", ".parquet": "read_parquet", ".json": "read_json_auto",
           ".jsonl": "read_json_auto", ".ndjson": "read_json_auto"}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m kumosql profile-table",
        description="Profile a table: row count, and per column nulls, distinct values, min, max, mean, quartiles, "
                    "string lengths and the most common values. Saves the profile where agents can read it.",
    )
    parser.add_argument("table", nargs="?", help="project.dataset.table for BigQuery; a table name for --duckdb; ignored for --file")
    parser.add_argument("--duckdb", type=Path, help="profile a table in this DuckDB database file (opened read-only)")
    parser.add_argument("--file", type=Path, help="profile a CSV, Parquet or JSON file with DuckDB")
    parser.add_argument("--project", help="BigQuery billing project (default: the one chosen in Settings)")
    parser.add_argument("--max-bytes", type=int, help="BigQuery byte cap per query (default: Settings → Scopes)")
    parser.add_argument("--sample-percent", type=float, help="profile a random share of the rows (above 0, at most 100)")
    parser.add_argument("--row-filter", help="one SQL condition applied to the rows first, such as \"status = 'open'\"")
    parser.add_argument("--columns", help="only these columns, comma separated")
    parser.add_argument("--exclude", help="leave out these columns, comma separated")
    parser.add_argument("--top-values", type=int, default=data_profile.DEFAULT_TOP_VALUES,
                        help=f"most common values kept per column, 0 for none (default {data_profile.DEFAULT_TOP_VALUES})")
    parser.add_argument("--no-values", action="store_true",
                        help="leave out the most common values and the min and max of string columns")
    parser.add_argument("--exact", action="store_true", help="count distinct values exactly on BigQuery (default: approximate)")
    parser.add_argument("--dry-run", action="store_true", help="print the queries (and BigQuery's byte estimate) and run nothing")
    parser.add_argument("--format", choices=("summary", "json"), default="summary", help="what to print (default summary, Markdown)")
    parser.add_argument("-o", "--output", type=Path, help="also write the printed text to this file")
    parser.add_argument("--name", help="saved profile name (default: from the table name)")
    parser.add_argument("--no-save", action="store_true", help="do not keep the profile in the data folder")
    return parser


def _executor(args, parser):
    if args.file and args.duckdb:
        parser.error("give --file or --duckdb, not both")
    if not args.file and not args.duckdb:
        return None, args.table
    import duckdb

    if args.file:
        reader = READERS.get(args.file.suffix.lower())
        if reader is None:
            parser.error("--file must be a .csv, .tsv, .parquet, .json, .jsonl or .ndjson file")
        if not args.file.is_file():
            parser.error(f"{args.file} is not a file")
        table = re.sub(r"[^A-Za-z0-9_]+", "_", args.file.stem).strip("_") or "data"
        if table[0].isdigit():
            table = "t_" + table
        connection = duckdb.connect()
        path = str(args.file).replace("'", "''")
        connection.execute(f'CREATE VIEW "{table}" AS SELECT * FROM {reader}(\'{path}\')')
        return DuckDBExecutor(connection), table
    if not args.table:
        parser.error("name the table to profile")
    if not args.duckdb.is_file():
        parser.error(f"{args.duckdb} is not a file")
    return DuckDBExecutor(duckdb.connect(str(args.duckdb), read_only=True)), args.table


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        executor, table = _executor(args, parser)
        if executor is None:
            if not table:
                parser.error("name the table to profile")
            executor = data_profile.BigQueryExecutor(args.project, args.max_bytes)
        split = lambda text: [item.strip() for item in text.split(",") if item.strip()] if text else None
        options = dict(
            include=split(args.columns), exclude=split(args.exclude) or (), sample_percent=args.sample_percent,
            row_filter=args.row_filter, top_values=args.top_values, include_values=not args.no_values,
            approximate=not args.exact,
        )
        if args.dry_run:
            return _dry_run(table, executor, options)
        profile = data_profile.profile_table(table, executor, **options)
        saved = None
        if not args.no_save:
            saved = data_profile_store.save(profile, args.name or data_profile_store.default_name(table))
    except ByteCapExceeded as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 3
    except ProfileError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    text = json.dumps(profile.to_json(), indent=2) + "\n" if args.format == "json" else data_profile_store.to_markdown(profile)
    if args.output:
        args.output.write_text(text, encoding="utf-8")
    sys.stdout.write(text)
    if saved:
        print(f"saved: {saved}", file=sys.stderr)
    return 0


def _dry_run(table: str, executor, options: dict) -> int:
    queries = data_profile.profile_queries(table, executor, **options)
    total = 0
    for number, sql in enumerate(queries, 1):
        estimate = executor.estimate(sql) if hasattr(executor, "estimate") else None
        total += estimate or 0
        size = "" if estimate is None else f" -- estimated bytes processed: {estimate:,}"
        print(f"-- query {number} of {len(queries)}{size}\n{sql};\n")
    if hasattr(executor, "estimate"):
        print(f"-- total estimated bytes processed: {total:,} (cap per query: {executor.max_bytes:,})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
