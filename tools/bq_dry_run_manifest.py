"""Dry-run every case of the BigQuery syntax manifest, to prove each fixture is valid GoogleSQL.

A dry run validates a statement without executing or billing it
(``configuration.dryRun``), so this is free. Results are written to
``tests/fixtures/bq_syntax/dry_run.json``; cases that name objects that do not
exist (a model, a connection, a bucket) are expected to fail on that and are
recorded with the error so the reason is visible.

Credentials: ``BQ_ACCESS_TOKEN`` (for example
``$env:BQ_ACCESS_TOKEN = gcloud auth print-access-token``), a service account, or
application-default credentials.

    python tools/bq_dry_run_manifest.py --project kumosql
    python tools/bq_dry_run_manifest.py --project kumosql --only query/qualify,ddl/create_view
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import bq_syntax_coverage as coverage  # noqa: E402
from kumosql.dryrun import access_token, dry_run  # noqa: E402

# Statements that change access, billing or organization settings are never submitted,
# even as dry runs: they are not worth any risk and say nothing about KumoSQL.
SKIP_PREFIXES = ("dcl/", "ddl/create_capacity", "ddl/create_reservation", "ddl/create_assignment",
                 "ddl/alter_organization", "ddl/alter_project", "ddl/alter_reservation")


def sql_for(case: dict) -> tuple[str | None, str]:
    """The SQL to submit, or None with the reason it is not submitted."""

    if case["id"].startswith(SKIP_PREFIXES):
        return None, "administrative statement, never submitted"
    text = coverage.case_text(case)
    if case["kind"] == "sql":
        return text, ""
    files = dict(coverage.PREAMBLE)
    files["definitions/case.sqlx"] = text
    pipeline = coverage._load(files)
    models = [m for m in pipeline.models.values() if m.path == "definitions/case.sqlx"]
    if not models:
        return None, "declaration only; no SQL"
    model = models[0]
    if model.kind in ("test", "incremental") or model.masked_expressions or not model.sql.strip():
        return None, "needs the Dataform compiler (JavaScript interpolation, incremental or test)"
    return model.sql, ""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--project", default="kumosql", help="project the dry runs are billed to (nothing is billed)")
    parser.add_argument("--location", default="US")
    parser.add_argument("--only", help="comma-separated case ids")
    parser.add_argument("--output", type=Path, default=coverage.FIXTURES / "dry_run.json")
    args = parser.parse_args(argv)

    token = access_token()
    previous = json.loads(args.output.read_text()) if args.output.is_file() else {}
    wanted = set(args.only.split(",")) if args.only else None
    results = dict(previous)
    for case in coverage.load_manifest():
        if wanted and case["id"] not in wanted:
            continue
        sql, reason = sql_for(case)
        if sql is None:
            results[case["id"]] = {"status": "not_run", "message": reason}
            continue
        outcome = dry_run(sql, args.project, location=args.location, token=token)
        if outcome.ok:
            results[case["id"]] = {"status": "ok", "bytes": outcome.total_bytes_processed}
        else:
            results[case["id"]] = {"status": "error", "reason": outcome.error_reason, "message": (outcome.error_message or "")[:300]}
        print(f"{results[case['id']]['status']:7} {case['id']}", flush=True)
    args.output.write_text(json.dumps(dict(sorted(results.items())), indent=1) + "\n")
    counts: dict[str, int] = {}
    for value in results.values():
        counts[value["status"]] = counts.get(value["status"], 0) + 1
    print(counts)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
