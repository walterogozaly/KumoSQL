"""Checks for the QED CockroachDB fixture produced by tools/qed_cockroach_to_sql.py."""

import importlib.util
import json
import sys
from pathlib import Path

import pytest
import sqlglot

_tools = Path(__file__).resolve().parent.parent / "tools"
sys.path.insert(0, str(_tools))
_spec = importlib.util.spec_from_file_location("qed_cockroach_to_sql", _tools / "qed_cockroach_to_sql.py")
conv = importlib.util.module_from_spec(_spec)
sys.modules["qed_cockroach_to_sql"] = conv
_spec.loader.exec_module(conv)

FIXTURES = Path(__file__).parent / "fixtures" / "qed_cockroach"
CASES = [json.loads(line) for line in (FIXTURES / "qed_cockroach_pairs.jsonl").read_text().splitlines()]
SKIPPED = [json.loads(line) for line in (FIXTURES / "qed_cockroach_skipped.jsonl").read_text().splitlines()]


def test_fixture_loads():
    assert len(CASES) > 700
    assert len({c["name"] for c in CASES}) == len(CASES)
    for c in CASES:
        assert {"name", "sql_a", "sql_b", "ddl", "schema_id", "schemas"} <= set(c)
        # a few pairs only use VALUES and read no table
        assert c["ddl"].startswith("CREATE TABLE") == bool(c["schemas"])
        assert c["name"].split("/")[0] in ("memo", "norm", "xform")


def test_summary_matches_fixture():
    summary = json.loads((FIXTURES / "summary.json").read_text())
    assert summary["source_commit"] == "9e9c2621d6d922007694a72f9cc2d5ed0de2eccd"
    assert summary["converted"] == len(CASES)
    assert summary["skipped"] == len(SKIPPED)
    assert summary["files"] == len(CASES) + len(SKIPPED) == 1287
    assert all(s["reason"] for s in SKIPPED)
    assert not {c["name"] for c in CASES} & {s["name"] for s in SKIPPED}


def test_licence_and_provenance_are_recorded():
    assert (FIXTURES / "LICENSE").read_text().startswith("MIT License")
    readme = (FIXTURES / "README.md").read_text()
    for text in ("9e9c262", "v23.1.3", "Business Source License"):
        assert text in readme


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_pair_parses(case):
    for key in ("sql_a", "sql_b"):
        tree = sqlglot.parse_one(case[key], read="mysql")
        assert isinstance(tree, sqlglot.exp.Query)
    # a pair that only uses VALUES reads no table and has no DDL
    for stmt in sqlglot.parse(case["ddl"], read="mysql") if case["ddl"] else []:
        assert isinstance(stmt, sqlglot.exp.Create)


def test_duckdb_runs_a_sample():
    duckdb = pytest.importorskip("duckdb")
    for case in CASES[::20]:
        con = duckdb.connect()
        for stmt in sqlglot.transpile(case["ddl"], read="mysql", write="duckdb"):
            con.execute(stmt)
        for key in ("sql_a", "sql_b"):
            con.execute(sqlglot.transpile(case[key], read="mysql", write="duckdb")[0]).fetchall()


# ---------------------------------------------------------------- the converter's rules
def scan_doc(types=("INT", "INT", "OID"), queries=None, help_=("",)):
    return {
        "help": list(help_),
        "schemas": [{"key": [[0]], "nullable": [False, True, True], "types": list(types)}],
        "queries": queries,
    }


def col(i, t="INT"):
    return {"column": i, "type": t}


def lit(v, t="INT"):
    return {"operand": [], "operator": v, "type": t}


def op(name, *operands, t="BOOL"):
    return {"operand": list(operands), "operator": name, "type": t}


def filt(cond, source=None):
    return {"filter": {"condition": op("AND", cond, t="BOOLEAN"), "source": source or {"scan": 0}}}


def project(target, source=None):
    return {"project": {"source": source or {"scan": 0}, "target": target}}


def convert(*queries, **kw):
    return conv.convert_case(scan_doc(queries=list(queries), **kw))


def skipped(*queries, **kw):
    with pytest.raises(conv.Skip) as e:
        convert(*queries, **kw)
    return str(e.value)


def test_operator_spellings():
    q = project([col(0)], filt(op("<=", col(1), lit("5"), t="BOOLEAN")))
    out = convert(q, q)
    assert "<= 5" in out["sql_a"]
    q = project([col(0)], filt(op("IS NOT", col(1), lit("5"))))
    assert "(NOT (t1.c1 <=> 5))" in convert(q, q)["sql_a"]
    q = project([col(0)], filt(op("<=>", col(1), col(0), t="BOOLEAN")))
    assert "(t1.c1 <=> t1.c0)" in convert(q, q)["sql_a"]
    q = project([col(0)], filt(op("IS", col(1), lit("NULL"))))
    assert "<=> NULL" in convert(q, q)["sql_a"]


def test_conjunct_lists_and_case_input():
    assert "WHERE TRUE" in convert(project([col(0)], filt(op("AND", t="BOOLEAN"))), project([col(0)]))["sql_a"]
    case = op("CASE", lit("TRUE", "BOOL"), op("EQ", col(0), lit("1")), col(1), lit("NULL"), t="INT")
    sql = convert(project([case]), project([col(0)]))["sql_a"]
    assert "CASE TRUE WHEN (t1.c0 = 1) THEN t1.c1 ELSE NULL END" in sql


def test_opaque_columns_pass_through_but_are_never_read():
    # column 2 is an OID: it may sit in the table and be dropped by a projection ...
    assert convert(project([col(0)]), project([col(0)]))["ddl"].count("BIGINT") >= 3
    # ... but not be compared, returned, grouped on a non-equality type or sorted on
    assert "OID" in skipped(project([col(0)], filt(op("EQ", col(2, "INT"), lit("1")))), project([col(0)]))
    assert "(no exact encoding)" in skipped({"scan": 0}, {"scan": 0}, types=("INT", "INT", "GEOMETRY"))
    sort = {"sort": {"collation": [[2, "INT", "ASCENDING"]], "source": {"scan": 0}, "limit": lit("1")}}
    assert "ORDER BY" in skipped(project([col(0)], sort), project([col(0)]))
    # an opaque type with plain equality may be returned
    assert convert({"scan": 0}, {"scan": 0})["sql_a"]


def test_unmodelled_things_are_skipped_with_a_reason():
    assert "FUNCTION" in skipped(project([op("FUNCTION", op("SCALAR LIST", col(0), t="ANYELEMENT"), t="INT")]), project([col(0)]))
    assert "LIMIT/OFFSET without ORDER BY" in skipped(
        {"sort": {"collation": [], "source": {"scan": 0}, "limit": lit("1")}}, project([col(0)]))
    assert "PLACEHOLDER" in skipped(project([col(0)], filt(op("EQ", col(0), lit("PLACEHOLDER")))), project([col(0)]))
    assert "non-literal divisor" in skipped(project([op("DIV", col(0), col(1), t="DECIMAL")]), project([col(0)]))
    assert "CONST AGG" in skipped(
        {"group": {"function": [{"operator": "CONST AGG", "operand": [col(1)], "type": "INT", "distinct": False, "ignoreNulls": False}],
                   "keys": [col(0)], "source": {"scan": 0}}}, project([col(0)]))


def test_values_with_no_columns_is_the_one_empty_row():
    empty = {"values": {"content": [[]], "schema": []}}
    out = convert(project([lit("1")], empty), project([lit("1")], empty))
    assert "(SELECT 1 AS z)" in out["sql_a"]
    assert out["ddl"] == "" and out["schemas"] == []


def duck_rows(sql):
    duckdb = pytest.importorskip("duckdb")
    return duckdb.connect().execute(sqlglot.transpile(sql, read="mysql", write="duckdb")[0]).fetchall()


def test_zero_column_relations_are_carried_as_a_row_count():
    # a projection with no columns keeps one constant column per row, so two such relations are bag-equal
    # exactly when their row counts agree
    nothing = {"values": {"content": [[]], "schema": []}}
    two = {"values": {"content": [[], []], "schema": []}}
    none = {"values": {"content": [], "schema": []}}
    both = convert(project([], filt(op("TRUE", t="BOOL"), nothing)), project([], nothing))
    assert "1 AS z" in both["sql_a"] and duck_rows(both["sql_a"]) == duck_rows(both["sql_b"]) == [(1,)]
    assert len(duck_rows(convert(two, none)["sql_a"])) == 2
    assert duck_rows(convert(two, none)["sql_b"]) == []
    # the zero-column join side still multiplies the rows of the other side
    joined = {"join": {"kind": "INNER", "condition": op("AND", t="BOOLEAN"), "left": two, "right": project([lit("7")], nothing)}}
    assert duck_rows(convert(joined, joined)["sql_a"]) == [(7,), (7,)]
    # a group by on a zero-column source counts its rows
    counted = {"group": {"function": [{"operator": "COUNT ROWS", "operand": [], "type": "INT", "distinct": False}], "keys": [], "source": two}}
    assert duck_rows(convert(counted, counted)["sql_a"]) == [(2,)]


def test_empty_values_may_carry_columns_of_any_type():
    empty = {"values": {"content": [], "schema": ["INT", "JSONB"]}}
    out = convert(empty, empty)
    assert duck_rows(out["sql_a"]) == []
    # but a JSONB cell would need a JSONB encoding
    full = {"values": {"content": [[lit("1"), lit("'[1]'", "JSONB")]], "schema": ["INT", "JSONB"]}}
    assert "VALUES column type JSONB" in skipped(full, full)


def test_division_is_exact_only_by_a_positive_power_of_two():
    def div(divisor):
        return project([op("DIV", col(0), lit(divisor, "DECIMAL" if "." in divisor else "INT"), t="DECIMAL")])

    for d in ("1", "2", "4", "16", "2.0", "0.5"):
        out = convert(div(d), project([col(0)]))
        assert " / " in out["sql_a"]
    for d, why in (("3", "power of two"), ("0", "by zero"), ("-2", "power of two"), ("10", "power of two"), ("0.1", "power of two")):
        assert why in skipped(div(d), project([col(0)])), d
    assert "non-literal divisor" in skipped(project([op("DIV", lit("4"), col(0), t="DECIMAL")]), project([col(0)]))
    # DuckDB's quotient by a power of two is the exact rational, as CockroachDB's decimal quotient is
    duckdb = pytest.importorskip("duckdb")
    from fractions import Fraction
    con = duckdb.connect()
    for d in (1, 2, 4, 8, 64):
        for x in (-1000001, -7, -1, 0, 1, 3, 25, 999999):
            assert Fraction(con.execute(f"SELECT CAST({x} AS BIGINT) / {d}").fetchone()[0]) == Fraction(x, d)


def test_modulo_by_a_nonzero_literal_keeps_the_dividends_sign_in_both_engines():
    def mod(divisor):
        return project([op("MOD", col(0), lit(divisor), t="INT")])

    assert " % 3" in convert(mod("3"), project([col(0)]))["sql_a"]
    assert "by zero" in skipped(mod("0"), project([col(0)]))
    assert "non-literal divisor" in skipped(project([op("MOD", col(0), col(1), t="INT")]), project([col(0)]))
    assert "non-integer" in skipped(project([op("MOD", col(0), lit("2.5", "DECIMAL"), t="DECIMAL")]), project([col(0)]))
    duckdb = pytest.importorskip("duckdb")
    con = duckdb.connect()
    for d in (3, 16, -3):
        for x in range(-40, 41):
            # truncated remainder (Go's and C's %), not the floored one Python's % gives
            assert con.execute(f"SELECT CAST({x} AS BIGINT) % ({d})").fetchone()[0] == x - d * int(x / d)


def test_concat_and_integer_to_string_casts():
    concat = project([op("CONCAT", col(0, "STRING"), lit("'foo'", "STRING"), t="STRING")])
    out = convert(concat, concat, types=("STRING", "INT", "OID"))
    assert "CONCAT(" in out["sql_a"]
    # NULL in, NULL out (MySQL's CONCAT, transpiled to DuckDB's ||, and CockroachDB's || agree)
    assert duck_rows("SELECT CONCAT(NULL, 'foo')") == [(None,)]
    assert "non-string" in skipped(project([op("CONCAT", col(0), lit("'x'", "STRING"), t="STRING")]), project([col(0)]))
    cast = project([op("CAST", col(0), t="STRING")])
    assert "CAST(t1.c0 AS CHAR)" in convert(cast, cast)["sql_a"]
    assert duck_rows("SELECT CAST(CAST(-12 AS BIGINT) AS CHAR)") == [("-12",)]
    # a decimal's text keeps its scale, which the stand-in does not
    dec = project([op("CAST", col(0, "DECIMAL"), t="STRING")])
    assert "scale" in skipped(dec, dec, types=("DECIMAL", "INT", "OID"))


def test_uuid_and_timestamptz_literals_are_converted_only_in_an_exact_spelling():
    uuid = "'37685f26-4b07-40ba-9bbf-42916ed9bc61'"
    eq = project([col(1)], filt(op("EQ", col(0, "UUID"), lit(uuid, "UUID"))))
    out = convert(eq, eq, types=("UUID", "INT", "OID"))
    assert f"= {uuid}" in out["sql_a"] and "t0_c0 VARCHAR(255)" in out["ddl"]
    shouting = lit(uuid.upper(), "UUID")
    assert "literal of type UUID" in skipped(project([col(1)], filt(op("EQ", col(0, "UUID"), shouting))), eq, types=("UUID", "INT", "OID"))
    # a UUID only meets a UUID
    mixed = project([col(1)], filt(op("EQ", col(0, "UUID"), lit("'x'", "STRING"))))
    assert "cross-type" in skipped(mixed, mixed, types=("UUID", "INT", "OID"))
    # a TIMESTAMPTZ literal is the UTC instant of its own offset
    stamp = lambda text: project([col(1)], filt(op("EQ", col(0, "TIMESTAMPTZ"), lit(text, "TIMESTAMPTZ"))))  # noqa: E731
    types = ("TIMESTAMPTZ", "INT", "OID")
    assert "TIMESTAMP '2020-04-11 06:25:41'" in convert(stamp("'2020-04-11 06:25:41+00'"), stamp("'2020-04-11 06:25:41+00'"), types=types)["sql_a"]
    assert "TIMESTAMP '2020-04-10 23:14:41'" in convert(stamp("'2020-04-11 06:25:41+07:11'"), stamp("'2020-04-11 06:25:41+07:11'"), types=types)["sql_a"]
    assert "TIMESTAMP '2020-04-11 09:25:41.500000'" in convert(stamp("'2020-04-11 06:25:41.5-03'"), stamp("'2020-04-11 06:25:41.5-03'"), types=types)["sql_a"]
    date_cast = project([col(1)], filt(op("EQ", col(0, "TIMESTAMPTZ"), op("CAST", lit("'2020-01-01'", "DATE"), t="TIMESTAMPTZ"))))
    assert "session time zone" in skipped(date_cast, date_cast, types=types)


def test_constraints_the_json_drops_are_recorded_not_skipped():
    out = convert(project([col(0)]), project([col(0)]), help_=("scan\n  check constraint expressions\n  computed column expressions",))
    assert out["dropped"] == ["CHECK constraint", "computed column"]
    assert "dropped" not in convert(project([col(0)]), project([col(0)]))


def test_set_operations_and_distinct():
    both = {"union": [project([col(0)]), project([col(0)])]}
    assert "UNION ALL" in convert(both, both)["sql_a"]
    assert "SELECT DISTINCT" in convert({"distinct": both}, both)["sql_a"]
    assert "EXCEPT" in convert({"except": [project([col(0)]), project([col(1)])]}, both)["sql_a"]


def test_opaque_column_ordinals_inside_an_apply_join_count_the_outer_columns():
    # Right side of an apply join: ordinals 0..2 are the left scan's columns, 3..5 the right scan's own.
    # Ordinal 2 is the LEFT scan's OID column; it used to be read as the right scan's own column 2, so a
    # projection of it passed silently and the pair was converted with the wrong column.
    scan = {"scan": 0}
    outer_oid = {"correlate": {"kind": "INNER", "left": scan, "right": project([col(3), col(2)])}}
    assert "(no exact encoding)" in skipped(project([col(0)], outer_oid), project([col(0)]))
    # Ordinal 3 is the right scan's first column (an INT) and is read as c0 of the right side.
    right_first = {"correlate": {"kind": "INNER", "left": scan, "right": project([col(3), col(4)])}}
    sql = convert(project([col(0)], right_first), project([col(0)]))["sql_a"]
    assert "t3.c0 AS c0, t3.c1 AS c1" in sql.replace("t2", "t3") or "t1.c0 AS c0, t1.c1 AS c1" in sql
