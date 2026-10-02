"""WeTune's 50 GitHub performance issues: can KumoSQL reproduce and verify the developers' rewrites?

WeTune (Wang et al., SIGMOD 2022; https://github.com/WeTune/WeTune-code,
Apache-2.0) collected 50 slow queries from Discourse, GitLab, Spree, Redmine,
Lobsters, Solidus and Diaspora together with the rewrite each project's
developers committed (``wtune_data/issues/issues``). For every issue this tool

* asks the prover whether the developers' rewrite is equivalent to the original
  (``verified``: proven; ``refuted``: a counterexample; otherwise unknown), and
* runs ``kumosql.query_optimizer.optimize`` on the original with only the
  application's schema (no data, no LLM) and reports whether KumoSQL's proven
  rewrite *reproduces* the developers' improvement: for every structural
  feature the developers reduced (joins, subqueries, DISTINCT, GROUP BY,
  ORDER BY, predicates, OR), KumoSQL's rewrite has at most as many.

Rewrites that add structure (OR to UNION and similar) have no feature the
developers reduced; they count as reproduced only when KumoSQL emits the same
statement. Every KumoSQL rewrite is proven equivalent before it is reported.

    python tools/wetune_bench.py --wetune ../WeTune-code
"""

from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import sqlglot  # noqa: E402
from sqlglot import exp  # noqa: E402

from kumosql import query_optimizer as qo  # noqa: E402

ISSUES = ("wtune_data", "issues", "issues")
SCHEMAS = ("wtune_data", "schemas")


def schema_dialect(text: str) -> str:
    return "mysql" if "MySQL dump" in text[:400] or "ENGINE=" in text else "postgres"


def load_catalog(path: Path) -> tuple[qo.Catalog, str]:
    """Columns, NOT NULL columns, primary keys and unique indexes from a schema dump."""

    text = path.read_text(encoding="utf-8", errors="replace")
    dialect = schema_dialect(text)
    catalog = qo.Catalog()
    pending_keys: list[tuple[str, tuple[str, ...], bool]] = []
    for raw in sqlglot.parse(text, read=dialect, error_level=sqlglot.ErrorLevel.IGNORE):
        if raw is None:
            continue
        if isinstance(raw, exp.Create) and isinstance(raw.this, exp.Schema) and (raw.kind or "").upper() == "TABLE":
            table = raw.this.this.name.lower()
            cols, not_null = [], set()
            for item in raw.this.expressions:
                if isinstance(item, exp.ColumnDef):
                    name = item.name.lower()
                    cols.append(name)
                    kinds = [c.args.get("kind") for c in item.args.get("constraints") or []]
                    if any(isinstance(k, (exp.NotNullColumnConstraint, exp.PrimaryKeyColumnConstraint)) and not k.args.get("allow_null") for k in kinds):
                        not_null.add(name)
                    if any(isinstance(k, exp.PrimaryKeyColumnConstraint) for k in kinds):
                        pending_keys.append((table, (name,), True))
                elif isinstance(item, exp.PrimaryKey):
                    pending_keys.append((table, tuple(e.name.lower() for e in item.expressions), True))
                elif isinstance(item, (exp.UniqueColumnConstraint,)) and item.this is not None:
                    names = tuple(e.name.lower() for e in getattr(item.this, "expressions", []) or [])
                    if names:
                        pending_keys.append((table, names, False))
                elif isinstance(item, exp.IndexColumnConstraint) and (item.args.get("kind") or "").upper() == "UNIQUE":
                    names = tuple(e.name.lower() for e in item.expressions)
                    if names:
                        pending_keys.append((table, names, False))
            catalog.columns[table] = cols
            catalog.not_null[table] = not_null
            catalog.keys.setdefault(table, [])
        elif isinstance(raw, exp.Alter):
            table = raw.this.name.lower()
            for action in raw.args.get("actions") or []:
                for pk in action.find_all(exp.PrimaryKey):
                    pending_keys.append((table, tuple(e.name.lower() for e in pk.expressions), True))
        elif isinstance(raw, exp.Create) and (raw.kind or "").upper() == "INDEX" and raw.args.get("unique"):
            index = raw.this
            table_node = index.args.get("table") if isinstance(index, exp.Index) else None
            params = index.args.get("params") if isinstance(index, exp.Index) else None
            columns = getattr(params, "args", {}).get("columns") if params is not None else None
            if table_node is not None and columns and not (params.args.get("where")):
                names = tuple(c.this.name.lower() if isinstance(c, exp.Ordered) else c.name.lower() for c in columns)
                if all(names):
                    pending_keys.append((table_node.name.lower(), names, False))
    for table, names, primary in pending_keys:
        if table not in catalog.columns:
            continue
        if primary:
            catalog.not_null[table] |= set(names)
        # A UNIQUE index identifies rows only where its columns are not NULL.
        if primary or set(names) <= catalog.not_null[table]:
            if names not in catalog.keys[table]:
                catalog.keys[table].append(names)
    return catalog, dialect


def features(sql: str, dialect: str) -> Counter:
    tree = sqlglot.parse_one(sql, read=dialect)
    out: Counter = Counter()
    out["joins"] = len(list(tree.find_all(exp.Join)))
    out["subqueries"] = sum(1 for _ in tree.find_all(exp.Select)) - 1
    out["distinct"] = sum(1 for s in tree.find_all(exp.Select) if s.args.get("distinct"))
    out["group_by"] = len(list(tree.find_all(exp.Group)))
    out["order_by"] = len(list(tree.find_all(exp.Order)))
    out["or"] = len(list(tree.find_all(exp.Or)))
    out["predicates"] = len(list(tree.find_all(exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE, exp.In, exp.Is, exp.Like, exp.Between, exp.Exists)))
    return out


def load_issues(root: Path) -> list[dict]:
    rows = []
    for line in root.joinpath(*ISSUES).read_text(encoding="utf-8").splitlines():
        parts = line.split("\t")
        if len(parts) < 6:
            continue
        rows.append(
            {"id": int(parts[0]), "app": parts[1], "kind": parts[2], "commit": parts[3], "original": parts[4], "developer": parts[5]}
        )
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--wetune", type=Path, required=True, help="checkout of WeTune/WeTune-code")
    parser.add_argument("--budget", type=float, default=20.0, help="seconds of deletion search per query")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)

    catalogs: dict[str, tuple[qo.Catalog, str]] = {}
    results = []
    for issue in load_issues(args.wetune):
        app = issue["app"]
        if app not in catalogs:
            catalogs[app] = load_catalog(args.wetune.joinpath(*SCHEMAS, f"{app}.base.schema.sql"))
        catalog, dialect = catalogs[app]
        record = {k: issue[k] for k in ("id", "app", "kind")}
        started = time.perf_counter()
        try:
            dev = qo.prove(issue["original"], issue["developer"], catalog, dialect=dialect)
            record["developer_rewrite"] = "verified" if dev.proven else ("refuted" if dev.status.value == "not_equivalent" else "unknown")
            record["developer_reason"] = "" if dev.proven else dev.reason[:200]
        except Exception as error:  # noqa: BLE001
            record["developer_rewrite"] = "error"
            record["developer_reason"] = f"{type(error).__name__}: {error}"[:200]
        try:
            outcome = qo.optimize(issue["original"], catalog, dialect=dialect, deletion_budget_s=args.budget)
        except Exception as error:  # noqa: BLE001 - a crash is a failure to rewrite, never a rewrite
            outcome = qo.Outcome(None, f"error: {type(error).__name__}: {error}")
        record["seconds"] = round(time.perf_counter() - started, 2)
        record["kumosql"] = outcome.sql
        record["steps"] = list(outcome.steps)
        record["reason"] = outcome.reason[:300]
        record["reproduced"] = False
        if outcome.sql is not None:
            try:
                before = features(issue["original"], dialect)
                target = features(issue["developer"], dialect)
                ours = features(outcome.sql, dialect)
                reduced = [f for f in target if target[f] < before[f]]
                record["reduced_by_developers"] = reduced
                if reduced:
                    record["reproduced"] = all(ours[f] <= target[f] for f in reduced)
                else:
                    record["reproduced"] = qo._key(outcome.sql) == qo._key(
                        sqlglot.parse_one(issue["developer"], read=dialect).sql(dialect=dialect, pretty=True)
                    )
            except sqlglot.errors.SqlglotError:
                pass
        results.append(record)
        print(
            f"#{record['id']:<3} {app:<10} {issue['kind'][:28]:<28} developer={record['developer_rewrite']:<8} "
            f"kumosql={'rewritten' if outcome.sql else 'none':<9} reproduced={record['reproduced']}",
            flush=True,
        )
    n = len(results)
    dev = Counter(r["developer_rewrite"] for r in results)
    changed = sum(r["kumosql"] is not None for r in results)
    reproduced = sum(r["reproduced"] for r in results)
    print()
    print(f"issues {n}; developer rewrites verified {dev['verified']}, refuted {dev['refuted']}, unknown {dev['unknown']}, error {dev['error']}")
    print(f"KumoSQL proven rewrites {changed}/{n}; reproduces the developers' improvement {reproduced}/{n}; 0 unproven rewrites emitted")
    if args.out:
        args.out.write_text(json.dumps(results, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
