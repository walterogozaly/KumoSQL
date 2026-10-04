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


# --- arbitrary picks: a shuffle cannot move a pick that follows hash order, so the pair stays unknown ---

PICKS = cx.Spec({"T": cx.Table("T", [cx.Column("ID", "INT", not_null=True), cx.Column("K", "INT")], primary_key=("ID",))})
GROUPED = "(SELECT k FROM T GROUP BY k) AS g"


@pytest.mark.parametrize(
    "left,right,why",
    [
        # both queries may return any group's k; DuckDB returns the first group's from one and the largest from the other
        (f"SELECT ANY_VALUE(k) FROM {GROUPED}", f"SELECT ANY_VALUE(k) FROM {GROUPED.replace('GROUP BY k', 'GROUP BY k ORDER BY k DESC')}", "ANY_VALUE over groups"),
        (f"SELECT first(k) FROM {GROUPED}", f"SELECT last(k) FROM {GROUPED}", "first against last"),
        (f"SELECT ANY_VALUE(k) FILTER (WHERE k > 0) FROM {GROUPED}", f"SELECT ANY_VALUE(k) FILTER (WHERE k > 0) FROM {GROUPED.replace('GROUP BY k', 'GROUP BY k ORDER BY k DESC')}", "a FILTER"),
        # LIMIT without ORDER BY keeps any one row, a tie in ORDER BY any one of the tied rows
        (f"SELECT k FROM {GROUPED} LIMIT 1", f"SELECT k FROM {GROUPED} ORDER BY k DESC LIMIT 1", "LIMIT with no ORDER BY"),
        (f"SELECT k FROM {GROUPED} ORDER BY k % 2 LIMIT 1", f"SELECT k FROM {GROUPED} ORDER BY k % 2, k DESC LIMIT 1", "a tie at the LIMIT"),
        ("SELECT ANY_VALUE(k) OVER (PARTITION BY id) FROM T", "SELECT k + 1 FROM T", "ANY_VALUE as a window function (refused, so no verdict)"),
    ],
    ids=lambda value: value if len(value) < 40 else "",
)
def test_a_difference_resting_on_an_arbitrary_pick_is_not_a_counterexample(left, right, why):
    for seed in range(3):
        assert cx.find_counterexample(PICKS, left, right, trials=100, seed=seed) is None, why


@pytest.mark.parametrize(
    "left,right",
    [
        ("SELECT k FROM T", "SELECT k + 1 FROM T"),
        # the pick is free of choice only when the group holds several values: here every group holds one
        ("SELECT k, ANY_VALUE(k) FROM T GROUP BY k", "SELECT k + 1, ANY_VALUE(k) FROM T GROUP BY k"),
        ("SELECT k FROM T ORDER BY k, id LIMIT 1", "SELECT k + 1 FROM T ORDER BY k, id LIMIT 1"),
    ],
)
def test_a_difference_that_needs_no_arbitrary_pick_is_still_found(left, right):
    assert any(cx.find_counterexample(PICKS, left, right, trials=100, seed=seed) for seed in range(3))


def test_guarded_picks_return_the_value_when_there_is_one_and_fail_when_free():
    sql = cx.guard_arbitrary_picks("SELECT k, ANY_VALUE(v), first(w) FILTER (WHERE v > 1) FROM t GROUP BY k")
    db = cx.duckdb.connect()
    db.execute("CREATE TABLE t (k INT, v INT, w INT)")
    db.execute("INSERT INTO t VALUES (1, 5, 7), (1, 5, 8), (2, NULL, 1), (2, NULL, 1)")
    with pytest.raises(cx.duckdb.Error, match="arbitrary pick"):
        db.execute(sql).fetchall()  # w is 7 or 8 in group 1
    db.execute("UPDATE t SET w = 7")
    assert sorted(db.execute(sql).fetchall(), key=str) == [(1, 5, 7), (2, None, None)]  # all NULLs pick NULL
    db.execute("UPDATE t SET v = 6 WHERE k = 1 AND w = 7 AND v = 5 AND rowid = (SELECT min(rowid) FROM t WHERE k = 1)")
    with pytest.raises(cx.duckdb.Error, match="arbitrary pick"):
        db.execute(sql).fetchall()  # v is 5 or 6 in group 1
    assert cx.guard_arbitrary_picks("SELECT 1 FROM t") == "SELECT 1 FROM t"
    assert cx.guard_arbitrary_picks("SELECT first(x ORDER BY y) FROM t") is None
    assert cx.guard_arbitrary_picks("SELECT any_value(x) OVER () FROM t") is None


def test_tie_breaking_variants_flip_the_tie_at_the_limit():
    sql = "SELECT k FROM T ORDER BY id % 2 LIMIT 1"
    asc, desc = cx.tie_breaking_variants(sql, 1)
    db = cx.duckdb.connect()
    db.execute("CREATE TABLE T (id INT, k INT)")
    db.execute("INSERT INTO T VALUES (2, 20), (4, 40)")  # tied on id % 2
    assert db.execute(asc).fetchall() == [(20,)] and db.execute(desc).fetchall() == [(40,)]
    assert cx.tie_breaking_variants("SELECT k FROM T ORDER BY k", 1) == []
