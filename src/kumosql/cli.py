"""Command-line interface for the SQL transformer."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from .lift_subqueries import lift_subqueries
from .equivalence import prove_equivalent
from .rewrite import apply_rules, attach_planner_check, available_rules
from .sqlx import looks_like_sqlx
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
    if args.report:
        print(
            f"statements={result.statements} transformed={result.transformed_statements} "
            f"lifted={result.lifted_subqueries} remaining={result.remaining_inline_subqueries} "
            f"diagnostics={len(result.diagnostics)}",
            file=sys.stderr,
        )
    if args.report or not result.success:
        for diagnostic in result.diagnostics:
            print(f"diagnostic: {diagnostic.code}: {diagnostic.message}", file=sys.stderr)

    if not result.success:
        return 2

    if args.output:
        args.output.write_text(result.sql + ("\n" if result.sql else ""), encoding="utf-8")
    else:
        sys.stdout.write(result.sql)
        if result.sql:
            sys.stdout.write("\n")

    return 0


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
        help="Exit 0 after writing output when it is unproven or only planner checked",
    )
    parser.add_argument(
        "--planner-project",
        help="Opt in to a BigQuery planning and output-schema check (credentials required)",
    )
    parser.add_argument("--planner-location", help="BigQuery job location for the opt-in planner check")
    parser.add_argument(
        "--planner-compiled-original",
        type=Path,
        help="Compiled SQL for the original SQLX input",
    )
    parser.add_argument(
        "--planner-compiled-rewritten",
        type=Path,
        help="Compiled SQL for the rewritten SQLX output",
    )
    args = parser.parse_args(argv)

    if (args.planner_location or args.planner_compiled_original or args.planner_compiled_rewritten) and not args.planner_project:
        parser.error("--planner-location and compiled SQL inputs require --planner-project")
    if bool(args.planner_compiled_original) != bool(args.planner_compiled_rewritten):
        parser.error("provide both --planner-compiled-original and --planner-compiled-rewritten")

    result = apply_rules(args.rule, args.input.read_text(encoding="utf-8"))
    if args.planner_project:
        result = attach_planner_check(
            result,
            args.planner_project,
            location=args.planner_location,
            compiled_original_sql=(
                args.planner_compiled_original.read_text(encoding="utf-8")
                if args.planner_compiled_original
                else None
            ),
            compiled_rewritten_sql=(
                args.planner_compiled_rewritten.read_text(encoding="utf-8")
                if args.planner_compiled_rewritten
                else None
            ),
        )

    for step in result.steps:
        print(
            f"{step.rule}: changes={step.changes} "
            f"verification={step.verification.status.value}",
            file=sys.stderr,
        )
        for diagnostic in step.diagnostics:
            print(f"  diagnostic: {diagnostic.code}: {diagnostic.message}", file=sys.stderr)
        for detail in step.verification.details:
            print(f"  detail: {detail}", file=sys.stderr)
    print(f"verification={result.verification.status.value}", file=sys.stderr)
    for check in result.verification.checks:
        print(
            f"  check {check.kind}={check.outcome}: {check.detail}",
            file=sys.stderr,
        )
        for key, value in check.evidence:
            if key == "scope":
                continue
            if key == "estimated_bytes_delta":
                if value is not None:
                    print(f"    estimated bytes delta: {value} bytes (estimate)", file=sys.stderr)
            elif key == "schema_differences":
                for difference in value:
                    print(f"    schema difference: {difference}", file=sys.stderr)
            else:
                print(f"    {key}={value}", file=sys.stderr)

    if not all(step.rule_success for step in result.steps):
        return 2

    if args.output:
        args.output.write_text(result.sql + ("\n" if result.sql else ""), encoding="utf-8")
    else:
        sys.stdout.write(result.sql)
        if result.sql:
            sys.stdout.write("\n")

    if not result.verification.trusted:
        if args.allow_unproven:
            print(
                "warning: output is not proven equivalent; accepted by --allow-unproven",
                file=sys.stderr,
            )
            return 0
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
        description="Check BigQuery query planning and output schemas without comparing query results"
    )
    parser.add_argument("sql", type=Path, help="SQL file (the original, when --rewritten is given)")
    parser.add_argument("--rewritten", type=Path, help="Rewritten SQL file to compare against")
    parser.add_argument("--project", required=True, help="Project to run the dry run in")
    parser.add_argument("--location", help="Job location, e.g. US or EU")
    args = parser.parse_args(argv)

    sql = args.sql.read_text(encoding="utf-8")
    if args.rewritten:
        rewritten_sql = args.rewritten.read_text(encoding="utf-8")
        if looks_like_sqlx(sql) or looks_like_sqlx(rewritten_sql):
            print("planner_check=not_run: compile both SQLX inputs before planning")
            print("scope=planning and output-schema comparison only; results were not compared")
            return 2
        check = check_rewrite(
            sql, rewritten_sql, args.project, location=args.location
        )
        print("planner_check=passed" if check.planned_same_schema else "planner_check=failed")
        print(check.reason)
        print(f"original_planned={check.original_planned}")
        print(f"rewritten_planned={check.rewritten_planned}")
        print(f"schema_matches={check.schema_matches}")
        print("scope=planning and output-schema comparison only; results were not compared")
        for difference in check.schema_differences:
            print(f"schema: {difference}")
        if check.estimated_bytes_delta is not None:
            print(f"estimated_bytes_delta={check.estimated_bytes_delta} (estimate)")
        return 0 if check.planned_same_schema else 2

    if looks_like_sqlx(sql):
        print("query_plan=not_run: compile SQLX before planning")
        return 2
    result = dry_run(sql, args.project, location=args.location)
    if not result.ok:
        print(f"query_plan=failed: {result.error_message}")
        return 2
    print("query_plan=passed")
    print("scope=planning only; query results were not compared")
    print(f"estimated_bytes={result.total_bytes_processed} (estimate)")
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
