"""Correlated scalar lookups over UNNEST (the GA4 ``event_params`` idiom) as one unknown function of the array."""

from __future__ import annotations

import pytest

pytest.importorskip("z3")

import sqlglot  # noqa: E402
from sqlglot import exp  # noqa: E402

from kumosql import nested_scalar_unnest  # noqa: E402
from kumosql.algebraic_equivalence import prove_equivalent_algebraic  # noqa: E402
from kumosql.nested_values import from_python, parse_type  # noqa: E402
from kumosql.result_equivalence import DatasetRunner, ExecutionError, SyntheticDataset, SyntheticTable, compare_outputs  # noqa: E402
from kumosql.smt_equivalence import SmtStatus  # noqa: E402

PARAMS = "ARRAY<STRUCT<key STRING, value STRUCT<string_value STRING, int_value INT64>>>"
TYPES = {"events": {"event_name": "STRING", "event_params": PARAMS, "user_properties": PARAMS, "scores": "ARRAY<INT64>"}}
SCHEMA = {t: list(c) for t, c in TYPES.items()}
ASSUMPTION = nested_scalar_unnest.ASSUMPTION


def lookup(expression="value.string_value", key="page_location", array="event_params", alias="", where=None):
    prefix = f"{alias}." if alias else ""
    expression = expression.replace("value.", f"{prefix}value.") if alias else expression
    test = where or f"{prefix}key = '{key}'"
    name = f" AS {alias}" if alias else ""
    return f"(SELECT {expression} FROM UNNEST({array}){name} WHERE {test})"


def select(*lookups, table="events", where=""):
    return "SELECT event_name, " + ", ".join(f"{item} AS v{i}" for i, item in enumerate(lookups)) + f" FROM {table}{where}"


def prove(left, right):
    return prove_equivalent_algebraic(left, right, schema=SCHEMA, types=TYPES, dialect="bigquery")


def proven(left, right):
    result = prove(left, right)
    assert result.status is SmtStatus.PROVEN_EQUIVALENT, result.reason
    return result


def not_proven(left, right):
    result = prove(left, right)
    assert result.status is not SmtStatus.PROVEN_EQUIVALENT, result.reason


# --- the same lookup written differently is one function ---------------------------------------------------------

SAME = {
    "flipped test": (lookup(), lookup(where="'page_location' = key")),
    "alias name": (lookup(alias="p"), lookup(alias="q")),
    "alias or none": (lookup(), lookup(alias="p")),
    "conjunct order": (
        lookup(where="key = 'k' AND value.string_value IS NOT NULL"),
        lookup(where="value.string_value IS NOT NULL AND key = 'k'"),
    ),
    "select as value": (lookup(), "(SELECT AS VALUE value.string_value FROM UNNEST(event_params) WHERE key = 'page_location')"),
    "output alias": (lookup(), "(SELECT value.string_value AS page FROM UNNEST(event_params) WHERE key = 'page_location')"),
    "parenthesized test": (lookup(), lookup(where="(key = 'page_location')")),
    "repeated conjunct": (lookup(where="key = 'k' AND key = 'k'"), lookup(where="key = 'k'")),
    "int field": (lookup("value.int_value", "ga_session_id"), lookup("value.int_value", where="'ga_session_id' = key")),
}


@pytest.mark.parametrize("name", SAME)
def test_the_same_lookup_written_differently_is_proven_equal(name):
    left, right = SAME[name]
    result = proven(select(left), select(right))
    assert ASSUMPTION in result.assumptions


def test_a_lookup_inside_a_filter_and_an_expression_is_matched():
    proven(
        f"SELECT event_name FROM events WHERE {lookup()} = 'home'",
        f"SELECT event_name FROM events WHERE {lookup(alias='p', where=chr(39) + 'page_location' + chr(39) + ' = p.key')} = 'home'",
    )
    proven(
        f"SELECT COALESCE({lookup()}, 'none') AS page FROM events",
        f"SELECT COALESCE({lookup(alias='x')}, 'none') AS page FROM events",
    )


def test_a_table_alias_on_the_outer_query_does_not_matter():
    proven(
        "SELECT e.event_name, (SELECT value.int_value FROM UNNEST(e.event_params) WHERE key = 'k') AS v FROM events AS e",
        "SELECT t.event_name, (SELECT p.value.int_value FROM UNNEST(t.event_params) AS p WHERE p.key = 'k') AS v FROM events AS t",
    )


def test_two_lookups_in_one_query_keep_their_own_identity():
    left = select(lookup(key="a"), lookup(key="b"))
    proven(left, select(lookup(key="a", alias="p"), lookup(key="b", alias="q")))
    not_proven(left, select(lookup(key="b"), lookup(key="a")))  # swapped columns
    not_proven(left, select(lookup(key="a"), lookup(key="a")))


def test_an_aggregate_lookup_returns_one_row_and_needs_no_assumption():
    count = "(SELECT COUNT(*) FROM UNNEST(items) AS i WHERE i.qty > 1)"
    types = {"orders": {"id": "INT64", "items": "ARRAY<STRUCT<qty INT64>>"}}
    result = prove_equivalent_algebraic(
        f"SELECT id, {count} AS n FROM orders",
        "SELECT id, (SELECT COUNT(*) FROM UNNEST(items) WHERE qty > 1) AS n FROM orders",
        schema={"orders": ["id", "items"]},
        types=types,
    )
    assert result.status is SmtStatus.PROVEN_EQUIVALENT, result.reason
    assert ASSUMPTION not in result.assumptions
    assert not any("uncorrelated" in a for a in result.assumptions)
    result = proven(
        select("(SELECT MAX(value.int_value) FROM UNNEST(event_params) WHERE key = 'k')"),
        select("(SELECT MAX(p.value.int_value) FROM UNNEST(event_params) AS p WHERE 'k' = p.key)"),
    )
    assert ASSUMPTION not in result.assumptions


# --- lookups that read differently are never equated -------------------------------------------------------------

DIFFERENT = {
    "other key": (lookup(key="k"), lookup(key="page_location")),
    "other field": (lookup("value.string_value"), lookup("value.int_value")),
    "other array": (lookup(), lookup(array="user_properties")),
    "negated test": (lookup(where="key = 'j'"), lookup(where="key <> 'j' AND key <> 'ga_session_id' AND key <> 'page_location'")),
    "extra conjunct": (lookup(where="key = 'k'"), lookup(where="key = 'k' AND value.int_value > 3")),
    "dropped test": (lookup(where="key = 'k'"), lookup(where="key = 'k' OR key = 'j'")),
    "max against plain": (lookup(), lookup("MAX(value.string_value)", "page_location")),
    "max against min": (lookup("MAX(value.int_value)", "k"), lookup("MIN(value.int_value)", "k")),
    "sum against max": (lookup("SUM(value.int_value)", "k"), lookup("MAX(value.int_value)", "k")),
    "count against plain": (lookup("COUNT(value.int_value)", "k"), lookup("value.int_value", "k")),
    "distinct count": (lookup("COUNT(DISTINCT value.int_value)", "k"), lookup("COUNT(value.int_value)", "k")),
    "cast": (lookup("value.string_value"), lookup("CAST(value.int_value AS STRING)")),
    "limit": (lookup(), lookup() .replace(")", " LIMIT 1)")),
    "order and limit": (lookup(), lookup().replace(")", " ORDER BY value.string_value LIMIT 1)")),
    "with offset": (lookup(), "(SELECT value.string_value FROM UNNEST(event_params) WITH OFFSET WHERE key = 'page_location')"),
}


@pytest.mark.parametrize("name", DIFFERENT)
def test_lookups_that_read_differently_are_not_proven_equal(name):
    left, right = DIFFERENT[name]
    not_proven(select(left), select(right))


def test_a_lookup_is_not_proven_equal_to_a_constant_or_to_itself_over_another_column():
    not_proven(select(lookup()), "SELECT event_name, NULL AS v0 FROM events")
    not_proven(select(lookup()), select(lookup()).replace("event_name,", "event_name || 'x' AS event_name,"))


def test_each_distinct_lookup_gets_its_own_function_number():
    lookups = nested_scalar_unnest.Lookups("bigquery", TYPES)
    tree = sqlglot.parse_one(select(lookup(key="a"), lookup(alias="p", key="a"), lookup(key="b")), read="bigquery")
    assert lookups.replace(tree) == 3
    assert lookups.classes.__len__() == 2 and lookups.unproven == 3
    numbers = [call.expressions[0].name for call in tree.find_all(exp.Anonymous) if call.name == nested_scalar_unnest.FUNCTION]
    assert numbers == ["0", "0", "1"]


# --- what is not a lookup is left alone --------------------------------------------------------------------------

DECLINED = [
    "(SELECT value.string_value FROM UNNEST(event_params) WHERE key = event_name)",  # reads another column of the outer row
    "(SELECT value.string_value FROM UNNEST(event_params) WHERE key = 'k' LIMIT 1)",
    "(SELECT DISTINCT value.string_value FROM UNNEST(event_params) WHERE key = 'k')",
    "(SELECT value.string_value FROM UNNEST(event_params) WITH OFFSET AS o WHERE key = 'k' AND o = 0)",
    "(SELECT value.string_value, key FROM UNNEST(event_params) WHERE key = 'k')",
    "(SELECT value.string_value FROM UNNEST(event_params) GROUP BY 1)",
    "(SELECT AS STRUCT key, value FROM UNNEST(event_params) WHERE key = 'k')",
    "(SELECT value.string_value FROM UNNEST(event_params) WHERE key = 'k' AND RAND() < 0.5)",
    "(SELECT value.string_value FROM UNNEST(event_params) WHERE key = 'k' AND value.string_value IN (SELECT 'a'))",
    "(SELECT value.string_value FROM UNNEST(event_params) AS p CROSS JOIN UNNEST(user_properties) AS u WHERE p.key = 'k')",
    "(SELECT value.string_value FROM UNNEST(GENERATE_ARRAY(1, 3)) WHERE key = 'k')",
    "(SELECT MAX(key) FROM UNNEST(event_params) WHERE MAX(key) = 'k')",
    "(SELECT nonexistent FROM UNNEST(event_params) WHERE key = 'k')",
]


@pytest.mark.parametrize("subquery", DECLINED)
def test_a_subquery_that_is_not_a_plain_lookup_is_left_alone(subquery):
    lookups = nested_scalar_unnest.Lookups("bigquery", TYPES)
    tree = sqlglot.parse_one(f"SELECT event_name, {subquery} AS v FROM events", read="bigquery")
    assert lookups.replace(tree) == 0
    assert not lookups.classes


def test_unqualified_names_need_the_declared_element_type():
    sql = f"SELECT event_name, {lookup()} AS v FROM events"
    assert nested_scalar_unnest.Lookups("bigquery", TYPES).replace(sqlglot.parse_one(sql, read="bigquery")) == 1
    assert nested_scalar_unnest.Lookups("bigquery", None).replace(sqlglot.parse_one(sql, read="bigquery")) == 0
    # an alias-qualified read needs no type
    aliased = f"SELECT event_name, {lookup(alias='p')} AS v FROM events"
    assert nested_scalar_unnest.Lookups("bigquery", None).replace(sqlglot.parse_one(aliased, read="bigquery")) == 1


def test_a_scalar_array_lookup_reads_through_its_alias():
    left = "SELECT event_name, (SELECT MAX(s) FROM UNNEST(scores) AS s WHERE s > 2) AS best FROM events"
    right = "SELECT event_name, (SELECT MAX(x) FROM UNNEST(scores) AS x WHERE 2 < x) AS best FROM events"
    proven(left, right)
    not_proven(left, "SELECT event_name, (SELECT MIN(x) FROM UNNEST(scores) AS x WHERE 2 < x) AS best FROM events")


def test_uncorrelated_scalar_subqueries_keep_their_own_assumption():
    schema, types = {"t": ["a"]}, {"t": {"a": "INT64"}}
    sql = "SELECT a FROM t WHERE a = (SELECT MAX(a) FROM t)"
    result = prove_equivalent_algebraic(sql, sql.replace("MAX(a)", "MAX(t.a)"), schema=schema, types=types)
    assert result.status is SmtStatus.PROVEN_EQUIVALENT
    assert ASSUMPTION not in result.assumptions


# --- both sides run on DuckDB through the BigQuery translation ----------------------------------------------------


def _params(*pairs):
    return [{"key": k, "value": {"string_value": s, "int_value": i}} for k, s, i in pairs]


# Every key at most once per row, so a plain lookup never fails (BigQuery would raise an error on two rows).
UNIQUE = [
    ("none", _params(), _params(), []),
    ("one", _params(("page_location", "home", None)), _params(("page_location", "x", None)), [1]),
    ("both", _params(("page_location", "home", None), ("ga_session_id", None, 5)), _params(), [3, 9, 4]),
    ("other", _params(("ga_session_id", None, 7), ("k", "kv", 2)), _params(("ga_session_id", None, 1)), [1, 2]),
    ("nulls", _params(("page_location", None, None), ("k", None, 4)), _params(("k", "u", None)), [5, 6]),
    ("big", _params(("k", "a", 3), ("j", "b", 4)), _params(("k", "z", 8), ("j", "b", 4)), [2, 2]),
]
# Repeated keys: only an aggregate lookup is defined here.
REPEATED = [
    ("none", _params(), _params(), []),
    ("twice", _params(("k", "a", 3), ("k", "b", 8)), _params(), [4]),
    ("dupe", _params(("k", "a", 3), ("k", "b", 3)), _params(), []),
    ("thrice", _params(("k", None, 2), ("k", "c", None), ("j", "d", 1), ("k", "e", 5)), _params(), [4]),
]


def _dataset(rows):
    columns = tuple(TYPES["events"].items())
    values = tuple(
        tuple(from_python(value, parse_type(t)) if t != "STRING" else value for value, (_, t) in zip(row, columns)) for row in rows
    )
    return SyntheticDataset(seed=0, tables={"events": SyntheticTable(columns=columns, rows=values)})


def _same_rows(left, right, rows=UNIQUE):
    with DatasetRunner(TYPES) as runner:
        data = _dataset(rows)
        return compare_outputs(
            runner.run(left, data, timeout=20), runner.run(right, data, timeout=20), check_column_names=False, float_digits=9
        )[0]


@pytest.mark.parametrize("name", SAME)
def test_equal_lookups_return_the_same_rows(name):
    left, right = SAME[name]
    assert _same_rows(select(left), select(right))


@pytest.mark.parametrize("name", ["other key", "other field", "other array", "negated test", "extra conjunct", "cast"])
def test_the_lookups_the_prover_keeps_apart_differ_on_data(name):
    left, right = DIFFERENT[name]
    assert not _same_rows(select(left), select(right))


@pytest.mark.parametrize("name", ["max against min", "sum against max", "count against plain", "distinct count"])
def test_the_aggregate_lookups_the_prover_keeps_apart_differ_on_data(name):
    left, right = DIFFERENT[name]
    if name == "count against plain":
        left, right = left, left.replace("COUNT(value.int_value)", "MAX(value.int_value)")
    assert not _same_rows(select(left), select(right), REPEATED)


def test_equal_aggregate_lookups_return_the_same_rows_with_repeated_keys():
    left = select("(SELECT MAX(value.int_value) FROM UNNEST(event_params) WHERE key = 'k')")
    right = select("(SELECT MAX(p.value.int_value) FROM UNNEST(event_params) AS p WHERE 'k' = p.key)")
    assert _same_rows(left, right, REPEATED)
