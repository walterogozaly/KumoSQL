"""Adapt sqlglot's optimizer fixtures into table-minimization cases.

sqlglot's optimizer tests (MIT) hold before/after pairs for rules that make a query simpler
without changing its rows: ``merge_subqueries`` (derived tables and CTEs merged into the outer
query), ``eliminate_ctes`` (unused CTEs) and ``eliminate_joins`` (LEFT JOINs that cannot change
the rows). Each "before" query is split into a pipeline: every CTE and every derived table in a
FROM or JOIN becomes its own table, and the outer query becomes the protected table ``result``.
sqlglot's expected output, as one table, is the reference. So a case asks: given these tables,
with only ``result`` protected, find something at least as simple as sqlglot's answer.

Every kept case is checked on DuckDB with the optimizer off: the unsplit input, the split
pipeline and the reference return the same bag of rows, with the same column names, on random
databases (NULLs, duplicates, empty tables). A pair that fails, uses LIMIT, a source outside the
plain test schema or SQL that does not survive the trip to BigQuery and back, is left out and
listed in the summary.

    git clone https://github.com/tobymao/sqlglot && git -C sqlglot checkout <commit>
    python tools/make_sqlglot_minimization_cases.py path/to/sqlglot \
        --out benchmarks/table_minimization/sourced-sqlglot.jsonl

The output is deterministic for a given sqlglot checkout and seed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import random
import subprocess
import sys

import duckdb
import sqlglot
from sqlglot import exp

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from kumosql.formatting import pipeline_complexity  # noqa: E402

FIXTURES = ("merge_subqueries", "eliminate_ctes", "eliminate_joins")
FAMILY = {
    "merge_subqueries": ["mergeable_tables", "passthrough_chain"],
    "eliminate_ctes": ["dead_tables"],
    "eliminate_joins": ["unused_columns_joins"],
}
# The plain part of sqlglot's test schema (tests/test_optimizer.py); ``nn`` is NOT NULL.
SOURCES = {
    "x": {"columns": {"a": "INT64", "b": "INT64"}},
    "y": {"columns": {"b": "INT64", "c": "INT64"}},
    "z": {"columns": {"b": "INT64", "c": "INT64"}},
    "w": {"columns": {"d": "STRING", "e": "STRING"}},
    "nn": {"columns": {"a": "INT64", "b": "INT64"}, "not_null": ["a", "b"]},
    "t_bool": {"columns": {"a": "BOOL", "b": "BOOL"}},
}
DUCK_TYPES = {"INT64": "BIGINT", "STRING": "VARCHAR", "BOOL": "BOOLEAN"}
VALUES = {"INT64": [1, 2, 3], "STRING": ["a", "b"], "BOOL": [True, False]}
DATABASES = 60
SEED = 7


def read_fixture(path: Path) -> list[dict]:
    """``# key: value`` headers, then the input and the expected output, each ending in ``;``."""

    pairs, meta, statements, buffer = [], {}, [], []
    for line in path.read_text().splitlines():
        if line.startswith("#") and not buffer:
            key, _, value = line[1:].partition(":")
            meta[key.strip()] = value.strip()
            continue
        if not line.strip() and not buffer:
            continue
        buffer.append(line)
        if line.rstrip().endswith(";"):
            statements.append("\n".join(buffer).rstrip().rstrip(";"))
            buffer = []
            if len(statements) == 2:
                pairs.append({"meta": meta, "input": statements[0], "expected": statements[1]})
                meta, statements = {}, []
    return pairs


def _fresh(name: str, taken: set[str]) -> str:
    base = name.lower() or "sub"
    if base in SOURCES or base == "result":
        base = f"{base}_t"
    candidate, n = base, 2
    while candidate in taken:
        candidate, n = f"{base}_{n}", n + 1
    taken.add(candidate)
    return candidate


def split(sql: str) -> dict[str, str] | None:
    """The query as a pipeline: CTEs and FROM/JOIN derived tables become tables, the rest ``result``."""

    tables: dict[str, exp.Expression] = {}
    taken: set[str] = set()

    def rename(tree: exp.Expression, old: str, new: str) -> None:
        for table in tree.find_all(exp.Table):
            if not table.args.get("db") and table.name.lower() == old.lower():
                alias = table.alias or old
                table.replace(exp.to_table(new).as_(alias) if alias != new else exp.to_table(new))

    def process(query: exp.Expression) -> exp.Expression:
        with_ = query.args.get("with_") or query.args.get("with")
        if with_ is not None:
            if with_.args.get("recursive"):
                raise ValueError("recursive CTE")
            ctes = list(with_.expressions)
            query.set("with_" if "with_" in query.args else "with", None)
            for i, cte in enumerate(ctes):
                name = _fresh(cte.alias, taken)
                tables[name] = process(cte.this.copy())
                for later in ctes[i + 1:]:
                    rename(later.this, cte.alias, name)
                rename(query, cte.alias, name)
        for source in list(_from_sources(query)):
            if isinstance(source, exp.Subquery) and isinstance(source.this, (exp.Select, exp.SetOperation)):
                alias = source.alias
                name = _fresh(alias or "sub", taken)
                tables[name] = process(source.this.copy())
                table = exp.to_table(name)
                columns = source.args.get("alias") and source.args["alias"].columns
                if columns:
                    raise ValueError("derived table with a column list")
                source.replace(table.as_(alias) if alias and alias != name else table)
        if isinstance(query, exp.SetOperation):
            query.set("this", process(query.this))
            query.set("expression", process(query.expression))
        return query

    tree = sqlglot.parse_one(sql)
    if tree.find(exp.Lateral) or tree.find(exp.Unnest):
        raise ValueError("LATERAL or UNNEST")
    result = process(tree)
    tables["result"] = result
    if len(tables) == 1:
        return None
    return {name: node.sql(dialect="bigquery") for name, node in tables.items()}


def _from_sources(query: exp.Expression):
    """Sources in this query's own FROM and JOINs (not inside expression subqueries)."""

    if not isinstance(query, exp.Select):
        return
    from_ = query.args.get("from_") or query.args.get("from")
    if from_ is not None:
        yield from_.this
    for join in query.args.get("joins") or []:
        yield join.this


def _referenced(sql: str) -> set[str]:
    return {t.name.lower() for t in sqlglot.parse_one(sql, read="bigquery").find_all(exp.Table)}


def _order(tables: dict[str, str]) -> list[str]:
    done: list[str] = []
    pending = dict(tables)
    while pending:
        ready = [n for n, sql in pending.items() if not (_referenced(sql) & (set(pending) - {n}))]
        if not ready:
            raise ValueError("cycle")
        for name in ready:
            done.append(name)
            del pending[name]
    return done


def _random_rows(rng: random.Random, source: str) -> list[list]:
    spec = SOURCES[source]
    rows = []
    for _ in range(rng.choice([0, 1, 2, 3, 4, 5, 6, 6])):
        row = []
        for column, kind in spec["columns"].items():
            nullable = column not in spec.get("not_null", [])
            row.append(None if nullable and rng.random() < 0.2 else rng.choice(VALUES[kind]))
        rows.append(row)
    return rows


def _key(row):
    return tuple((value is None, str(type(value)), value if value is not None else 0) for value in row)


def _run(sources: dict[str, list[list]], tables: dict[str, str] | None, query: str | None, read: str):
    db = duckdb.connect()
    for name, rows in sources.items():
        cols = ", ".join(f"{c} {DUCK_TYPES[t]}" for c, t in SOURCES[name]["columns"].items())
        db.execute(f"CREATE TABLE {name} ({cols})")
        if rows:
            marks = ", ".join("?" for _ in SOURCES[name]["columns"])
            db.executemany(f"INSERT INTO {name} VALUES ({marks})", rows)
    db.execute("PRAGMA disable_optimizer")
    if tables is not None:
        for name in _order(tables):
            duck = sqlglot.transpile(tables[name], read=read, write="duckdb")[0]
            db.execute(f"CREATE TABLE {name} AS {duck}")
        cursor = db.execute("SELECT * FROM result")
    else:
        cursor = db.execute(sqlglot.transpile(query, read=read, write="duckdb")[0])
    rows = cursor.fetchall()
    names = [d[0] for d in cursor.description]
    db.close()
    return names, sorted(rows, key=_key)


def verify(original_sql: str, pipeline: dict[str, str], reference: dict[str, str], used: set[str]) -> str | None:
    """None when all three agree on every random database, else why not."""

    rng = random.Random(SEED)
    for i in range(DATABASES):
        data = {name: _random_rows(rng, name) for name in sorted(used)}
        try:
            base = _run(data, None, original_sql, "")
            split_ = _run(data, pipeline, None, "bigquery")
            ref = _run(data, reference, None, "bigquery")
        except Exception as error:  # noqa: BLE001 - any failure leaves the case out
            return f"does not run on DuckDB: {str(error).splitlines()[0][:120]}"
        if split_ != base:
            return f"split pipeline differs from the input on database {i}"
        if ref[1] != base[1] or [n.lower() for n in ref[0]] != [n.lower() for n in base[0]]:
            return f"sqlglot's output differs from the input on database {i}"
    return None


def _mutants(sql: str):
    """Tempting one-step edits of a single query: a dropped filter conjunct, an outer join made inner,
    a dropped DISTINCT."""

    tree = sqlglot.parse_one(sql, read="bigquery")
    for where in tree.find_all(exp.Where):
        conjuncts = list(where.this.flatten()) if isinstance(where.this, exp.And) else [where.this]
        for i, dropped in enumerate(conjuncts):
            copy = tree.copy()
            target = next(w for w in copy.find_all(exp.Where) if w == where)
            rest = [c.copy() for j, c in enumerate(conjuncts) if j != i]
            if rest:
                target.set("this", exp.and_(*rest))
            else:
                target.pop()
            yield f"drops the filter {dropped.sql(dialect='bigquery')}", copy.sql(dialect="bigquery")
    for index, join in enumerate(tree.find_all(exp.Join)):
        if join.side:
            copy = tree.copy()
            target = list(copy.find_all(exp.Join))[index]
            target.set("side", None)
            yield f"turns the {join.side} JOIN into an inner join", copy.sql(dialect="bigquery")
    for index, select in enumerate(tree.find_all(exp.Select)):
        if select.args.get("distinct"):
            copy = tree.copy()
            list(copy.find_all(exp.Select))[index].set("distinct", None)
            yield "drops a DISTINCT", copy.sql(dialect="bigquery")


def traps(original_sql: str, reference_sql: str, used: set[str], limit: int = 2) -> list[dict]:
    """Mutants of the reference that differ from the input, each with a database that shows it."""

    found = []
    for note, mutant in _mutants(reference_sql):
        rng = random.Random(SEED + 1)
        for _ in range(DATABASES):
            data = {name: _random_rows(rng, name) for name in sorted(used)}
            try:
                base = _run(data, None, original_sql, "")
                bad = _run(data, {"result": mutant}, None, "bigquery")
            except Exception:  # noqa: BLE001 - a mutant that does not run is no trap
                break
            if bad[1] != base[1]:
                found.append({"note": note, "tables": {"result": mutant}, "changes": ["result"],
                              "witness": {name: rows for name, rows in data.items()}})
                break
        if len(found) == limit:
            break
    return found


def _as_one_query(tables: dict[str, str]) -> str:
    order = _order(tables)
    if order == ["result"]:
        return tables["result"]
    return "WITH " + ", ".join(f"{name} AS ({tables[name]})" for name in order) + " SELECT * FROM result"


def proved(pipeline: dict[str, str], reference: dict[str, str], sources: set[str]) -> bool:
    """Whether KumoSQL's algebraic prover shows the reference's ``result`` equal to the pipeline's."""

    from kumosql.algebraic_equivalence import prove_equivalent_algebraic
    from kumosql.smt_equivalence import SmtStatus

    if pipeline == reference:
        return True
    try:
        result = prove_equivalent_algebraic(
            _as_one_query(pipeline), _as_one_query(reference),
            schema={name: list(SOURCES[name]["columns"]) for name in sources}, compare_names=True, timeout_ms=5000)
    except Exception:  # noqa: BLE001 - a prover failure is "not proved"
        return False
    return result.status is SmtStatus.PROVEN_EQUIVALENT


def convert(sqlglot_dir: Path) -> tuple[list[dict], list[dict]]:
    commit = subprocess.run(["git", "-C", str(sqlglot_dir), "rev-parse", "HEAD"],
                            capture_output=True, text=True, check=True).stdout.strip()
    cases, skipped = [], []
    for fixture in FIXTURES:
        pairs = read_fixture(sqlglot_dir / "tests" / "fixtures" / "optimizer" / f"{fixture}.sql")
        for index, pair in enumerate(pairs, 1):
            title = pair["meta"].get("title", "")
            where = f"{fixture} #{index} {title}".strip()

            def skip(reason: str) -> None:
                skipped.append({"fixture": fixture, "index": index, "title": title, "reason": reason})

            if any(k not in ("title", "note", "leave_tables_isolated", "execute") for k in pair["meta"]):
                skip("needs options: " + ", ".join(sorted(pair["meta"])))
                continue
            try:
                tree = sqlglot.parse_one(pair["input"])
                expected = sqlglot.parse_one(pair["expected"])
            except Exception as error:  # noqa: BLE001
                skip(f"does not parse: {error}")
                continue
            if tree.find(exp.Limit) or tree.find(exp.Rand) or tree.find(exp.Order) and tree.find(exp.Window):
                skip("LIMIT, RAND or ordered window: rows are not a fixed bag")
                continue
            used = {t.name.lower() for t in tree.find_all(exp.Table)}
            ctes = {c.alias.lower() for c in tree.find_all(exp.CTE)}
            sources = used - ctes
            if not sources or not sources <= set(SOURCES):
                skip(f"reads tables outside the plain test schema: {sorted(sources - set(SOURCES))}")
                continue
            try:
                pipeline = split(pair["input"])
            except Exception as error:  # noqa: BLE001
                skip(f"cannot split: {error}")
                continue
            if pipeline is None:
                skip("one table already: nothing to split")
                continue
            reference = {"result": expected.sql(dialect="bigquery")}
            problem = verify(pair["input"], pipeline, reference, sources)
            if problem:
                skip(problem)
                continue
            try:
                original_score, reference_score = pipeline_complexity(pipeline), pipeline_complexity(reference)
            except ValueError as error:
                skip(f"sqlfluff cannot score it: {error}")
                continue
            case_id = f"sqlglot-{fixture.replace('_', '-')}-{index:03d}"
            kept = reference_score["score"] >= original_score["score"]
            if kept:  # sqlglot leaves the subqueries in place, so its answer is the pipeline itself
                reference, reference_score = dict(pipeline), original_score
            cases.append({
                "id": case_id,
                "source": f"sqlglot@{commit[:12]} tests/fixtures/optimizer/{fixture}.sql",
                "families": FAMILY[fixture],
                "split": "held_out" if int(hashlib.sha256(case_id.encode("utf-8")).hexdigest(), 16) % 5 == 0 else "dev",
                "dialect": "bigquery",
                "sources": {name: SOURCES[name] for name in sorted(sources)},
                "tables": pipeline,
                "protected": ["result"],
                "original": {"complexity": original_score},
                "reference": {"tables": reference, "complexity": reference_score},
                "reference_kind": "external",
                "traps": traps(pair["input"], expected.sql(dialect="bigquery"), sources),
                "verification": {"engine": f"duckdb {duckdb.__version__}, optimizer off",
                                 "databases": DATABASES, "seed": SEED,
                                 "proved": {"result": proved(pipeline, reference, sources)},
                                 "checked": "unsplit input, split pipeline and reference agree"},
                "note": f"{where}. Input split into tables (each CTE and FROM/JOIN derived table); "
                        f"reference is sqlglot's expected output"
                        + (" (it keeps the subqueries, so the reference is the pipeline itself)" if kept else "")
                        + ".",
            })
    return cases, skipped


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("sqlglot_dir", type=Path)
    parser.add_argument("--out", type=Path, default=ROOT / "benchmarks" / "table_minimization" / "sourced-sqlglot.jsonl")
    args = parser.parse_args(argv)
    cases, skipped = convert(args.sqlglot_dir)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text("".join(json.dumps(case, sort_keys=False) + "\n" for case in cases))
    reasons: dict[str, int] = {}
    for item in skipped:
        reason = item["reason"].split(":")[0]
        reasons[reason] = reasons.get(reason, 0) + 1
    print(json.dumps({"written": len(cases), "held_out": sum(c["split"] == "held_out" for c in cases),
                      "simpler_reference": sum(c["reference"]["complexity"]["score"] < c["original"]["complexity"]["score"]
                                               for c in cases),
                      "skipped": len(skipped), "skipped_by_reason": reasons}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
