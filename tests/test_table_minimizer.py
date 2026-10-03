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


# The six worked examples of an outside review of pipeline minimization: each pairs a pipeline with a
# tempting rewrite that changes a protected table. The rewrite must not be proved, DuckDB must show it
# differs on the witness, and the minimizer's own answer must agree with the original.

_DUCK_TYPES = {"INT64": "BIGINT", "STRING": "VARCHAR", "BOOL": "BOOLEAN", "NUMERIC": "DECIMAL(38, 9)"}
_DOMAINS = {"INT64": [-1, 0, 1, 2, 10, 100, None], "STRING": ["W", "E", None], "BOOL": [True, False, None],
            "NUMERIC": [0, 10, 2.5, None]}


def _run(tables, protected, sources, data, dialect="bigquery"):
    con = duckdb.connect()
    for name, columns in sources.items():
        con.execute(f"CREATE TABLE {name} ({', '.join(f'{c} {_DUCK_TYPES[t]}' for c, t in columns.items())})")
        if data.get(name):
            con.executemany(f"INSERT INTO {name} VALUES ({', '.join('?' for _ in columns)})", data[name])
    pending = dict(tables)
    while pending:
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


def _random_data(sources, seed):
    rng = random.Random(seed)
    return {name: [tuple(rng.choice(_DOMAINS[t]) for t in columns.values()) for _ in range(rng.randint(0, 6))]
            for name, columns in sources.items()}


EXAMPLES = {
    "protected stage keeps its rows": dict(
        sources={"raw_orders": {"id": "INT64", "amount": "INT64"}},
        tables={"stage_a": "SELECT id, amount FROM raw_orders", "stage_b": "SELECT id, amount FROM stage_a",
                "final_orders": "SELECT id, amount FROM stage_b WHERE amount > 0"},
        protected=["stage_a", "final_orders"],
        trap={"stage_a": "SELECT id, amount FROM raw_orders WHERE amount > 0",
              "final_orders": "SELECT id, amount FROM stage_a"},
        witness={"raw_orders": [(1, -1)]},
    ),
    "shared siblings by UNION ALL": dict(
        sources={"raw_sales": {"id": "INT64", "region": "STRING", "amount": "INT64"}},
        tables={"west": "SELECT id, region, amount FROM raw_sales WHERE region = 'W'",
                "large": "SELECT id, region, amount FROM raw_sales WHERE amount >= 100",
                "combined": "SELECT id, region, amount FROM west UNION ALL SELECT id, region, amount FROM large",
                "final_sales": "SELECT id, COUNT(*) AS n FROM combined GROUP BY id"},
        protected=["west", "final_sales"],
        trap={"west": "SELECT id, region, amount FROM raw_sales WHERE region = 'W'",
              "common_sales": "SELECT id, region, amount FROM raw_sales WHERE region = 'W' "
                              "UNION ALL SELECT id, region, amount FROM raw_sales WHERE amount >= 100",
              "final_sales": "SELECT id, COUNT(*) AS n FROM (SELECT id, region, amount FROM common_sales WHERE region = 'W' "
                             "UNION ALL SELECT id, region, amount FROM common_sales WHERE amount >= 100) AS c GROUP BY id"},
        witness={"raw_sales": [(1, "W", 100)]},
    ),
    "LEFT JOIN ON predicate is not demand": dict(
        sources={"raw_events": {"id": "INT64", "value": "INT64"}, "d": {"id": "INT64"}},
        tables={"all_events": "SELECT id, value FROM raw_events",
                "joined": "SELECT e.id FROM all_events AS e LEFT JOIN d ON e.value > 0 AND e.id = d.id"},
        protected=["joined"],
        trap={"all_events": "SELECT id, value FROM raw_events WHERE value > 0",
              "joined": "SELECT e.id FROM all_events AS e LEFT JOIN d ON e.value > 0 AND e.id = d.id"},
        witness={"raw_events": [(1, None)], "d": []},
    ),
    "DISTINCT on a superset": dict(
        sources={"raw_items": {"id": "INT64", "active": "BOOL", "price": "INT64"}},
        tables={"items": "SELECT id, active, price FROM raw_items",
                "active_items": "SELECT id, active, price FROM items WHERE active IS TRUE",
                "expensive_active": "SELECT id, price FROM active_items WHERE price > 10",
                "final_items": "SELECT id, price FROM expensive_active"},
        protected=["items", "expensive_active", "final_items"],
        trap={"items": "SELECT DISTINCT id, active, price FROM raw_items",
              "expensive_active": "SELECT id, price FROM items WHERE active IS TRUE AND price > 10",
              "final_items": "SELECT id, price FROM expensive_active"},
        witness={"raw_items": [(1, True, 20), (1, True, 20)]},
    ),
    "AVG is not SUM over COUNT(*)": dict(
        sources={"raw_metrics": {"k": "INT64", "x": "NUMERIC"}},
        tables={"sums": "SELECT k, SUM(x) AS total FROM raw_metrics GROUP BY k",
                "averages": "SELECT k, AVG(x) AS mean FROM raw_metrics GROUP BY k",
                "report": "SELECT s.k, s.total, a.mean FROM sums AS s JOIN averages AS a ON s.k = a.k",
                "final_metrics": "SELECT k, total, mean FROM report"},
        protected=["sums", "final_metrics"],
        trap={"stats": "SELECT k, SUM(x) AS total, COUNT(*) AS n FROM raw_metrics GROUP BY k",
              "sums": "SELECT k, total FROM stats",
              "final_metrics": "SELECT k, total, SAFE_DIVIDE(total, n) AS mean FROM stats WHERE k IS NOT NULL"},
        witness={"raw_metrics": [(1, 10), (1, None)]},
    ),
    "AVG keeps the join's dropped NULL key": dict(
        sources={"raw_metrics": {"k": "INT64", "x": "NUMERIC"}},
        tables={"sums": "SELECT k, SUM(x) AS total FROM raw_metrics GROUP BY k",
                "averages": "SELECT k, AVG(x) AS mean FROM raw_metrics GROUP BY k",
                "report": "SELECT s.k, s.total, a.mean FROM sums AS s JOIN averages AS a ON s.k = a.k",
                "final_metrics": "SELECT k, total, mean FROM report"},
        protected=["sums", "final_metrics"],
        trap={"sums": "SELECT k, SUM(x) AS total FROM raw_metrics GROUP BY k",
              "final_metrics": "SELECT k, SUM(x) AS total, AVG(x) AS mean FROM raw_metrics GROUP BY k"},
        witness={"raw_metrics": [(None, 10)]},
    ),
    "a global COUNT(*) keeps its empty-input row": dict(
        sources={"raw_logs": {"k": "INT64"}},
        tables={"logs": "SELECT k FROM raw_logs", "metrics": "SELECT COUNT(*) AS n FROM logs",
                "final_counts": "SELECT n FROM metrics"},
        protected=["metrics", "final_counts"],
        trap={"metrics": "SELECT COUNT(*) AS n FROM raw_logs GROUP BY k", "final_counts": "SELECT n FROM metrics"},
        witness={"raw_logs": []},
    ),
}


@pytest.mark.parametrize("name", sorted(EXAMPLES))
def test_review_examples_traps_are_refused_and_answers_agree(name):
    case = EXAMPLES[name]
    sources = {k: {"columns": v} for k, v in case["sources"].items()}
    tables, protected = case["tables"], case["protected"]
    # the trap really is wrong, on the witness
    assert _run(tables, protected, case["sources"], case["witness"]) != _run(case["trap"], protected, case["sources"], case["witness"])
    verdicts = table_minimizer.verify_tables(tables, case["trap"], protected, sources=sources)
    assert any(v.status in ("unknown", "missing") for v in verdicts.values()), verdicts
    result = minimize_tables(tables, protected, sources=sources)
    assert set(protected) <= set(result.tables)
    for data in [case["witness"], *(_random_data(case["sources"], seed) for seed in range(30))]:
        assert _run(tables, protected, case["sources"], data) == _run(result.tables, protected, case["sources"], data)
    # the minimizer's answer passes the same check
    assert all(v.status in ("proved", "unchanged") for v in
               table_minimizer.verify_tables(tables, result.tables, protected, sources=sources).values())


def test_non_deterministic_tables_are_never_folded_or_rewritten():
    tables = {
        "sample": "SELECT id, amount FROM orders WHERE RAND() < 0.5",
        "a": "SELECT id FROM sample",
        "b": "SELECT amount FROM sample",
        "firsts": "SELECT id FROM orders ORDER BY id LIMIT 3",
        "c": "SELECT id FROM firsts",
    }
    result = minimize_tables(tables, ["a", "b", "c"], sources=SOURCES)
    assert result.tables["sample"] == tables["sample"] and result.tables["firsts"] == tables["firsts"]


def test_a_with_table_never_captures_a_folded_table():
    # folding ``base`` into ``report`` would write ``FROM orders``, which report's own WITH table named
    # ``orders`` would capture
    tables = {
        "base": "SELECT id, amount FROM orders WHERE amount > 0",
        "report": "WITH orders AS (SELECT 1 AS id) SELECT b.id, b.amount FROM base AS b JOIN orders AS o ON b.id = o.id",
    }
    result = minimize_tables(tables, ["report"], sources=SOURCES)
    _assert_same(tables, result.tables, ["report"], databases=40)
    capture = {"report": "WITH orders AS (SELECT 1 AS id) SELECT b.id, b.amount FROM "
                         "(SELECT id, amount FROM orders WHERE amount > 0) AS b JOIN orders AS o ON b.id = o.id"}
    assert table_minimizer.verify_tables(tables, capture, ["report"], sources=SOURCES)["report"].status == "unknown"


def test_tables_with_one_name_in_two_datasets_stay_apart():
    tables = {
        "a.t": "SELECT id, amount FROM orders WHERE amount > 0",
        "b.t": "SELECT id, amount FROM orders",
        "r": "SELECT id FROM a.t",
        "s": "SELECT COUNT(*) AS n FROM b.t",
    }
    result = minimize_tables(tables, ["r", "s"], sources=SOURCES)
    con = duckdb.connect()
    con.execute("CREATE TABLE orders (id BIGINT, customer_id BIGINT, amount BIGINT, status VARCHAR)")
    con.execute("INSERT INTO orders VALUES (1, 1, -5, 'paid'), (2, 1, 3, 'open'), (3, 2, NULL, NULL)")
    con.execute("CREATE SCHEMA a")
    con.execute("CREATE SCHEMA b")

    def outputs(pipeline):
        for name in ("a.t", "b.t", "r", "s"):
            con.execute(f"DROP VIEW IF EXISTS {name}")
        for name in ("a.t", "b.t", "r", "s"):
            if name in pipeline:
                con.execute(f"CREATE VIEW {name} AS {sqlglot.transpile(pipeline[name], read='bigquery', write='duckdb')[0]}")
        return {name: sorted(con.execute(f"SELECT * FROM {name}").fetchall(), key=str) for name in ("r", "s")}

    assert outputs(tables) == outputs(result.tables)
    swapped = {**tables, "r": "SELECT id FROM b.t"}
    assert table_minimizer.verify_tables(tables, swapped, ["r", "s"], sources=SOURCES)["r"].status == "unknown"


SHARED = {
    "rpt_a": "SELECT p.customer_id, SUM(p.amount) AS total FROM (SELECT o.customer_id, o.amount FROM orders AS o "
    "JOIN customers AS c ON o.customer_id = c.id WHERE c.region = 'eu' AND o.status = 'paid') AS p GROUP BY p.customer_id",
    "rpt_b": "SELECT q.customer_id, COUNT(*) AS n FROM (SELECT o.customer_id, o.amount FROM orders AS o "
    "JOIN customers AS c ON o.customer_id = c.id WHERE c.region = 'eu' AND o.status = 'paid') AS q GROUP BY q.customer_id",
    "rpt_c": "WITH paid_eu AS (SELECT o.customer_id, o.amount FROM orders AS o JOIN customers AS c "
    "ON o.customer_id = c.id WHERE c.region = 'eu' AND o.status = 'paid') SELECT MAX(amount) AS biggest FROM paid_eu",
}


def test_factoring_moves_a_repeated_query_into_one_new_table():
    protected = ["rpt_a", "rpt_b", "rpt_c"]
    plain = minimize_tables(SHARED, protected, sources=SOURCES)
    assert plain.added == []
    result = minimize_tables(SHARED, protected, sources=SOURCES, factor=True)
    assert result.added == ["paid_eu"]  # named after the CTE it replaces
    assert set(result.tables) == {*protected, "paid_eu"}
    assert result.score < plain.score
    assert all("JOIN" not in result.tables[name].upper() and "paid_eu" in result.tables[name] for name in protected)
    assert {proof.status for proof in result.proofs.values()} == {"proved"}
    assert result.to_json()["added"] == ["paid_eu"]
    _assert_same(SHARED, result.tables, protected)


def test_checked_tables_keep_their_rows_and_fixed_tables_their_sql():
    tables = {
        "stage": "SELECT id, customer_id, amount, status FROM orders WHERE status = 'paid'",
        "report": "SELECT customer_id, SUM(amount) AS total FROM stage GROUP BY customer_id",
    }
    moved = {  # the filter moved downstream: the report is the same, the stage is not
        "stage": "SELECT id, customer_id, amount, status FROM orders",
        "report": "SELECT customer_id, SUM(amount) AS total FROM stage WHERE status = 'paid' GROUP BY customer_id",
    }
    verify = table_minimizer.verify_tables
    assert set(verify(tables, moved, ["report"], sources=SOURCES)) == {"report"}
    found = verify(tables, moved, ["report"], sources=SOURCES, checked=["stage"])
    assert found["report"].status == "proved" and found["stage"].status == "unknown"
    narrowed = {**tables, "stage": "SELECT id, customer_id, amount FROM orders WHERE status = 'paid'"}
    assert verify(tables, narrowed, ["report"], sources=SOURCES, checked=["stage"])["stage"].status == "proved"
    # a fixed table is read as given: a candidate that changes it proves nothing
    found = verify(tables, moved, ["report"], sources=SOURCES, fixed={"stage": ["id", "customer_id", "amount", "status"]})
    assert found["report"].status == "unknown"

    shared = {
        "stage": "SELECT id, customer_id, CASE WHEN amount > 10 THEN 'big' WHEN amount > 0 THEN 'small' ELSE 'none' END AS size, "
        "CASE WHEN status = 'open' THEN 1 ELSE 0 END AS is_open FROM orders WHERE status <> 'void' AND amount IS NOT NULL",
        "a": "SELECT customer_id, size FROM stage WHERE id > 3",
        "b": "SELECT size, COUNT(*) AS n FROM stage GROUP BY size",
        "c": "SELECT id FROM stage WHERE size = 'big'",
    }
    pruned = minimize_tables(shared, ["a", "b", "c"], sources=SOURCES, checked=["stage"])
    assert pruned.moves == ["prune unused columns of stage"] and "is_open" not in pruned.tables["stage"]
    kept = minimize_tables(shared, ["a", "b", "c"], sources=SOURCES, checked=["stage"], keep_columns={"stage": ["is_open"]})
    assert kept.tables == shared  # a column its assertions name is never pruned
    fixed = minimize_tables(shared, ["a", "b", "c"], sources=SOURCES, fixed={"stage": None})
    assert fixed.tables["stage"] == shared["stage"]
    _assert_same(shared, pruned.tables, ["a", "b", "c"])


def test_lower_score_only_skips_rewrites_that_do_not_lower_the_score():
    tables = {"report": "SELECT customer_id AS c, SUM(amount) AS total FROM orders GROUP BY customer_id"}
    result = minimize_tables(tables, ["report"], sources=SOURCES, lower_score_only=True)
    assert result.tables == tables and result.moves == []


def test_tables_that_reuse_with_names_can_be_folded_together():
    # the dbt style: every staging table is WITH source AS (...), renamed AS (...) SELECT * FROM renamed
    tables = {
        "stg_orders": "WITH source AS (SELECT * FROM orders), renamed AS (SELECT id AS order_id, customer_id, amount "
        "FROM source WHERE amount > 0) SELECT * FROM renamed",
        "stg_customers": "WITH source AS (SELECT * FROM customers), renamed AS (SELECT id AS customer_id, region FROM source) "
        "SELECT * FROM renamed",
        "report": "SELECT c.region, SUM(o.amount) AS total FROM stg_orders AS o JOIN stg_customers AS c "
        "ON o.customer_id = c.customer_id GROUP BY c.region",
    }
    result = minimize_tables(tables, ["report"], sources=SOURCES)
    assert set(result.tables) == {"report"} and result.proofs["report"].status == "proved"
    _assert_same(tables, result.tables, ["report"])
    # the renaming for the proof follows scopes: a WITH name read in its own body is the pipeline's table
    sql = "WITH orders AS (SELECT * FROM orders), b AS (SELECT * FROM orders) SELECT * FROM b JOIN (WITH b AS (SELECT 1 AS x) SELECT * FROM b) AS q ON TRUE"
    tree = sqlglot.parse_one(sql, read="bigquery")
    counter = [0]
    for clause in reversed(list(tree.find_all(sqlglot.exp.With))):
        table_minimizer._unique_ctes(clause.parent, counter)
    assert tree.sql(dialect="bigquery") == (
        "WITH orders__kumo2 AS (SELECT * FROM orders), b__kumo3 AS (SELECT * FROM orders__kumo2 AS orders) "
        "SELECT * FROM b__kumo3 AS b JOIN (WITH b__kumo1 AS (SELECT 1 AS x) SELECT * FROM b__kumo1 AS b) AS q ON TRUE")
