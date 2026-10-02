"""Score constraint-dependent rewrites: which declared guarantees a proof needs, and whether it fails without them.

Every case is a rewrite (``original`` -> ``rewritten``) that is only valid when some
declared NOT NULL columns, keys and foreign keys hold: removing a join that a foreign key
makes redundant, ``NOT IN`` to ``NOT EXISTS`` (needs both sides NOT NULL), dropping a
``DISTINCT`` (needs a key *and* non-NULL), ``COUNT(x)`` to ``COUNT(*)``. Cases carry the
guarantees they need, written from the SQL semantics (``requires``), or ``"valid": false``
for a rewrite that is wrong even with every declared fact (a control).

For each case this checks:

* the prover proves a valid rewrite with every declared fact, and
  ``needed_guarantees`` reports exactly the expected set (the proof says what it relies on);
* the proof holds on random databases that satisfy the declarations (executed in DuckDB);
* for every required guarantee, the proof fails when that one fact is removed (and a
  database that satisfies all the other facts but not this one makes the queries differ,
  so the refusal was right);
* a control is never proved, and any counterexample the prover returns respects the
  declarations and really separates the queries.

``wrong`` counts false proofs (a proof on a fact set for which a differing database
exists) and incorrect counterexamples; it must stay 0.

    python tools/constraint_rewrite_bench.py             # development cases
    python tools/constraint_rewrite_bench.py --held-out  # held-out cases (final evaluation only)
"""

from __future__ import annotations

import json
from pathlib import Path
import random
import re
import sys
import time

import sqlglot

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from kumosql.constraint_dependence import Guarantee, constraints_with, guarantees_of, needed_guarantees  # noqa: E402
from kumosql.algebraic_equivalence import prove_equivalent_algebraic  # noqa: E402
from kumosql.smt_equivalence import SmtStatus, TableConstraints  # noqa: E402

FIXTURES = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "constraint_rewrites"
DOMAINS = {"INT64": [0, 1, 2, 3, 4], "STRING": ["a", "b", "EU"]}
DUCK = {"INT64": "BIGINT", "STRING": "VARCHAR"}


def load_cases(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def schema_parts(schema: dict) -> tuple[dict, dict]:
    columns = {t: list(spec["columns"]) for t, spec in schema["tables"].items()}
    constraints = {}
    for table, spec in schema["tables"].items():
        constraints[table] = TableConstraints(
            not_null=frozenset(spec.get("not_null", ())),
            keys=tuple(tuple(k) for k in spec.get("keys", ())),
            foreign_keys=tuple((tuple(fk["columns"]), fk["parent"], tuple(fk["parent_columns"])) for fk in spec.get("foreign_keys", ())),
        )
    return columns, constraints


def random_database(schema: dict, facts: list[Guarantee], rng: random.Random) -> dict[str, list[tuple]]:
    """Random rows that satisfy exactly ``facts`` (other declared facts may or may not hold)."""

    not_null = {(g.table, g.columns[0]) for g in facts if g.kind == "not_null"}
    keys = {t: [g.columns for g in facts if g.kind == "unique" and g.table == t] for t in schema["tables"]}
    fks = {t: [g for g in facts if g.kind == "foreign_key" and g.table == t] for t in schema["tables"]}
    order, seen = [], set()

    def visit(table: str) -> None:
        if table in seen:
            return
        seen.add(table)
        for fk in fks[table]:
            visit(fk.parent)
        order.append(table)

    for table in schema["tables"]:
        visit(table)
    db: dict[str, list[tuple]] = {}
    for table in order:
        spec = schema["tables"][table]
        names = list(spec["columns"])
        rows, used = [], [set() for _ in keys[table]]
        for _ in range(rng.choice([0, 1, 2, 3, 4, 5])):
            row = {}
            for name in names:
                value = rng.choice(DOMAINS[spec["columns"][name]])
                if (table, name) not in not_null and rng.random() < 0.3:
                    value = None
                row[name] = value
            ok = True
            for fk in fks[table]:
                parent_names = list(schema["tables"][fk.parent]["columns"])
                options = [tuple(p[parent_names.index(c)] for c in fk.parent_columns) for p in db[fk.parent]]
                options = [o for o in options if None not in o]
                nullable = all((table, c) not in not_null for c in fk.columns)
                if options and (not nullable or rng.random() > 0.2):
                    for c, v in zip(fk.columns, rng.choice(options)):
                        row[c] = v
                elif nullable:
                    for c in fk.columns:
                        row[c] = None
                else:
                    ok = False
            if not ok:
                continue
            for index, key in enumerate(keys[table]):
                value = tuple(row[c] for c in key)
                if None not in value and value in used[index]:
                    ok = False
            if not ok:
                continue
            for index, key in enumerate(keys[table]):
                used[index].add(tuple(row[c] for c in key))
            rows.append(tuple(row[n] for n in names))
        db[table] = rows
    return db


def literal(value) -> str:
    return "NULL" if value is None else str(value) if isinstance(value, int) else "'" + str(value).replace("'", "''") + "'"


def connection(schema: dict):
    import duckdb

    db = duckdb.connect(":memory:")
    for table, spec in schema["tables"].items():
        db.execute(f'CREATE TABLE "{table}" (' + ", ".join(f'"{c}" {DUCK[t]}' for c, t in spec["columns"].items()) + ")")
    return db


def load(db, data: dict) -> None:
    for table, rows in data.items():
        db.execute(f'DELETE FROM "{table}"')
        if rows:
            db.execute(f'INSERT INTO "{table}" VALUES ' + ", ".join("(" + ", ".join(literal(v) for v in row) + ")" for row in rows))


def to_duck(sql: str) -> str:
    return sqlglot.transpile(sql, read="bigquery", write="duckdb")[0]


def bag(db, sql: str):
    from collections import Counter

    return Counter(db.execute(sql).fetchall())


def satisfies(schema: dict, data: dict, facts: list[Guarantee]) -> bool:
    for g in facts:
        names = list(schema["tables"][g.table]["columns"])
        rows = data.get(g.table, [])
        if g.kind == "not_null" and any(r[names.index(g.columns[0])] is None for r in rows):
            return False
        if g.kind == "unique":
            values = [tuple(r[names.index(c)] for c in g.columns) for r in rows]
            values = [v for v in values if None not in v]
            if len(values) != len(set(values)):
                return False
        if g.kind == "foreign_key":
            parent_names = list(schema["tables"][g.parent]["columns"])
            parents = {tuple(p[parent_names.index(c)] for c in g.parent_columns) for p in data.get(g.parent, [])}
            for r in rows:
                value = tuple(r[names.index(c)] for c in g.columns)
                if None not in value and value not in parents:
                    return False
    return True


def find_difference(schema, db, left, right, facts, rng, trials) -> bool:
    """Whether some database satisfying ``facts`` makes the queries differ."""

    left, right = to_duck(left), to_duck(right)
    for _ in range(trials):
        load(db, random_database(schema, facts, rng))
        if bag(db, left) != bag(db, right):
            return True
    return False


def counterexample_ok(schema, db, case, result, facts) -> bool:
    """The prover's database, completed with legal values for columns the queries never read, separates the queries."""

    ce = result.counterexample
    required = {(g.table, g.columns[0]) for g in facts if g.kind == "not_null"}
    data = {table: [] for table in schema["tables"]}

    def fit(value, kind):
        if value is None or (kind == "INT64") == isinstance(value, int) and not isinstance(value, bool):
            return value
        return int(value) if kind == "INT64" and isinstance(value, (bool, float)) else (0 if kind == "INT64" else str(value))

    for table, spec in schema["tables"].items():
        for r in ce.tables.get(table) or ce.tables.get(table.upper()) or []:
            row = []
            for c, t in spec["columns"].items():
                if c in r:
                    row.append(fit(r[c], t))
                else:
                    row.append((0 if t == "INT64" else "a") if (table, c) in required else None)
            data[table].append(tuple(row))
    # parents a foreign key needs: add a row holding the referenced values
    for _ in range(4):
        for g in (f for f in facts if f.kind == "foreign_key"):
            names = list(schema["tables"][g.table]["columns"])
            pspec = schema["tables"][g.parent]
            pnames = list(pspec["columns"])
            have = {tuple(p[pnames.index(c)] for c in g.parent_columns) for p in data[g.parent]}
            for row in list(data[g.table]):
                value = tuple(row[names.index(c)] for c in g.columns)
                if None in value or value in have:
                    continue
                parent = [(0 if t == "INT64" else "a") if (g.parent, c) in required else None for c, t in pspec["columns"].items()]
                for c, v in zip(g.parent_columns, value):
                    parent[pnames.index(c)] = v
                data[g.parent].append(tuple(parent))
                have.add(value)
    if not satisfies(schema, data, facts):
        return False
    load(db, data)
    return bag(db, to_duck(case["original"])) != bag(db, to_duck(case["rewritten"]))


def kind_of(result) -> str:
    if result.proven:
        return "proven"
    if result.status is SmtStatus.NOT_EQUIVALENT:
        return "refuted"
    reason = result.reason.lower()
    if reason.startswith("unsupported") or "parse error" in reason:
        return "unsupported"
    return "timeout" if "timed out" in reason else "unknown"


def run(filename: str = "cases.json", trials: int = 120, seed: int = 3) -> dict:
    data = load_cases(filename)
    out = {
        "cases": len(data["cases"]),
        "checks": 0,
        "proven": 0,
        "refuted": 0,
        "unknown": 0,
        "unsupported": 0,
        "timeout": 0,
        "error": 0,
        "rewrites_changed": 0,
        "rewrites_proved": 0,
        "rewrites_verified": 0,
        "ablations": 0,
        "ablations_refused": 0,
        "exact_guarantees": 0,
        "valid_cases": 0,
        "counterexamples": 0,
        "wrong": [],
        "label_wrong": [],
        "unverified": [],
        "different_set": [],
        "missed": [],
        "seconds": 0.0,
    }
    start = time.time()
    for case in data["cases"]:
        schema = data["schemas"][case["schema"]]
        columns, constraints = schema_parts(schema)
        db = connection(schema)
        rng = random.Random(f"{seed}:{case['id']}")
        offered_all = guarantees_of(constraints)
        valid = case.get("valid", True)
        out["rewrites_changed"] += int(case["original"] != case["rewritten"])
        try:
            report = needed_guarantees(case["original"], case["rewritten"], schema=columns, constraints=constraints)
        except Exception as error:  # noqa: BLE001
            out["error"] += 1
            out["checks"] += 1
            out["wrong"].append((case["id"], f"crash: {error!r}"))
            continue
        offered = list(report.offered)
        # main check
        out["checks"] += 1
        if valid:
            out["valid_cases"] += 1
        if report.status == "proven":
            out["rewrites_proved"] += 1
            differs = find_difference(schema, db, case["original"], case["rewritten"], offered, rng, trials)
            if differs:
                out["wrong"].append((case["id"], "proof with all declared facts, but a database differs"))
            else:
                out["rewrites_verified"] += 1
            if not valid:
                out["wrong"].append((case["id"], "a control was proved"))
            else:
                expected = {json.dumps(r, sort_keys=True) for r in case["requires"]}
                got = {json.dumps(_as_requirement(g), sort_keys=True) for g in report.needed}
                if expected == got:
                    out["exact_guarantees"] += 1
                    out["proven"] += 1
                else:
                    out["unknown"] += 1
                    out["different_set"].append((case["id"], sorted(g.label for g in report.needed), [_label(r) for r in case["requires"]]))
        else:
            kind = kind_of(report.result)
            if valid:
                out["unknown" if kind in ("refuted", "unknown") else kind] += 1
                out["missed"].append((case["id"], kind, report.result.reason[:100]))
            else:
                differs = find_difference(schema, db, case["original"], case["rewritten"], offered, rng, trials)
                if differs:
                    out["refuted"] += 1
                else:
                    out["unverified"].append((case["id"], "control has no differing database"))
                    out["refuted"] += 1
            if report.result.status is SmtStatus.NOT_EQUIVALENT and report.result.counterexample:
                out["counterexamples"] += 1
                if not counterexample_ok(schema, db, case, report.result, offered):
                    out["wrong"].append((case["id"], "counterexample violates a declaration or does not separate the queries"))
        # ablations: remove each expected-required fact, keep every other offered fact
        if valid:
            for requirement in case["requires"]:
                fact = _fact(requirement)
                out["checks"] += 1
                out["ablations"] += 1
                rest = [g for g in offered if g != fact]
                try:
                    result = prove_equivalent_algebraic(case["original"], case["rewritten"], schema=columns, constraints=constraints_with(rest) or None, dialect="bigquery")
                except Exception as error:  # noqa: BLE001
                    out["error"] += 1
                    out["wrong"].append((case["id"], f"crash without {fact.label}: {error!r}"))
                    continue
                differs = find_difference(schema, db, case["original"], case["rewritten"], rest, rng, trials)
                if fact not in offered:
                    out["label_wrong"].append((case["id"], f"{fact.label} is not declared in the schema"))
                    continue
                if result.proven:
                    if differs:
                        out["wrong"].append((case["id"], f"proved without {fact.label}, but a database differs"))
                    else:
                        out["label_wrong"].append((case["id"], f"{fact.label} is not needed: nothing breaks without it"))
                    continue
                out["ablations_refused"] += 1
                if differs:
                    out["refuted"] += 1
                else:
                    out["label_wrong"].append((case["id"], f"no database breaks the rewrite without {fact.label}"))
                if result.status is SmtStatus.NOT_EQUIVALENT and result.counterexample:
                    out["counterexamples"] += 1
                    if not counterexample_ok(schema, db, case, result, rest):
                        out["wrong"].append((case["id"], f"counterexample without {fact.label} violates a declaration or does not separate the queries"))
    out["seconds"] = round(time.time() - start, 1)
    return out


def _as_requirement(g: Guarantee) -> dict:
    spec = {"kind": g.kind, "table": g.table, "columns": list(g.columns)}
    if g.kind == "foreign_key":
        spec.update(parent=g.parent, parent_columns=list(g.parent_columns))
    return spec


def _fact(requirement: dict) -> Guarantee:
    return Guarantee(
        requirement["kind"], requirement["table"], tuple(requirement["columns"]), requirement.get("parent", ""), tuple(requirement.get("parent_columns", ()))
    )


def _label(requirement: dict) -> str:
    return _fact(requirement).label


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    result = run("held_out.json" if "--held-out" in args else "cases.json")
    keys = [k for k, v in result.items() if not isinstance(v, list)]
    print(json.dumps({k: result[k] for k in keys}, indent=1))
    for key in ("wrong", "label_wrong", "unverified", "different_set", "missed"):
        for item in result[key]:
            print(key.upper(), item)
    return 1 if (result["wrong"] or result["label_wrong"]) else 0


if __name__ == "__main__":
    raise SystemExit(main())
