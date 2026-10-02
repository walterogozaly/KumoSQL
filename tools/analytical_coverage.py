"""Run public analytical SQL benchmarks (TPC-DS, DSB, SQLStorm, ...) through every KumoSQL stage.

There is no official score for a static tool on these benchmarks, so this one is ours:

* **clean**: the share of queries that every stage handles with no crash, no timeout and no silent
  loss (a lost read, a rewrite proven equivalent that changes results, a query called different
  from itself).
* **full**: the share where every stage also gives a real answer rather than an honest
  "unsupported".

The stages are those of the syntax coverage suite (``tools/bq_syntax_coverage.py``: parse, load,
graph, fingerprint, cleanup, format, prover) plus **execution**: the original query and each
rewrite (cleanup rules, formatter) run in DuckDB on generated tables for the corpus's schema, and a
rewrite marked proven that returns different rows is a failure. A rewrite that changes results but
was *not* marked proven was caught by the verifier, so it is reported (``caught``) but not counted
as damage.

Each query is written for Postgres; it is converted to BigQuery with sqlglot first, and queries
sqlglot cannot convert are counted separately (``convert``), outside the score.

    python tools/benchmark_corpora.py fetch sqlstorm dsb
    python tools/analytical_coverage.py                       # 200 queries from each corpus
    python tools/analytical_coverage.py dsb --limit 0         # every DSB query
    python tools/analytical_coverage.py --failures            # list what failed and why
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import signal
import sys
import time
from collections import Counter, defaultdict
from multiprocessing import Pool
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))
os.environ.setdefault("KUMOSQL_TIMING", "0")

import benchmark_corpora as corpora_mod  # noqa: E402

_SPEC = importlib.util.spec_from_file_location("bq_syntax_coverage", ROOT / "tools" / "bq_syntax_coverage.py")
cov = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(cov)

import sqlglot  # noqa: E402

from kumosql import rewrite  # noqa: E402
from kumosql.equivalence import prove_equivalent, window_is_tie_stable  # noqa: E402
from kumosql.smt_equivalence import SmtStatus, prove_equivalent_smt  # noqa: E402
from kumosql.result_equivalence import ResultEquivalenceStatus, check_result_equivalence  # noqa: E402

PASS, UNSUPPORTED, FAIL, NA = cov.PASS, cov.UNSUPPORTED, cov.FAIL, cov.NA
STAGES = ("parse", "load", "graph", "fingerprint", "cleanup", "format", "prover", "execution")
EXECUTION_SEEDS = (1, 2, 3)


class _Timeout(Exception):
    pass


def _alarm(*_):
    raise _Timeout()


def _without_root_limit(sql: str) -> str | None:
    query = sqlglot.parse_one(sql, read="bigquery")
    if query.args.get("limit") is None and query.args.get("offset") is None:
        return None
    for key in ("order", "limit", "offset"):
        query.set(key, None)
    return query.sql(dialect="bigquery")


def _agree_before_limit(original: str, rewritten: str, schema) -> bool:
    """Whether two queries that end in the same ORDER BY .. LIMIT return the same rows without it.

    With ties on the ordering, DuckDB may keep different tied rows for two spellings of
    one query; both are valid answers, so that is not a difference.
    """

    left, right = _without_root_limit(original), _without_root_limit(rewritten)
    if left is None or right is None:
        return False
    tail = lambda sql: [sqlglot.parse_one(sql, read="bigquery").args.get(k) for k in ("order", "limit", "offset")]
    if [t and t.sql(dialect="bigquery") for t in tail(original)] != [t and t.sql(dialect="bigquery") for t in tail(rewritten)]:
        return False
    return check_result_equivalence(left, right, schema, seeds=EXECUTION_SEEDS).equivalent


def _tie_sensitive(sql: str) -> bool:
    return any(not window_is_tie_stable(w) for w in sqlglot.parse_one(sql, read="bigquery").find_all(sqlglot.exp.Window))


def _execution(original: str, rewrites: dict[str, object], schema) -> tuple[str, str]:
    """Run the original and each changed rewrite on generated data; proven rewrites must agree."""

    if schema is None:
        return NA, "no schema for this corpus"
    changed = {name: result for name, result in rewrites.items() if result is not None and result.sql != original}
    if not changed:
        return NA, "no rewrite changed the query"
    notes, ties = [], []
    for name, result in changed.items():
        check = check_result_equivalence(original, result.sql, schema, seeds=EXECUTION_SEEDS, check_column_names=True)
        if check.status is ResultEquivalenceStatus.ERROR:
            if check.reason.startswith("left side failed"):
                return NA, "the original does not run on DuckDB"
            if result.verification.trusted:
                return FAIL, f"{name}: proven rewrite fails to run: {check.reason[:120]}"
            notes.append(f"{name}: unproven rewrite fails to run")
        elif check.status is ResultEquivalenceStatus.DIFFERENT and _agree_before_limit(original, result.sql, schema):
            ties.append(name)
        elif check.status is ResultEquivalenceStatus.DIFFERENT and _tie_sensitive(original):
            notes.append(f"{name}: results differ, but a tie-sensitive window (ROW_NUMBER, LAG, ...) can differ between any two runs")
        elif check.status is ResultEquivalenceStatus.DIFFERENT:
            if result.verification.trusted:
                return FAIL, f"{name}: proven rewrite returns different rows (seed {check.failing_seed})"
            notes.append(f"caught: {name} changes results; the verifier refused it")
    if notes:
        return UNSUPPORTED, "; ".join(notes)[:160]
    return PASS, f"{', '.join(ties)}: same rows before the shared ORDER BY .. LIMIT (tied rows kept differ)" if ties else ""


def stage_prover(sql: str, schema) -> tuple[str, str]:
    """The query compared with itself, with the corpus's table columns known (as a catalog would)."""

    if cov._single_query(sql) is None:
        return NA, "not a single query"
    body = sql.strip().rstrip(";")
    columns = {table: list(cols) for table, cols in (schema or {}).items()} or None
    try:
        smt = prove_equivalent_smt(body, body, timeout_ms=3000, schema=columns)
        ast = prove_equivalent(body, body)
    except Exception as exc:  # noqa: BLE001
        return FAIL, f"prover crashed: {cov._error(exc)}"
    if smt.status == SmtStatus.NOT_EQUIVALENT:
        return FAIL, "claims a query differs from itself"
    if smt.status == SmtStatus.PROVEN_EQUIVALENT:
        return PASS, "smt proven"
    if ast.proven:
        return PASS, "ast proven"
    return UNSUPPORTED, smt.reason[:120]


def run_query(item: tuple[str, str, str, int]) -> tuple[str, dict]:
    query_id, text, corpus, timeout = item
    signal.signal(signal.SIGALRM, _alarm)
    out: dict[str, tuple[str, str]] = {}
    started = time.time()
    try:
        signal.alarm(timeout)
        try:
            sql = corpora_mod.to_bigquery(text)
        except Exception as exc:  # noqa: BLE001
            return query_id, {"convert": (UNSUPPORTED, cov._error(exc))}
        out["convert"] = (PASS, "")
        out["parse"] = cov.stage_parse(sql)
        pipeline, *loaded = cov.stage_load_sql(sql, out["parse"][0])
        out["load"] = tuple(loaded)
        out["graph"] = cov.stage_graph(pipeline, out["parse"][0], sql) if pipeline is not None else (NA, "")
        out["fingerprint"] = cov.stage_fingerprint(sql)
        rewrites: dict[str, object] = {"cleanup": None, "format": None}
        try:
            rewrites["cleanup"] = rewrite.apply_rules(cov.CLEANUP_RULES, sql)
            out["cleanup"] = cov._classify(rewrites["cleanup"], sql)
        except Exception as exc:  # noqa: BLE001
            out["cleanup"] = (FAIL, cov._error(exc))
        try:
            rewrites["format"] = rewrite.apply_rule("format_sql", sql)
            out["format"] = cov.stage_format(sql, out["parse"][0], rewrites["format"])
        except Exception as exc:  # noqa: BLE001
            out["format"] = (FAIL, cov._error(exc))
        out["prover"] = stage_prover(sql, corpora_mod.schema(corpus))
        try:
            out["execution"] = _execution(sql, rewrites, corpora_mod.schema(corpus))
        except Exception as exc:  # noqa: BLE001
            out["execution"] = (FAIL, f"execution check crashed: {cov._error(exc)}")
        signal.alarm(0)
    except _Timeout:
        out["timeout"] = (FAIL, f"over {timeout}s")
    except Exception as exc:  # noqa: BLE001
        out["crash"] = (FAIL, cov._error(exc))
    finally:
        signal.alarm(0)
    out["seconds"] = round(time.time() - started, 2)  # type: ignore[assignment]
    return query_id, out


def sample(corpus: str, limit: int, split: str = "dev") -> list[tuple[str, str]]:
    """Every query of a split, or ``limit`` of them spread evenly through it (stable across runs).

    ``split`` is ``dev`` (four in five queries), ``held-out`` (the rest, kept for final
    evaluation; see ``benchmark_corpora.held_out``) or ``all``.
    """

    items = [
        (qid, text) for qid, text in corpora_mod.queries(corpus)
        if split == "all" or corpora_mod.held_out(qid) == (split == "held-out")
    ]
    if limit and len(items) > limit:
        step = len(items) / limit
        items = [items[int(i * step)] for i in range(limit)]
    return items


def run(
    corpora: list[str], limit: int = 200, jobs: int | None = None, timeout: int = 120, log: Path | None = None,
    split: str = "dev",
) -> dict[str, dict]:
    """Run every sampled query; with ``log``, append each result as a JSON line and skip ones already there."""

    done: dict[str, dict] = {}
    if log is not None and log.exists():
        for line in log.read_text().splitlines():
            if line.strip():
                record = json.loads(line)
                done[record["id"]] = record["result"]
    work = [
        (qid, text, corpus, timeout)
        for corpus in corpora
        for qid, text in sample(corpus, limit, split)
        if qid not in done
    ]
    results = {qid: done[qid] for corpus in corpora for qid, _ in sample(corpus, limit, split) if qid in done}
    sink = log.open("a") if log is not None else None
    try:
        with Pool(jobs or os.cpu_count()) as pool:
            for query_id, result in pool.imap_unordered(run_query, work, chunksize=1):
                results[query_id] = result
                if sink is not None:
                    sink.write(json.dumps({"id": query_id, "result": result}) + "\n")
                    sink.flush()
    finally:
        if sink is not None:
            sink.close()
    return results


def verdict(result: dict) -> str:
    """One outcome per query: ``convert`` (sqlglot could not convert it; outside the score),
    ``timeout``, ``error`` (a crash outside any stage), ``fail``, ``unsupported`` (no failure,
    but some stage honestly declined) or ``pass`` (every stage gave a real answer)."""

    if result.get("convert", (PASS,))[0] != PASS:
        return "convert"
    if "timeout" in result:
        return "timeout"
    if "crash" in result:
        return "error"
    statuses = [v[0] for k, v in result.items() if k != "seconds"]
    if FAIL in statuses:
        return "fail"
    return "unsupported" if UNSUPPORTED in statuses else "pass"


OUTCOMES = ("pass", "unsupported", "fail", "timeout", "error", "convert")


def summary(results: dict[str, dict]) -> dict[str, dict]:
    by_corpus: dict[str, list[dict]] = defaultdict(list)
    for query_id, result in results.items():
        by_corpus[query_id.rsplit("/", 1)[0]].append(result)
    table = {}
    for corpus, rows in sorted(by_corpus.items()):
        verdicts = Counter(verdict(r) for r in rows)
        scored = len(rows) - verdicts["convert"]
        table[corpus] = {
            "queries": len(rows),
            "converted": scored,
            "outcomes": {o: verdicts[o] for o in OUTCOMES},
            # clean: nothing failed, timed out or crashed; full: every stage answered
            "clean": verdicts["pass"] + verdicts["unsupported"],
            "full": verdicts["pass"],
            "fail": verdicts["fail"] + verdicts["timeout"] + verdicts["error"],
            "stages": {s: dict(Counter(r[s][0] for r in rows if s in r)) for s in STAGES},
        }
    return table


def total(table: dict[str, dict]) -> dict:
    out = {"queries": 0, "converted": 0, "clean": 0, "full": 0, "fail": 0, "outcomes": Counter()}
    for row in table.values():
        for key in ("queries", "converted", "clean", "full", "fail"):
            out[key] += row[key]
        out["outcomes"].update(row["outcomes"])
    out["outcomes"] = dict(out["outcomes"])
    return out


def markdown(table: dict[str, dict]) -> str:
    def pct(n, d):
        return f"{n}/{d} ({100 * n / d:.1f}%)" if d else "–"

    lines = [
        "| Corpus | Queries | Converted | Clean | Full | pass / unsupported / fail / timeout / error | "
        + " | ".join(STAGES) + " |",
        "|---|---:|---:|---:|---:|---|" + "---|" * len(STAGES),
    ]
    rows = dict(table)
    if len(table) > 1:
        rows["**all**"] = {**total(table), "stages": {}}
    for corpus, row in rows.items():
        cells = [cov._cell(Counter(row["stages"].get(s, {}))) if row["stages"].get(s) else "" for s in STAGES]
        o = row["outcomes"]
        lines.append(
            f"| {corpus} | {row['queries']} | {row['converted']} | {pct(row['clean'], row['converted'])} "
            f"| {pct(row['full'], row['converted'])} | {o['pass']} / {o['unsupported']} / {o['fail']} / {o['timeout']} / {o['error']} | "
            + " | ".join(cells) + " |"
        )
    return "\n".join(lines) + "\n"


def failures(results: dict[str, dict], include_unsupported: bool = False) -> list[str]:
    wanted = {FAIL, UNSUPPORTED} if include_unsupported else {FAIL}
    lines = []
    for query_id in sorted(results):
        for stage, value in results[query_id].items():
            if stage != "seconds" and value[0] in wanted:
                lines.append(f"{query_id}::{stage}: {value[0]}: {value[1]}")
    return lines


def reasons(results: dict[str, dict], top: int = 15) -> str:
    """The most common reasons per stage, with digits folded so similar reasons group."""

    groups: dict[str, Counter] = defaultdict(Counter)
    for result in results.values():
        for stage, value in result.items():
            if stage != "seconds" and value[0] in (FAIL, UNSUPPORTED):
                groups[f"{stage} {value[0]}"][re.sub(r"\d+", "N", value[1])[:110]] += 1
    lines = []
    for key in sorted(groups):
        lines.append(key)
        lines += [f"  {n:5d}  {reason}" for reason, n in groups[key].most_common(top)]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("corpora", nargs="*", help="corpora to run (default: every fetched corpus)")
    parser.add_argument("--limit", type=int, default=200, help="queries per corpus, spread evenly; 0 for all")
    parser.add_argument("--jobs", type=int, default=None)
    parser.add_argument("--timeout", type=int, default=120, help="seconds per query before it counts as a failure")
    parser.add_argument("--split", choices=("dev", "held-out", "all"), default="dev",
                        help="dev while improving KumoSQL; held-out only for a final score")
    parser.add_argument("--json", type=Path, help="write per-query results here")
    parser.add_argument("--log", type=Path, help="append each result here as it finishes; a rerun resumes from it")
    parser.add_argument("--markdown", type=Path, help="write the score table here")
    parser.add_argument("--failures", action="store_true", help="list every failed stage")
    parser.add_argument("--reasons", action="store_true", help="group fail and unsupported reasons by stage")
    args = parser.parse_args(argv)
    names = args.corpora or corpora_mod.corpora()
    if not names:
        parser.error("no corpora fetched; run tools/benchmark_corpora.py fetch sqlstorm dsb")
    results = run(names, args.limit, args.jobs, args.timeout, args.log, args.split)
    table = summary(results)
    text = markdown(table)
    print(text)
    if args.json:
        args.json.write_text(json.dumps(results, indent=1, sort_keys=True))
    if args.markdown:
        args.markdown.write_text(text)
    if args.reasons:
        print(reasons(results))
    if args.failures:
        print("\n".join(failures(results)))
    return 1 if any(row["fail"] for row in table.values()) else 0


if __name__ == "__main__":
    sys.exit(main())
