"""R7 (merge of a full re-run): the proof, a near miss for each condition, and the dedup abstraction."""

import pytest

pytest.importorskip("duckdb")

from kumosql.incremental import SourceTable, check_incremental, parse_incremental_sqlx  # noqa: E402
from kumosql.incremental_merge import (  # noqa: E402
    RULE,
    canonical_query,
    dedup_abstraction,
    prove_full_rerun_merge,
)

EVENTS = {
    "events": SourceTable(
        {"id": "INT64", "customer_id": "INT64", "ts": "TIMESTAMP", "v": "INT64"},
        ("id",),
        "ts",
    )
}
SCHEMA = {"events": ["id", "customer_id", "ts", "v"]}
EV = '${ref("events")}'
KEY = ', uniqueKey: ["id"]'
CKEY = ', uniqueKey: ["customer_id"]'
INSERTS = {"insert_new", "insert_late", "insert_boundary", "empty"}
UPDATES = INSERTS | {"update", "update_touch"}
ROWS = f"SELECT id, customer_id, ts, v FROM {EV}"
NOT_NULL = "WHERE customer_id IS NOT NULL"
BY_CUSTOMER = (
    f"SELECT customer_id, SUM(v) AS total FROM {EV} {NOT_NULL} GROUP BY customer_id"
)


def model(body: str, config: str = KEY, incremental: str = ""):
    tail = f"\n${{when(incremental(), `{incremental}`)}}" if incremental else ""
    return parse_incremental_sqlx(
        f'config {{ type: "incremental"{config} }}\n{body}{tail}\n', "m"
    )


def proved(m, kinds, sources=EVENTS, tables=None) -> bool:
    return prove_full_rerun_merge(m, sources, frozenset(kinds), tables) is not None


def dedup(order: str, where: str = NOT_NULL) -> str:
    return f"{ROWS} {where} QUALIFY ROW_NUMBER() OVER (PARTITION BY customer_id ORDER BY {order}) = 1"


# --- the proved shapes --------------------------------------------------------------------------


PROVED = {
    "bare select, updates in place": (model(ROWS), UPDATES),
    "always-true incremental filter": (model(ROWS, incremental="WHERE 1 = 1"), UPDATES),
    "TRUE incremental filter": (model(ROWS, incremental="WHERE TRUE"), UPDATES),
    "SELECT * wrapper": (model(f"SELECT * FROM (SELECT id, v FROM {EV})"), UPDATES),
    "filter on a key": (model(f"{ROWS} WHERE id > 0"), UPDATES),
    "filter on a column no insert changes": (
        model(f"{ROWS} WHERE v > 0"),
        {"insert_new", "insert_late"},
    ),
    "group by a non-null column": (model(BY_CUSTOMER, CKEY), INSERTS | {"duplicate"}),
    "coalesced group key": (
        model(
            f"SELECT COALESCE(customer_id, 0) AS customer_id, COUNT(*) AS n FROM {EV} GROUP BY 1",
            CKEY,
        ),
        {"insert_new"},
    ),
    "dedup with a total order": (model(dedup("ts DESC, id"), CKEY), INSERTS),
    "dedup, rows copied": (model(dedup("ts DESC, id"), CKEY), INSERTS | {"duplicate"}),
}


@pytest.mark.parametrize("m, kinds", PROVED.values(), ids=list(PROVED))
def test_proves_a_merge_of_a_full_rerun(m, kinds):
    verdict = prove_full_rerun_merge(m, EVENTS, frozenset(kinds))
    assert verdict is not None and verdict.outcome == "safe" and verdict.rule == RULE


def test_the_verdict_comes_from_check_incremental_without_a_search():
    verdict = check_incremental(model(ROWS), EVENTS, UPDATES)
    assert (verdict.outcome, verdict.rule) == ("safe", RULE)


# --- near misses, one per condition ---------------------------------------------------------------
# Each model is the proved shape above with a single change, and the change is what loses the proof.


def test_query_that_differs_from_the_full_query():
    assert proved(model(ROWS), UPDATES)
    assert not proved(
        model(ROWS, incremental="WHERE ts > TIMESTAMP '2024-01-01'"), UPDATES
    )
    assert not proved(model(ROWS, incremental="WHERE 1 = 2"), UPDATES)
    assert not proved(model(ROWS, incremental="WHERE v > 0"), UPDATES)
    # a real divergence: the filtered incremental run misses rows the full refresh has
    assert (
        check_incremental(
            model(ROWS, incremental="WHERE ts > TIMESTAMP '2024-01-01'"),
            EVENTS,
            {"insert_late"},
            seeds=15,
        ).outcome
        == "diverges"
    )


def test_key_that_is_not_unique():
    assert proved(model(BY_CUSTOMER, CKEY), INSERTS)
    assert not proved(
        model(f"SELECT customer_id, id, v FROM {EV} {NOT_NULL}", CKEY), INSERTS
    )  # not grouped
    assert not proved(
        model(
            f"SELECT customer_id, v, SUM(id) AS s FROM {EV} {NOT_NULL} GROUP BY customer_id, v",
            CKEY,
        ),
        INSERTS,
    )
    assert proved(model(ROWS), INSERTS)
    assert not proved(
        model(ROWS), INSERTS | {"duplicate"}
    )  # re-delivered rows share the key
    assert not proved(
        model(f"SELECT id, customer_id FROM {EV}", ', uniqueKey: ["customer_id"]'),
        INSERTS,
    )


def test_key_that_can_be_null():
    assert proved(model(BY_CUSTOMER, CKEY), INSERTS)
    nullable = f"SELECT customer_id, SUM(v) AS total FROM {EV} GROUP BY customer_id"
    assert not proved(
        model(nullable, CKEY), INSERTS
    )  # customer_id is not declared NOT NULL
    assert not proved(
        model(f"SELECT v, COUNT(*) AS n FROM {EV} GROUP BY v", ', uniqueKey: ["v"]'),
        {"insert_new"},
    )
    assert proved(model(ROWS), INSERTS)
    assert not proved(
        model(ROWS), INSERTS | {"null_key"}
    )  # the contract can insert a NULL id
    assert proved(
        model(
            f"SELECT COALESCE(customer_id, 0) AS customer_id FROM {EV} GROUP BY 1", CKEY
        ),
        {"insert_new"},
    )


def test_keys_that_can_leave_the_output():
    assert proved(model(ROWS), UPDATES)
    assert not proved(model(ROWS), INSERTS | {"delete"})
    assert not proved(
        model(f"{ROWS} WHERE v > 0"), UPDATES
    )  # an update can push v to 0
    assert not proved(
        model(BY_CUSTOMER, CKEY), INSERTS | {"update"}
    )  # an update can move a row out of its group
    assert not proved(
        model(f"{ROWS} WHERE v > (SELECT AVG(v) FROM {EV})"), {"insert_new"}
    )  # a new row can raise the average
    assert not proved(model(f"{ROWS} ORDER BY ts LIMIT 5"), {"insert_new"})
    assert proved(model(f"{ROWS} WHERE v > 0"), {"insert_new"})
    # a real divergence: the merge never deletes the row that left
    assert (
        check_incremental(
            model(f"{ROWS} WHERE v > 0"), EVENTS, {"insert_new", "update"}, seeds=40
        ).outcome
        == "diverges"
    )


def test_ties():
    assert proved(model(dedup("ts DESC, id"), CKEY), INSERTS)
    assert not proved(
        model(dedup("ts DESC"), CKEY), INSERTS
    )  # two rows of a customer can share ts
    assert not proved(model(dedup("ts DESC"), CKEY), INSERTS | {"duplicate"})
    assert not proved(
        model(
            f"SELECT customer_id, ANY_VALUE(v) AS v FROM {EV} {NOT_NULL} GROUP BY customer_id",
            CKEY,
        ),
        INSERTS,
    )
    assert not proved(model(f"{ROWS} QUALIFY RAND() < 2", KEY), INSERTS)


def test_configuration_that_no_longer_matches_the_theorem():
    assert proved(model(ROWS), UPDATES)
    assert not proved(
        model(ROWS, config=""), UPDATES
    )  # append, no uniqueKey: duplicates
    assert not proved(
        model(
            ROWS,
            config=KEY
            + ", bigquery: { updatePartitionFilter: \"ts > TIMESTAMP '2020-01-01'\" }",
        ),
        UPDATES,
    )
    deleting = parse_incremental_sqlx(
        f'config {{ type: "incremental"{KEY} }}\npre_operations {{ ${{when(incremental(), `DELETE FROM ${{self()}} WHERE v > 5`)}} }}\n{ROWS}\n',
        "m",
    )
    assert deleting.pre_operations and not proved(deleting, UPDATES)


def test_star_modifiers_are_not_dropped_as_a_wrapper():
    # the incremental run selects fewer columns than the full refresh: not the same query
    body = f"SELECT * ${{when(incremental(), `EXCEPT (v)`)}} FROM (SELECT id, v FROM {EV})"
    assert not proved(model(body), UPDATES)
    assert proved(model(f"SELECT * FROM (SELECT id, v FROM {EV})"), UPDATES)
    star = "SELECT * {} FROM (SELECT id, v FROM t)"
    for modifier in ("EXCEPT (v)", "REPLACE (1 AS v)"):
        assert canonical_query(star.format(modifier)) != canonical_query("SELECT id, v FROM t")


def test_unknown_columns_are_no_proof():
    assert not proved(model(f"SELECT id, missing FROM {EV}"), UPDATES)
    assert not proved(
        model(f"SELECT id FROM {EV}", ', uniqueKey: ["missing"]'), UPDATES
    )


# --- canonical_query -------------------------------------------------------------------------------


def test_canonical_query_drops_only_always_true_filters_and_star_wrappers():
    base = canonical_query("SELECT id FROM t")
    assert canonical_query("SELECT id FROM t WHERE 1 = 1") == base
    assert canonical_query("SELECT id FROM t WHERE TRUE AND (1 = 1)") == base
    assert canonical_query("SELECT * FROM (SELECT id FROM t) AS x") == base
    assert canonical_query("SELECT id FROM t WHERE 1 = 2") != base
    assert canonical_query(
        "SELECT id FROM t WHERE id > 1 AND 1 = 1"
    ) == canonical_query("SELECT id FROM t WHERE id > 1")
    assert canonical_query(
        "SELECT * EXCEPT (v) FROM (SELECT id, v FROM t)"
    ) != canonical_query("SELECT id, v FROM t")
    assert canonical_query("SELECT * FROM (SELECT id FROM t) WHERE id > 1") != base
    assert canonical_query("SELECT FROM WHERE (") is None


# --- the dedup abstraction ---------------------------------------------------------------------------

OVER = "ROW_NUMBER() OVER (PARTITION BY customer_id ORDER BY ts DESC)"
CTE = f"WITH r AS (SELECT customer_id, ts, {OVER} AS rn FROM events)"

GROUPED = {
    "QUALIFY = 1": f"SELECT id, customer_id, ts FROM events QUALIFY {OVER} = 1",
    "QUALIFY <= 1": f"SELECT customer_id, ts FROM events QUALIFY {OVER} <= 1",
    "QUALIFY on an alias": f"SELECT customer_id, {OVER} AS rn FROM events QUALIFY rn = 1",
    "QUALIFY with a star": f"SELECT * FROM events QUALIFY {OVER} = 1",
    "WHERE rn = 1 over a CTE used once": f"{CTE} SELECT customer_id, ts FROM r WHERE rn = 1",
    "WHERE rn = 1 over a derived table": f"SELECT customer_id, ts FROM (SELECT customer_id, ts, {OVER} AS rn FROM events) AS r WHERE rn = 1",
}


@pytest.mark.parametrize("sql", GROUPED.values(), ids=list(GROUPED))
def test_dedup_is_read_as_a_group_by_on_the_partition(sql):
    grouped = dedup_abstraction(sql, SCHEMA)
    assert (
        grouped is not None
        and "GROUP BY customer_id" in grouped
        and "ROW_NUMBER" not in grouped
    )
    assert "QUALIFY" not in grouped and "rn = 1" not in grouped


UNCHANGED = {
    "CTE read twice": f"{CTE} SELECT customer_id, ts FROM r WHERE rn = 1 UNION ALL SELECT customer_id, ts FROM r WHERE rn = 1",
    "extra filter next to rn = 1": f"{CTE} SELECT customer_id, ts FROM r WHERE rn = 1 AND ts > 0",
    "second row of each partition": f"SELECT customer_id, ts FROM events QUALIFY {OVER} = 2",
    "no PARTITION BY": "SELECT customer_id, ts FROM events QUALIFY ROW_NUMBER() OVER (ORDER BY ts) = 1",
    "RANK is not read": "SELECT customer_id, ts FROM events QUALIFY RANK() OVER (PARTITION BY customer_id ORDER BY ts) = 1",
    "a join next to the derived table": f"SELECT r.customer_id FROM (SELECT customer_id, {OVER} AS rn FROM events) AS r JOIN events e ON e.id = r.customer_id WHERE r.rn = 1",
    "an aggregate already groups": f"SELECT customer_id, COUNT(*) AS c FROM events GROUP BY customer_id QUALIFY {OVER.replace('ts DESC', 'c')} = 1",
    "no dedup at all": "SELECT id FROM events",
}


@pytest.mark.parametrize("sql", UNCHANGED.values(), ids=list(UNCHANGED))
def test_dedup_abstraction_leaves_other_shapes_alone(sql):
    assert dedup_abstraction(sql, SCHEMA) is None


def test_dedup_reaches_the_proof_through_the_cte_but_not_when_it_is_read_twice():
    once = f"WITH r AS (SELECT id, customer_id, ts, ROW_NUMBER() OVER (PARTITION BY customer_id ORDER BY ts DESC, id) AS rn FROM {EV} {NOT_NULL}) SELECT customer_id, ts, id FROM r WHERE rn = 1"
    twice = once + " UNION ALL SELECT customer_id, ts, id FROM r WHERE rn = 1"
    derived = f"SELECT customer_id, ts, id FROM (SELECT id, customer_id, ts, ROW_NUMBER() OVER (PARTITION BY customer_id ORDER BY ts DESC, id) AS rn FROM {EV} {NOT_NULL}) WHERE rn = 1"
    assert proved(model(once, CKEY), INSERTS)
    assert proved(model(derived, CKEY), INSERTS)
    assert not proved(model(twice, CKEY), INSERTS)
