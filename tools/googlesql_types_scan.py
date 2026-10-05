"""Unlabelled scan of the GoogleSQL type checker over real BigQuery SQL: every finding it reports is a claim that the query
is invalid, and these corpora hold queries that run, so the target is zero findings.

The corpora are ``tests/fixtures/spider2/gold`` (the published Spider 2.0 BigQuery gold queries) and
``tests/fixtures/bq_corpora/*`` (open-source Dataform and plain-SQL BigQuery projects, loaded whole with
``load_sqlx_project``; each model's query, incremental query and operations are typed). Neither carries table schemas, so
by default the catalog is empty and a finding that needs a schema must stay silent. ``--chain`` also puts each project
model's inferred output columns in the catalog, in dependency order, so queries over upstream models are checked against
real column lists.

Each query is typed under an exception guard; a crash is reported, not hidden. Findings are counted per code with a
sample of each, and ``--json`` prints everything for hand checking.

    python tools/googlesql_types_scan.py [--chain] [--samples 5] [--json] [--corpus spider2|bq_corpora]
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import traceback
from collections import Counter, defaultdict
from pathlib import Path

logging.getLogger("sqlglot").setLevel(logging.CRITICAL)
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import sqlglot  # noqa: E402
from sqlglot import exp  # noqa: E402

from kumosql.googlesql_types import Catalog, infer  # noqa: E402

SPIDER2 = ROOT / "tests" / "fixtures" / "spider2" / "gold"
CORPORA = ROOT / "tests" / "fixtures" / "bq_corpora"


def spider2_queries() -> list[tuple[str, str]]:
    """``(label, sql)`` for every Spider 2.0 gold query."""

    return [(f"spider2/{p.stem}", p.read_text(encoding="utf-8", errors="replace")) for p in sorted(SPIDER2.glob("*.sql"))]


def project_queries(root: Path) -> list[tuple[str, str, str | None, bool]]:
    """``(label, sql, model key or None, scripted)`` for each model query, incremental query and operation of a project.
    ``scripted`` is True for the queries of a model whose operations declare variables: its query can read them."""

    from kumosql.pipeline import load_sqlx_project

    pipeline = load_sqlx_project(root)
    out: list[tuple[str, str, str | None, bool]] = []
    ordered: list[str] = []  # upstream models first, so --chain has their columns when it reaches a reader
    seen: set[str] = set()

    def visit(key: str) -> None:
        if key in seen or key not in pipeline.models:
            return
        seen.add(key)
        for dep in pipeline.models[key].declared_dependencies:
            visit(dep.key)
        ordered.append(key)

    for key in pipeline.models:
        visit(key)
    for key in ordered:
        model = pipeline.models[key]
        label = f"{root.name}/{model.path or key}"
        scripted = any(_SCRIPT_VARIABLES.search(s) for s in model.scripts)
        if model.sql and model.sql.strip():
            out.append((label, model.sql, key, scripted))
        for n, sql in enumerate(model.incremental_sql):
            out.append((f"{label}#incremental{n}", sql, None, scripted))
        for n, sql in enumerate(model.operations_sql):
            out.append((f"{label}#op{n}", sql, None, False))
    return out


_SCRIPT_VARIABLES = re.compile(
    r"\bDECLARE\b|\bFOR\s+\w+\s+IN\b|\bCREATE\s+(?:OR\s+REPLACE\s+)?(?:TEMP\w*\s+)?(?:TABLE\s+)?(?:FUNCTION|PROCEDURE)\b|"
    r"\bEXECUTE\s+IMMEDIATE\b|@\w+",
    re.I,
)


def statements(sql: str, report: dict) -> list[object]:
    """What to type for one chunk of SQL: the text itself when it is one statement (so pipe syntax is recognised), else
    each query inside a multi-statement script (a bare query, or the query of a CREATE TABLE/VIEW or INSERT). A script
    that declares variables or parameters is not typed: a query's names can then come from outside it."""

    try:
        parsed = [s for s in sqlglot.parse(sql, read="bigquery") if s is not None]
    except Exception:  # noqa: BLE001 - infer reports the parse error
        return [sql]
    if len(parsed) <= 1 and (not parsed or isinstance(parsed[0], exp.Query)):
        return [sql]
    if len(parsed) > 1 and _SCRIPT_VARIABLES.search(sql):
        report["scripts_skipped"] += 1
        return []
    found: list[object] = []
    for tree in parsed:
        if isinstance(tree, exp.Query):
            found.append(tree)
        elif isinstance(tree, exp.Create) and (tree.args.get("kind") or "").upper() in ("TABLE", "VIEW") and \
                isinstance(tree.expression, exp.Query):
            found.append(tree.expression)
        elif isinstance(tree, exp.Insert) and isinstance(tree.expression, exp.Query):
            found.append(tree.expression)
    return found


def scan_one(label: str, sql: str, catalog: Catalog, report: dict):
    """Type one chunk of SQL and record its findings; returns the typed query when the chunk is one statement."""

    items = statements(sql, report)
    result = None
    for item in items:
        report["statements"] += 1
        try:
            typed = infer(item, catalog)
        except Exception as exc:  # noqa: BLE001 - the scan reports a crash and goes on
            report["crashes"].append({"label": label, "error": f"{type(exc).__name__}: {exc}"[:300],
                                      "trace": traceback.format_exc().splitlines()[-3:]})
            continue
        result = typed if len(items) == 1 else None
        if typed.error:
            report["errors"][typed.error.split(":")[0][:60]] += 1
        elif typed.columns is not None:
            report["typed"] += 1
        for f in typed.findings:
            snippet = ""
            if f.node is not None:
                try:
                    snippet = f.node.sql("bigquery")[:200]
                except Exception:  # noqa: BLE001
                    snippet = "?"
            report["findings"].append({"code": f.code, "label": label, "message": f.message, "node": snippet})
    return result


def run(corpus: str | None = None, chain: bool = False) -> dict:
    report: dict = {"queries": Counter(), "statements": 0, "typed": 0, "scripts_skipped": 0, "crashes": [], "errors": Counter(), "findings": []}
    if corpus in (None, "spider2"):
        queries = spider2_queries()
        report["queries"]["spider2"] = len(queries)
        for label, sql in queries:
            scan_one(label, sql, Catalog(), report)
    if corpus in (None, "bq_corpora"):
        for root in sorted(p for p in CORPORA.iterdir() if p.is_dir()):
            try:
                queries = project_queries(root)
            except Exception as exc:  # noqa: BLE001
                report["crashes"].append({"label": f"{root.name}#load", "error": f"{type(exc).__name__}: {exc}"[:300]})
                continue
            report["queries"]["bq_corpora"] += len(queries)
            catalog = Catalog()
            for label, sql, key, scripted in queries:
                if scripted:  # its pre-operations declare variables its query may read
                    report["scripts_skipped"] += 1
                    continue
                typed = scan_one(label, sql, catalog, report)
                if chain and key is not None and typed is not None and typed.columns is not None \
                        and all(c.name for c in typed.columns):
                    catalog.add(key, typed.columns)
    return report


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--corpus", choices=("spider2", "bq_corpora"))
    ap.add_argument("--chain", action="store_true", help="add each project model's inferred columns to the catalog")
    ap.add_argument("--samples", type=int, default=5, help="findings printed per code")
    ap.add_argument("--json", action="store_true", help="print the whole report as JSON")
    args = ap.parse_args()
    report = run(args.corpus, args.chain)
    counts = Counter(f["code"] for f in report["findings"])
    if args.json:
        print(json.dumps({**report, "queries": dict(report["queries"]), "errors": dict(report["errors"]),
                          "counts": dict(counts)}, indent=1))
    else:
        print(f"queries: {dict(report['queries'])}  statements typed: {report['statements']}  scripts skipped (declare variables): {report['scripts_skipped']}  with output columns: {report['typed']}")
        print(f"crashes: {len(report['crashes'])}  untyped (reason): {dict(report['errors'].most_common(8))}")
        for crash in report["crashes"][:10]:
            print("  CRASH", crash["label"], crash["error"])
        print(f"findings: {sum(counts.values())}  {dict(counts)}")
        by_code: dict[str, list] = defaultdict(list)
        for f in report["findings"]:
            by_code[f["code"]].append(f)
        for code, items in sorted(by_code.items()):
            print(f"\n{code}: {len(items)}")
            for f in items[: args.samples]:
                print(f"  {f['label']}: {f['message']}  [{f['node']}]")
    return 1 if report["findings"] or report["crashes"] else 0


if __name__ == "__main__":
    sys.exit(main())
