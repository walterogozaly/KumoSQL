"""Count the assumptions the prover evals' proofs carry, before and after the numeric semantics of issue #484.

PR #602 taught the SMT prover to read numbers the way BigQuery does and to track runtime errors, which lets a proof
drop assumptions it used to carry ("FLOAT64 values are never NaN", "runtime errors are not modeled", "SUM and AVG
are independent of row order", "+, - and * are exact"). This tool measures how many proofs of the prover evals did:

    python tools/numeric_assumption_report.py                      # before = the parent of the #602 merge, after = this checkout
    python tools/numeric_assumption_report.py --evals qed rbot     # some evals only
    python tools/numeric_assumption_report.py --json report.json   # also write every number and every proof
    python tools/numeric_assumption_report.py --after-json a.json --before-json b.json   # compare two saved collections

For every pair of the evals (each eval's own loader and its own prover call, so the pairs and the schemas are the
eval's) it records whether the prover proves the pair and the assumption labels the proof lists. The same collection
runs twice, once on a git worktree of the commit before #602 and once on this checkout. The report then lists, per
assumption label, how many proofs carried it before and after, and for the proofs that touch numeric expressions
how many were re-proved with fewer labels. A proof that was lost (proven before, not after) is checked with the
eval's own executed counterexample search and listed: a lost proof is acceptable only when the pair is really different.

**Touches numeric expressions** (the rule, stated so the percentage means something): a proof that carried any
numeric label before (a label that mentions NaN, runtime errors, FLOAT64 or INT64 values, SUM or AVG, exact
arithmetic, numeric types or a numeric difference), or whose two queries contain arithmetic (``+ - * / %``, ``DIV``,
unary minus, ``ABS``, ``ROUND``, ``CEIL``, ``FLOOR``, ``SQRT``, ``POW`` and the like) or a numeric literal. Because every
SMT proof used to carry the NaN, runtime-error and SUM/AVG labels, the first half of the rule covers every proof the
SMT prover made; the report therefore also gives the same figures for the queries-only rule and for arithmetic alone.

**Fewer labels** means the proof's label set after has fewer entries than before. A label that was replaced by one
the same size (the runtime-error label replaced by the verdict ``same``) is counted apart, as ``replaced``.

The collection is a worker mode of this script (``collect``), so it runs under whichever ``kumosql`` ``PYTHONPATH`` names.
Nothing here changes the prover. Rerun the report after other prover changes land: it always compares against the
same fixed commit, so the numbers show the sum of everything since.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from collections import Counter
import json
import logging
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parent.parent
TOOLS = Path(__file__).resolve().parent
# The first parent of the merge commit of PR #602 (80c0720d): master as it was before the numeric semantics.
BEFORE_COMMIT = "b77a25955f726647dbf6feb0e6261787279ad6a2"

EVALS = (
    "qed", "rbot", "sqlsolver-calcite", "sqlsolver-spark", "sqlsolver-tpcc", "sqlsolver-tpch",
    "singh", "cosette", "cosette-adapted", "spes", "querybooster", "calcite-mined",
)
# Not one of the prover evals of the headline: the BigQuery-dialect, typed pairs PR #602 added (the prover's numeric side).
EXTRA_EVALS = ("numeric-traps",)
ALL_EVALS = EVALS + EXTRA_EVALS
# Evals that are split over several worker processes (the slow ones).
SHARDS = {"singh": 4, "querybooster": 2, "cosette-adapted": 2, "calcite-mined": 3}
REFUTE_TRIALS = 100

# --- the numeric rule ---------------------------------------------------------------------------------------

NUMERIC_LABEL = re.compile(
    r"nan\b|runtime error|float64|int64|\bsum and avg\b|\bexact\b|numeric|overflow|rounding|division|\babs\(", re.IGNORECASE
)


def is_numeric_label(label: str) -> bool:
    """Whether an assumption label is about numbers (NaN, errors, FLOAT64 or INT64 values, SUM/AVG, exact arithmetic)."""

    return bool(NUMERIC_LABEL.search(label))


def _arithmetic_types():
    from sqlglot import exp

    return (
        exp.Add, exp.Sub, exp.Mul, exp.Div, exp.IntDiv, exp.Mod, exp.Neg, exp.Abs, exp.Round, exp.Ceil, exp.Floor,
        exp.Sqrt, exp.Pow, exp.Ln, exp.Exp, exp.Log, exp.Sign,
    )


def query_numeric_kinds(queries: list[str], dialect: str = "mysql") -> set[str]:
    """``{"arithmetic"}`` when a query has arithmetic, ``{"literal"}`` for a numeric literal, both or neither.

    A query that does not parse contributes nothing. Arithmetic means ``+ - * / %``, ``DIV``, unary minus on a column or
    expression and the numeric functions (``ABS``, ``ROUND``, ``CEIL``, ``FLOOR``, ``SQRT``, ``POW``, ``LN``, ``EXP``, ``LOG``,
    ``SIGN``); a decimal or exponent literal counts as arithmetic too (it is a FLOAT64). A plain number (``x > 5``,
    ``LIMIT 10``) is a ``literal``.
    """

    import sqlglot
    from sqlglot import exp

    arithmetic = _arithmetic_types()
    kinds: set[str] = set()
    for sql in queries:
        try:
            tree = sqlglot.parse_one(sql, read=dialect)
        except Exception:  # noqa: BLE001 - an unreadable query adds no evidence
            continue
        for node in tree.walk():
            node = node[0] if isinstance(node, tuple) else node
            if isinstance(node, exp.Neg) and isinstance(node.this, exp.Literal):
                continue  # a negative literal is a literal
            if isinstance(node, arithmetic):
                kinds.add("arithmetic")
            elif isinstance(node, exp.Literal) and not node.is_string:
                text = str(node.this)
                kinds.add("arithmetic" if re.search(r"[.eE]", text) else "literal")
    return kinds


# --- the comparison ----------------------------------------------------------------------------------------


def key(record: dict) -> tuple:
    return record["eval"], record["index"]


def _numeric_content(record: dict) -> set[str]:
    return query_numeric_kinds([record["left"], record["right"]], record.get("dialect", "mysql"))


def analyse(before: list[dict], after: list[dict], refuted: dict | None = None) -> dict:
    """Join the two collections pair by pair and count (see the module docstring for the definitions).

    ``refuted`` maps ``"eval:index"`` to True, False or None (no answer) for lost proofs: whether the eval's own executed
    search found a database on which the two queries differ.
    """

    refuted = refuted or {}
    after_by_key = {key(r): r for r in after}
    pairs = []
    mismatched = []
    for old in before:
        new = after_by_key.get(key(old))
        if new is None:
            continue
        if (old["left"], old["right"]) != (new["left"], new["right"]):
            mismatched.append(f"{old['eval']}:{old['index']}")
            continue
        pairs.append((old, new))
    labels_before: Counter = Counter()
    labels_after: Counter = Counter()
    labels_numeric: dict[str, bool] = {}
    dropped: Counter = Counter()
    added: Counter = Counter()
    per_eval: dict[str, Counter] = {}
    lost, gained = [], []
    rules = {"label_or_query": Counter(), "query": Counter(), "arithmetic": Counter()}
    fewer_proofs = []
    for old, new in pairs:
        stats = per_eval.setdefault(old["eval"], Counter())
        stats["pairs"] += 1
        if old["proven"]:
            stats["proven_before"] += 1
        if new["proven"]:
            stats["proven_after"] += 1
        before_labels, after_labels = set(old["assumptions"]), set(new["assumptions"])
        for label in before_labels | after_labels:
            labels_numeric.setdefault(label, is_numeric_label(label))
        if old["proven"]:
            labels_before.update(before_labels)
        if new["proven"]:
            labels_after.update(after_labels)
        if new["proven"] and not old["proven"]:
            gained.append(f"{old['eval']}:{old['index']}")
        if old["proven"] and not new["proven"]:
            lost.append((old, new))
            continue
        if not old["proven"]:
            continue
        kinds = _numeric_content(old)
        has_label = any(labels_numeric[label] for label in before_labels)
        touches = {
            "label_or_query": has_label or bool(kinds),
            "query": bool(kinds),
            "arithmetic": "arithmetic" in kinds,
        }
        fewer = len(after_labels) < len(before_labels)
        replaced = not fewer and before_labels != after_labels and len(after_labels) == len(before_labels)
        more = len(after_labels) > len(before_labels)
        for rule, hit in touches.items():
            if not hit:
                continue
            count = rules[rule]
            count["touching"] += 1
            count["fewer" if fewer else "replaced" if replaced else "more" if more else "same"] += 1
            if rule == "label_or_query":
                stats["touching"] += 1
                stats["fewer"] += fewer
                if fewer:
                    dropped.update(before_labels - after_labels)
                    fewer_proofs.append(f"{old['eval']}:{old['index']}")
                added.update(after_labels - before_labels)
    lost_rows = []
    for old, new in lost:
        answer = refuted.get(f"{old['eval']}:{old['index']}")
        lost_rows.append({
            "eval": old["eval"], "index": old["index"], "left": old["left"], "right": old["right"],
            "counterexample": answer, "before_labels": old["assumptions"], "reason": new.get("reason", ""),
        })
    headline = rules["label_or_query"]
    touching_before = headline["touching"] + sum(1 for row in lost_rows if _lost_touches(row))
    return {
        "pairs": len(pairs),
        "mismatched": mismatched,
        "proofs_before": sum(1 for o, _ in pairs if o["proven"]),
        "proofs_after": sum(1 for _, n in pairs if n["proven"]),
        "labels": {
            label: {"numeric": labels_numeric[label], "before": labels_before[label], "after": labels_after[label]}
            for label in sorted(labels_numeric, key=lambda l: (-labels_before[l] - labels_after[l], l))
        },
        "rules": {rule: dict(count) for rule, count in rules.items()},
        "touching_before_including_lost": touching_before,
        "dropped": dict(dropped.most_common()),
        "added": dict(added.most_common()),
        "per_eval": {name: dict(stats) for name, stats in per_eval.items()},
        "lost": lost_rows,
        "gained": gained,
    }


def _lost_touches(row: dict) -> bool:
    return any(is_numeric_label(label) for label in row["before_labels"]) or bool(query_numeric_kinds([row["left"], row["right"]]))


def percent(part: int, whole: int) -> str:
    return f"{100 * part / whole:.1f}%" if whole else "n/a"


# --- collection (worker mode) --------------------------------------------------------------------------


def _install_capture(dialect: str | None = None):
    """Keep every result the algebraic prover returns, so the last one is the pair's final answer.

    With ``dialect`` the prover reads every pair in that dialect instead of the eval's own.
    """

    from kumosql import algebraic_equivalence as algebraic

    captured: list = []
    original = algebraic.prove_equivalent_algebraic

    def recording(*args, **kwargs):
        if dialect:
            kwargs["dialect"] = dialect
        result = original(*args, **kwargs)
        captured.append(result)
        return result

    algebraic.prove_equivalent_algebraic = recording

    def restore():
        algebraic.prove_equivalent_algebraic = original

    return captured, restore


def _items(name: str):
    """``(index, left, right, dialect, prove, refute)`` for every pair of an eval, from the eval's own loader.

    ``prove()`` runs the eval's own prover call (it returns nothing: the captured result is the answer);
    ``refute()`` runs the eval's executed counterexample search and returns True when the queries differ.
    """

    sys.path.insert(0, str(TOOLS))
    if name == "qed":
        import qed_bench as bench
        import sqlsolver_bench as sb

        schemas: dict = {}
        for index, case in enumerate(bench.load_cases()):
            if case["schema_id"] not in schemas:
                tables = bench._tables(case["ddl"])
                schemas[case["schema_id"]] = (tables, sb.new_database(tables))
            tables, db = schemas[case["schema_id"]]
            left, right = case["sql_a"], case["sql_b"]
            yield index, left, right, "mysql", _call(sb.prove_result, left, right, tables, True), _differ(sb, left, right, tables, db, True)
    elif name == "rbot":
        import rbot_bench as bench
        import sqlsolver_bench as sb

        tables = sb.load_schema(bench.FIXTURES / "create_tables.sql")
        db = sb.new_database(tables)
        for index, (_, left, right) in enumerate(bench.load_pairs()):
            left, right = bench.normalise(left), bench.normalise(right)
            yield index, left, right, "mysql", _call(sb.prove_result, left, right, tables, True), _differ(sb, left, right, tables, db, True)
    elif name.startswith("sqlsolver-"):
        import sqlsolver_bench as sb

        suite = name.split("-", 1)[1]
        pairs_file, schema_file = sb.SUITES[suite]
        tables = sb.load_schema(sb.FIXTURES / schema_file)
        db = sb.new_database(tables)
        constants = suite in sb.CONSTANT_GROUPING
        skipped = sb.must_not_prove(suite)
        for index, (left, right) in enumerate(sb.load_pairs(sb.FIXTURES / pairs_file)):
            if index in skipped:
                continue  # the eval keeps these out of its score: they hold only if tie-breaking is fixed
            if constants:
                left, right = sb.calcite_operators(left), sb.calcite_operators(right)
            yield index, left, right, "mysql", _call(sb.prove_result, left, right, tables, constants), _differ(sb, left, right, tables, db, constants)
    elif name in ("cosette", "cosette-adapted", "spes"):
        import cosette_bench as bench
        import sqlsolver_bench as sb

        constants = name == "spes"
        schemas = {}
        for index, case in enumerate(bench.load(name)):
            if case["ddl"] not in schemas:
                tables = bench._tables(case["ddl"])
                schemas[case["ddl"]] = (tables, sb.new_database(tables))
            tables, db = schemas[case["ddl"]]
            left, right = bench.repaired(case["sql_a"], case["sql_b"], tables)
            if case.get("label", "equivalent") == "not_equivalent":
                continue  # a pair labelled different has no proof to compare
            yield index, left, right, "mysql", _call(sb.prove_result, left, right, tables, constants), _cosette_differ(bench, sb, left, right, tables, db, constants)
    elif name == "calcite-mined":
        import calcite_mined_bench as bench
        import sqlsolver_bench as sb

        schemas_json = json.loads((bench.FIXTURES / "schemas.json").read_text(encoding="utf-8"))
        loaded: dict = {}
        for index, pair in enumerate(bench.load_pairs()):
            if pair["schema_id"] not in loaded:
                tables = bench._tables(schemas_json[pair["schema_id"]]["ddl"])
                loaded[pair["schema_id"]] = (tables, sb.new_database(tables))
            tables, db = loaded[pair["schema_id"]]
            left, right = pair["sql_a"], pair["sql_b"]

            def refute(left=left, right=right, tables=tables, db=db):
                counter = sb.differ(left, right, tables, db, REFUTE_TRIALS)
                return counter not in (None, False) and bench._really_differ(counter)

            yield index, left, right, "mysql", _call(sb.prove_result, left, right, tables), refute
    elif name == "singh":
        import singh_bedathur_bench as bench

        for index, pair in enumerate(bench.load_pairs()):
            yield index, pair.left, pair.right, "mysql", _call(bench.prove, pair), _singh_refute(bench, pair)
    elif name == "querybooster":
        import querybooster_bench as bench

        cases, schemas = bench.load_cases()
        for index, case in enumerate(cases):
            schema = bench.schema_for(case, schemas)
            dialect = "postgres" if case.family == "tweets-cast" else schema.dialect
            yield index, case.left, case.right, dialect, _call(bench.prove, case.left, case.right, schema), _querybooster_refute(bench, case, schema)
    elif name == "numeric-traps":
        import numeric_traps_bench as bench
        from kumosql.smt_equivalence import prove_equivalent_smt

        for index, case in enumerate(bench.load_cases("all")):
            def prove(case=case):
                return prove_equivalent_smt(case.left, case.right, schema=bench.SCHEMA, types=bench.TYPES, timeout_ms=bench.TIMEOUT_MS)

            # No executed search here: the case's label, written from BigQuery's documented rules, says whether a proof is false.
            yield index, case.left, case.right, "bigquery", prove, (lambda case=case: case.label in ("not_equivalent", "introduces_error"))
    else:
        raise SystemExit(f"unknown eval {name}")


def _call(function, *args):
    return lambda: function(*args)


def _differ(sb, left, right, tables, db, constants):
    def refute():
        return sb.differ(left, right, tables, db, REFUTE_TRIALS, constants=constants) not in (None, False)

    return refute


def _cosette_differ(bench, sb, left, right, tables, db, constants):
    def refute():
        if sb.differ(left, right, tables, db, REFUTE_TRIALS, constants=constants) not in (None, False):
            return True
        return bench.search(left, right, tables, constants) not in (None, False)

    return refute


def _singh_refute(bench, pair):
    return lambda: bench.decide(pair).kind in ("different", "wrong")


def _querybooster_refute(bench, case, schema):
    return lambda: bench.check(case, schema, case.left, case.right)["outcome"] == "refuted"


def collect(evals: list[str], shard: tuple[int, int] = (0, 1), only: dict | None = None, refute: bool = False, limit: int | None = None, dialect: str | None = None) -> list[dict]:
    """One record per pair: whether the prover proves it, with which labels. With ``refute`` the records answer whether
    the eval's executed search separates the queries, for the pairs ``only`` names."""

    logging.getLogger("sqlglot").setLevel(logging.CRITICAL)
    os.environ.setdefault("KUMOSQL_TIMING", "0")
    captured, restore = _install_capture(dialect)
    records = []
    try:
        records = _collect(evals, shard, only, refute, limit, captured)
    finally:
        restore()
    return records


def _collect(evals, shard, only, refute, limit, captured) -> list[dict]:
    records = []
    for name in evals:
        wanted = None if only is None else set(only.get(name, ()))
        count = 0
        for index, left, right, dialect, prove, search in _items(name):
            if wanted is not None and index not in wanted:
                continue
            if limit is not None and count >= limit:
                break
            if index % shard[1] != shard[0]:
                continue
            count += 1
            record = {"eval": name, "index": index, "left": left, "right": right, "dialect": dialect}
            if refute:
                try:
                    record["counterexample"] = bool(search())
                except Exception as error:  # noqa: BLE001 - no answer is not a counterexample
                    record["counterexample"], record["error"] = None, f"{type(error).__name__}: {error}"[:200]
                records.append(record)
                continue
            captured.clear()
            started = time.time()
            error = ""
            returned = None
            try:
                returned = prove()
            except BaseException as raised:  # noqa: BLE001 - a crash is a failure to prove, never a proof
                if isinstance(raised, KeyboardInterrupt):
                    raise
                error = f"{type(raised).__name__}: {raised}"[:200]
            result = returned if hasattr(returned, "status") else captured[-1] if captured else None
            record.update(
                proven=bool(result is not None and result.proven and not error),
                status=result.status.name if result is not None else "ERROR",
                assumptions=sorted(result.assumptions) if result is not None and result.proven and not error else [],
                reason=(result.reason if result is not None else error)[:160],
                seconds=round(time.time() - started, 2),
            )
            records.append(record)
    return records


# --- orchestration -----------------------------------------------------------------------------------------


def _worker(tree: Path, out: Path, args: list[str]) -> list[dict]:
    env = dict(os.environ, PYTHONPATH=str(tree / "src"), KUMOSQL_TIMING="0", PYTHONHASHSEED="0")
    command = [sys.executable, str(Path(__file__).resolve()), "collect", "--out", str(out), *args]
    done = subprocess.run(command, cwd=ROOT, env=env, capture_output=True, text=True)
    if done.returncode != 0:
        raise RuntimeError(f"collection failed ({' '.join(args)}): {done.stderr[-1500:]}")
    return json.loads(out.read_text(encoding="utf-8"))


def collect_tree(tree: Path, evals: list[str], jobs: int, limit: int | None, scratch: Path, tag: str, bigquery: bool = False) -> list[dict]:
    work = []
    for name in evals:
        for shard in range(SHARDS.get(name, 1)):
            args = ["--evals", name, "--shard", f"{shard}/{SHARDS.get(name, 1)}"] + (["--limit", str(limit)] if limit else []) + (["--bigquery"] if bigquery else [])
            work.append((scratch / f"{tag}-{name}-{shard}.json", args))
    with ThreadPoolExecutor(max_workers=jobs) as pool:
        parts = list(pool.map(lambda w: _worker(tree, *w), work))
    records = [r for part in parts for r in part]
    return sorted(records, key=lambda r: (ALL_EVALS.index(r["eval"]), r["index"]))


def refute_lost(lost: list[dict], jobs: int, scratch: Path) -> dict:
    """The eval's own executed search on each lost pair (run on this checkout; the search does not depend on the prover)."""

    by_eval: dict[str, list[int]] = {}
    for row in lost:
        by_eval.setdefault(row["eval"], []).append(row["index"])
    work = []
    for name, indexes in by_eval.items():
        ids = scratch / f"ids-{name}.json"
        ids.write_text(json.dumps({name: indexes}), encoding="utf-8")
        work.append((scratch / f"refute-{name}.json", ["--evals", name, "--ids", str(ids), "--refute"]))
    answers = {}
    with ThreadPoolExecutor(max_workers=jobs) as pool:
        for part in pool.map(lambda w: _worker(ROOT, *w), work):
            for record in part:
                answers[f"{record['eval']}:{record['index']}"] = record.get("counterexample")
    return answers


def _commit(tree: Path) -> str:
    done = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=tree, capture_output=True, text=True)
    return done.stdout.strip()


def render(report: dict, before_label: str, after_label: str) -> str:
    lines = []
    out = lines.append
    out(f"Assumption labels of the prover evals' proofs: before = {before_label}, after = {after_label}")
    out(f"{report['pairs']} pairs compared; proofs before {report['proofs_before']}, after {report['proofs_after']}; "
        f"lost {len(report['lost'])}, gained {len(report['gained'])}"
        + (f"; {len(report['mismatched'])} pairs skipped because the two collections disagree on their text" if report["mismatched"] else ""))
    out("")
    out("Proofs carrying each label (numeric labels first)")
    out(f"  {'label':100} {'numeric':>7} {'before':>7} {'after':>6}")
    for label, row in sorted(report["labels"].items(), key=lambda kv: (not kv[1]["numeric"], -kv[1]["before"], kv[0])):
        out(f"  {label[:100]:100} {'yes' if row['numeric'] else 'no':>7} {row['before']:7d} {row['after']:6d}")
    out("")
    names = {
        "label_or_query": "carried a numeric label before, or the queries have arithmetic or a numeric literal (the stated rule)",
        "query": "the queries have arithmetic or a numeric literal",
        "arithmetic": "the queries have arithmetic (a plain number does not count)",
    }
    out("Numeric-touching proofs re-proved with fewer labels")
    for rule, text in names.items():
        count = report["rules"][rule]
        touching = count.get("touching", 0)
        out(f"  {text}: {count.get('fewer', 0)} of {touching} = {percent(count.get('fewer', 0), touching)} "
            f"(same {count.get('same', 0)}, replaced {count.get('replaced', 0)}, more {count.get('more', 0)})")
    stated = report["rules"]["label_or_query"].get("fewer", 0)
    out(f"  stated rule counting a lost proof against the percentage: {stated} of {report['touching_before_including_lost']} = "
        f"{percent(stated, report['touching_before_including_lost'])}")
    out("")
    out("Per eval (stated rule)")
    out(f"  {'eval':18} {'pairs':>6} {'proofs before':>14} {'after':>6} {'touching':>9} {'fewer':>6} {'%':>7}")
    for name in ALL_EVALS:
        row = report["per_eval"].get(name)
        if row:
            out(f"  {name:18} {row['pairs']:6d} {row.get('proven_before', 0):14d} {row.get('proven_after', 0):6d} "
                f"{row.get('touching', 0):9d} {row.get('fewer', 0):6d} {percent(row.get('fewer', 0), row.get('touching', 0)):>7}")
    out("")
    out("Labels dropped by the re-proved proofs: " + (", ".join(f"{label} x{n}" for label, n in report["dropped"].items()) or "none"))
    out("Labels added by the re-proved proofs: " + (", ".join(f"{label} x{n}" for label, n in report["added"].items()) or "none"))
    out("")
    out(f"Proofs lost ({len(report['lost'])}): proven before, not proven after")
    for row in report["lost"]:
        verdict = {True: "false proof (counterexample found, or the case label for numeric-traps)", False: "NO counterexample found", None: "not checked"}[row["counterexample"]]
        out(f"  {row['eval']}:{row['index']} [{verdict}] {' '.join(row['left'].split())[:140]}")
        out(f"      now: {row['reason'][:150]}")
    if report["gained"]:
        out(f"Proofs gained ({len(report['gained'])}): " + ", ".join(report["gained"][:40]) + (" ..." if len(report["gained"]) > 40 else ""))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="mode")
    worker = sub.add_parser("collect", help="worker: write the records of some evals as JSON (runs under the PYTHONPATH's kumosql)")
    worker.add_argument("--evals", nargs="+", default=list(ALL_EVALS))
    worker.add_argument("--bigquery", action="store_true")
    worker.add_argument("--out", required=True)
    worker.add_argument("--shard", default="0/1", help="i/n: only pairs whose index mod n is i")
    worker.add_argument("--limit", type=int)
    worker.add_argument("--ids", help="JSON {eval: [index]}: only these pairs")
    worker.add_argument("--refute", action="store_true", help="run the eval's executed search instead of the prover")
    parser.add_argument("--evals", nargs="+", choices=ALL_EVALS, default=list(EVALS), help=f"default: the prover evals; {', '.join(EXTRA_EVALS)} is the BigQuery-dialect eval of #602")
    parser.add_argument("--bigquery", action="store_true", help="read every pair as BigQuery SQL (the evals use MySQL or PostgreSQL), on both sides")
    parser.add_argument("--before", default=BEFORE_COMMIT, help="commit to measure as 'before' (default: the parent of the #602 merge)")
    parser.add_argument("--before-json", type=Path, help="a saved 'before' collection instead of running one")
    parser.add_argument("--after-json", type=Path, help="a saved 'after' collection instead of running one")
    parser.add_argument("--save", type=Path, help="write both collections here (a JSON object with before and after)")
    parser.add_argument("--json", type=Path, help="write the report numbers and the lost proofs as JSON")
    parser.add_argument("--jobs", type=int, default=2, help="worker processes per side (default 2)")
    parser.add_argument("--limit", type=int, help="the first N pairs of each eval (a quick check)")
    parser.add_argument("--no-refute", action="store_true", help="do not search for counterexamples to the lost proofs")
    args = parser.parse_args(argv)

    if args.mode == "collect":
        number, count = (int(x) for x in args.shard.split("/"))
        only = json.loads(Path(args.ids).read_text(encoding="utf-8")) if args.ids else None
        records = collect(args.evals, (number, count), only, args.refute, args.limit, "bigquery" if args.bigquery else None)
        Path(args.out).write_text(json.dumps(records), encoding="utf-8")
        return 0

    with tempfile.TemporaryDirectory(prefix="kumosql-assumptions-") as folder:
        scratch = Path(folder)
        before_label = args.before[:8]
        if args.before_json:
            before = json.loads(args.before_json.read_text(encoding="utf-8"))
            before = before["before"] if isinstance(before, dict) else before
        else:
            tree = scratch / "before"
            subprocess.run(["git", "worktree", "add", "--detach", "-q", str(tree), args.before], cwd=ROOT, check=True)
            try:
                before = collect_tree(tree, args.evals, args.jobs, args.limit, scratch, "before", args.bigquery)
            finally:
                subprocess.run(["git", "worktree", "remove", "--force", str(tree)], cwd=ROOT, check=False)
        if args.after_json:
            after = json.loads(args.after_json.read_text(encoding="utf-8"))
            after = after["after"] if isinstance(after, dict) else after
        else:
            after = collect_tree(ROOT, args.evals, args.jobs, args.limit, scratch, "after", args.bigquery)
        after_label = _commit(ROOT) or "this checkout"
        if args.save:
            args.save.write_text(json.dumps({"before": before, "after": after}), encoding="utf-8")
        report = analyse(before, after)
        if report["lost"] and not args.no_refute:
            report = analyse(before, after, refute_lost(report["lost"], args.jobs, scratch))
    print(render(report, before_label, after_label))
    if args.json:
        args.json.write_text(json.dumps(report, indent=1), encoding="utf-8")
    return 1 if any(row["counterexample"] is False for row in report["lost"]) else 0


if __name__ == "__main__":
    raise SystemExit(main())
