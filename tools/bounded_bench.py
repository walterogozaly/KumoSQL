"""Bounded verification (z3, at most N rows per table) on the equivalence suites, and a differential test of its encoding.

    python tools/bounded_bench.py differential calcite      # the encoding vs DuckDB on random databases
    python tools/bounded_bench.py run literature --rows 3   # bounded check of the pairs the executed search leaves open

``differential`` compiles every query of a suite to its symbolic relation, pins the symbolic database to a
random concrete one, and compares the result with what DuckDB returns for the same query. Any mismatch
is a bug in the encoding (a bounded verdict could then be wrong), so ``wrong`` must stay 0 before the
``run`` numbers mean anything.

``run`` checks each pair with ``kumosql.bounded_equivalence``. Evidence levels are kept apart:
``different`` is a counterexample replayed on DuckDB, ``bounded`` is "no counterexample within N rows per
table" (not a proof), ``unknown`` is anything else (unsupported SQL, timeout, unconfirmed model).
"""

from __future__ import annotations

import argparse
import multiprocessing
import random
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))

import sqlglot  # noqa: E402
import z3  # noqa: E402

import verieql_bench as vb  # noqa: E402
from kumosql import bounded_equivalence as be  # noqa: E402
from kumosql import counterexample as cx  # noqa: E402

# --- VeriEQL cases -> bounded schema -------------------------------------------------------------


def schema_for_case(case: dict) -> be.BoundedSchema:
    """The case's tables, keys, NOT NULLs, foreign keys, consecutive ids and predicates as a bounded schema."""

    tables: dict[str, be.BTable] = {}
    for name, columns in case["schema"].items():
        cols = []
        for column, kind in columns.items():
            if kind.startswith("ENUM"):
                cols.append(be.BColumn(column, "ENUM", values=tuple(kind.split(",")[1:])))
            else:
                cols.append(be.BColumn(column, kind))
        tables[name] = be.BTable(name, cols)
    schema = be.BoundedSchema(tables)

    def split(ref: str) -> tuple[str, str]:
        for table in tables:
            if ref.startswith(table + "__") and ref[len(table) + 2 :] in {c.name for c in tables[table].columns}:
                return table, ref[len(table) + 2 :]
        raise KeyError(ref)

    for constraint in case.get("constraint") or []:
        (kind, body), = constraint.items()
        if kind == "primary":
            refs = [split(v["value"]) for v in body]
            table = tables[refs[0][0]]
            # a table has one primary key: as in the shared harness, a later one replaces an earlier one
            if getattr(table, 'primary_index', None) is not None:
                table.keys[table.primary_index] = tuple(c for _, c in refs)
            else:
                table.primary_index = len(table.keys)
                table.keys.append(tuple(c for _, c in refs))
            for _, c in refs:
                table.column(c).not_null = True
        elif kind == "not_null":
            t, c = split(body["value"])
            tables[t].column(c).not_null = True
        elif kind == "foreign":
            (ct, cc), (pt, pc) = split(body[0]["value"]), split(body[1]["value"])
            tables[ct].foreign_keys.append(((cc,), pt, (pc,)))
        elif kind == "unique":
            refs = [split(v["value"]) for v in body]
            tables[refs[0][0]].keys.append(tuple(c for _, c in refs))
        elif kind in ("inc", "consec"):
            t, c = split(body["value"])
            tables[t].column(c).not_null = True
            schema.extra.append(_consecutive(t, c))
        else:
            schema.extra.append(_predicate(constraint, split))
    return schema


def _consecutive(table: str, column: str):
    def build(db: be.SymbolicDatabase) -> list:
        spec = db.schema.tables[table]
        index = [c.name for c in spec.columns].index(column)
        return [z3.Implies(slot.present, slot.vals[index].val == position + 1) for position, slot in enumerate(db.tables[table])]

    return build


def _predicate(constraint: dict, split):
    """A cross-row predicate (VeriEQL JSON): holds for every combination of present rows of the tables it names."""

    used: list[str] = []

    def compile_expr(node):
        if isinstance(node, dict):
            if "value" in node:
                t, c = split(node["value"])
                if t not in used:
                    used.append(t)
                return lambda env, t=t, c=c: env[(t, c)]
            if "literal" in node:
                value = node["literal"]
                return lambda env, v=value: _literal(v)
            if "date" in node:
                return lambda env, v=node["date"]: be.const(__import__("datetime").date.fromisoformat(v), "date")
            (op, args), = node.items()
            if op in ("and", "or"):
                parts = [compile_expr(a) for a in args]
                join = be.logical_and if op == "and" else be.logical_or
                return lambda env: join([p(env) for p in parts])
            if op == "not":
                inner = compile_expr(args)
                return lambda env: be.logical_not(inner(env))
            if op == "imply":
                a, b = compile_expr(args[0]), compile_expr(args[1])
                return lambda env: be.logical_or([be.logical_not(a(env)), b(env)])
            if op == "in":
                value, options = compile_expr(args[0]), [compile_expr(a) for a in args[1]]
                return lambda env: be.logical_or([be.compare("eq", value(env), o(env)) for o in options])
            if op == "between":
                v, lo, hi = (compile_expr(a) for a in args)
                return lambda env: be.logical_and([be.compare("gte", v(env), lo(env)), be.compare("lte", v(env), hi(env))])
            if op in ("gt", "gte", "lt", "lte", "eq", "neq"):
                a, b = compile_expr(args[0]), compile_expr(args[1])
                return lambda env: be.compare(op, a(env), b(env))
            raise KeyError(op)
        return lambda env, v=node: _literal(v)

    test = compile_expr(constraint)
    names = list(used)

    def build(db: be.SymbolicDatabase) -> list:
        import itertools

        spec = db.schema.tables
        out = []
        slots = [db.tables[n] for n in names]
        for combo in itertools.product(*slots):
            env = {}
            for name, row in zip(names, combo):
                for column, value in zip(spec[name].columns, row.vals):
                    env[(name, column.name)] = value
            out.append(z3.Implies(z3.And(*[r.present for r in combo]), be.truth(test(env))))
        return out

    return build


def _literal(value):
    if isinstance(value, bool):
        return be.const(value, "bool")
    if isinstance(value, int):
        return be.const(value, "int")
    if isinstance(value, float):
        return be.const(be.Fraction(str(value)), "real")
    return be.const(str(value), "str")


# --- differential test ---------------------------------------------------------------------------


def differential_case(case: dict, trials: int = 12) -> Counter:
    """Encoding vs DuckDB for both queries of one case on random constraint-respecting databases."""

    out: Counter = Counter()
    try:
        spec = vb.build_spec(case)
        schema = schema_for_case(case)
    except Exception:
        out["skipped: constraints"] += 1
        return out
    left, right = case["pair"]
    try:
        searcher = cx.Searcher(spec, left, right)
    except Exception:
        out["skipped: parse"] += 1
        return out
    if not searcher.runs():
        out["skipped: DuckDB rejects"] += 1
        return out
    generator = cx._Generator(spec, searcher.constants, random.Random(case["index"]))
    for sql, duck in ((left, searcher.left_sql), (right, searcher.right_sql)):
        status = None
        for trial in range(trials):
            data = generator.database(searcher.used, 3 if trial % 2 else 2)
            if data is None:
                continue
            data = {name: rows for name, rows in data.items()}
            try:
                try:
                    mine = be.evaluate(sql, schema, data, dialect="mysql")
                except be.Unsupported as error:
                    status = f"unsupported: {str(error)[:50]}"
                    break
                for name in searcher.used:
                    table = spec.tables[name]
                    searcher.db.execute(f'DELETE FROM "{name}"')
                    if data[name]:
                        searcher.db.executemany(f'INSERT INTO "{name}" VALUES ({", ".join("?" * len(table.columns))})', data[name])
                theirs = searcher.db.execute(duck).fetchall()
            except Exception as error:
                status = f"error: {type(error).__name__}"
                break
            if cx._bag(mine) != cx._bag(theirs):
                # an ungrouped column or a LIMIT tie has an arbitrary answer: only a stable result counts
                if _stable(searcher, spec, data, duck):
                    status = "MISMATCH"
                    out["mismatch sample"] += 0
                    out[f"mismatch {case['index']}: {sql[:90]}"] += 1
                    break
        out[status or "agree"] += 1
    return out


def _stable(searcher, spec, data, duck) -> bool:
    rng = random.Random(1)
    try:
        seen = None
        for _ in range(3):
            for name in searcher.used:
                rows = list(data[name])
                rng.shuffle(rows)
                searcher.db.execute(f'DELETE FROM "{name}"')
                if rows:
                    searcher.db.executemany(f'INSERT INTO "{name}" VALUES ({", ".join("?" * len(spec.tables[name].columns))})', rows)
            result = cx._bag(searcher.db.execute(duck).fetchall())
            if seen is not None and result != seen:
                return False
            seen = result
    except Exception:
        return False
    return True


def _differential_work(case):
    try:
        return differential_case(case)
    except Exception as error:  # pragma: no cover
        return Counter({f"crash {type(error).__name__}: {str(error)[:60]}": 1})


def _differential_child(case, queue):
    queue.put(_differential_work(case))


def _guarded_differential(case, seconds: float = 120.0):
    """One case in its own process, killed after `seconds` (a compile that never ends must not stall the suite)."""

    context = multiprocessing.get_context("fork")
    queue = context.Queue()
    process = context.Process(target=_differential_child, args=(case, queue))
    process.start()
    try:
        return queue.get(timeout=seconds)
    except Exception:
        return Counter({"timeout": 1})
    finally:
        process.kill()
        process.join()


def differential(suite: str, jobs: int, every: int, limit: int | None) -> int:
    from concurrent.futures import ThreadPoolExecutor

    cases = vb.load_cases(suite)[::every][:limit]
    with ThreadPoolExecutor(jobs) as pool:
        results = list(pool.map(_guarded_differential, cases))
    total: Counter = Counter()
    for r in results:
        total.update(r)
    mismatches = sorted(k for k in total if k.startswith("mismatch "))
    for key, value in sorted(total.items(), key=lambda kv: -kv[1]):
        if not key.startswith("mismatch ") and key != "mismatch sample":
            print(f"{value:6}  {key}")
    for key in mismatches:
        print("MISMATCH", key)
    return 1 if mismatches else 0


# --- bounded run ---------------------------------------------------------------------------------


def _status(result, rows: int) -> str:
    """bounded (the whole requested bound), partial (a smaller bound finished before a timeout), different or unknown."""

    if result.status is be.BoundedStatus.BOUNDED_EQUIVALENT:
        return "bounded" if result.bound >= rows else "partial"
    return {"different": "different", "unknown": "unknown"}[result.status.value]


def _sizes(data: dict) -> int:
    return max([len(rows) for rows in data.values()] + [0])


def published_size(case: dict, record: dict) -> tuple[bool | None, int]:
    """Does VeriEQL's published counterexample differ on DuckDB, and what is its largest table?"""

    import duckdb

    script = record.get("counterexample")
    if not script:
        return None, 0
    from kumosql.duckdb_load import small_database
    db = small_database()
    results = []
    try:
        for statement in [x for x in sqlglot.parse(script, read="mysql") if x is not None]:
            sql = statement.sql(dialect="duckdb")
            if statement.key == "select":
                results.append(cx._bag(db.execute(sql).fetchall()))
            else:
                db.execute(sql)
        rows = 0
        for (name,) in db.execute("select table_name from information_schema.tables").fetchall():
            rows = max(rows, db.execute(f'select count(*) from "{name}"').fetchone()[0])
    except Exception:
        return None, 0
    return (results[-2] != results[-1] if len(results) >= 2 else None), rows


def run_case(args):
    """Baseline verdict (the executed search and the unbounded prover), then the bounded check, then cross-checks."""

    case, rows, timeout_ms, budget, states = args
    index = case["index"]
    out = {"index": index}
    base = vb.decide(case)
    out["baseline"] = base.status
    try:
        schema = schema_for_case(case)
    except Exception as error:
        out.update(bounded="unknown", reason=f"constraints: {type(error).__name__}", bound=0, seconds=0.0)
        return out
    left, right = case["pair"]
    try:
        result = be.check_bounded(left, right, schema, rows=rows, dialect="mysql", timeout_ms=timeout_ms, budget_s=budget,
                                  replay=be.DuckDBReplay(schema, left, right, "mysql"))
    except Exception as error:
        out.update(bounded="unknown", reason=f"crash: {type(error).__name__}: {str(error)[:60]}", bound=0, seconds=0.0)
        return out
    out.update(
        bounded=_status(result, rows),
        reason=result.reason, bound=result.bound, seconds=round(result.seconds, 2),
    )
    if result.counterexample is not None:
        out["counterexample"] = {k: [list(map(str, r)) for r in v] for k, v in result.counterexample.items()}
    # cross-checks: any contradiction is a bug in the encoding (or the prover) and counts as wrong
    flags = []
    if result.bounded_equivalent:
        if base.status == vb.DIFFERENT:
            flags.append("bounded vs executed counterexample")  # the executed one may be bigger than the bound
        record = states.get(tuple(case["pair"]))
        if record:
            differs, size = published_size(case, record)
            if differs and size <= result.bound:
                flags.append(f"WRONG: VeriEQL's published counterexample ({size} rows per table) differs")
        try:
            searcher = cx.Searcher(vb.build_spec(case), left, right)
            found = searcher.search(300, seed=index + 7)
            if found is not None and _sizes(found.tables) <= result.bound:
                flags.append("WRONG: random search found a counterexample within the bound")
        except Exception:
            pass
    if result.status is be.BoundedStatus.DIFFERENT and base.status == vb.EQUIVALENT:
        flags.append("WRONG: counterexample against a proof")
    if flags:
        out["flags"] = flags
    return out


def run(suite: str, rows: int, jobs: int, every: int, limit: int | None, timeout_ms: int, budget: float, dump: str | None) -> int:
    import json

    cases = vb.load_cases(suite)[::every][:limit]
    states = vb.load_veri_states(suite)
    began = time.time()
    with multiprocessing.Pool(jobs) as pool:
        results = pool.map(run_case, [(c, rows, timeout_ms, budget, states) for c in cases], chunksize=1)
    counts = Counter(r["bounded"] for r in results)
    print(f"{suite}: {len(cases)} cases at {rows} rows: {dict(counts)} in {time.time() - began:.0f}s")
    table = Counter((r["baseline"], r["bounded"]) for r in results)
    for (baseline, bounded), count in sorted(table.items()):
        print(f"   executed+prover {baseline:10} bounded {bounded:10} {count}")
    reasons = Counter(r["reason"].split(" at ")[0][:70] for r in results if r["bounded"] == "unknown")
    for reason, count in reasons.most_common(15):
        print(f"{count:6}  unknown: {reason}")
    wrong = [r for r in results if any(f.startswith("WRONG") for f in r.get("flags", []))]
    print(f"WRONG: {len(wrong)}", [(r["index"], r["flags"]) for r in wrong])
    if dump:
        with open(dump, "w", encoding="utf-8") as out:
            for r in results:
                out.write(json.dumps(r) + "\n")
    return 1 if wrong else 0


# --- the SQLSolver-style suites (SQLSolver, QED, R-Bot, Cosette, SPES) --------------------------------

import sqlsolver_bench as sb  # noqa: E402

PAIR_SUITES = ("sqlsolver-calcite", "sqlsolver-spark", "sqlsolver-tpch", "sqlsolver-tpcc", "qed", "rbot", "cosette", "spes")


def schema_from_tables(tables) -> be.BoundedSchema:
    """The shared harness' tables (columns, NOT NULLs, primary and unique keys) as a bounded schema."""

    out = {}
    for table in tables.values():
        out[table.name] = be.BTable(
            table.name,
            [be.BColumn(c.name, c.type, c.not_null) for c in table.columns],
            keys=([tuple(table.primary_key)] if table.primary_key else []) + [tuple(k) for k in table.unique],
        )
    return be.BoundedSchema(out)


def _ddl_tables(ddl: str):
    import tempfile

    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "schema.sql"
        path.write_text(ddl, encoding="utf-8")
        return sb.load_schema(path)


def pair_cases(suite: str) -> list[dict]:
    """``name, left, right, tables, constants, label`` for each pair of a suite."""

    import json

    out = []
    if suite.startswith("sqlsolver-"):
        name = suite.split("-", 1)[1]
        pairs_file, schema_file = sb.SUITES[name]
        tables = sb.load_schema(sb.FIXTURES / schema_file)
        for index, (left, right) in enumerate(sb.load_pairs(sb.FIXTURES / pairs_file)):
            out.append(dict(name=f"{name}-{index}", left=left, right=right, tables=tables, constants=name in sb.CONSTANT_GROUPING, label="equivalent"))
    elif suite == "qed":
        import qed_bench

        schemas = {}
        for case in qed_bench.load_cases():
            tables = schemas.setdefault(case["schema_id"], _ddl_tables(case["ddl"]))
            out.append(dict(name=case["name"], left=case["sql_a"], right=case["sql_b"], tables=tables, constants=False, label="equivalent"))
    elif suite == "rbot":
        import rbot_bench

        tables = sb.load_schema(rbot_bench.FIXTURES / "create_tables.sql")
        for name, left, right in rbot_bench.load_pairs():
            try:
                left, right = rbot_bench.normalise(left), rbot_bench.normalise(right)
            except Exception:
                continue
            out.append(dict(name=name, left=left, right=right, tables=tables, constants=True, label="equivalent"))
    else:
        import cosette_bench

        schemas = {}
        for case in cosette_bench.load(suite):
            tables = schemas.setdefault(case["ddl"], _ddl_tables(case["ddl"]))
            out.append(dict(name=case["name"], left=case["sql_a"], right=case["sql_b"], tables=tables, constants=suite == "spes", label=case.get("label", "equivalent")))
    return out


def pair_work(args):
    case, rows, timeout_ms, budget = args
    tables, left, right, constants = case["tables"], case["left"], case["right"], case["constants"]
    out = {"name": case["name"], "label": case["label"]}
    try:
        proof = bool(sb.default_prove(left, right, tables, constants) if constants else sb.default_prove(left, right, tables))
    except Exception:
        proof = False
    db = sb.new_database(tables)
    counter = sb.differ(left, right, tables, db, 30, constants=constants)
    executed = counter not in (None, False)
    out["baseline"] = "proven" if proof else "refuted" if executed else "unknown"
    schema = schema_from_tables(tables)

    def prepare(sql):
        sql = sb.spark_days(sql)
        return sb.name_values(sql) if constants else sql

    def translate(sql):
        text = sb.to_dialect(prepare(sql), "duckdb")
        return sb.constant_groupings(text) if constants else text

    try:
        result = be.check_bounded(left, right, schema, rows=rows, dialect="mysql", timeout_ms=timeout_ms, budget_s=budget,
                                  replay=be.DuckDBReplay(schema, left, right, "mysql", translate=translate),                                   prepare=prepare, group_constants=constants)
    except Exception as error:
        out.update(bounded="unknown", reason=f"crash: {type(error).__name__}: {str(error)[:60]}", bound=0, seconds=0.0)
        return out
    out.update(
        bounded=_status(result, rows),
        reason=result.reason, bound=result.bound, seconds=round(result.seconds, 2),
    )
    if result.counterexample is not None:
        out["counterexample"] = {k: [list(map(str, r)) for r in v] for k, v in result.counterexample.items()}
    flags = []
    if proof and result.status is be.BoundedStatus.DIFFERENT:
        flags.append("WRONG: counterexample against a proof")
    if result.status is be.BoundedStatus.DIFFERENT and case["label"] == "equivalent" and not proof:
        flags.append("label dispute: counterexample for a pair the authors call equivalent")
    if result.bounded_equivalent and case["label"] == "not_equivalent":
        flags.append("label dispute: no counterexample within the bound for a pair the authors call different")
    if flags:
        out["flags"] = flags
    return out


def run_pairs(suite: str, rows: int, jobs: int, limit: int | None, timeout_ms: int, budget: float, dump: str | None) -> int:
    import json

    cases = pair_cases(suite)[:limit]
    began = time.time()
    with multiprocessing.Pool(jobs) as pool:
        results = pool.map(pair_work, [(c, rows, timeout_ms, budget) for c in cases], chunksize=1)
    counts = Counter(r["bounded"] for r in results)
    print(f"{suite}: {len(cases)} pairs at {rows} rows: {dict(counts)} in {time.time() - began:.0f}s")
    for (baseline, bounded), count in sorted(Counter((r["baseline"], r["bounded"]) for r in results).items()):
        print(f"   baseline {baseline:8} bounded {bounded:10} {count}")
    for reason, count in Counter(r["reason"].split(" at ")[0][:70] for r in results if r["bounded"] == "unknown").most_common(15):
        print(f"{count:6}  unknown: {reason}")
    wrong = [r for r in results if any(f.startswith("WRONG") for f in r.get("flags", []))]
    disputes = [r["name"] for r in results if any(f.startswith("label dispute") for f in r.get("flags", []))]
    print(f"WRONG: {len(wrong)}", [(r["name"], r["flags"]) for r in wrong])
    print(f"label disputes: {len(disputes)}", disputes[:20])
    if dump:
        with open(dump, "w", encoding="utf-8") as out:
            for r in results:
                out.write(json.dumps(r) + "\n")
    return 1 if wrong else 0


# --- Singh & Bedathur LeetCode pairs ---------------------------------------------------------------


def singh_work(args):
    """The suite's own verdict first; the bounded check runs on the pairs it leaves unknown, and on every proof as a cross-check."""

    import singh_bedathur_bench as sbb

    pair, rows, timeout_ms, budget, check_all = args
    out = {"name": pair.key}
    verdict = sbb.decide(pair, 300)
    out["baseline"] = verdict.outcome or verdict.kind
    if verdict.kind == "different" and not check_all:
        out.update(bounded="skipped", reason="already refuted by the suite", bound=0, seconds=0.0)
        return out
    trees = [sqlglot.parse_one(pair.left, read="mysql"), sqlglot.parse_one(pair.right, read="mysql")]
    kinds = sbb.column_kinds(trees, pair.tables)
    names = {"VARCHAR": "VARCHAR", "DATE": "DATE"}
    schema = be.BoundedSchema({
        table: be.BTable(table, [be.BColumn(c, names.get(kinds.get((table, c), "BIGINT"), "DECIMAL" if str(kinds.get((table, c), "")).startswith("DECIMAL") else "BIGINT")) for c in columns])
        for table, columns in pair.tables.items()
    })
    try:
        result = be.check_bounded(pair.left, pair.right, schema, rows=rows, dialect="mysql", timeout_ms=timeout_ms, budget_s=budget)
    except Exception as error:
        out.update(bounded="unknown", reason=f"crash: {type(error).__name__}: {str(error)[:60]}", bound=0, seconds=0.0)
        return out
    out.update(
        bounded=_status(result, rows),
        reason=result.reason, bound=result.bound, seconds=round(result.seconds, 2),
    )
    if result.counterexample is not None:
        out["counterexample"] = {k: [list(map(str, r)) for r in v] for k, v in result.counterexample.items()}
    flags = []
    if verdict.kind == "equivalent" and result.status is be.BoundedStatus.DIFFERENT:
        flags.append("WRONG: counterexample against a proof")
    if verdict.kind == "different" and result.bounded_equivalent and result.bound >= 4:
        flags.append("bounded vs executed counterexample")
    if flags:
        out["flags"] = flags
    return out


def run_singh(rows: int, jobs: int, every: int, limit: int | None, timeout_ms: int, budget: float, dump: str | None, check_all: bool) -> int:
    import json

    import singh_bedathur_bench as sbb

    pairs = sbb.load_pairs()[::every][:limit]
    began = time.time()
    with multiprocessing.Pool(jobs) as pool:
        results = pool.map(singh_work, [(p, rows, timeout_ms, budget, check_all) for p in pairs], chunksize=1)
    print(f"singh: {len(pairs)} pairs at {rows} rows: {dict(Counter(r['bounded'] for r in results))} in {time.time() - began:.0f}s")
    for (baseline, bounded), count in sorted(Counter((r["baseline"], r["bounded"]) for r in results).items()):
        print(f"   baseline {baseline:12} bounded {bounded:10} {count}")
    for reason, count in Counter(r["reason"].split(" at ")[0][:70] for r in results if r["bounded"] == "unknown").most_common(15):
        print(f"{count:6}  unknown: {reason}")
    wrong = [r for r in results if any(f.startswith("WRONG") for f in r.get("flags", []))]
    print(f"WRONG: {len(wrong)}", [(r["name"], r["flags"]) for r in wrong])
    if dump:
        with open(dump, "w", encoding="utf-8") as out:
            for r in results:
                out.write(json.dumps(r) + "\n")
    return 1 if wrong else 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("mode", choices=["differential", "run"])
    parser.add_argument("suite", choices=list(vb.SUITES) + list(PAIR_SUITES) + ["singh"])
    parser.add_argument("--check-all", action="store_true", help="singh: also run the bounded check on pairs the suite already refuted")
    parser.add_argument("--rows", type=int, default=3)
    parser.add_argument("--jobs", type=int, default=4)
    parser.add_argument("--every", type=int, default=1)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--timeout-ms", type=int, default=20000)
    parser.add_argument("--budget", type=float, default=60.0)
    parser.add_argument("--dump", help="write one JSON line per case to this file")
    args = parser.parse_args(argv)
    if args.suite == "singh":
        return run_singh(args.rows, args.jobs, args.every, args.limit, args.timeout_ms, args.budget, args.dump, args.check_all)
    if args.suite in PAIR_SUITES:
        if args.mode == "differential":
            raise SystemExit("differential testing runs on the VeriEQL suites")
        return run_pairs(args.suite, args.rows, args.jobs, args.limit, args.timeout_ms, args.budget, args.dump)
    if args.mode == "differential":
        return differential(args.suite, args.jobs, args.every, args.limit)
    return run(args.suite, args.rows, args.jobs, args.every, args.limit, args.timeout_ms, args.budget, args.dump)


if __name__ == "__main__":
    raise SystemExit(main())
