"""Write tests/fixtures/mv_reuse/outer_union_cases.json: outer-join, key-aware, union-compensation and
set-operation view-matching cases, each with a checked answer.

Every case is a model, a query and ``expect``: ``rewrite`` (the query can be answered by reading the model)
or ``none`` (it cannot). A ``rewrite`` case carries ``witness``, a replacement over ``mv0`` written by hand;
``traps`` are tempting replacements that are wrong. ``tests/test_outer_union_mv_cases.py`` re-checks both
on random databases: every witness must agree with the query and every trap must be refuted, so a label
never rests on the engine under test. A ``none`` case says why no rewrite exists in ``why``.

Families (``origin``):

* ``outer``: outer-join views (the view-matching problem of Larson and Zhou, "View matching for outer-join
  views", VLDB 2005): an inner or narrower outer join read from a wider outer-join view, the anti join,
  aggregates over outer-join views, and the traps where a null-extended row cannot be told apart.
* ``keys``: views with extra joins that keep every row exactly once (a NOT NULL foreign key to a unique
  parent), and injective grouping keys.
* ``union``: the view covers part of the query's range and the rest is read from the base tables.
  Adapted from StarRocks 3.3.0 ``MvRewriteUnionTest`` (Apache-2.0,
  fe/fe-core/src/test/java/com/starrocks/sql/optimizer/rule/transformation/materialization/), tables and
  SQL kept, the partition refresh of ``testUnionAllRewriteWithExtraPredicates`` read as the view's filter
  ``k1 < 3``; the NULL, overlap, missing-column and AVG traps are written for KumoSQL.
* ``setop``: views that are set operations, read through a filter or a column reorder.

Schemas: ``hr`` is Calcite's HR schema with its foreign key (``tools/mv_reuse_bench.py``), ``hr_plain``
the same tables with no keys, ``shop`` and ``sr`` are defined below.
"""

from __future__ import annotations

import json
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "mv_reuse" / "outer_union_cases.json"

SCHEMAS = {
    "shop": {
        "customers": {"columns": [["c_id", "int", True], ["c_name", "text", False], ["c_region", "text", False]], "keys": [["c_id"]]},
        "orders": {
            "columns": [["o_id", "int", True], ["o_cust", "int", False], ["o_total", "float", True], ["o_date", "date", True], ["o_status", "text", False]],
            "keys": [["o_id"]],
            "foreign_keys": [[["o_cust"], "customers", ["c_id"]]],
        },
        "parts": {"columns": [["p_id", "int", True], ["p_name", "text", False], ["p_brand", "text", False]], "keys": [["p_id"]]},
        "items": {
            "columns": [["i_order", "int", True], ["i_line", "int", True], ["i_part", "int", True], ["i_qty", "int", False], ["i_price", "float", False]],
            "keys": [["i_order", "i_line"]],
            "foreign_keys": [[["i_order"], "orders", ["o_id"]], [["i_part"], "parts", ["p_id"]]],
        },
        "notes": {"columns": [["n_order", "int", False], ["n_text", "text", False]]},
    },
    # StarRocks MvRewriteUnionTest's tables (StarRocks columns are nullable unless declared NOT NULL)
    "sr": {
        "emps2": {"columns": [["empid", "int", True], ["deptno", "int", True], ["name", "text", True], ["salary", "float", False]]},
        "depts2": {"columns": [["deptno", "int", True], ["name", "text", True]]},
        "t02": {"columns": [["v1", "int", False], ["v2", "int", False], ["v3", "int", False]]},
        "test_all_type2": {
            "columns": [["t1a", "text", False], ["t1b", "int", False], ["t1c", "int", False], ["t1d", "int", False], ["t1e", "float", False], ["t1f", "float", False], ["t1g", "int", False], ["id_date", "date", False]]
        },
        "mt1": {"columns": [["k1", "int", False], ["k2", "text", False], ["v1", "int", False], ["v2", "int", False]]},
    },
}

OC = "SELECT o.o_id AS o_id, o.o_total AS o_total, o.o_status AS o_status, c.c_id AS c_id, c.c_name AS c_name, c.c_region AS c_region FROM orders o LEFT JOIN customers c ON o.o_cust = c.c_id"
FULL = "SELECT o.o_id AS o_id, o.o_total AS o_total, c.c_id AS c_id, c.c_name AS c_name FROM orders o FULL JOIN customers c ON o.o_cust = c.c_id"
IOC = (
    "SELECT i.i_order AS i_order, i.i_line AS i_line, i.i_qty AS i_qty, o.o_id AS o_id, o.o_status AS o_status, c.c_id AS c_id, c.c_name AS c_name "
    "FROM items i LEFT JOIN orders o ON i.i_order = o.o_id LEFT JOIN customers c ON o.o_cust = c.c_id"
)
OC_AGG = "SELECT c.c_region AS c_region, o.o_status AS o_status, COUNT(*) AS n, SUM(o.o_total) AS total FROM orders o LEFT JOIN customers c ON o.o_cust = c.c_id GROUP BY c.c_region, o.o_status"
OC_JOIN = "FROM orders o JOIN customers c ON o.o_cust = c.c_id"
OC_LEFT = "FROM orders o LEFT JOIN customers c ON o.o_cust = c.c_id"

LARSON_ZHOU = "Larson and Zhou, View matching for outer-join views (VLDB 2005); written for KumoSQL"
OWN = "written for KumoSQL"
STARROCKS = "StarRocks 3.3.0 MvRewriteUnionTest.{} (Apache-2.0), adapted"


def case(name, origin, schema, model, query, expect, *, witness=None, traps=(), why=None, source=OWN):
    out = {"id": f"ou.{origin}.{name}", "name": name, "origin": origin, "schema": schema, "materialization": model, "query": query, "expect": expect, "source": source}
    if witness:
        out["witness"] = witness
    if traps:
        out["traps"] = list(traps)
    if why:
        out["why"] = why
    return out


CASES = [
    # ---- outer-join views (Larson and Zhou's terms: the inner part, and each null-extended part)
    case("inner-from-left", "outer", "shop", OC, f"SELECT o.o_id, c.c_name {OC_JOIN}", "rewrite", witness="SELECT o_id, c_name FROM mv0 WHERE c_id IS NOT NULL", traps=["SELECT o_id, c_name FROM mv0"], source=LARSON_ZHOU),
    case("left-projection", "outer", "shop", OC, f"SELECT o.o_id, c.c_name {OC_LEFT}", "rewrite", witness="SELECT o_id, c_name FROM mv0", source=LARSON_ZHOU),
    case("preserved-side-filter", "outer", "shop", OC, f"SELECT o.o_id, c.c_region {OC_LEFT} WHERE o.o_total > 100", "rewrite", witness="SELECT o_id, c_region FROM mv0 WHERE o_total > 100", source=LARSON_ZHOU),
    case("null-side-filter-in-where", "outer", "shop", OC, f"SELECT o.o_id, c.c_name {OC_LEFT} WHERE c.c_region = 'EU'", "rewrite", witness="SELECT o_id, c_name FROM mv0 WHERE c_region = 'EU'", traps=["SELECT o_id, c_name FROM mv0 WHERE c_region = 'EU' OR c_id IS NULL"], source=LARSON_ZHOU),
    case(
        "null-side-filter-in-on-keyed",
        "outer",
        "shop",
        OC,
        "SELECT o.o_id, c.c_name FROM orders o LEFT JOIN customers c ON o.o_cust = c.c_id AND c.c_region = 'EU'",
        "rewrite",
        witness="SELECT o_id, CASE WHEN c_region = 'EU' THEN c_name END AS c_name FROM mv0",
        traps=["SELECT o_id, c_name FROM mv0 WHERE c_region = 'EU' OR c_id IS NULL", "SELECT o_id, c_name FROM mv0"],
    ),
    case(
        "null-side-filter-in-on-unkeyed",
        "outer",
        "shop",
        "SELECT o.o_total AS o_total, n.n_text AS n_text FROM orders o LEFT JOIN notes n ON n.n_order = o.o_id",
        "SELECT o.o_total, n.n_text FROM orders o LEFT JOIN notes n ON n.n_order = o.o_id AND n.n_text = 'x'",
        "none",
        traps=["SELECT o_total, CASE WHEN n_text = 'x' THEN n_text END AS n_text FROM mv0", "SELECT o_total, n_text FROM mv0 WHERE n_text = 'x' OR n_text IS NULL"],
        why="orders with notes {x, y} and an order with no note, versus orders with {x}, {y} and none, give the same view rows and different query rows",
    ),
    case("single-table-keyed-parent", "outer", "shop", OC, "SELECT o_id, o_total FROM orders WHERE o_status = 'open'", "rewrite", witness="SELECT o_id, o_total FROM mv0 WHERE o_status = 'open'", source=LARSON_ZHOU),
    case(
        "single-table-unkeyed-child",
        "outer",
        "shop",
        "SELECT o.o_total AS o_total, n.n_text AS n_text FROM orders o LEFT JOIN notes n ON n.n_order = o.o_id",
        "SELECT o_total FROM orders",
        "none",
        traps=["SELECT o_total FROM mv0", "SELECT DISTINCT o_total FROM mv0"],
        why="an order with two notes gives two view rows; nothing in the view says which rows are one order",
    ),
    case(
        "single-table-dedup-by-key",
        "outer",
        "shop",
        "SELECT o.o_id AS o_id, o.o_total AS o_total, n.n_text AS n_text FROM orders o LEFT JOIN notes n ON n.n_order = o.o_id",
        "SELECT o_id, o_total FROM orders",
        "rewrite",
        witness="SELECT DISTINCT o_id, o_total FROM mv0",
        traps=["SELECT o_id, o_total FROM mv0"],
    ),
    case("anti-join", "outer", "shop", OC, f"SELECT o.o_id {OC_LEFT} WHERE c.c_id IS NULL", "rewrite", witness="SELECT o_id FROM mv0 WHERE c_id IS NULL", traps=["SELECT o_id FROM mv0 WHERE c_name IS NULL"], source=LARSON_ZHOU),
    case("not-exists", "outer", "shop", OC, "SELECT o.o_id FROM orders o WHERE NOT EXISTS (SELECT 1 FROM customers c WHERE c.c_id = o.o_cust)", "rewrite", witness="SELECT o_id FROM mv0 WHERE c_id IS NULL"),
    case("full-to-left", "outer", "shop", FULL, f"SELECT o.o_id, o.o_total, c.c_name {OC_LEFT}", "rewrite", witness="SELECT o_id, o_total, c_name FROM mv0 WHERE o_id IS NOT NULL", traps=["SELECT o_id, o_total, c_name FROM mv0"], source=LARSON_ZHOU),
    case("full-to-right", "outer", "shop", FULL, "SELECT o.o_id, c.c_id, c.c_name FROM orders o RIGHT JOIN customers c ON o.o_cust = c.c_id", "rewrite", witness="SELECT o_id, c_id, c_name FROM mv0 WHERE c_id IS NOT NULL", source=LARSON_ZHOU),
    case("full-to-inner", "outer", "shop", FULL, f"SELECT o.o_id, c.c_name {OC_JOIN}", "rewrite", witness="SELECT o_id, c_name FROM mv0 WHERE o_id IS NOT NULL AND c_id IS NOT NULL", traps=["SELECT o_id, c_name FROM mv0 WHERE o_id IS NOT NULL"], source=LARSON_ZHOU),
    case(
        "full-to-one-side-by-key",
        "outer",
        "shop",
        FULL,
        "SELECT c.c_name FROM customers c",
        "rewrite",
        witness="SELECT c_name FROM (SELECT DISTINCT c_id, c_name FROM mv0 WHERE c_id IS NOT NULL) AS t",
        traps=["SELECT c_name FROM mv0 WHERE c_id IS NOT NULL"],
    ),
    case("chain-inner-then-left", "outer", "shop", IOC, "SELECT i.i_order, i.i_qty, c.c_name FROM items i JOIN orders o ON i.i_order = o.o_id LEFT JOIN customers c ON o.o_cust = c.c_id", "rewrite", witness="SELECT i_order, i_qty, c_name FROM mv0 WHERE o_id IS NOT NULL", source=LARSON_ZHOU),
    case("chain-all-inner", "outer", "shop", IOC, "SELECT i.i_order, i.i_qty, c.c_name FROM items i JOIN orders o ON i.i_order = o.o_id JOIN customers c ON o.o_cust = c.c_id", "rewrite", witness="SELECT i_order, i_qty, c_name FROM mv0 WHERE c_id IS NOT NULL", traps=["SELECT i_order, i_qty, c_name FROM mv0"], source=LARSON_ZHOU),
    case("chain-first-table-only", "outer", "shop", IOC, "SELECT i_order, i_line, i_qty FROM items WHERE i_qty > 2", "rewrite", witness="SELECT i_order, i_line, i_qty FROM mv0 WHERE i_qty > 2", source=LARSON_ZHOU),
    case("aggregate-over-left", "outer", "shop", OC, f"SELECT c.c_region, COUNT(*) AS n, SUM(o.o_total) AS total {OC_LEFT} GROUP BY c.c_region", "rewrite", witness="SELECT c_region, COUNT(*) AS n, SUM(o_total) AS total FROM mv0 GROUP BY c_region"),
    case("aggregate-inner-from-left", "outer", "shop", OC, f"SELECT c.c_name, SUM(o.o_total) AS total {OC_JOIN} GROUP BY c.c_name", "rewrite", witness="SELECT c_name, SUM(o_total) AS total FROM mv0 WHERE c_id IS NOT NULL GROUP BY c_name", traps=["SELECT c_name, SUM(o_total) AS total FROM mv0 GROUP BY c_name"]),
    case("aggregated-left-rollup", "outer", "shop", OC_AGG, f"SELECT c.c_region, SUM(o.o_total) AS total, COUNT(*) AS n {OC_LEFT} GROUP BY c.c_region", "rewrite", witness="SELECT c_region, SUM(total) AS total, SUM(n) AS n FROM mv0 GROUP BY c_region"),
    case(
        "aggregated-left-to-inner",
        "outer",
        "shop",
        OC_AGG,
        f"SELECT c.c_region, SUM(o.o_total) AS total {OC_JOIN} GROUP BY c.c_region",
        "none",
        traps=["SELECT c_region, SUM(total) AS total FROM mv0 WHERE c_region IS NOT NULL GROUP BY c_region", "SELECT c_region, SUM(total) AS total FROM mv0 GROUP BY c_region"],
        why="an order without a customer and an order whose customer has no region fall in the same view group",
    ),
    case(
        "wrong-direction",
        "outer",
        "shop",
        OC,
        "SELECT c.c_id, c.c_name, o.o_id FROM customers c LEFT JOIN orders o ON o.o_cust = c.c_id",
        "none",
        traps=["SELECT c_id, c_name, o_id FROM mv0 WHERE c_id IS NOT NULL"],
        why="a customer without orders is not in the view",
    ),
    case(
        "view-filters-null-side-in-on",
        "outer",
        "shop",
        "SELECT o.o_id AS o_id, c.c_name AS c_name FROM orders o LEFT JOIN customers c ON o.o_cust = c.c_id AND c.c_region = 'EU'",
        f"SELECT o.o_id, c.c_name {OC_LEFT}",
        "none",
        traps=["SELECT o_id, c_name FROM mv0"],
        why="the view drops the names of customers outside EU",
    ),
    case(
        "view-filters-preserved-side",
        "outer",
        "shop",
        f"SELECT o.o_id AS o_id, o.o_total AS o_total, c.c_name AS c_name {OC_LEFT} WHERE o.o_total > 100",
        f"SELECT o.o_id, c.c_name {OC_LEFT}",
        "rewrite",
        witness=f"SELECT o_id, c_name FROM mv0 UNION ALL SELECT o.o_id, c.c_name {OC_LEFT} WHERE NOT (o.o_total > 100)",
        traps=["SELECT o_id, c_name FROM mv0"],
    ),
    case("filters-on-both-sides", "outer", "shop", OC, f"SELECT o.o_id {OC_JOIN} WHERE c.c_region = 'EU' AND o.o_status = 'open'", "rewrite", witness="SELECT o_id FROM mv0 WHERE c_region = 'EU' AND o_status = 'open'", source=LARSON_ZHOU),
    case("coalesce-of-null-side", "outer", "shop", OC, f"SELECT o.o_id, COALESCE(c.c_name, 'none') AS who {OC_LEFT}", "rewrite", witness="SELECT o_id, COALESCE(c_name, 'none') AS who FROM mv0"),
    case(
        "no-presence-column",
        "outer",
        "shop",
        "SELECT o.o_id AS o_id, c.c_name AS c_name FROM orders o LEFT JOIN customers c ON o.o_cust = c.c_id",
        f"SELECT o.o_id, c.c_name {OC_JOIN}",
        "none",
        traps=["SELECT o_id, c_name FROM mv0 WHERE c_name IS NOT NULL"],
        why="an order whose customer has no name looks like an order without a customer",
    ),
    # ---- extra joins that keep every row once, and injective grouping keys
    case("fk-join-dropped", "keys", "hr", "SELECT e.empid AS empid, e.name AS name, d.name AS dname FROM emps e JOIN depts d ON e.deptno = d.deptno", "SELECT empid, name FROM emps", "rewrite", witness="SELECT empid, name FROM mv0"),
    case(
        "unkeyed-join-kept",
        "keys",
        "hr",
        "SELECT e.empid AS empid, e.name AS name FROM emps e JOIN dependents p ON e.empid = p.empid",
        "SELECT empid, name FROM emps",
        "none",
        traps=["SELECT empid, name FROM mv0", "SELECT DISTINCT empid, name FROM mv0"],
        why="employees without dependents are missing and employees with two dependents repeat",
    ),
    case(
        "nullable-fk-join",
        "keys",
        "shop",
        f"SELECT o.o_id AS o_id, o.o_total AS o_total, c.c_name AS c_name {OC_JOIN}",
        "SELECT o_id, o_total FROM orders",
        "none",
        traps=["SELECT o_id, o_total FROM mv0"],
        why="orders with no customer are not in the view",
    ),
    case("nullable-fk-join-not-null-query", "keys", "shop", f"SELECT o.o_id AS o_id, o.o_total AS o_total, c.c_name AS c_name {OC_JOIN}", "SELECT o_id, o_total FROM orders WHERE o_cust IS NOT NULL", "rewrite", witness="SELECT o_id, o_total FROM mv0"),
    case("not-null-fk-join", "keys", "shop", "SELECT i.i_order AS i_order, i.i_qty AS i_qty, p.p_brand AS p_brand FROM items i JOIN parts p ON i.i_part = p.p_id", "SELECT i_order, i_qty FROM items WHERE i_qty > 2", "rewrite", witness="SELECT i_order, i_qty FROM mv0 WHERE i_qty > 2"),
    case(
        "two-fk-joins-one-dropped",
        "keys",
        "shop",
        "SELECT i.i_order AS i_order, i.i_qty AS i_qty, p.p_name AS p_name, o.o_date AS o_date FROM items i JOIN parts p ON i.i_part = p.p_id JOIN orders o ON i.i_order = o.o_id",
        "SELECT i.i_qty, p.p_name FROM items i JOIN parts p ON i.i_part = p.p_id",
        "rewrite",
        witness="SELECT i_qty, p_name FROM mv0",
    ),
    case(
        "fk-join-with-parent-filter",
        "keys",
        "shop",
        "SELECT i.i_order AS i_order, i.i_qty AS i_qty FROM items i JOIN parts p ON i.i_part = p.p_id WHERE p.p_brand = 'A'",
        "SELECT i_order, i_qty FROM items",
        "none",
        traps=["SELECT i_order, i_qty FROM mv0"],
        why="items of other brands are not in the view",
    ),
    case("injective-group-key", "keys", "hr_plain", "SELECT deptno, COUNT(DISTINCT name) AS n FROM emps GROUP BY deptno", "SELECT deptno * 2 AS d2, COUNT(DISTINCT name) AS n FROM emps GROUP BY deptno * 2", "rewrite", witness="SELECT deptno * 2 AS d2, n FROM mv0"),
    case(
        "non-injective-group-key",
        "keys",
        "hr_plain",
        "SELECT deptno, COUNT(DISTINCT name) AS n FROM emps GROUP BY deptno",
        "SELECT deptno % 10 AS d, COUNT(DISTINCT name) AS n FROM emps GROUP BY deptno % 10",
        "none",
        traps=["SELECT deptno % 10 AS d, SUM(n) AS n FROM mv0 GROUP BY deptno % 10", "SELECT deptno % 10 AS d, MAX(n) AS n FROM mv0 GROUP BY deptno % 10"],
        why="distinct counts of two departments that share a remainder cannot be added",
    ),
    case("injective-key-with-other-key", "keys", "hr_plain", "SELECT deptno, name, SUM(salary) AS s FROM emps GROUP BY deptno, name", "SELECT deptno + 1 AS d, name, SUM(salary) AS s FROM emps GROUP BY deptno + 1, name", "rewrite", witness="SELECT deptno + 1 AS d, name, s FROM mv0"),
    # ---- union compensation: the view covers part of the query's range
    case(
        "single-table-range",
        "union",
        "sr",
        "SELECT empid, deptno, name, salary FROM emps2 WHERE empid < 3",
        "SELECT empid, deptno, name, salary FROM emps2 WHERE empid < 5",
        "rewrite",
        witness="SELECT empid, deptno, name, salary FROM mv0 UNION ALL SELECT empid, deptno, name, salary FROM emps2 WHERE empid < 5 AND empid >= 3",
        traps=["SELECT empid, deptno, name, salary FROM mv0", "SELECT empid, deptno, name, salary FROM mv0 UNION ALL SELECT empid, deptno, name, salary FROM emps2 WHERE empid < 5"],
        source=STARROCKS.format("testUnionRewrite1"),
    ),
    case(
        "single-table-range-subset",
        "union",
        "sr",
        "SELECT empid, deptno, name, salary FROM emps2 WHERE empid < 3",
        "SELECT deptno, empid FROM emps2 WHERE empid < 5",
        "rewrite",
        witness="SELECT deptno, empid FROM mv0 UNION ALL SELECT deptno, empid FROM emps2 WHERE empid >= 3 AND empid < 5",
        source=STARROCKS.format("testUnionRewrite1"),
    ),
    case(
        "join-range",
        "union",
        "sr",
        "SELECT emps2.empid, emps2.salary, depts2.deptno, depts2.name FROM emps2 JOIN depts2 USING (deptno) WHERE depts2.deptno < 100",
        "SELECT emps2.empid, emps2.salary, depts2.deptno, depts2.name FROM emps2 JOIN depts2 USING (deptno) WHERE depts2.deptno < 120",
        "rewrite",
        witness="SELECT empid, salary, deptno, name FROM mv0 UNION ALL SELECT emps2.empid, emps2.salary, depts2.deptno, depts2.name FROM emps2 JOIN depts2 USING (deptno) WHERE depts2.deptno >= 100 AND depts2.deptno < 120",
        source=STARROCKS.format("testUnionRewrite2"),
    ),
    case(
        "self-join-range",
        "union",
        "sr",
        "SELECT emps2.empid, emps2.salary, d1.deptno, d1.name AS name1, d2.name AS name2 FROM emps2 JOIN depts2 d1 ON emps2.deptno = d1.deptno JOIN depts2 d2 ON emps2.deptno = d2.deptno WHERE d1.deptno < 100",
        "SELECT emps2.empid, emps2.salary, d1.deptno, d1.name AS name1, d2.name AS name2 FROM emps2 JOIN depts2 d1 ON emps2.deptno = d1.deptno JOIN depts2 d2 ON emps2.deptno = d2.deptno WHERE d1.deptno < 120",
        "rewrite",
        witness=(
            "SELECT empid, salary, deptno, name1, name2 FROM mv0 UNION ALL SELECT emps2.empid, emps2.salary, d1.deptno, d1.name, d2.name "
            "FROM emps2 JOIN depts2 d1 ON emps2.deptno = d1.deptno JOIN depts2 d2 ON emps2.deptno = d2.deptno WHERE d1.deptno >= 100 AND d1.deptno < 120"
        ),
        source=STARROCKS.format("testUnionRewrite3"),
    ),
    case(
        "aggregate-range-on-group-key",
        "union",
        "sr",
        "SELECT t02.v1 AS v1, test_all_type2.t1d, SUM(test_all_type2.t1c) AS total_sum, COUNT(test_all_type2.t1c) AS total_num FROM t02 JOIN test_all_type2 ON t02.v1 = test_all_type2.t1d WHERE t02.v1 < 100 GROUP BY v1, test_all_type2.t1d",
        "SELECT t02.v1 AS v1, test_all_type2.t1d, SUM(test_all_type2.t1c) AS total_sum, COUNT(test_all_type2.t1c) AS total_num FROM t02 JOIN test_all_type2 ON t02.v1 = test_all_type2.t1d WHERE t02.v1 < 120 GROUP BY v1, test_all_type2.t1d",
        "rewrite",
        witness=(
            "SELECT v1, t1d, total_sum, total_num FROM mv0 UNION ALL SELECT t02.v1, test_all_type2.t1d, SUM(test_all_type2.t1c), COUNT(test_all_type2.t1c) "
            "FROM t02 JOIN test_all_type2 ON t02.v1 = test_all_type2.t1d WHERE t02.v1 >= 100 AND t02.v1 < 120 GROUP BY t02.v1, test_all_type2.t1d"
        ),
        source=STARROCKS.format("testUnionRewrite4"),
    ),
    case(
        "left-join-aggregate-range",
        "union",
        "sr",
        "SELECT t02.v1 AS v1, test_all_type2.t1d, SUM(test_all_type2.t1c) AS total_sum, COUNT(test_all_type2.t1c) AS total_num FROM t02 LEFT JOIN test_all_type2 ON t02.v1 = test_all_type2.t1d WHERE t02.v1 < 100 GROUP BY v1, test_all_type2.t1d",
        "SELECT t02.v1 AS v1, test_all_type2.t1d, SUM(test_all_type2.t1c) AS total_sum, COUNT(test_all_type2.t1c) AS total_num FROM t02 LEFT JOIN test_all_type2 ON t02.v1 = test_all_type2.t1d WHERE t02.v1 < 120 GROUP BY v1, test_all_type2.t1d",
        "rewrite",
        witness=(
            "SELECT v1, t1d, total_sum, total_num FROM mv0 UNION ALL SELECT t02.v1, test_all_type2.t1d, SUM(test_all_type2.t1c), COUNT(test_all_type2.t1c) "
            "FROM t02 LEFT JOIN test_all_type2 ON t02.v1 = test_all_type2.t1d WHERE t02.v1 >= 100 AND t02.v1 < 120 GROUP BY t02.v1, test_all_type2.t1d"
        ),
        source=STARROCKS.format("testUnionRewrite5"),
    ),
    case(
        "aggregate-range-off-group-key",
        "union",
        "sr",
        "SELECT deptno, name, SUM(salary) AS salary FROM emps2 WHERE empid < 5 GROUP BY deptno, name",
        "SELECT deptno, name, SUM(salary) AS salary FROM emps2 GROUP BY deptno, name",
        "rewrite",
        witness="SELECT deptno, name, SUM(salary) AS salary FROM (SELECT deptno, name, salary FROM mv0 UNION ALL SELECT deptno, name, SUM(salary) FROM emps2 WHERE empid >= 5 GROUP BY deptno, name) AS u GROUP BY deptno, name",
        traps=["SELECT deptno, name, salary FROM mv0 UNION ALL SELECT deptno, name, SUM(salary) FROM emps2 WHERE empid >= 5 GROUP BY deptno, name"],
        source=STARROCKS.format("testUnionRewrite7"),
    ),
    case("filter-point", "union", "sr", "SELECT k1, k2, v1, v2 FROM mt1 WHERE k1 < 3", "SELECT k1, k2, v1, v2 FROM mt1 WHERE k1 = 1", "rewrite", witness="SELECT k1, k2, v1, v2 FROM mv0 WHERE k1 = 1", source=STARROCKS.format("testUnionAllRewriteWithExtraPredicates")),
    case("filter-same", "union", "sr", "SELECT k1, k2, v1, v2 FROM mt1 WHERE k1 < 3", "SELECT k1, k2, v1, v2 FROM mt1 WHERE k1 < 3", "rewrite", witness="SELECT k1, k2, v1, v2 FROM mv0", source=STARROCKS.format("testUnionAllRewriteWithExtraPredicates")),
    case("filter-narrower", "union", "sr", "SELECT k1, k2, v1, v2 FROM mt1 WHERE k1 < 3", "SELECT k1, k2, v1, v2 FROM mt1 WHERE k1 < 2", "rewrite", witness="SELECT k1, k2, v1, v2 FROM mv0 WHERE k1 < 2", source=STARROCKS.format("testUnionAllRewriteWithExtraPredicates")),
    case(
        "range-wider",
        "union",
        "sr",
        "SELECT k1, k2, v1, v2 FROM mt1 WHERE k1 < 3",
        "SELECT k1, k2, v1, v2 FROM mt1 WHERE k1 < 6",
        "rewrite",
        witness="SELECT k1, k2, v1, v2 FROM mv0 UNION ALL SELECT k1, k2, v1, v2 FROM mt1 WHERE k1 < 6 AND k1 >= 3",
        source=STARROCKS.format("testUnionAllRewriteWithExtraPredicates"),
    ),
    case(
        "range-wider-extra-predicate",
        "union",
        "sr",
        "SELECT k1, k2, v1, v2 FROM mt1 WHERE k1 < 3",
        "SELECT k1, k2, v1, v2 FROM mt1 WHERE k1 < 6 AND k2 LIKE 'a%'",
        "rewrite",
        witness="SELECT k1, k2, v1, v2 FROM mv0 WHERE k2 LIKE 'a%' UNION ALL SELECT k1, k2, v1, v2 FROM mt1 WHERE k1 >= 3 AND k1 < 6 AND k2 LIKE 'a%'",
        source=STARROCKS.format("testUnionAllRewriteWithExtraPredicates"),
    ),
    case(
        "range-not-equal",
        "union",
        "sr",
        "SELECT k1, k2, v1, v2 FROM mt1 WHERE k1 < 3",
        "SELECT k1, k2, v1, v2 FROM mt1 WHERE k1 != 3 AND k2 LIKE 'a%'",
        "rewrite",
        witness="SELECT k1, k2, v1, v2 FROM mv0 WHERE k2 LIKE 'a%' UNION ALL SELECT k1, k2, v1, v2 FROM mt1 WHERE k1 != 3 AND k1 >= 3 AND k2 LIKE 'a%'",
        source=STARROCKS.format("testUnionAllRewriteWithExtraPredicates"),
    ),
    case(
        "range-lower-bound",
        "union",
        "sr",
        "SELECT k1, k2, v1, v2 FROM mt1 WHERE k1 < 3",
        "SELECT k1, k2, v1, v2 FROM mt1 WHERE k1 > 0 AND k2 LIKE 'a%'",
        "rewrite",
        witness="SELECT k1, k2, v1, v2 FROM mv0 WHERE k1 > 0 AND k2 LIKE 'a%' UNION ALL SELECT k1, k2, v1, v2 FROM mt1 WHERE k1 >= 3 AND k2 LIKE 'a%'",
        source=STARROCKS.format("testUnionAllRewriteWithExtraPredicates"),
    ),
    case(
        "nullable-complement",
        "union",
        "sr",
        "SELECT v1, v2, v3 FROM t02 WHERE v2 < 10",
        "SELECT v1, v2, v3 FROM t02 WHERE v1 < 5",
        "rewrite",
        witness="SELECT v1, v2, v3 FROM mv0 WHERE v1 < 5 UNION ALL SELECT v1, v2, v3 FROM t02 WHERE v1 < 5 AND (v2 < 10) IS NOT TRUE",
        traps=["SELECT v1, v2, v3 FROM mv0 WHERE v1 < 5 UNION ALL SELECT v1, v2, v3 FROM t02 WHERE v1 < 5 AND NOT (v2 < 10)"],
    ),
    case(
        "missing-column",
        "union",
        "sr",
        "SELECT empid, deptno FROM emps2 WHERE empid < 3",
        "SELECT empid, name FROM emps2 WHERE empid < 5",
        "none",
        traps=["SELECT m.empid, e.name FROM mv0 m JOIN emps2 e ON e.empid = m.empid UNION ALL SELECT empid, name FROM emps2 WHERE empid >= 3 AND empid < 5"],
        why="the view has no name; reading it back from emps2 by empid repeats rows that share an empid",
    ),
    case(
        "overlapping-ranges",
        "union",
        "sr",
        "SELECT empid, deptno, name, salary FROM emps2 WHERE empid < 3",
        "SELECT empid, deptno FROM emps2 WHERE empid > 1 AND empid < 5",
        "rewrite",
        witness="SELECT empid, deptno FROM mv0 WHERE empid > 1 UNION ALL SELECT empid, deptno FROM emps2 WHERE empid >= 3 AND empid < 5",
        traps=["SELECT empid, deptno FROM mv0 WHERE empid > 1 UNION ALL SELECT empid, deptno FROM emps2 WHERE empid > 1 AND empid < 5"],
    ),
    case(
        "aggregate-range-regrouped",
        "union",
        "sr",
        "SELECT deptno, SUM(salary) AS s FROM emps2 WHERE empid < 3 GROUP BY deptno",
        "SELECT deptno, SUM(salary) AS s FROM emps2 WHERE empid < 5 GROUP BY deptno",
        "rewrite",
        witness="SELECT deptno, SUM(s) AS s FROM (SELECT deptno, s FROM mv0 UNION ALL SELECT deptno, SUM(salary) FROM emps2 WHERE empid >= 3 AND empid < 5 GROUP BY deptno) AS u GROUP BY deptno",
        traps=["SELECT deptno, s FROM mv0 UNION ALL SELECT deptno, SUM(salary) FROM emps2 WHERE empid >= 3 AND empid < 5 GROUP BY deptno"],
    ),
    case(
        "average-without-count",
        "union",
        "sr",
        "SELECT deptno, AVG(salary) AS a FROM emps2 WHERE empid < 3 GROUP BY deptno",
        "SELECT deptno, AVG(salary) AS a FROM emps2 WHERE empid < 5 GROUP BY deptno",
        "none",
        traps=["SELECT deptno, AVG(a) AS a FROM (SELECT deptno, a FROM mv0 UNION ALL SELECT deptno, AVG(salary) FROM emps2 WHERE empid >= 3 AND empid < 5 GROUP BY deptno) AS u GROUP BY deptno"],
        why="an average without its count cannot be combined with another",
    ),
    # ---- views that are set operations
    case("union-all-branch", "setop", "hr_plain", "SELECT empid, deptno FROM emps WHERE deptno = 10 UNION ALL SELECT empid, deptno FROM emps WHERE deptno = 20", "SELECT empid FROM emps WHERE deptno = 10", "rewrite", witness="SELECT empid FROM mv0 WHERE deptno = 10", traps=["SELECT empid FROM mv0"]),
    case(
        "intersect-all-filter",
        "setop",
        "hr_plain",
        "SELECT deptno FROM emps INTERSECT ALL SELECT deptno FROM depts",
        "SELECT deptno FROM emps WHERE deptno > 10 INTERSECT ALL SELECT deptno FROM depts WHERE deptno > 10",
        "rewrite",
        witness="SELECT deptno FROM mv0 WHERE deptno > 10",
    ),
    case(
        "except-all-filter",
        "setop",
        "hr_plain",
        "SELECT name FROM emps EXCEPT ALL SELECT name FROM dependents",
        "SELECT name FROM emps WHERE name = 'Bill' EXCEPT ALL SELECT name FROM dependents",
        "rewrite",
        witness="SELECT name FROM mv0 WHERE name = 'Bill'",
    ),
    case(
        "except-all-left-operand",
        "setop",
        "hr_plain",
        "SELECT name FROM emps EXCEPT ALL SELECT name FROM dependents",
        "SELECT name FROM emps",
        "none",
        traps=["SELECT name FROM mv0"],
        why="names that dependents also have are removed from the view",
    ),
    case("intersect-all-commuted", "setop", "hr_plain", "SELECT deptno, name FROM emps INTERSECT ALL SELECT deptno, name FROM depts", "SELECT name, deptno FROM depts INTERSECT ALL SELECT name, deptno FROM emps", "rewrite", witness="SELECT name, deptno FROM mv0"),
    case(
        "union-distinct-to-all",
        "setop",
        "hr_plain",
        "SELECT deptno FROM emps UNION SELECT deptno FROM depts",
        "SELECT deptno FROM emps UNION ALL SELECT deptno FROM depts",
        "none",
        traps=["SELECT deptno FROM mv0"],
        why="the view has lost the duplicates",
    ),
    case("union-all-to-distinct", "setop", "hr_plain", "SELECT deptno FROM emps UNION ALL SELECT deptno FROM depts", "SELECT deptno FROM emps UNION SELECT deptno FROM depts", "rewrite", witness="SELECT DISTINCT deptno FROM mv0"),
]


def main() -> None:
    ids = [c["id"] for c in CASES]
    assert len(ids) == len(set(ids)), "duplicate case ids"
    payload = {"description": __doc__.strip().splitlines()[0], "schemas": SCHEMAS, "cases": CASES}
    OUT.write_text(json.dumps(payload, indent=1) + "\n", encoding="utf-8")
    print(f"{len(CASES)} cases written to {OUT}")


if __name__ == "__main__":
    main()
