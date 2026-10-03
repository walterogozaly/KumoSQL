"""Score the prover on rewrites between join types and LEFT JOIN.

Each pair in ``tests/fixtures/join_rewrites/`` rewrites a CROSS, comma, INNER, RIGHT or FULL
join, or a semi or anti join, into (or out of) a LEFT JOIN. Every label was checked by hand
(``why`` says the reason) and by running both queries on random DuckDB databases that satisfy
the pair's declared constraints. A non-equivalent pair carries the smallest database found on
which the two queries return different rows.

For each pair this checks:

* the prover's verdict (with the executed counterexample search on), so a pair is proved,
  refuted or left unknown;
* a proved pair returns the same rows on ``--databases`` random databases (DuckDB, with
  the optimizer off whenever the plain run disagrees, as ``kumosql.duckdb_load.run_unoptimized``);
* the stored counterexample of a non-equivalent pair still separates the queries.

``wrong`` counts proofs of non-equivalent pairs, refutations of equivalent ones and proofs
that a random database contradicts. It must stay 0.

    python tools/join_rewrite_bench.py             # development pairs
    python tools/join_rewrite_bench.py --held-out  # held-out pairs (final evaluation only)
"""

from __future__ import annotations

from collections import Counter
import json
from pathlib import Path
import random
import sys
import time

import sqlglot

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from kumosql.algebraic_equivalence import prove_equivalent_algebraic  # noqa: E402
from kumosql.smt_equivalence import SmtStatus, TableConstraints  # noqa: E402

FIXTURES = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "join_rewrites"
SCHEMA = {"a": ["id", "k", "x"], "b": ["id", "k", "y"], "c": ["id", "k", "z"]}
TYPES = {table: {column: "INT64" for column in columns} for table, columns in SCHEMA.items()}
_KEYED = {table: TableConstraints(not_null=frozenset({"id"}), keys=(("id",),)) for table in SCHEMA}
_FK = ((("k",), "b", ("id",)),)
# Declared facts per pair: ``keys`` makes every ``id`` a NOT NULL key; ``fk`` adds a.k NOT NULL
# referencing b.id; ``fknull`` declares that foreign key with a.k nullable; ``fknn`` makes a.k and b.k NOT NULL.
CONSTRAINTS = {
    "none": None,
    "keys": _KEYED,
    "fk": dict(_KEYED, a=TableConstraints(not_null=frozenset({"id", "k"}), keys=(("id",),), foreign_keys=_FK)),
    "fknull": dict(_KEYED, a=TableConstraints(not_null=frozenset({"id"}), keys=(("id",),), foreign_keys=_FK)),
    "fknn": dict(
        _KEYED,
        a=TableConstraints(not_null=frozenset({"id", "k"}), keys=(("id",),)),
        b=TableConstraints(not_null=frozenset({"id", "k"}), keys=(("id",),)),
    ),
}


def load_pairs(held_out: bool = False) -> list[dict]:
    path = FIXTURES / ("held_out.jsonl" if held_out else "pairs.jsonl")
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def random_database(rng: random.Random, constraints: str) -> dict[str, list[list]]:
    """At most four rows per table over a small domain, satisfying ``constraints``."""

    db: dict[str, list[list]] = {}
    for table, columns in SCHEMA.items():
        count = rng.randint(0, 4)
        ids = rng.sample(range(1, 7), count)
        db[table] = [
            [ids[i] if column == "id" and constraints != "none" else (None if rng.random() < 0.25 else rng.randint(0, 3)) for column in columns]
            for i in range(count)
        ]
    if constraints in ("fk", "fknull"):
        parents = [row[0] for row in db["b"]]
        if not parents and constraints == "fk":
            db["a"] = []
        for row in db["a"]:
            row[1] = rng.choice(parents) if parents and not (constraints == "fknull" and rng.random() < 0.3) else None
    if constraints == "fknn":
        for table in ("a", "b"):
            for row in db[table]:
                row[1] = rng.randint(0, 3) if row[1] is None else row[1]
    return db


def _connect(db: dict[str, list[list]]):
    import duckdb

    from kumosql.duckdb_load import insert_rows

    con = duckdb.connect()
    for table, columns in SCHEMA.items():
        con.execute(f"CREATE TABLE {table} ({', '.join(c + ' BIGINT' for c in columns)})")
        insert_rows(con, table, db.get(table, []))
    return con


def differ(con, left: str, right: str) -> bool:
    """The two queries return different bags of rows (confirmed with DuckDB's optimizer off)."""

    from kumosql.duckdb_load import run_unoptimized

    if Counter(con.execute(left).fetchall()) == Counter(con.execute(right).fetchall()):
        return False
    plain_left, plain_right = run_unoptimized(con, left, right)
    return Counter(plain_left) != Counter(plain_right)


def _duckdb(sql: str) -> str:
    return sqlglot.transpile(sql, read="bigquery", write="duckdb")[0]


def run(held_out: bool = False, databases: int = 100, seed: int = 0) -> dict:
    pairs = load_pairs(held_out)
    rng = random.Random(seed)
    coverage = Counter()
    by_category: dict[str, Counter] = {}
    wrong, witnessed, missed = [], 0, []
    started = time.perf_counter()
    for pair in pairs:
        result = prove_equivalent_algebraic(
            pair["left"], pair["right"], schema=SCHEMA, types=TYPES, constraints=CONSTRAINTS[pair["constraints"]],
            dialect="bigquery", search_counterexample=True,
        )
        status = {SmtStatus.PROVEN_EQUIVALENT: "proven", SmtStatus.NOT_EQUIVALENT: "refuted"}.get(result.status, "unknown")
        coverage[status] += 1
        tally = by_category.setdefault(pair["category"], Counter())
        tally[("equivalent " if pair["equivalent"] else "not equivalent ") + status] += 1
        left, right = _duckdb(pair["left"]), _duckdb(pair["right"])
        if pair["equivalent"]:
            if status == "refuted":
                wrong.append((pair["name"], "refuted an equivalent pair"))
            elif status == "proven":
                for _ in range(databases):
                    con = _connect(random_database(rng, pair["constraints"]))
                    try:
                        if differ(con, left, right):
                            wrong.append((pair["name"], "a random database separates a proved pair"))
                            break
                    finally:
                        con.close()
            if status != "proven":
                missed.append(pair["name"])
        else:
            if status == "proven":
                wrong.append((pair["name"], "proved a non-equivalent pair"))
            con = _connect(pair["counterexample"])
            try:
                witnessed += differ(con, left, right)
            finally:
                con.close()
    equivalent = sum(p["equivalent"] for p in pairs)
    return {
        "pairs": len(pairs),
        "equivalent": equivalent,
        "not_equivalent": len(pairs) - equivalent,
        "proven": coverage["proven"],
        "refuted": coverage["refuted"],
        "unknown": coverage["unknown"],
        "witnessed": witnessed,
        "wrong": wrong,
        "missed": missed,
        "by_category": {k: dict(v) for k, v in sorted(by_category.items())},
        "seconds": round(time.perf_counter() - started, 1),
    }


def main(argv: list[str]) -> int:
    result = run(held_out="--held-out" in argv)
    print(json.dumps(result, indent=2))
    return 1 if result["wrong"] else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
