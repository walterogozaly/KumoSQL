"""The counterexample search reloads only tables whose rows changed and skips databases already tried."""

import pytest

pytest.importorskip("duckdb")

from kumosql import counterexample as cx  # noqa: E402


def spec():
    return cx.Spec({
        "EMP": cx.Table("EMP", [cx.Column("EMPNO", "INT", not_null=True), cx.Column("DEPTNO", "INT"), cx.Column("NAME", "VARCHAR")], primary_key=("EMPNO",)),
        "DEPT": cx.Table("DEPT", [cx.Column("DEPTNO", "INT", not_null=True), cx.Column("NAME", "VARCHAR")], primary_key=("DEPTNO",)),
    })


JOINED = "SELECT e.EMPNO, d.NAME FROM EMP AS e JOIN DEPT AS d ON e.DEPTNO = d.DEPTNO"


def contents(searcher, name):
    return searcher.db.execute(f'SELECT * FROM "{name}"').fetchall()


def test_tables_hold_the_last_rows_loaded():
    searcher = cx.Searcher(spec(), JOINED, JOINED)
    loads = [
        {"EMP": [(1, 10, "a"), (2, 20, None)], "DEPT": [(10, "x")]},
        {"EMP": [(1, 10, "a"), (2, 20, None)], "DEPT": [(10, "x"), (20, "y")]},  # EMP unchanged
        {"EMP": [], "DEPT": [(10, "x"), (20, "y")]},  # EMP emptied, DEPT unchanged
        {"EMP": [], "DEPT": []},
        {"EMP": [(3, None, "it's")], "DEPT": []},  # EMP filled from empty
        {"EMP": [(2, 20, None), (1, 10, "a")], "DEPT": [(10, "x")]},  # same rows in another order
    ]
    for data in loads:
        searcher._load(data)
        for name, rows in data.items():
            assert contents(searcher, name) == rows


def test_a_failed_load_leaves_no_stale_table_behind():
    searcher = cx.Searcher(spec(), JOINED, JOINED)
    good = {"EMP": [(1, 10, "a")], "DEPT": [(10, "x")]}
    searcher._load(good)
    with pytest.raises(cx.duckdb.Error):
        searcher._load({"EMP": [(1, 10, "a"), (1, 20, "b")], "DEPT": [("not a number", "y")]})
    searcher._load(good)
    for name, rows in good.items():
        assert contents(searcher, name) == rows


def test_the_targeted_search_does_not_confuse_the_reload_bookkeeping():
    searcher = cx.Searcher(spec(), JOINED, JOINED)
    data = {"EMP": [(1, 10, "a")], "DEPT": [(10, "x")]}
    searcher._load(data)
    searcher._search_targeted(__import__("random").Random(0))  # writes the tables itself
    searcher._load(data)
    for name, rows in data.items():
        assert contents(searcher, name) == rows


def test_skipping_databases_already_tried_finds_the_same_counterexample():
    left = "SELECT DEPTNO FROM EMP"
    right = "SELECT DISTINCT DEPTNO FROM EMP"  # differs only once two rows share a department
    for seed in range(3):
        fresh = cx.Searcher(spec(), left, right).search(150, seed=seed, targeted=False)
        warmed = cx.Searcher(spec(), left, right)
        warmed.search(150, seed=seed + 100, targeted=False)
        assert warmed._agreed, "the earlier search left databases on which the queries agree"
        again = warmed.search(150, seed=seed, targeted=False)
        assert fresh is not None and again == fresh


def test_an_equivalent_pair_skips_databases_it_already_ran():
    searcher = cx.Searcher(spec(), JOINED, "SELECT e.EMPNO, d.NAME FROM DEPT AS d JOIN EMP AS e ON d.DEPTNO = e.DEPTNO")
    assert searcher.search(100, seed=1) is None
    tried = len(searcher._agreed)
    executed = []
    original = searcher.db
    searcher.db = type("Counting", (), {"execute": lambda self, sql, *a: executed.append(sql) or original.execute(sql, *a), "executemany": lambda self, *a: original.executemany(*a)})()
    assert searcher.search(100, seed=1) is None
    assert tried and not [sql for sql in executed if sql in (searcher.left_sql, searcher.right_sql)]
