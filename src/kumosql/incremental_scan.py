"""Check every incremental model of a Dataform project against its full refresh.

``python -m kumosql incremental-report PROJECT`` finds the ``type: "incremental"``
actions under ``definitions/``, rebuilds each one's source tables from the
columns its query reads, and asks :func:`kumosql.incremental.check_incremental`
about three contracts:

* ``append_only``: sources only ever gain newer rows;
* ``late_and_duplicate``: rows may also arrive late, or be delivered again;
* ``mutable``: rows may also be updated or deleted.

The outcomes are ``safe`` (a proof rule applies), ``diverges`` (a replayable counterexample), ``nondeterministic``
(the full refresh itself depends on tie-breaking, shown by a witness: there is no single table for the incremental
run to equal), ``unknown`` (no divergence found, no proof), ``unsupported`` (the model cannot be simulated, or not
one generated source state ran) and ``timeout``. A ``diverges`` row also carries the repairs that
:mod:`kumosql.incremental_repairs` can propose and prove, as SQLX patches; nothing is applied.

Source schemas are *inferred* (column names and a type guess from the name; an
``id`` column is taken as the source's unique key and a ``*_at``/``ts`` column as
its event time). A verdict is therefore about the model on those assumed
sources, not on the real tables; ``--source-schema`` replaces the guess with
real columns. A ``diverges`` verdict always carries a counterexample that
replays; ``unknown`` means no divergence was found and no proof rule applies.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
from pathlib import Path
import re
import sys

import sqlglot

from .incremental import (
    IncrementalError,
    check_incremental,
    parse_incremental_sqlx,
)
from .incremental_repairs import Repair, propose_repairs
from .incremental_sources import guess_type, infer_sources  # noqa: F401 - re-exported: the scan's source inference
from .resilience import extended_path
from .sqlx import split_sqlx_sections

CONTRACTS: dict[str, tuple[str, ...]] = {
    "append_only": ("insert_new", "empty"),
    "late_and_duplicate": ("insert_new", "insert_late", "duplicate", "empty"),
    "mutable": ("insert_new", "update", "delete", "empty"),
}


@dataclass(frozen=True)
class ScanRow:
    model: str
    path: str
    contract: str
    outcome: str  # safe | diverges | nondeterministic | unknown | unsupported | timeout
    rule: str
    detail: str = ""
    statements: int | None = None  # size of the counterexample
    repairs: tuple[Repair, ...] = ()  # proven repairs for a diverging model, smallest first; patches, never applied
    repair_note: str = ""  # for a diverging model with no repair: why each single edit was refused


def _incremental_files(root: Path) -> list[Path]:
    root = extended_path(root)
    base = root / "definitions" if (root / "definitions").is_dir() else root
    found = []
    for path in sorted(base.rglob("*.sqlx")):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if re.search(r"\btype\s*:\s*[\"']incremental[\"']", text):
            found.append(path)
    return found


def _target_name(text: str, path: Path) -> str:
    for kind, body in split_sqlx_sections(text):
        if kind == "block" and body.lstrip().startswith("config"):
            named = re.search(r"\bname\s*:\s*[\"']([^\"']+)[\"']", body)
            if named:
                return named.group(1)
    return path.stem


def scan_project(
    root: str | Path,
    *,
    contracts: dict[str, tuple[str, ...]] | None = None,
    source_schema: dict[str, dict[str, str]] | None = None,
    seeds: int = 20,
    limit: int | None = None,
    repairs: bool = True,
) -> list[ScanRow]:
    root = extended_path(root)
    contracts = contracts or CONTRACTS
    rows: list[ScanRow] = []
    files = _incremental_files(root)
    for path in files[:limit]:
        relative = str(path.relative_to(root))
        text = path.read_text(encoding="utf-8", errors="replace")
        target = _target_name(text, path)
        try:
            model = parse_incremental_sqlx(text, target)
            sources = infer_sources(model, source_schema)
        except (IncrementalError, sqlglot.errors.SqlglotError) as exc:
            for name in contracts:
                rows.append(ScanRow(target, relative, name, "unsupported", "parse", str(exc)[:120]))
            continue
        for name, kinds in contracts.items():
            verdict = check_incremental(model, sources, kinds, seeds=seeds, time_limit=20)
            found: tuple[Repair, ...] = ()
            note = ""
            if repairs and verdict.outcome == "diverges":
                report = propose_repairs(text, target, sources, kinds, path=relative)
                found = tuple(report.repairs)
                if not found:
                    note = "; ".join(f"{'+'.join(r.edits)}: {r.reason}" for r in report.refused if len(r.edits) == 1)[:300]
            rows.append(
                ScanRow(
                    target,
                    relative,
                    name,
                    verdict.outcome,
                    verdict.rule,
                    verdict.detail,
                    verdict.counterexample.size if verdict.counterexample else None,
                    found,
                    note,
                )
            )
    return rows


def summarise(rows: list[ScanRow]) -> dict[str, dict[str, int]]:
    summary: dict[str, dict[str, int]] = {}
    for row in rows:
        summary.setdefault(row.contract, {}).setdefault(row.outcome, 0)
        summary[row.contract][row.outcome] += 1
    return summary


def repair_summary(rows: list[ScanRow]) -> dict[str, dict[str, int]]:
    """Per contract: how many models diverge and how many of those have at least one proven repair."""

    summary: dict[str, dict[str, int]] = {}
    for row in rows:
        if row.outcome == "diverges":
            entry = summary.setdefault(row.contract, {"diverges": 0, "repaired": 0})
            entry["diverges"] += 1
            entry["repaired"] += bool(row.repairs)
    return summary


def row_json(row: ScanRow) -> dict:
    data = {k: v for k, v in row.__dict__.items() if k != "repairs"}
    data["repairs"] = [r.to_json() for r in row.repairs]
    return data


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check each Dataform incremental model against its full refresh")
    parser.add_argument("root", type=Path, help="Dataform project root")
    parser.add_argument("--source-schema", type=Path, help='JSON of source table columns, e.g. {"events": {"id": "INT64", "ts": "TIMESTAMP"}}')
    parser.add_argument("--seeds", type=int, default=20, help="Random change sequences tried per model and contract")
    parser.add_argument("--limit", type=int, help="Only the first N incremental models")
    parser.add_argument("--no-repairs", action="store_true", help="Do not propose repairs for diverging models")
    parser.add_argument("--diffs", action="store_true", help="Print each proposed repair as a unified diff of the SQLX (never applied)")
    parser.add_argument("--json", action="store_true", help="Machine-readable output")
    args = parser.parse_args(argv)
    if not args.root.is_dir():
        print("incremental-report: project folder not found", file=sys.stderr)
        return 2
    overrides = json.loads(args.source_schema.read_text(encoding="utf-8")) if args.source_schema else None
    rows = scan_project(args.root, source_schema=overrides, seeds=args.seeds, limit=args.limit, repairs=not args.no_repairs)
    summary = summarise(rows)
    if args.json:
        print(json.dumps({"summary": summary, "repairs": repair_summary(rows), "rows": [row_json(r) for r in rows]}, indent=1))
        return 0
    models = len({r.model for r in rows})
    print(f"{models} incremental models; source columns and keys are inferred, so read each answer as 'on those assumed sources'.")
    for name, counts in summary.items():
        print(f"  {name}: " + ", ".join(f"{n} {k}" for k, n in sorted(counts.items())))
    for name, counts in repair_summary(rows).items():
        print(f"  repairs [{name}]: {counts['repaired']} of {counts['diverges']} diverging models have a proven repair")
    for row in rows:
        if row.outcome == "diverges":
            print(f"  diverges [{row.contract}] {row.model}: {row.detail} ({row.statements} source statements)")
            for repair in row.repairs[:1]:
                print(f"    repair: {repair.summary} ({repair.rule}; full refresh: {repair.full_refresh})")
                if args.diffs:
                    print("".join("      " + line for line in repair.diff.splitlines(keepends=True)))
            if not row.repairs and row.repair_note:
                print(f"    no repair: {row.repair_note}")
        elif row.outcome == "nondeterministic":
            print(f"  nondeterministic [{row.contract}] {row.model}: {row.detail}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
