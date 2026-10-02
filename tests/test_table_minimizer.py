"""Table minimizer: the simplest set of tables that keeps every protected table proved unchanged."""

import json
import random
from collections import Counter

import pytest

pytest.importorskip("z3")
pytest.importorskip("duckdb")

import duckdb
import sqlglot

from kumosql import table_minimizer
from kumosql.table_minimizer import MinimizationError, minimize_case, minimize_tables, pipeline_score

ORDERS = {"columns": {"id": "INT64", "customer_id": "INT64", "amount": "INT64", "status": "STRING"}, "key": ["id"]}
CUSTOMERS = {"columns": {"id": "INT64", "name": "STRING", "region": "STRING"}, "key": ["id"]}
SOURCES = {"orders": ORDERS, "customers": CUSTOMERS}


def _rows(seed):
    rng = random.Random(seed)
    orders = [
        (i, rng.choice([1, 2, 3, None]), rng.choice([-5, 0, 3, 10, 20, None]), rng.choice(["paid", "open", None]))
        for i in range(rng.randint(0, 12))
    ]
    customers = [(i, rng.choice(["a", "b", None]), rng.choice(["eu", "us", None])) for i in range(1, rng.randint(1, 5))]
    return {"orders": orders, "customers": customers}


def _outputs(tables, protected, dialect, data):
    """Each protected table's rows (as a multiset) on ``data``, run in DuckDB."""

    con = duckdb.connect()
    for name, spec in SOURCES.items():
        cols = ", ".join(f"{c} {'BIGINT' if t == 'INT64' else 'VARCHAR'}" for c, t in spec["columns"].items())
        con.execute(f"CREATE TABLE {name} ({cols})")
        if data[name]:
            marks = ", ".join("?" for _ in spec["columns"])
            con.executemany(f"INSERT INTO {name} VALUES ({marks})", data[name])
    pending = dict(tables)
    while pending:  # create views readers last
        for name, sql in list(pending.items()):
            try:
                con.execute(f"CREATE VIEW {name} AS {sqlglot.transpile(sql, read=dialect, write='duckdb')[0]}")
                del pending[name]
            except duckdb.CatalogException:
                continue
    out = {}
    for name in protected:
        result = con.execute(f"SELECT * FROM {name}")
        out[name] = ([d[0].lower() for d in result.description], Counter(result.fetchall()))
    return out


def _assert_same(before, after, protected, dialect="bigquery", databases=40):
    for seed in range(databases):
        data = _rows(seed)
        assert _outputs(before, protected, dialect, data) == _outputs(after, protected, dialect, data), seed


CHAIN = {
    "t1": "SELECT id, customer_id, amount, status FROM orders",
    "t2": "SELECT id, customer_id, amount, status FROM t1 WHERE amount > 0",
    "t3": "SELECT customer_id, SUM(amount) AS total FROM t2 WHERE status = 'paid' GROUP BY customer_id",
    "t4": "SELECT id, CASE WHEN amount > 10 THEN 'big' ELSE 'small' END AS size FROM t1",
    "t5": "SELECT * FROM t2",
    "t6": "SELECT customer_id, COUNT(*) AS n FROM t5 GROUP BY customer_id",
}


def test_chain_folds_into_the_protected_tables_with_proofs():
    result = minimize_tables(CHAIN, ["t3", "t6"], sources=SOURCES)
    assert set(result.tables) == {"t3", "t6"}
    assert result.removed == ["t1", "t2", "t4", "t5"]
    assert result.score < result.original_score == pipeline_score(CHAIN)
    assert result.score == pipeline_score(result.tables)
    assert {p.status for p in result.proofs.values()} == {"proved"}
    assert all("t2" not in sql and "t1" not in sql for sql in result.tables.values())
    _assert_same(CHAIN, result.tables, ["t3", "t6"])
    data = result.to_json()
    assert data["proofs"]["t3"]["assumptions"] and data["moves"]
    json.dumps(data)


def test_unprotected_tables_nobody_needs_are_dropped_and_the_rest_left_alone():
    tables = {
        "report": "SELECT customer_id, SUM(amount) AS total FROM orders GROUP BY customer_id",
        "leftover": "SELECT id FROM orders WHERE status = 'open'",
    }
    result = minimize_tables(tables, ["report"], sources=SOURCES)
    assert result.tables == {"report": tables["report"]}  # the protected SQL is returned exactly as given
    assert result.proofs["report"].status == "unchanged"


def test_traps_are_never_taken():
    # DISTINCT, LIMIT-free aggregation and a filter on a LEFT JOIN's right side: folding must keep each meaning
    tables = {
        "d": "SELECT DISTINCT customer_id, status FROM orders",
        "per_customer": "SELECT customer_id, COUNT(*) AS n FROM d GROUP BY customer_id",
        "j": "SELECT o.id, c.region FROM orders AS o LEFT JOIN customers AS c ON o.customer_id = c.id",
        "eu": "SELECT id, region FROM j WHERE region = 'eu' OR region IS NULL",
        "agg": "SELECT customer_id, MAX(amount) AS top FROM orders GROUP BY customer_id",
        "top": "SELECT customer_id, top FROM agg WHERE top > 5",
    }
    protected = ["per_customer", "eu", "top"]
    result = minimize_tables(tables, protected, sources=SOURCES)
    assert result.score <= result.original_score
    _assert_same(tables, result.tables, protected, databases=60)


def test_output_names_are_kept_even_through_a_star():
    tables = {
        "renamed": "SELECT id AS order_id, amount AS value FROM orders",
        "report": "SELECT * FROM renamed",
    }
    result = minimize_tables(tables, ["report"], sources=SOURCES)
    _assert_same(tables, result.tables, ["report"])
    assert set(result.tables) == {"report"}


def test_a_shared_intermediate_is_kept_when_folding_would_cost_more():
    heavy = (
        "SELECT id, customer_id, CASE WHEN amount > 10 THEN 'big' WHEN amount > 0 THEN 'small' ELSE 'none' END AS size, "
        "ROW_NUMBER() OVER (PARTITION BY customer_id ORDER BY id) AS rn FROM orders WHERE status = 'paid' AND amount IS NOT NULL"
    )
    tables = {
        "base": heavy,
        "a": "SELECT customer_id, size FROM base WHERE rn = 1",
        "b": "SELECT size, COUNT(*) AS n FROM base GROUP BY size",
        "c": "SELECT id FROM base WHERE size = 'big'",
    }
    result = minimize_tables(tables, ["a", "b", "c"], sources=SOURCES)
    assert result.score <= result.original_score
    _assert_same(tables, result.tables, ["a", "b", "c"])


def test_time_limit_returns_the_input_unchanged():
    result = minimize_tables(CHAIN, ["t3"], sources=SOURCES, max_seconds=0)
    assert result.tables == CHAIN and result.stopped == "time limit"
    assert result.proofs["t3"].status == "unchanged"


def test_unreadable_tables_and_what_they_read_are_kept():
    tables = {
        "base": "SELECT id, amount FROM orders",
        "script": "CALL proc((SELECT COUNT(*) FROM base))",
        "report": "SELECT id FROM base",
    }
    result = minimize_tables(tables, ["report"], sources=SOURCES)
    assert result.tables["script"] == tables["script"] and "base" in result.tables


def test_input_errors():
    with pytest.raises(MinimizationError, match="not one of the tables"):
        minimize_tables(CHAIN, ["nope"], sources=SOURCES)
    with pytest.raises(MinimizationError, match="cycle"):
        minimize_tables({"a": "SELECT x FROM b", "b": "SELECT x FROM a"}, ["a"])
    with pytest.raises(MinimizationError, match="both a source and a table"):
        minimize_tables({"orders": "SELECT 1 AS x"}, ["orders"], sources=SOURCES)


def test_duckdb_dialect_and_the_harness_entry_point():
    case = {"id": "t", "dialect": "duckdb", "sources": SOURCES, "tables": CHAIN, "protected": ["t6"]}
    out = minimize_case(case)
    assert set(out) == {"t6"}
    _assert_same(CHAIN, out, ["t6"], dialect="duckdb")


def test_cli(tmp_path, capsys):
    path = tmp_path / "case.json"
    path.write_text(json.dumps({"tables": CHAIN, "protected": ["t3"], "sources": SOURCES}), encoding="utf-8")
    assert table_minimizer.main([str(path), "--max-seconds", "60"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert set(data["tables"]) == {"t3"} and data["proofs"]["t3"]["status"] == "proved"
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"tables": CHAIN, "protected": ["zzz"]}), encoding="utf-8")
    assert table_minimizer.main([str(bad)]) == 2


TWENTY = {
    "t1": "SELECT * FROM orders",
    "t2": "SELECT id, customer_id, amount, status FROM t1 WHERE status = 'paid'",
    "t3": "SELECT id, customer_id, amount, status FROM t1 WHERE status = 'paid'",
    "t4": "SELECT id, customer_id, amount, CASE WHEN amount > 10 THEN 'big' ELSE 'small' END AS size, "
          "ROW_NUMBER() OVER (PARTITION BY customer_id ORDER BY id) AS rn FROM t2",
    "t5": "SELECT customer_id, SUM(amount) AS total FROM t4 GROUP BY customer_id",
    "t6": "SELECT customer_id, COUNT(*) AS n FROM t3 GROUP BY customer_id",
    "t7": "SELECT c.id, c.name, c.region FROM customers AS c",
    "t8": "SELECT t7.region, t5.total FROM t5 JOIN t7 ON t5.customer_id = t7.id",
    "t9": "SELECT region, SUM(total) AS total FROM t8 GROUP BY region",
    "t10": "SELECT * FROM t9 WHERE total > 0",
    "t11": "SELECT id, name FROM t7 WHERE region = 'eu'",
    "t12": "SELECT * FROM t11",
    "t13": "SELECT id FROM t12",
    "t14": "SELECT customer_id, n FROM t6 WHERE n > 1",
    "t15": "SELECT id, amount FROM t1 WHERE amount IS NOT NULL",
    "t16": "SELECT SUM(amount) AS s FROM t15",
    "t17": "SELECT * FROM t16",
    "t18": "SELECT t14.customer_id, t14.n, t7.name FROM t14 LEFT JOIN t7 ON t14.customer_id = t7.id",
    "t19": "SELECT DISTINCT region FROM t7",
    "t20": "SELECT COUNT(*) AS regions FROM t19",
}


def test_twenty_tables_with_five_protected():
    protected = ["t10", "t13", "t17", "t18", "t20"]
    result = minimize_tables(TWENTY, protected, sources=SOURCES, max_seconds=240)
    assert result.original_score == 27.0 and result.score <= 16.0
    assert set(protected) <= set(result.tables) and len(result.tables) <= 9
    assert all(result.proofs[name].status in ("proved", "unchanged") for name in protected)
    _assert_same(TWENTY, result.tables, protected, databases=40)
