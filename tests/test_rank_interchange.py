"""``ROW_NUMBER``/``RANK``/``DENSE_RANK`` interchange (``kumosql.rank_interchange``) on ``events(id, user_id, ts, value)``.

Every case that must rewrite is run on DuckDB (``bigquery_on_duckdb``, one thread, optimizer off) before and after
on random databases that respect the declared keys and NOT NULL columns and carry ties and NULLs. Every case that
must not rewrite names the failed precondition and carries a database on which the rewrite it would have made
changes the result, so the refusal is not just caution.
"""

import functools
import random

import pytest

pytest.importorskip("z3")
duckdb = pytest.importorskip("duckdb")

import sqlglot  # noqa: E402

from kumosql import bigquery_on_duckdb as bd  # noqa: E402
from kumosql.algebraic_equivalence import prove_equivalent_algebraic  # noqa: E402
from kumosql.duckdb_load import insert_rows, run_unoptimized  # noqa: E402
from kumosql.rank_interchange import rank_interchange  # noqa: E402
from kumosql.smt_equivalence import TableConstraints  # noqa: E402

SCHEMA = {"events": ["id", "user_id", "ts", "value"]}
TYPES = {"events": {"id": "INT64", "user_id": "INT64", "ts": "INT64", "value": "INT64"}}

ID_KEY = ([("id",)], {"id"})
USER_TS = ([("user_id", "ts")], {"id", "user_id", "ts"})
NO_FACTS = ([], set())

W = "PARTITION BY user_id ORDER BY ts"


def random_rows(rng, scenario):
    keys, not_null = scenario
    rows, seen = [], set()
    if rng.random() < 0.08:
        return rows  # an empty table: a global aggregate still returns a row, a window query none
    for ident in range(1, rng.randint(1, 8) + 1):
        for _ in range(20):
            row = [ident, rng.choice([1, 2, 3, None]), rng.choice([1, 2, 3, None]), rng.choice([5, 6, None])]
            for position, name in enumerate(SCHEMA["events"]):
                if name in not_null and row[position] is None:
                    row[position] = rng.randint(1, 3)
            marks = [tuple(row[SCHEMA["events"].index(c)] for c in key) for key in keys]
            if not any((i, m) in seen for i, m in enumerate(marks)):
                seen.update((i, m) for i, m in enumerate(marks))
                rows.append(tuple(row))
                break
    return rows


@functools.lru_cache(maxsize=None)
def duck(sql):
    return bd.faithful(sqlglot.parse_one(sql, read="bigquery")).sql(dialect="duckdb")


def run(sql, rows, reverse=False):
    db = duckdb.connect(config={"threads": 1})
    bd.configure(db)
    db.execute("CREATE TABLE events (id BIGINT, user_id BIGINT, ts BIGINT, value BIGINT)")
    insert_rows(db, "events", list(reversed(rows)) if reverse else rows)
    return sorted(run_unoptimized(db, duck(sql))[0], key=repr)


def rewritten(sql, scenario):
    keys, not_null = scenario
    tree = sqlglot.parse_one(sql, read="bigquery")
    out = rank_interchange(tree.copy(), {"events": keys}, {"events": frozenset(not_null)}, SCHEMA, TYPES, "bigquery")
    text = out.sql(dialect="bigquery")
    return text if text != tree.sql(dialect="bigquery") else None


# (name, sql, scenario, text the rewrite must contain, text it must no longer contain)
MUST_REWRITE = [
    ("dense_rank_one_is_rank_one", f"SELECT user_id, value FROM events QUALIFY DENSE_RANK() OVER ({W}) = 1", NO_FACTS, "RANK() OVER", "DENSE_RANK"),
    ("dense_rank_at_most_one", f"SELECT user_id, value FROM events QUALIFY DENSE_RANK() OVER ({W}) <= 1", NO_FACTS, "RANK() OVER (PARTITION BY user_id ORDER BY ts) = 1", "<="),
    ("dense_rank_below_two", f"SELECT user_id, value FROM events QUALIFY DENSE_RANK() OVER ({W} DESC) < 2", NO_FACTS, ") = 1", "< 2"),
    ("dense_rank_one_on_the_left", f"SELECT user_id, value FROM events QUALIFY 1 >= DENSE_RANK() OVER ({W})", NO_FACTS, ") = 1", ">="),
    ("rank_at_most_one", f"SELECT user_id, value FROM events QUALIFY RANK() OVER ({W}) <= 1", NO_FACTS, ") = 1", "<="),
    ("dense_rank_one_through_an_alias", f"SELECT user_id, DENSE_RANK() OVER ({W}) AS r, value FROM events QUALIFY r = 1", NO_FACTS, "RANK() OVER", "DENSE_RANK"),
    ("dense_rank_one_among_other_conditions", f"SELECT user_id, value FROM events WHERE value > 5 QUALIFY DENSE_RANK() OVER ({W}) = 1 AND value < 7", NO_FACTS, "RANK() OVER", "DENSE_RANK"),
    (
        "derived_column_dense_rank_one",
        f"SELECT user_id, value FROM (SELECT user_id, value, DENSE_RANK() OVER ({W}) AS rn FROM events) AS d WHERE rn = 1",
        NO_FACTS, "RANK() OVER", "DENSE_RANK",
    ),
    (
        "derived_column_read_in_the_select_list_too",
        f"SELECT d.user_id, d.rn FROM (SELECT user_id, DENSE_RANK() OVER ({W} DESC) AS rn FROM events) AS d WHERE d.rn <= 1 AND d.user_id > 1",
        NO_FACTS, "RANK() OVER", "DENSE_RANK",
    ),
    (
        "derived_column_dense_rank_one_joined_elsewhere",
        f"SELECT d.user_id, e.value FROM (SELECT user_id, DENSE_RANK() OVER ({W}) AS rn FROM events) AS d JOIN events AS e ON e.user_id = d.user_id WHERE d.rn = 1",
        NO_FACTS, "RANK() OVER", "DENSE_RANK",
    ),
    (
        "derived_column_read_in_a_join_condition_too",
        f"SELECT d.user_id, e.value FROM (SELECT user_id, DENSE_RANK() OVER ({W}) AS rn FROM events) AS d LEFT JOIN events AS e ON e.id = d.rn WHERE d.rn = 1",
        NO_FACTS, "RANK() OVER", "DENSE_RANK",
    ),
    (
        "derived_column_on_the_nullable_side_of_a_join",
        f"SELECT e.id, d.user_id FROM events AS e LEFT JOIN (SELECT user_id, DENSE_RANK() OVER ({W}) AS rn FROM events) AS d ON d.rn = e.id WHERE d.rn = 1",
        NO_FACTS, "RANK() OVER", "DENSE_RANK",
    ),
    ("total_order_rank_is_row_number", f"SELECT user_id, RANK() OVER ({W}) AS r FROM events", USER_TS, "ROW_NUMBER()", "RANK() OVER"),
    ("total_order_dense_rank_is_row_number", f"SELECT user_id, DENSE_RANK() OVER ({W} DESC) AS r FROM events", USER_TS, "ROW_NUMBER()", "DENSE_RANK"),
    (
        "total_order_filter_beyond_the_first_row",
        f"SELECT user_id, ts FROM events QUALIFY RANK() OVER ({W}) <= 2",
        USER_TS, "ROW_NUMBER()", "RANK() OVER",
    ),
    (
        "total_order_through_the_key_in_the_order",
        "SELECT user_id FROM events QUALIFY DENSE_RANK() OVER (PARTITION BY user_id ORDER BY ts DESC, id) = 2",
        ID_KEY, "ROW_NUMBER()", "DENSE_RANK",
    ),
    (
        "total_order_through_the_partition",
        "SELECT id, RANK() OVER (PARTITION BY id ORDER BY ts) AS r FROM events",
        ID_KEY, "ROW_NUMBER()", "RANK() OVER",
    ),
    (
        "total_order_over_a_filtered_table",
        f"SELECT user_id, RANK() OVER ({W}) AS r FROM events WHERE value IS NOT NULL",
        USER_TS, "ROW_NUMBER()", "RANK() OVER",
    ),
]


@pytest.mark.parametrize("name,sql,scenario,has,lacks", MUST_REWRITE, ids=[c[0] for c in MUST_REWRITE])
def test_rewrites(name, sql, scenario, has, lacks):
    text = rewritten(sql, scenario)
    assert text is not None and has in text
    if lacks == "RANK() OVER":  # RANK stays in the text for the "DENSE_RANK to RANK" cases, not for these
        assert "RANK() OVER" not in text.replace("DENSE_RANK() OVER", "")
    else:
        assert lacks not in text


@pytest.mark.parametrize("name,sql,scenario,has,lacks", MUST_REWRITE, ids=[c[0] for c in MUST_REWRITE])
def test_rewrite_agrees_on_duckdb(name, sql, scenario, has, lacks):
    text = rewritten(sql, scenario)
    assert text is not None
    rng = random.Random(f"rank-interchange-{name}")
    for trial in range(30):
        rows = random_rows(rng, scenario)
        before = run(sql, rows)
        assert before == run(sql, rows, reverse=True), rows  # no case reads a tied row's position
        assert before == run(text, rows), rows


# (name, failed precondition, sql, scenario, what the refused rewrite would give, rows where they differ)
MUST_NOT_REWRITE = [
    (
        "row_number_one_without_a_total_order",
        "ROW_NUMBER keeps one of the tied rows, RANK keeps them all",
        f"SELECT user_id, value FROM events QUALIFY ROW_NUMBER() OVER ({W}) = 1",
        ID_KEY,
        f"SELECT user_id, value FROM events QUALIFY RANK() OVER ({W}) = 1",
        [(1, 1, 3, 5), (2, 1, 3, 6)],
    ),
    (
        "rank_at_most_two_is_not_dense_rank_at_most_two",
        "beyond the first row RANK and DENSE_RANK differ",
        f"SELECT user_id, value FROM events QUALIFY RANK() OVER ({W}) <= 2",
        ID_KEY,
        f"SELECT user_id, value FROM events QUALIFY DENSE_RANK() OVER ({W}) <= 2",
        [(1, 1, 1, 5), (2, 1, 1, 6), (3, 1, 2, 7)],
    ),
    (
        "dense_rank_two_stays",
        "only the first row is interchangeable",
        f"SELECT user_id, value FROM events QUALIFY DENSE_RANK() OVER ({W}) = 2",
        ID_KEY,
        f"SELECT user_id, value FROM events QUALIFY RANK() OVER ({W}) = 2",
        [(1, 1, 1, 5), (2, 1, 1, 6), (3, 1, 2, 7)],
    ),
    (
        "rank_in_the_select_list_without_a_total_order",
        "the value of RANK on a tied row is not ROW_NUMBER",
        f"SELECT user_id, RANK() OVER ({W}) AS r FROM events",
        ID_KEY,
        f"SELECT user_id, ROW_NUMBER() OVER ({W}) AS r FROM events",
        [(1, 1, 3, 5), (2, 1, 3, 5)],
    ),
    (
        "dense_rank_in_the_select_list_stays",
        "its value beyond the first row is not RANK",
        f"SELECT user_id, DENSE_RANK() OVER ({W}) AS r FROM events",
        ID_KEY,
        f"SELECT user_id, RANK() OVER ({W}) AS r FROM events",
        [(1, 1, 1, 5), (2, 1, 1, 6), (3, 1, 2, 7)],
    ),
    (
        "key_not_covered_by_the_window",
        "id is a key but the window's PARTITION BY and ORDER BY do not contain it",
        f"SELECT id, RANK() OVER ({W}) AS r FROM events",
        ID_KEY,
        f"SELECT id, ROW_NUMBER() OVER ({W}) AS r FROM events",
        [(1, 1, 3, 5), (2, 1, 3, 5)],
    ),
    (
        "window_over_a_join_that_repeats_rows",
        "a join can repeat rows, so a key of the table is not a key of the window's input",
        f"SELECT a.user_id, RANK() OVER (PARTITION BY a.user_id ORDER BY a.ts) AS r FROM events AS a JOIN events AS b ON a.user_id = b.user_id",
        USER_TS,
        f"SELECT a.user_id, ROW_NUMBER() OVER (PARTITION BY a.user_id ORDER BY a.ts) AS r FROM events AS a JOIN events AS b ON a.user_id = b.user_id",
        [(1, 1, 1, 5), (2, 1, 2, 6)],
    ),
]


@pytest.mark.parametrize("name,why,sql,scenario,refused,rows", MUST_NOT_REWRITE, ids=[c[0] for c in MUST_NOT_REWRITE])
def test_declines_when_a_precondition_fails(name, why, sql, scenario, refused, rows):
    assert rewritten(sql, scenario) is None, why


@pytest.mark.parametrize("name,why,sql,scenario,refused,rows", MUST_NOT_REWRITE, ids=[c[0] for c in MUST_NOT_REWRITE])
def test_the_refused_rewrite_would_have_changed_the_result(name, why, sql, scenario, refused, rows):
    assert any(run(sql, rows, reverse=reverse) != run(refused, rows, reverse=reverse) for reverse in (False, True)), why


def test_a_window_with_a_frame_or_over_a_group_is_left_alone():
    assert rewritten("SELECT user_id, RANK() OVER (PARTITION BY user_id ORDER BY ts) FROM events GROUP BY user_id, ts", USER_TS) is None
    assert rewritten(f"SELECT user_id FROM events QUALIFY DENSE_RANK() OVER ({W} ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) = 1", NO_FACTS) is None


# --- the prover -------------------------------------------------------------------------------


def proven(left, right, scenario):
    keys, not_null = scenario
    constraints = {"events": TableConstraints(not_null=frozenset(not_null), keys=tuple(keys))}
    return prove_equivalent_algebraic(left, right, schema=SCHEMA, types=TYPES, constraints=constraints, dialect="bigquery").proven


def q(fn, cond, order="ts"):
    return f"SELECT user_id, value FROM events QUALIFY {fn}() OVER (PARTITION BY user_id ORDER BY {order}) {cond}"


PROOFS = [
    ("rank_one_is_dense_rank_one_without_any_key", q("RANK", "= 1"), q("DENSE_RANK", "= 1"), NO_FACTS),
    ("dense_rank_at_most_one_is_rank_one", q("DENSE_RANK", "<= 1"), q("RANK", "= 1"), NO_FACTS),
    ("rank_below_two_is_dense_rank_one_nullable_desc", q("RANK", "< 2", "ts DESC"), q("DENSE_RANK", "= 1", "ts DESC"), ID_KEY),
    ("row_number_is_rank_when_the_order_is_total", q("ROW_NUMBER", "= 1"), q("RANK", "= 1"), USER_TS),
    ("row_number_is_dense_rank_when_the_order_is_total", q("ROW_NUMBER", "= 1"), q("DENSE_RANK", "<= 1"), USER_TS),
    ("rank_is_dense_rank_beyond_the_first_row_when_total", q("RANK", "<= 2"), q("DENSE_RANK", "<= 2"), USER_TS),
]


@pytest.mark.parametrize("name,left,right,scenario", PROOFS, ids=[c[0] for c in PROOFS])
def test_proves(name, left, right, scenario):
    assert proven(left, right, scenario)
    assert proven(right, left, scenario)


@pytest.mark.parametrize("name,left,right,scenario", PROOFS, ids=[c[0] for c in PROOFS])
def test_proved_pairs_agree_on_duckdb(name, left, right, scenario):
    rng = random.Random(f"proof-{name}")
    for trial in range(25):
        rows = random_rows(rng, scenario)
        assert run(left, rows) == run(right, rows), rows


NOT_PROVED = [
    ("row_number_is_not_rank_without_a_total_order", q("ROW_NUMBER", "= 1"), q("RANK", "= 1"), ID_KEY, [(1, 1, 3, 5), (2, 1, 3, 6)]),
    ("row_number_is_not_dense_rank_without_any_key", q("ROW_NUMBER", "= 1"), q("DENSE_RANK", "= 1"), NO_FACTS, [(1, 1, 3, 5), (2, 1, 3, 6)]),
    ("rank_is_not_dense_rank_beyond_the_first_row", q("RANK", "<= 2"), q("DENSE_RANK", "<= 2"), ID_KEY, [(1, 1, 1, 5), (2, 1, 1, 6), (3, 1, 2, 7)]),
    ("rank_two_is_not_dense_rank_two", q("RANK", "= 2"), q("DENSE_RANK", "= 2"), ID_KEY, [(1, 1, 1, 5), (2, 1, 1, 6), (3, 1, 2, 7)]),
]


@pytest.mark.parametrize("name,left,right,scenario,rows", NOT_PROVED, ids=[c[0] for c in NOT_PROVED])
def test_not_proved_where_the_functions_differ(name, left, right, scenario, rows):
    assert not proven(left, right, scenario)
    assert not proven(right, left, scenario)
    assert any(run(left, rows, reverse=reverse) != run(right, rows) for reverse in (False, True))
