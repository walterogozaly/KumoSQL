"""Key-growth analysis: each documented rule, a near miss for each, and the contract's key facts."""

import pytest

from kumosql.incremental import SourceTable
from kumosql.incremental_monotone import analyze, classify, contract_constraints

SOURCES = {
    "events": SourceTable(
        {"id": "INT64", "customer_id": "INT64", "ts": "TIMESTAMP", "v": "INT64"},
        ("id",),
        "ts",
    ),
    "customers": SourceTable({"id": "INT64", "tier": "STRING"}, ("id",)),
}
INSERT = {"insert_new", "insert_late"}
KEYED = INSERT | {"update"}
TOUCHED = INSERT | {"update_touch"}
ROWS = "SELECT id, customer_id, ts, v FROM events"
CLASSIFIED = (
    "SELECT e.id, c.tier FROM events e {join} JOIN customers c ON c.id = e.customer_id"
)
WINDOW = "ROW_NUMBER() OVER (PARTITION BY {p} ORDER BY ts)"
BY_CUSTOMER = "SELECT customer_id, COUNT(*) AS n, MAX(v) AS hi, MIN(v) AS lo FROM events GROUP BY customer_id"
NO_GUARANTEE = "no guarantee"
NOT_UNDERSTOOD = "not understood"


def read(sql, kinds, tables=None):
    """The sorted stable output columns, NO_GUARANTEE when the analysis gives none, NOT_UNDERSTOOD when it refuses."""

    growth = analyze(sql, SOURCES, kinds, tables)
    if growth is None:
        return NOT_UNDERSTOOD
    return sorted(growth.stable_names()) if growth.stable is not None else NO_GUARANTEE


ALL = ["customer_id", "id", "ts", "v"]

# (query, kinds, tables the contract changes, what must be stable). Each rule is followed by near misses.
CASES = {
    # sources: frozen, growing, keyed, anything else
    "growing source": (ROWS, INSERT, None, ALL),
    "growing source, duplicates": (ROWS, INSERT | {"duplicate"}, None, ALL),
    "frozen source": (ROWS, INSERT, ("customers",), ALL),
    "no change at all is frozen": (ROWS, {"empty"}, None, ALL),
    "keyed source keeps its key and event time": (ROWS, KEYED, None, ["id", "ts"]),
    "keyed source, update_touch moves the event time": (ROWS, TOUCHED, None, ["id"]),
    "deleting source": (ROWS, INSERT | {"delete"}, None, NO_GUARANTEE),
    "keyed source of a table without a key": (
        "SELECT id, tier FROM customers",
        KEYED,
        None,
        ["id"],
    ),
    # positive filters
    "filter on growing rows": (ROWS + " WHERE v > 0", INSERT, None, ALL),
    "NOT over a growing row's own columns": (
        ROWS + " WHERE NOT v > 0",
        INSERT,
        None,
        ALL,
    ),
    "filter on a key of a keyed source": (
        ROWS + " WHERE id > 0",
        KEYED,
        None,
        ["id", "ts"],
    ),
    "filter on the event time of a keyed source": (
        ROWS + " WHERE ts > TIMESTAMP '2020-01-01'",
        KEYED,
        None,
        ["id", "ts"],
    ),
    "near miss: filter on a column an update changes": (
        ROWS + " WHERE v > 0",
        KEYED,
        None,
        NO_GUARANTEE,
    ),
    "near miss: filter on an event time update_touch moves": (
        ROWS + " WHERE ts > TIMESTAMP '2020-01-01'",
        TOUCHED,
        None,
        NO_GUARANTEE,
    ),
    "near miss: filter on a deleting source": (
        ROWS + " WHERE v > 0",
        INSERT | {"delete"},
        None,
        NO_GUARANTEE,
    ),
    "positive IN over a growing query": (
        "SELECT id FROM events WHERE customer_id IN (SELECT id FROM customers)",
        INSERT,
        None,
        ["id"],
    ),
    "positive EXISTS over a growing query": (
        "SELECT id FROM events e WHERE EXISTS (SELECT 1 FROM customers c WHERE c.id = e.customer_id)",
        INSERT,
        None,
        ["id"],
    ),
    "near miss: IN over a query that updates": (
        "SELECT id FROM events WHERE customer_id IN (SELECT id FROM customers)",
        KEYED,
        None,
        NO_GUARANTEE,
    ),
    "NOT IN against a frozen query": (
        "SELECT id FROM events WHERE customer_id NOT IN (SELECT id FROM customers)",
        INSERT,
        ("events",),
        ["id"],
    ),
    "near miss: NOT IN against a growing query": (
        "SELECT id FROM events WHERE customer_id NOT IN (SELECT id FROM customers)",
        INSERT,
        None,
        NO_GUARANTEE,
    ),
    "NOT EXISTS against a frozen query": (
        "SELECT id FROM events e WHERE NOT EXISTS (SELECT 1 FROM customers c WHERE c.id = e.customer_id)",
        INSERT,
        ("events",),
        ["id"],
    ),
    "near miss: NOT EXISTS against a growing query": (
        "SELECT id FROM events e WHERE NOT EXISTS (SELECT 1 FROM customers c WHERE c.id = e.customer_id)",
        INSERT,
        None,
        NO_GUARANTEE,
    ),
    "near miss: comparison with a growing aggregate": (
        "SELECT id FROM events WHERE v > (SELECT AVG(v) FROM events)",
        INSERT,
        None,
        NO_GUARANTEE,
    ),
    # joins
    "inner join of growing tables": (
        CLASSIFIED.format(join="INNER"),
        INSERT,
        None,
        ["id", "tier"],
    ),
    "near miss: inner join of keyed tables": (
        CLASSIFIED.format(join="INNER"),
        KEYED,
        None,
        NO_GUARANTEE,
    ),
    "near miss: inner join with one keyed side": (
        CLASSIFIED.format(join="INNER"),
        KEYED,
        ("events",),
        NO_GUARANTEE,
    ),
    "left join keeps the left side": (
        CLASSIFIED.format(join="LEFT"),
        INSERT,
        None,
        ["id"],
    ),
    "left join adds a frozen right side": (
        CLASSIFIED.format(join="LEFT"),
        INSERT,
        ("events",),
        ["id", "tier"],
    ),
    "near miss: left join does not trust a changing right side": (
        CLASSIFIED.format(join="LEFT"),
        KEYED,
        ("customers",),
        ["id"],
    ),
    "near miss: left join with a deleting left side": (
        CLASSIFIED.format(join="LEFT"),
        INSERT | {"delete"},
        ("events",),
        NO_GUARANTEE,
    ),
    "right join is not read": (
        CLASSIFIED.format(join="RIGHT"),
        INSERT,
        None,
        NOT_UNDERSTOOD,
    ),
    "full join is not read": (
        CLASSIFIED.format(join="FULL"),
        INSERT,
        None,
        NOT_UNDERSTOOD,
    ),
    # GROUP BY and HAVING
    "group by a growing column": (BY_CUSTOMER, INSERT, None, ["customer_id"]),
    "group by a key of a keyed source": (
        "SELECT id, SUM(v) AS s FROM events GROUP BY id",
        KEYED,
        None,
        ["id"],
    ),
    "near miss: group by a column an update changes": (
        BY_CUSTOMER,
        KEYED,
        None,
        NO_GUARANTEE,
    ),
    "near miss: aggregates are never stable": (
        "SELECT customer_id, SUM(v) AS s FROM events GROUP BY customer_id",
        INSERT,
        None,
        ["customer_id"],
    ),
    "HAVING on the group column": (
        BY_CUSTOMER + " HAVING customer_id > 0",
        INSERT,
        None,
        ["customer_id"],
    ),
    "HAVING COUNT >= n": (
        BY_CUSTOMER + " HAVING COUNT(*) >= 2",
        INSERT,
        None,
        ["customer_id"],
    ),
    "HAVING MAX > n": (
        BY_CUSTOMER + " HAVING MAX(v) > 2",
        INSERT,
        None,
        ["customer_id"],
    ),
    "HAVING MIN < n": (
        BY_CUSTOMER + " HAVING MIN(v) < 2",
        INSERT,
        None,
        ["customer_id"],
    ),
    "near miss: HAVING COUNT < n": (
        BY_CUSTOMER + " HAVING COUNT(*) < 2",
        INSERT,
        None,
        NO_GUARANTEE,
    ),
    "near miss: HAVING MIN > n": (
        BY_CUSTOMER + " HAVING MIN(v) > 2",
        INSERT,
        None,
        NO_GUARANTEE,
    ),
    "near miss: HAVING MAX < n": (
        BY_CUSTOMER + " HAVING MAX(v) < 2",
        INSERT,
        None,
        NO_GUARANTEE,
    ),
    "near miss: HAVING SUM (values can be negative)": (
        BY_CUSTOMER + " HAVING SUM(v) > 2",
        INSERT,
        None,
        NO_GUARANTEE,
    ),
    "near miss: HAVING COUNT on updating rows": (
        BY_CUSTOMER + " HAVING COUNT(*) >= 2",
        KEYED,
        None,
        NO_GUARANTEE,
    ),
    "near miss: WHERE inside a group that updates": (
        "SELECT customer_id, COUNT(*) AS n FROM events WHERE v > 0 GROUP BY customer_id",
        KEYED,
        None,
        NO_GUARANTEE,
    ),
    # QUALIFY dedup
    "QUALIFY ROW_NUMBER = 1": (
        f"{ROWS} QUALIFY {WINDOW.format(p='customer_id')} = 1",
        INSERT,
        None,
        ["customer_id"],
    ),
    "QUALIFY ROW_NUMBER <= k": (
        f"{ROWS} QUALIFY {WINDOW.format(p='customer_id')} <= 2",
        INSERT,
        None,
        ["customer_id"],
    ),
    "QUALIFY RANK = 1": (
        f"{ROWS} QUALIFY {WINDOW.format(p='customer_id').replace('ROW_NUMBER', 'RANK')} = 1",
        INSERT,
        None,
        ["customer_id"],
    ),
    "QUALIFY on an output alias": (
        f"SELECT customer_id, {WINDOW.format(p='customer_id')} AS rn FROM events QUALIFY rn = 1",
        INSERT,
        None,
        ["customer_id"],
    ),
    "QUALIFY by a key of a keyed source": (
        "SELECT id, ts FROM events QUALIFY " + WINDOW.format(p="id") + " = 1",
        KEYED,
        None,
        ["id"],
    ),
    "near miss: QUALIFY keeps the second row": (
        f"{ROWS} QUALIFY {WINDOW.format(p='customer_id')} = 2",
        INSERT,
        None,
        NO_GUARANTEE,
    ),
    "near miss: QUALIFY partition an update changes": (
        f"{ROWS} QUALIFY {WINDOW.format(p='customer_id')} = 1",
        KEYED,
        None,
        NO_GUARANTEE,
    ),
    # UNION
    "UNION ALL of growing branches": (
        "SELECT id, v FROM events UNION ALL SELECT id, 0 FROM customers",
        INSERT,
        None,
        ["id", "v"],
    ),
    "UNION DISTINCT": (
        "SELECT id FROM events UNION DISTINCT SELECT id FROM customers",
        INSERT,
        None,
        ["id"],
    ),
    "UNION ALL, one branch keyed": (
        "SELECT id, v FROM events UNION ALL SELECT id, 0 FROM customers",
        KEYED,
        ("events",),
        ["id"],
    ),
    "near miss: UNION ALL, one branch deletes": (
        "SELECT id, v FROM events UNION ALL SELECT id, 0 FROM customers",
        INSERT | {"delete"},
        ("events",),
        NO_GUARANTEE,
    ),
    # EXCEPT, INTERSECT
    "EXCEPT against a frozen query": (
        "SELECT id FROM events EXCEPT DISTINCT SELECT id FROM customers",
        INSERT,
        ("events",),
        ["id"],
    ),
    "near miss: EXCEPT against a growing query": (
        "SELECT id FROM events EXCEPT DISTINCT SELECT id FROM customers",
        INSERT,
        None,
        NO_GUARANTEE,
    ),
    "near miss: EXCEPT with an updating left side": (
        "SELECT id FROM events EXCEPT DISTINCT SELECT id FROM customers",
        KEYED,
        ("events",),
        NO_GUARANTEE,
    ),
    "INTERSECT of growing queries": (
        "SELECT id FROM events INTERSECT DISTINCT SELECT id FROM customers",
        INSERT,
        None,
        ["id"],
    ),
    "near miss: INTERSECT of updating queries": (
        "SELECT id FROM events INTERSECT DISTINCT SELECT id FROM customers",
        KEYED,
        None,
        NO_GUARANTEE,
    ),
    # everything else
    "DISTINCT keeps the stable set": (
        "SELECT DISTINCT customer_id FROM events",
        INSERT,
        None,
        ["customer_id"],
    ),
    "CTE": (
        "WITH r AS (SELECT id, v FROM events WHERE v > 0) SELECT id FROM r",
        INSERT,
        None,
        ["id"],
    ),
    "near miss: LIMIT": (ROWS + " LIMIT 3", INSERT, None, NO_GUARANTEE),
    "near miss: RAND is not stable": (
        "SELECT id, RAND() AS r FROM events",
        INSERT,
        None,
        ["id"],
    ),
    "near miss: the clock is not stable": (
        "SELECT id, CURRENT_TIMESTAMP() AS now FROM events",
        INSERT,
        None,
        ["id"],
    ),
    "near miss: window values are not stable": (
        "SELECT id, SUM(v) OVER (PARTITION BY customer_id) AS s FROM events",
        INSERT,
        None,
        ["id"],
    ),
    "near miss: a bare aggregate": ("SELECT SUM(v) AS s FROM events", INSERT, None, []),
    "unknown table": ("SELECT id FROM nope", INSERT, None, NOT_UNDERSTOOD),
    "recursive WITH": (
        "WITH RECURSIVE r AS (SELECT 1 AS n UNION ALL SELECT n + 1 FROM r WHERE n < 3) SELECT n FROM r",
        INSERT,
        None,
        NOT_UNDERSTOOD,
    ),
    "unparseable": ("SELECT FROM WHERE (", INSERT, None, NOT_UNDERSTOOD),
}


@pytest.mark.parametrize(
    "sql, kinds, tables, expected", CASES.values(), ids=list(CASES)
)
def test_rule_and_its_near_misses(sql, kinds, tables, expected):
    assert read(sql, kinds, tables) == expected


def test_star_modifiers_change_the_output_columns():
    assert analyze("SELECT * EXCEPT (id, ts) FROM events", SOURCES, INSERT).names == ("customer_id", "v")
    assert analyze("SELECT * REPLACE (1 AS v) FROM events", SOURCES, INSERT) is None
    assert analyze("SELECT * FROM events", SOURCES, INSERT).names == ("id", "customer_id", "ts", "v")


def test_frozen_and_growing_flags():
    assert analyze(ROWS, SOURCES, INSERT, ("customers",)).frozen is True
    growing = analyze(ROWS, SOURCES, INSERT)
    assert (growing.frozen, growing.grows) == (False, True)
    keyed = analyze(ROWS, SOURCES, KEYED)
    assert (keyed.frozen, keyed.grows) == (False, False)  # rows change in place
    assert analyze("SELECT 1 AS x", SOURCES, KEYED).frozen is True
    assert analyze(ROWS + " WHERE v > 0", SOURCES, INSERT).grows is True
    assert (
        analyze(BY_CUSTOMER, SOURCES, INSERT).grows is False
    )  # a group's row changes as it gains rows


def test_classify_names_each_sources_state_and_stable_columns():
    states = classify(SOURCES, KEYED, ("events",))
    assert states["customers"][0] == "frozen"
    assert states["events"] == ("keyed", frozenset({"id", "ts"}))
    assert classify(SOURCES, INSERT)["events"][0] == "growing"
    assert classify(SOURCES, {"empty"})["events"][0] == "frozen"
    assert classify(SOURCES, INSERT | {"delete"})["events"] == ("changing", None)
    assert classify(SOURCES, TOUCHED)["events"][1] == frozenset({"id"})
    # a keyless table cannot be "keyed": an update has nothing to hold still
    keyless = {"log": SourceTable({"a": "INT64"})}
    assert classify(keyless, KEYED)["log"][0] == "changing"


# --- contract_constraints --------------------------------------------------------------------------


def facts(kinds, tables=None, **options):
    return contract_constraints(SOURCES, kinds, tables, **options)


def test_declared_keys_are_unique_and_non_null_under_inserts_updates_and_deletes():
    for kinds in (INSERT, KEYED, INSERT | {"delete"}, {"empty"}):
        constraint = facts(kinds)["events"]
        assert constraint.keys == (("id",),) and constraint.not_null == frozenset(
            {"id"}
        )
    assert facts(INSERT)["customers"].keys == (("id",),)


def test_duplicate_breaks_uniqueness_but_not_non_null():
    constraint = facts(INSERT | {"duplicate"})["events"]
    assert constraint.keys == () and constraint.not_null == frozenset({"id"})


def test_duplicate_keeps_the_key_when_only_rows_that_agree_must_be_equal():
    constraint = facts(INSERT | {"duplicate"}, exact_copies=True)["events"]
    assert constraint.keys == (("id",),) and constraint.not_null == frozenset({"id"})


def test_null_key_breaks_both_facts_even_for_exact_copies():
    for options in ({}, {"exact_copies": True}):
        constraint = facts(INSERT | {"null_key"}, **options)["events"]
        assert constraint.keys == () and constraint.not_null == frozenset()


def test_only_the_tables_that_change_lose_their_facts():
    constraints = facts(INSERT | {"duplicate", "null_key"}, ("customers",))
    assert constraints["events"].keys == (("id",),) and constraints[
        "events"
    ].not_null == frozenset({"id"})
    assert (
        constraints["customers"].keys == ()
        and constraints["customers"].not_null == frozenset()
    )


def test_other_columns_never_get_a_not_null_fact_and_keyless_tables_get_none():
    constraints = contract_constraints(
        {"log": SourceTable({"a": "INT64", "b": "INT64"})}, INSERT
    )
    assert constraints["log"].keys == () and constraints["log"].not_null == frozenset()
    assert "v" not in facts(INSERT)["events"].not_null
