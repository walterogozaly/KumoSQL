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


# ---- #538: a protected output must not change under a script, a name's case, a name clash or a nested WITH

RAW = {"raw": {"x": "INT64"}}


def _min(tables, protected, sources=None, **kw):
    return minimize_tables(tables, protected, sources=sources or RAW, timeout_ms=1000, max_seconds=60, max_steps=15, **kw)


def _execute(tables):
    """Each table's rows after running ``tables`` in dependency order over ``raw(x) = (1)``, in DuckDB."""

    con = duckdb.connect()
    con.execute("CREATE TABLE raw (x BIGINT)")
    con.execute("INSERT INTO raw VALUES (1)")
    pending = dict(tables)
    while pending:
        progressed = False
        for name, sql in list(pending.items()):
            try:
                con.execute(f"CREATE VIEW {name} AS {sqlglot.transpile(sql, read='bigquery', write='duckdb')[0]}")
            except duckdb.CatalogException:
                continue
            del pending[name]
            progressed = True
        assert progressed, pending
    return {name: sorted(con.execute(f"SELECT * FROM {name}").fetchall()) for name in tables}


@pytest.mark.parametrize("sql", [
    "SELECT 1 AS x; SELECT 2 AS x",
    "SELECT 1 AS x; ",  # one statement and a stray semicolon is still one query
])
def test_single_query_counts_statements(sql):
    from kumosql.minimizer_identity import single_query

    assert (single_query(sql, "bigquery") is not None) == (sql.count(";") == 1 and sql.strip().endswith(";"))


@pytest.mark.parametrize("name, tables", [
    # a script's last result is not its first SELECT
    ("last-result", {"stage": "SELECT x FROM raw", "out": "SELECT x FROM stage; SELECT x + 100 AS x FROM stage"}),
    # a later statement's effect on a table is part of the script
    ("effect", {"effect": "SELECT 0 AS x; DELETE FROM raw; SELECT 0 AS x", "out": "SELECT x FROM raw"}),
    # a later statement's reads are dependencies
    ("tail", {"tail": "SELECT x FROM raw", "out": "SELECT 0 AS x; SELECT x FROM tail"}),
])
def test_a_script_is_kept_whole_with_everything_it_mentions(name, tables):
    result = _min(tables, ["out"])
    assert result.tables == tables and not result.moves and not result.removed
    assert result.proofs["out" if "out" in tables else name].status in {"unchanged"}


def test_a_candidate_script_is_not_checked_as_its_first_select():
    tables = {"stage": "SELECT x FROM raw", "out": "SELECT x FROM stage"}
    candidate = {"stage": "SELECT x FROM raw", "out": "SELECT x FROM stage; SELECT x + 100 AS x FROM stage"}
    assert table_minimizer.verify_tables(tables, candidate, ["out"], sources=RAW)["out"].status == "unknown"
    candidate = {"stage": "SELECT x FROM raw; SELECT x + 100 AS x FROM raw", "out": "SELECT x FROM stage"}
    assert table_minimizer.verify_tables(tables, candidate, ["out"], sources=RAW)["out"].status == "unknown"


def test_a_protected_script_gets_a_result_instead_of_none():
    tables = {"out": "SELECT 1 AS x; SELECT 2 AS x"}
    assert _min(tables, ["out"]).proofs["out"].status == "unchanged"
    assert table_minimizer.verify_tables(tables, {}, ["out"], sources=RAW)["out"].status == "missing"
    assert table_minimizer.verify_tables(tables, tables, ["out"], sources=RAW)["out"].status == "unchanged"
    assert table_minimizer.verify_tables(tables, {"out": "SELECT 2 AS x"}, ["out"], sources=RAW)["out"].status == "unknown"


@pytest.mark.parametrize("external", ["p.D.stage", "p.d.Stage"])
def test_names_that_differ_only_by_case_are_different_tables(external):
    tables = {"p.d.stage": "SELECT x FROM raw", "out": f"SELECT x FROM `{external}`"}
    result = _min(tables, ["out"])
    assert result.tables == tables and result.proofs["out"].status == "unchanged"
    # a declared source of that name is not "both a source and a table" either
    declared = _min(tables, ["out"], sources={**RAW, external: {"x": "INT64"}})
    assert declared.tables == tables
    # and a changed model is not credited to the reader of the other-case name
    changed = {**tables, "p.d.stage": "SELECT 99 AS x"}
    assert table_minimizer.verify_tables(tables, changed, ["out"], sources=RAW)["out"].status == "unknown"


def test_a_case_clash_pins_what_it_touches_but_leaves_the_rest_to_the_search():
    tables = {
        "p.d.stage": "SELECT x FROM raw",
        "out": "SELECT x FROM `p.D.stage`",
        "a": "SELECT x FROM raw",
        "b": "SELECT x + 1 AS y FROM a",
    }
    result = _min(tables, ["out", "b"])
    assert result.tables["out"] == tables["out"] and "p.d.stage" in result.tables
    assert "a" not in result.tables  # the unrelated chain is still folded, and proved
    assert result.proofs["b"].status == "proved"


def test_a_bare_name_cannot_be_overwritten_by_an_internal_one():
    tables = {"t": "SELECT 1 AS x", "kumo_min.tables.t": "SELECT 2 AS x", "out": "SELECT x FROM t"}
    result = _min(tables, ["out"])
    assert result.original_score == pipeline_score(tables)
    assert _execute(result.tables)["out"] == [(1,)]
    assert _min({"kumo_min.tables.t": "SELECT 2 AS x", "out": "SELECT x FROM kumo_min.tables.t"}, ["out"]).tables


def test_a_bare_name_and_a_two_part_name_stay_apart():
    tables = {"t": "SELECT 1 AS x", "tables.t": "SELECT 2 AS x", "out": "SELECT x FROM t", "out2": "SELECT x FROM tables.t"}
    result = _min(tables, ["out", "out2"])
    assert result.original_score == pipeline_score(tables)
    assert _execute({"t": "SELECT 1 AS x", "out": result.tables["out"]})["out"] == [(1,)]


NESTED = ("SELECT x FROM stage UNION ALL "
          "SELECT x FROM (WITH stage AS (SELECT 100 AS x) SELECT x FROM stage)")


def test_a_with_table_hides_a_physical_read_only_inside_its_own_query():
    from kumosql.refactor import _table_nodes

    tree = sqlglot.parse_one(NESTED, read="bigquery")
    assert [(t.name, t.sql()) for t in _table_nodes(tree)] == [("stage", "stage")]  # the outer read only
    # a WITH table sees the ones listed before it, so ``b`` in the first is a physical table
    tree = sqlglot.parse_one("WITH a AS (SELECT x FROM b), b AS (SELECT x FROM a) SELECT x FROM a", read="bigquery")
    assert [t.name for t in _table_nodes(tree)] == ["b"]


def test_a_nested_with_table_does_not_hide_the_outer_read():
    tables = {"stage": "SELECT x FROM raw", "out": NESTED}
    changed = {**tables, "stage": "SELECT 99 AS x"}
    assert table_minimizer.verify_tables(tables, changed, ["out"], sources=RAW)["out"].status == "unknown"
    assert _execute(tables)["out"] == [(1,), (100,)] and _execute(changed)["out"] == [(99,), (100,)]
    result = _min(tables, ["out"])
    assert _execute(result.tables)["out"] == [(1,), (100,)]  # whatever it kept or folded, out keeps its rows


def test_an_answer_never_reads_a_table_it_removed():
    from kumosql.minimizer_identity import single_query

    tables = {"stage": "SELECT x FROM raw", "out": NESTED}
    setup, names, mapping, back = table_minimizer._prepare(tables, ["out"], RAW, "bigquery", 1000)
    state = {k: v for k, v in setup.original.items() if not k.endswith(".stage")}
    ok, _, why = table_minimizer._check(setup, state)
    assert not ok and "no longer exists" in why
    out = {"out": "SELECT x FROM stage"}
    assert not table_minimizer._holds_together(setup, state, out, names, back, "bigquery")
    assert single_query(out["out"], "bigquery") is not None
