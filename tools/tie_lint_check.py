"""Measure the tie lint's false alarms on a Dataform project, end to end.

    python tools/make_dataform_fixture.py OUT --models 3000 --seed 11
    python tools/tie_lint_check.py OUT [--budget 10] [--check-budget 30] [--limit N]

The lint (``python -m kumosql ties``) runs each model on its direct inputs, with the keys and NOT NULL
columns its upstream models guarantee. That is a shortcut: a witness could in principle give an input
rows its upstream query would never produce. This check removes the shortcut. For every finding it
inlines the upstream table and view models into the query, down to declared sources and incremental
tables (whose rows are not their query's output), and searches for a storage order that changes the
result of the inlined query, with data only in those roots. A finding the end-to-end search confirms is
real; one it cannot confirm is a *false alarm candidate* (the search is bounded, so it is the
unconfirmed count that is reported, not a proof of absence). Findings whose own upstream models also have
unknown sites are counted separately: the end-to-end difference may come from those instead.

Prints one line of counts: sites, witnessed findings, unwitnessed unknowns, confirmed, false alarm
candidates, inconclusive.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import time

import sqlglot
from sqlglot import exp

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from kumosql.pipeline import load_sqlx_project  # noqa: E402
from kumosql.prover_schema import from_pipeline  # noqa: E402
from kumosql.tie_lint import (  # noqa: E402
    _Facts,
    _canonical,
    _clean,
    _read_tables,
    _table_spelling,
    _typed_columns,
    _witness_inputs,
    lint_pipeline,
)
from kumosql.tie_witness import find_tie_witness, replay  # noqa: E402


def inline(sql: str, facts: _Facts, depth: int = 0) -> str:
    """``sql`` with every upstream table or view model replaced by its (inlined) query."""

    tree = sqlglot.parse_one(sql, read="bigquery")
    for table in list(_read_tables(tree)):
        model = facts.model_of(_table_spelling(table))
        if model is None or model.kind not in ("table", "view") or _clean(model) is not None or depth > 12:
            continue
        inner = sqlglot.parse_one(inline(model.sql, facts, depth + 1), read="bigquery")
        alias = table.alias or table.name
        table.replace(exp.Subquery(this=inner, alias=exp.TableAlias(this=exp.to_identifier(alias))))
    return tree.sql(dialect="bigquery")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("root", type=Path)
    parser.add_argument("--budget", type=float, default=10.0, help="witness search per model in the lint")
    parser.add_argument("--check-budget", type=float, default=30.0, help="end-to-end search per finding")
    parser.add_argument("--limit", type=int, help="only the first N query models")
    parser.add_argument("--json", type=Path, help="write the findings and their status here")
    parser.add_argument("--lint", type=Path, help="reuse the output of `python -m kumosql ties PROJECT --json` instead of linting again")
    args = parser.parse_args()

    started = time.monotonic()
    pipeline = load_sqlx_project(args.root)
    schema = from_pipeline(pipeline)
    if args.lint:
        saved = json.loads(args.lint.read_text(encoding="utf-8"))
        summary, finding_models = saved["summary"], [f["model"] for f in saved["findings"]]
        unknown_models = set(finding_models) | {u["model"] for u in saved["unwitnessed"]}
    else:
        result = lint_pipeline(pipeline, schema, budget=args.budget, limit=args.limit)
        summary, finding_models = result.summary(), [f.model for f in result.findings]
        unknown_models = set(finding_models) | {u.model for u in result.unwitnessed}
    facts = _Facts(pipeline, schema)
    shapes: dict[tuple, str] = {}
    rows, counts = [], {"confirmed": 0, "ambiguous": 0, "unconfirmed": 0, "inconclusive": 0}
    for name in finding_models:
        model = pipeline.models[name]
        status = "confirmed"
        try:
            flat = inline(model.sql, facts)
            typed = _typed_columns(flat, facts)
            if isinstance(typed, str):
                status = "inconclusive"
            else:
                _, rules, foreign_keys = _witness_inputs(typed, facts)
                # the roots keep only what was declared: the inlined models are gone, so nothing is derived
                shape = _canonical(flat, typed, rules, foreign_keys)[0]  # models of one shape get one search
                if shape not in shapes:
                    witness = find_tie_witness(flat, typed, rules, foreign_keys=foreign_keys, budget=args.check_budget)
                    shapes[shape] = "confirmed" if witness is not None and replay(witness) else "unconfirmed"
                status = shapes[shape]
        except Exception as error:  # noqa: BLE001 - reported, never hidden
            status = "inconclusive"
            print(f"  {name}: {type(error).__name__}: {str(error)[:100]}", file=sys.stderr)
        if status == "confirmed":
            ancestors = _ancestors(pipeline, facts, model)
            if ancestors & unknown_models:
                status = "ambiguous"
        counts[status] += 1
        if len(rows) % 50 == 49:
            print(f"  {len(rows) + 1} of {len(finding_models)} findings checked", file=sys.stderr, flush=True)
        rows.append({"model": name, "status": status})
    print(
        f"sites {summary['sites']}; witnessed findings {summary['findings']}; unwitnessed unknowns {summary['unwitnessed']}; "
        f"confirmed end to end {counts['confirmed']}; confirmed but an upstream model is also unknown {counts['ambiguous']}; "
        f"false alarm candidates {counts['unconfirmed']}; inconclusive {counts['inconclusive']}; {time.monotonic() - started:.0f} s"
    )
    if args.json:
        args.json.write_text(json.dumps({"summary": summary, "counts": counts, "findings": rows}, indent=1), encoding="utf-8")
    return 0


def _ancestors(pipeline, facts: _Facts, model, seen: set[str] | None = None) -> set[str]:
    seen = set() if seen is None else seen
    if _clean(model) is not None:
        return seen
    for table in _read_tables(sqlglot.parse_one(model.sql, read="bigquery")):
        parent = facts.model_of(_table_spelling(table))
        if parent is not None and parent.key not in seen:
            seen.add(parent.key)
            _ancestors(pipeline, facts, parent, seen)
    return seen


if __name__ == "__main__":
    raise SystemExit(main())
