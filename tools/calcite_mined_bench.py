"""Score the pairs mined from Apache Calcite's current rule tests.

``tools/calcite_plan_to_sql.py`` turns each test's ``planBefore`` and
``planAfter`` into SQL (``tests/fixtures/calcite_mined``). For every pair this
reports ``proven`` (unbounded proof), ``refuted`` (no proof, and a random
DuckDB database on which the two queries return different bags), ``unknown``
and ``wrong`` (proven, yet a counterexample exists; must stay 0). Results are
also split into pairs new to every other corpus and pairs SQLSolver, QED or
R-Bot already hold.

    python tools/calcite_mined_bench.py
"""

from __future__ import annotations

from collections import Counter
import datetime
import json
from pathlib import Path
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sqlsolver_bench as sb  # noqa: E402

FIXTURES = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "calcite_mined"


def load_pairs() -> list[dict]:
    return [json.loads(line) for line in (FIXTURES / "pairs.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]


def _value(value):
    """DuckDB widens a DATE to TIMESTAMP WITH TIME ZONE when a branch casts NULL to TIMESTAMP; compare the date."""

    if isinstance(value, datetime.datetime) and value.time() == datetime.time(0) and value.utcoffset() in (None, datetime.timedelta(0)):
        return value.date()
    if isinstance(value, float):
        return round(value, 9)
    return value


def _really_differ(counter) -> bool:
    left, right = counter[2], counter[3]
    def norm(bag):
        out = Counter()
        for row, count in bag.items():
            out[tuple(_value(v) for v in row)] += count
        return out

    return norm(left) != norm(right)


def _tables(ddl: str) -> dict:
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "schema.sql"
        path.write_text(ddl, encoding="utf-8")
        return sb.load_schema(path)


def run(prove=sb.default_prove, trials: int = 30, limit: int | None = None) -> dict:
    schemas = json.loads((FIXTURES / "schemas.json").read_text(encoding="utf-8"))
    out = {"total": 0, "proven": [], "refuted": [], "unknown": [], "wrong": [], "new": {"total": 0, "proven": 0, "refuted": 0}, "seconds": 0.0}
    loaded: dict[str, tuple] = {}
    start = time.time()
    for pair in load_pairs()[:limit]:
        out["total"] += 1
        key = pair["schema_id"]
        if key not in loaded:
            tables = _tables(schemas[key]["ddl"])
            loaded[key] = (tables, sb.new_database(tables))
        tables, db = loaded[key]
        left, right = pair["sql_a"], pair["sql_b"]
        try:
            proof = prove(left, right, tables)
        except Exception:  # a crash is a failure to prove, never a proof
            proof = False
        counter = sb.differ(left, right, tables, db, trials)
        found = counter not in (None, False) and _really_differ(counter)
        name = pair["name"]
        if proof and found:
            out["wrong"].append(name)
        elif proof:
            out["proven"].append(name)
        elif found:
            out["refuted"].append(name)
        else:
            out["unknown"].append(name)
        if pair["new"]:
            out["new"]["total"] += 1
            out["new"]["proven"] += bool(proof and not found)
            out["new"]["refuted"] += bool(found and not proof)
    out["scored"] = out["total"] - len(out["refuted"])  # a pair with a replayed counterexample is not one to prove
    out["new"]["scored"] = out["new"]["total"] - out["new"]["refuted"]
    out["seconds"] = time.time() - start
    return out


def main() -> int:
    r = run()
    print(
        f"calcite-mined {len(r['proven'])}/{r['scored']} proved ({r['total']} pairs), {len(r['refuted'])} refuted (counterexample), "
        f"{len(r['unknown'])} unknown, {len(r['wrong'])} wrong; new to every other corpus: "
        f"{r['new']['proven']}/{r['new']['scored']} proved; {r['seconds']:.1f}s"
    )
    for key in ("refuted", "wrong"):
        if r[key]:
            print(f"  {key}: {', '.join(r[key])}")
    return 1 if r["wrong"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
