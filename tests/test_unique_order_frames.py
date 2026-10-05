"""ROWS frames over a unique order read as the RANGE frames they equal (``kumosql.unique_order_frames``).

Every must-prove pair is also run on DuckDB (optimizer off, one thread) over databases that keep ``id`` unique and
put ties and NULLs in every other column, and every must-not-prove pair carries a database on which the two
queries return different bags.
"""

import pytest

pytest.importorskip("z3")
duckdb = pytest.importorskip("duckdb")

import sqlglot  # noqa: E402

from kumosql import bigquery_on_duckdb  # noqa: E402
from kumosql.algebraic_equivalence import normalize, prove_equivalent_algebraic  # noqa: E402
from kumosql.duckdb_load import insert_rows, run_unoptimized  # noqa: E402
from kumosql.smt_equivalence import TableConstraints  # noqa: E402

SCHEMA = {"events": ["id", "user_id", "ts", "value"]}
TYPES = {"events": {c: "INT64" for c in SCHEMA["events"]}}
KEYED = {"events": TableConstraints(not_null=frozenset({"id"}), keys=(("id",),))}
KEY_NOT_NULL_ONLY = {"events": TableConstraints(not_null=frozenset({"id"}))}
KEY_NULLABLE = {"events": TableConstraints(keys=(("id",),))}
COMPOSITE = {"events": TableConstraints(not_null=frozenset({"user_id", "ts"}), keys=(("user_id", "ts"),))}
SEL = "SELECT id, user_id"


def over(function, frame, order="ORDER BY id", partition="PARTITION BY user_id"):
    return f"{SEL}, {function} OVER ({partition} {order} {frame}) AS w FROM events"


ROWS_RUNNING = "ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW"

# (name, left, right, constraints)
MUST_PROVE = [
    ("running_sum", over("SUM(value)", ROWS_RUNNING), over("SUM(value)", ""), KEYED),
    ("running_sum_short_frame", over("SUM(value)", "ROWS UNBOUNDED PRECEDING"), over("SUM(value)", ""), KEYED),
    ("running_sum_against_explicit_range", over("SUM(value)", ROWS_RUNNING),
     over("SUM(value)", "RANGE BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW"), KEYED),
    ("running_count_star_desc", over("COUNT(*)", ROWS_RUNNING, "ORDER BY id DESC"), over("COUNT(*)", "", "ORDER BY id DESC"), KEYED),
    ("running_max_extra_order_key", over("MAX(value)", ROWS_RUNNING, "ORDER BY ts, id"), over("MAX(value)", "", "ORDER BY ts, id"), KEYED),
    ("running_min_no_partition", over("MIN(value)", ROWS_RUNNING, partition=""), over("MIN(value)", "", partition=""), KEYED),
    ("running_avg", over("AVG(value)", ROWS_RUNNING), over("AVG(value)", ""), KEYED),
    ("running_countif", over("COUNTIF(value > 2)", ROWS_RUNNING), over("COUNTIF(value > 2)", ""), KEYED),
    ("running_logical_or", over("LOGICAL_OR(value > 2)", ROWS_RUNNING), over("LOGICAL_OR(value > 2)", ""), KEYED),
    ("running_bit_or", over("BIT_OR(value)", ROWS_RUNNING), over("BIT_OR(value)", ""), KEYED),
    ("rows_to_partition_end", over("SUM(value)", "ROWS BETWEEN CURRENT ROW AND UNBOUNDED FOLLOWING"),
     over("SUM(value)", "RANGE BETWEEN CURRENT ROW AND UNBOUNDED FOLLOWING"), KEYED),
    ("only_the_current_row", over("SUM(value)", "ROWS BETWEEN CURRENT ROW AND CURRENT ROW"),
     over("SUM(value)", "RANGE BETWEEN CURRENT ROW AND CURRENT ROW"), KEYED),
    ("rows_whole_partition_needs_no_key", over("SUM(value)", "ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING", ""),
     over("SUM(value)", "", ""), None),
    ("last_value_running", over("LAST_VALUE(value)", ROWS_RUNNING), over("LAST_VALUE(value)", ""), KEYED),
    ("first_value_running", over("FIRST_VALUE(value)", ROWS_RUNNING), over("FIRST_VALUE(value)", ""), KEYED),
    ("composite_key", over("SUM(value)", ROWS_RUNNING, "ORDER BY ts, user_id"), over("SUM(value)", "", "ORDER BY ts, user_id"), COMPOSITE),
    ("composite_key_with_the_partition_key", over("SUM(value)", ROWS_RUNNING, "ORDER BY ts"), over("SUM(value)", "", "ORDER BY ts"), COMPOSITE),
    ("table_alias", "SELECT e.id, SUM(e.value) OVER (PARTITION BY e.user_id ORDER BY e.id ROWS UNBOUNDED PRECEDING) AS w FROM events AS e",
     "SELECT e.id, SUM(e.value) OVER (PARTITION BY e.user_id ORDER BY e.id) AS w FROM events AS e", KEYED),
    ("inside_a_derived_table", f"SELECT d.id, d.w FROM ({over('SUM(value)', ROWS_RUNNING)}) AS d".replace("SELECT id, user_id", "SELECT id, user_id", 1),
     f"SELECT d.id, d.w FROM ({over('SUM(value)', '')}) AS d", KEYED),
    ("filtered_input", over("SUM(value)", ROWS_RUNNING).replace("FROM events", "FROM events WHERE value > 1"),
     over("SUM(value)", "").replace("FROM events", "FROM events WHERE value > 1"), KEYED),
    # no ORDER BY: every row is a peer, so these frames are the whole partition for the navigation functions too
    ("first_value_unordered_full_rows_frame", over("FIRST_VALUE(value)", "ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING", ""),
     over("FIRST_VALUE(value)", "", ""), None),
    ("first_value_unordered_range_frame", over("FIRST_VALUE(value)", "RANGE BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW", ""),
     over("FIRST_VALUE(value)", "", ""), None),
    ("countif_default_frame_spelled_out", over("COUNTIF(value > 2)", "RANGE BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW", "ORDER BY ts"),
     over("COUNTIF(value > 2)", "", "ORDER BY ts"), None),
    ("logical_and_full_frame_drops_order", over("LOGICAL_AND(value > 0)", "ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING", "ORDER BY ts"),
     over("LOGICAL_AND(value > 0)", "", ""), None),
]

# (name, left, right, constraints, witness rows (id, user_id, ts, value))
MUST_NOT_PROVE = [
    ("no_key_declared", over("SUM(value)", ROWS_RUNNING), over("SUM(value)", ""), None,
     [(1, 1, 1, 1), (1, 1, 1, 10), (3, 1, 2, 2)]),
    ("order_by_does_not_hold_the_key", over("SUM(value)", ROWS_RUNNING, "ORDER BY ts"), over("SUM(value)", "", "ORDER BY ts"), KEYED,
     [(1, 1, 1, 1), (2, 1, 1, 10), (3, 1, 2, 2)]),
    ("key_not_declared_not_null", over("SUM(value)", ROWS_RUNNING), over("SUM(value)", ""), KEY_NULLABLE,
     [(None, 1, 1, 1), (None, 1, 1, 10), (3, 1, 2, 2)]),
    ("not_null_without_a_key", over("SUM(value)", ROWS_RUNNING), over("SUM(value)", ""), KEY_NOT_NULL_ONLY,
     [(1, 1, 1, 1), (1, 1, 1, 10), (3, 1, 2, 2)]),
    ("part_of_a_composite_key", over("SUM(value)", ROWS_RUNNING, "ORDER BY ts", ""), over("SUM(value)", "", "ORDER BY ts", ""), COMPOSITE,
     [(1, 1, 1, 1), (2, 2, 1, 10), (3, 1, 2, 2)]),
    ("key_hidden_in_an_expression", over("SUM(value)", ROWS_RUNNING, "ORDER BY id + 0"), over("SUM(value)", "", "ORDER BY id + 0"), KEYED,
     None),
    ("offset_frame_is_rows_not_distance", over("SUM(value)", "ROWS BETWEEN 1 PRECEDING AND CURRENT ROW"),
     over("SUM(value)", "RANGE BETWEEN 1 PRECEDING AND CURRENT ROW"), KEYED, [(1, 1, 1, 1), (5, 1, 1, 10), (6, 1, 2, 2)]),
    ("join_can_repeat_a_key", f"SELECT e.id, SUM(e.value) OVER (ORDER BY e.id ROWS UNBOUNDED PRECEDING) AS w FROM events AS e JOIN events AS f ON e.user_id = f.user_id",
     "SELECT e.id, SUM(e.value) OVER (ORDER BY e.id) AS w FROM events AS e JOIN events AS f ON e.user_id = f.user_id", KEYED,
     [(1, 1, 1, 1), (2, 1, 1, 10)]),
    ("rows_unordered_is_not_the_partition", over("SUM(value)", ROWS_RUNNING, ""), over("SUM(value)", "", ""), KEYED,
     [(1, 1, 1, 1), (2, 1, 1, 10)]),
    ("last_value_unordered_running_rows", over("LAST_VALUE(value)", ROWS_RUNNING, ""), over("LAST_VALUE(value)", "", ""), KEYED,
     [(1, 1, 1, 1), (2, 1, 1, 10)]),
    ("last_value_rows_frame_with_peers", over("LAST_VALUE(value)", ROWS_RUNNING, "ORDER BY ts"), over("LAST_VALUE(value)", "", "ORDER BY ts"), KEYED,
     [(1, 1, 1, 1), (2, 1, 1, 10), (3, 1, 2, 5)]),
    ("rows_frame_next_to_other_order", over("SUM(value)", ROWS_RUNNING, "ORDER BY id DESC"), over("SUM(value)", "", "ORDER BY id"), KEYED,
     [(1, 1, 1, 1), (2, 1, 1, 10)]),
    ("countif_running_not_the_partition", over("COUNTIF(value > 2)", "", "ORDER BY ts"), over("COUNTIF(value > 2)", "", ""), None,
     [(1, 1, 1, 5), (2, 1, 2, 5)]),
]

DATABASES = [
    [(1, 1, 1, 1), (2, 1, 1, 10), (3, 1, 2, 2), (4, 2, 3, None), (5, 2, None, 4), (6, 3, 3, 3)],
    [(1, 1, 3, 5), (2, 1, 3, 5), (3, 2, 3, 7), (4, 2, 4, 1), (5, None, 3, 2), (6, None, None, None), (7, None, 3, 2)],
    [(10, 1, 7, 7), (20, 1, 7, 7), (30, 1, 7, 7), (40, 1, 7, 7)],
    [],
]


COMPOSITE_DATABASES = [
    [(1, 1, 1, 1), (2, 1, 2, 10), (3, 2, 1, 2), (4, 2, 2, None), (5, 3, 3, 3), (6, 1, 3, 3)],
    [(1, 1, 1, 5), (2, 1, 2, 5), (3, 2, 1, 5)],
    [],
]


def _databases(constraints):
    return COMPOSITE_DATABASES if constraints is COMPOSITE else DATABASES


def _duck(sql):
    return bigquery_on_duckdb.to_duckdb_sql(sqlglot.parse_one(sql, read="bigquery"))


def _database(rows):
    db = duckdb.connect()
    bigquery_on_duckdb.configure(db)
    db.execute("SET threads=1")
    db.execute("CREATE TABLE events (id BIGINT, user_id BIGINT, ts BIGINT, value BIGINT)")
    insert_rows(db, "events", rows)
    return db


def _rows(left, right, rows):
    db = _database(rows)
    return [sorted(result, key=repr) for result in run_unoptimized(db, *(_duck(q) for q in (left, right)))]


def _proven(left, right, constraints):
    return prove_equivalent_algebraic(left, right, schema=SCHEMA, types=TYPES, constraints=constraints, dialect="bigquery").proven


@pytest.mark.parametrize("name,left,right,constraints", MUST_PROVE, ids=[p[0] for p in MUST_PROVE])
def test_proves(name, left, right, constraints):
    assert _proven(left, right, constraints)
    assert _proven(right, left, constraints)


@pytest.mark.parametrize("name,left,right,constraints", MUST_PROVE, ids=[p[0] for p in MUST_PROVE])
def test_proved_pairs_agree_on_duckdb(name, left, right, constraints):
    for rows in _databases(constraints):  # the declared key holds in every database
        a, b = _rows(left, right, rows)
        assert a == b, rows


@pytest.mark.parametrize("name,left,right,constraints,rows", MUST_NOT_PROVE, ids=[p[0] for p in MUST_NOT_PROVE])
def test_does_not_prove(name, left, right, constraints, rows):
    assert not _proven(left, right, constraints)
    assert not _proven(right, left, constraints)


@pytest.mark.parametrize("name,left,right,constraints,rows", [p for p in MUST_NOT_PROVE if p[4]], ids=[p[0] for p in MUST_NOT_PROVE if p[4]])
def test_witness_tells_the_pair_apart(name, left, right, constraints, rows):
    a, b = _rows(left, right, rows)
    assert a != b


def _normal(sql, constraints):
    kwargs = {}
    if constraints:
        kwargs["not_null"] = {t: c.not_null for t, c in constraints.items()}
        kwargs["keys"] = {t: [tuple(k) for k in c.keys] for t, c in constraints.items()}
    return normalize(sql, schema=SCHEMA, types=TYPES, **kwargs)


def test_the_rewrite_only_changes_the_frame_kind():
    rows = over("SUM(value)", ROWS_RUNNING)
    assert "ROWS" not in _normal(rows, KEYED)
    assert "ROWS" in _normal(rows, KEY_NULLABLE)


def test_rewritten_windows_return_the_same_rows_on_duckdb():
    # the rewritten text itself, not only its equality with the other spelling
    for name, left, right, constraints in MUST_PROVE:
        if constraints is None:
            continue
        rewritten = _duck(_normal(left, constraints))
        for rows in _databases(constraints):
            a, b = run_unoptimized(_database(rows), _duck(left), rewritten)
            assert sorted(a, key=repr) == sorted(b, key=repr), (name, rows)
