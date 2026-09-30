"""Command-line interface for the SQL transformer."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from .lift_subqueries import lift_subqueries
from .equivalence import prove_equivalent
from .rewrite import apply_rules, available_rules
from .dryrun import check_rewrite, dry_run
from .fingerprint import Location, compare_snapshots, plan_output_comparison, summarize_comparison
from .scopes import Scope, delete_scope, get_scope, list_scopes, parse_scope, save_scope
from .pipeline import load_compiled_graph, load_sqlx_project


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Lift BigQuery FROM/JOIN subqueries into CTEs")
    parser.add_argument("input", type=Path, help="Input SQL file")
    parser.add_argument("-o", "--output", type=Path, help="Output SQL file; stdout if omitted")
    parser.add_argument("--report", action="store_true", help="Print transformation summary to stderr")
    args = parser.parse_args(argv)

    result = lift_subqueries(args.input.read_text(encoding="utf-8"))
    if args.output:
        args.output.write_text(result.sql + ("\n" if result.sql else ""), encoding="utf-8")
    else:
        sys.stdout.write(result.sql)
        if result.sql:
            sys.stdout.write("\n")

    if args.report:
        print(
            f"statements={result.statements} transformed={result.transformed_statements} "
            f"lifted={result.lifted_subqueries} remaining={result.remaining_inline_subqueries} "
            f"diagnostics={len(result.diagnostics)}",
            file=sys.stderr,
        )
        for diagnostic in result.diagnostics:
            print(diagnostic, file=sys.stderr)

    return 0 if result.success else 2


def prove_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Conservatively prove two BigQuery queries equivalent")
    parser.add_argument("left", type=Path, help="Left SQL file")
    parser.add_argument("right", type=Path, help="Right SQL file")
    parser.add_argument(
        "--respect-row-order",
        action="store_true",
        help="Require explicit and identical result ordering",
    )
    parser.add_argument("--verifier-sql", type=Path, help="Write generated bag-verifier SQL")
    args = parser.parse_args(argv)

    result = prove_equivalent(
        args.left.read_text(encoding="utf-8"),
        args.right.read_text(encoding="utf-8"),
        ignore_row_order=not args.respect_row_order,
    )
    print(result.status.value)
    print(result.reason)
    for diagnostic in result.diagnostics:
        print(f"diagnostic: {diagnostic}")
    if args.verifier_sql and result.verifier_sql:
        args.verifier_sql.write_text(result.verifier_sql + "\n", encoding="utf-8")
    return 0 if result.proven else 2


def rewrite_main(argv: list[str] | None = None) -> int:
    rules = available_rules()
    parser = argparse.ArgumentParser(
        description="Apply registered rewrite rules and verify each output for equivalence",
        epilog="Rules: " + "; ".join(f"{name}: {rule.summary}" for name, rule in rules.items()),
    )
    parser.add_argument("input", type=Path, help="Input SQL or SQLX file")
    parser.add_argument(
        "-r",
        "--rule",
        action="append",
        choices=sorted(rules),
        required=True,
        help="Rule to apply; repeat to apply several in order",
    )
    parser.add_argument("-o", "--output", type=Path, help="Output file; stdout if omitted")
    parser.add_argument(
        "--allow-unproven",
        action="store_true",
        help="Exit 0 even when equivalence could not be proven",
    )
    args = parser.parse_args(argv)

    result = apply_rules(args.rule, args.input.read_text(encoding="utf-8"))
    if args.output:
        args.output.write_text(result.sql + ("\n" if result.sql else ""), encoding="utf-8")
    else:
        sys.stdout.write(result.sql)
        if result.sql:
            sys.stdout.write("\n")

    for step in result.steps:
        print(
            f"{step.rule}: changes={step.changes} ok={step.rule_success} "
            f"verification={step.verification.status.value}",
            file=sys.stderr,
        )
        for diagnostic in step.diagnostics:
            print(f"  {diagnostic}", file=sys.stderr)
        for detail in step.verification.details:
            print(f"  unproven: {detail}", file=sys.stderr)
    print(f"verification={result.verification.status.value}", file=sys.stderr)

    if not all(step.rule_success for step in result.steps):
        return 2
    if not result.verification.trusted and not args.allow_unproven:
        return 3
    return 0


def pipeline_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Analyse a whole Dataform or SQL pipeline: graph, dead columns, duplicate logic"
    )
    parser.add_argument("root", type=Path, help="Dataform project root, folder of .sql files, or compiled graph JSON")
    parser.add_argument(
        "--source-schema",
        type=Path,
        help='JSON mapping of source tables to columns, e.g. {"p.d.t": {"id": "INT64"}}',
    )
    parser.add_argument("--min-nodes", type=int, default=12, help="Smallest SELECT subtree to report as a duplicate")
    parser.add_argument(
        "--similarity",
        type=float,
        default=0.7,
        help="Smallest tree similarity (0-1) for SELECTs to be reported as near-duplicates",
    )
    parser.add_argument("--scope", help="Limit the report to a saved scope (see kumosql-scopes)")
    parser.add_argument("-o", "--output", type=Path, help="Write the JSON report here; stdout if omitted")
    args = parser.parse_args(argv)

    scope = None
    if args.scope:
        scope = get_scope(args.scope)
        if scope is None:
            parser.error(f"no saved scope named {args.scope!r}")
    pipeline = _load_pipeline(args.root, args.source_schema)
    try:
        data = pipeline.report(min_nodes=args.min_nodes, similarity=args.similarity, scope=scope)
    except ValueError as exc:
        parser.error(str(exc))
    if scope is not None and not data["models"]:
        print(f"warning: scope {scope.name!r} matches no models", file=sys.stderr)
    report = json.dumps(data, indent=2)
    if args.output:
        args.output.write_text(report + "\n", encoding="utf-8")
    else:
        print(report)
    return 0


def scopes_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Manage saved scopes: named labels of authors, projects or any other field"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list", help="Show saved scopes")
    add = commands.add_parser("add", help="Create or replace a scope")
    add.add_argument("name", help='Scope name, e.g. "My Team"')
    add.add_argument(
        "--field",
        action="append",
        nargs="+",
        metavar=("FIELD", "VALUE"),
        required=True,
        help="A field and its values, e.g. --field author ana@co.com bo@co.com (end a value with * for a prefix)",
    )
    remove = commands.add_parser("remove", help="Delete a scope")
    remove.add_argument("name")
    args = parser.parse_args(argv)

    if args.command == "list":
        for scope in list_scopes():
            print(json.dumps(scope.to_json()))
    elif args.command == "add":
        fields: dict[str, list[str]] = {}
        for field, *values in args.field:
            fields.setdefault(field, []).extend(values)
        try:
            save_scope(parse_scope({"name": args.name, "fields": fields}))
        except ValueError as exc:
            parser.error(str(exc))
    elif not delete_scope(args.name):
        parser.error(f"no saved scope named {args.name!r}")
    return 0


def dry_run_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Dry-run a BigQuery query, or check that a rewrite plans with the same schema"
    )
    parser.add_argument("sql", type=Path, help="SQL file (the original, when --rewritten is given)")
    parser.add_argument("--rewritten", type=Path, help="Rewritten SQL file to compare against")
    parser.add_argument("--project", required=True, help="Project to run the dry run in")
    parser.add_argument("--location", help="Job location, e.g. US or EU")
    args = parser.parse_args(argv)

    sql = args.sql.read_text(encoding="utf-8")
    if args.rewritten:
        check = check_rewrite(
            sql, args.rewritten.read_text(encoding="utf-8"), args.project, location=args.location
        )
        print("ok" if check.ok else "rejected")
        print(check.reason)
        for difference in check.schema_differences:
            print(f"schema: {difference}")
        if check.bytes_delta is not None:
            print(f"bytes_delta={check.bytes_delta}")
        return 0 if check.ok else 2

    result = dry_run(sql, args.project, location=args.location)
    if not result.ok:
        print(f"error: {result.error_message}")
        return 2
    print(f"bytes={result.total_bytes_processed}")
    for field in result.schema:
        print(field.describe())
    return 0


def _load_pipeline(root: Path, source_schema_path: Path | None):
    source_schema = (
        json.loads(source_schema_path.read_text(encoding="utf-8")) if source_schema_path else None
    )
    if root.is_file():
        return load_compiled_graph(root, source_schema=source_schema)
    return load_sqlx_project(root, source_schema=source_schema)


def compare_outputs_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Generate BigQuery SQL comparing every model's output before and after a refactor"
    )
    commands = parser.add_subparsers(dest="command", required=True)

    def planned(name: str, help_text: str) -> argparse.ArgumentParser:
        command = commands.add_parser(name, help=help_text)
        command.add_argument("root", type=Path, help="Pipeline before the refactor (project, folder or graph JSON)")
        command.add_argument("--after-root", type=Path, help="Pipeline after the refactor, if its code differs")
        command.add_argument("--source-schema", type=Path, help="JSON mapping of source tables to columns")
        for side in ("before", "after"):
            command.add_argument(f"--{side}-project", help=f"Project of the {side} tables")
            command.add_argument(f"--{side}-dataset", help=f"Dataset of the {side} tables")
            command.add_argument(f"--{side}-dataset-suffix", default="", help="Appended to each dataset")
            command.add_argument(f"--{side}-table-prefix", default="", help="Prepended to each table name")
            command.add_argument(f"--{side}-table-suffix", default="", help="Appended to each table name")
        command.add_argument("--models", nargs="+", help="Only these models")
        command.add_argument("--ignore-column", action="append", default=[], help="Skip this column everywhere")
        command.add_argument(
            "--normalize",
            action="append",
            default=[],
            metavar="COLUMN=TEMPLATE",
            help='SQL applied to a column on both sides, e.g. "amount=ROUND({col}, 6)"',
        )
        command.add_argument(
            "--where", action="append", default=[], metavar="MODEL=PREDICATE", help="Filter a model's scans"
        )
        return command

    planned("compare", "one query comparing every model's fingerprints")
    fingerprint = planned("fingerprint", "one query fingerprinting one side, to save and compare later")
    fingerprint.add_argument("--side", choices=["before", "after"], default="before")
    drilldown = planned("drilldown", "the rows that differ for one model")
    drilldown.add_argument("--model", required=True)
    drilldown.add_argument("--keys", help="Comma-separated key columns")
    drilldown.add_argument("--columns", help="Comma-separated columns to compare (default: all)")
    drilldown.add_argument("--limit", type=int, default=100)
    summarize = commands.add_parser(
        "summarize", help="summarise query results exported as JSON (e.g. bq query --format=json)"
    )
    summarize.add_argument("results", type=Path, help="Results of the compare query, or the before fingerprints")
    summarize.add_argument("after", type=Path, nargs="?", help="The after fingerprints, when comparing snapshots")
    args = parser.parse_args(argv)

    if args.command == "summarize":
        first = json.loads(args.results.read_text(encoding="utf-8"))
        if args.after:
            diffs = compare_snapshots(first, json.loads(args.after.read_text(encoding="utf-8")))
        else:
            diffs = summarize_comparison(first)
        for diff in diffs:
            print(f"{diff.model}: {diff.status}" + (f" ({diff.note})" if diff.note else ""))
        return 0 if all(diff.matches for diff in diffs) else 1

    def location(side: str) -> Location:
        return Location(
            getattr(args, f"{side}_project"),
            getattr(args, f"{side}_dataset"),
            getattr(args, f"{side}_dataset_suffix"),
            getattr(args, f"{side}_table_prefix"),
            getattr(args, f"{side}_table_suffix"),
        )

    def pairs(values: list[str], flag: str) -> dict[str, str]:
        result = {}
        for value in values:
            name, sep, rest = value.partition("=")
            if not sep:
                parser.error(f"{flag} expects NAME=VALUE, got {value!r}")
            result[name] = rest
        return result

    before = _load_pipeline(args.root, args.source_schema)
    after = _load_pipeline(args.after_root, args.source_schema) if args.after_root else None
    try:
        plan = plan_output_comparison(
            before,
            after,
            before_location=location("before"),
            after_location=location("after"),
            models=args.models,
            normalize=pairs(args.normalize, "--normalize"),
            ignore_columns=args.ignore_column,
            where=pairs(args.where, "--where"),
        )
        if args.command == "compare":
            sql = plan.compare_sql()
        elif args.command == "fingerprint":
            sql = plan.fingerprint_sql(args.side)
        else:
            sql = plan.drilldown_sql(
                args.model,
                keys=_split(args.keys),
                columns=_split(args.columns) if args.columns else None,
                limit=args.limit,
            )
    except (KeyError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    for diagnostic in plan.diagnostics:
        print(f"diagnostic: {diagnostic.model}: {diagnostic.code}: {diagnostic.message}", file=sys.stderr)
    print(sql)
    return 0


def _split(value: str | None) -> list[str]:
    return [part.strip() for part in (value or "").split(",") if part.strip()]


if __name__ == "__main__":
    raise SystemExit(main())
