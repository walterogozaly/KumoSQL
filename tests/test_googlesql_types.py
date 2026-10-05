"""The GoogleSQL type checker's public API against behavior GoogleSQL documents.

Every case here is a rule of the GoogleSQL language (supertypes and literal coercion in ``docs/conversion_rules.md``,
name scoping and set operations in the query syntax reference), not the current eval score: a case holds whatever the
typer's coverage grows to. Where GoogleSQL fixes no type, or the query is invalid, a case asserts the typer *did not
guess*: an unknown output type (``None``) or unknown columns. The rule is "anything uncertain is unknown".

Each test builds its own catalog, so the file is safe to run in parallel.
"""

from types import SimpleNamespace

import pytest
import sqlglot
from sqlglot import exp

from kumosql.googlesql_types import (
    BIGNUMERIC,
    FINDING_CODES,
    INT64,
    NUMERIC,
    STRING,
    Catalog,
    Column,
    GType,
    StructField,
    infer,
    parse_type,
)

TABLES = {
    "t": {
        "a": "INT64", "b": "STRING", "c": "FLOAT64", "d": "DATE", "n": "NUMERIC", "i32": "INT32", "u32": "UINT32",
        "f32": "FLOAT32", "ts": "TIMESTAMP", "dt": "DATETIME", "arr": "ARRAY<INT64>",
        "st": "STRUCT<x INT64, y STRING>", "sa": "ARRAY<STRUCT<k STRING, v INT64>>",
    },
    "u": {"a": "INT64", "e": "STRING"},
    "s": {"a": "INT64", "b": "STRING"},
}


def _parses(sql: str) -> bool:
    try:
        sqlglot.parse_one(sql, read="bigquery")
        return True
    except Exception:  # noqa: BLE001
        return False


# BY NAME / CORRESPONDING with modes and ON / BY lists need a sqlglot that parses them (30 does, 26.0.0 does not); a
# query the installed sqlglot cannot parse has unknown columns, which test_unparsed_set_operation_syntax_is_unknown holds.
SET_OPERATION_SYNTAX = {
    "SELECT 1 AS x INNER UNION ALL BY NAME SELECT 1 AS x": None,
    "SELECT 1 AS x UNION ALL STRICT CORRESPONDING SELECT 1 AS x": None,
    "SELECT 1 AS x UNION ALL CORRESPONDING BY (x) SELECT 1 AS x": None,
    "SELECT 1 AS x UNION ALL BY NAME ON (x) SELECT 1 AS x": None,
}
SET_OPERATION_SYNTAX_PARSES = all(_parses(sql) for sql in SET_OPERATION_SYNTAX)
needs_set_operation_syntax = pytest.mark.skipif(
    not SET_OPERATION_SYNTAX_PARSES, reason="the installed sqlglot cannot parse BY NAME / CORRESPONDING modes and lists"
)


def catalog() -> Catalog:
    return Catalog.from_types(TABLES, complete=True)  # lists every table, so a missing one is a finding


def columns(sql: str, cat: Catalog | None = None):
    """``[(name, type text or None)]``, or None when the query's columns are unknown."""

    typed = infer(sql, cat or catalog())
    if typed.columns is None:
        return None
    return [(c.name, c.type.sql() if c.type is not None else None) for c in typed.columns]


def types(sql: str, cat: Catalog | None = None):
    got = columns(sql, cat)
    return None if got is None else [t for _, t in got]


def codes(sql: str, cat: Catalog | None = None) -> list[str]:
    return [f.code for f in infer(sql, cat or catalog()).findings]


# --- GType and parse_type ----------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text, printed",
    [
        ("INT64", "INT64"),
        ("integer", "INT64"),
        ("FLOAT", "FLOAT64"),  # a BigQuery schema's FLOAT is 64-bit; GoogleSQL's 32-bit float is FLOAT32
        ("float64", "FLOAT64"),
        ("FLOAT32", "FLOAT32"),
        ("decimal", "NUMERIC"),
        ("BIGDECIMAL", "BIGNUMERIC"),
        ("BOOLEAN", "BOOL"),
        ("STRING(10)", "STRING"),
        ("NUMERIC(10, 2)", "NUMERIC"),
        ("ARRAY<STRUCT<a INT64, b STRING>>", "ARRAY<STRUCT<a INT64, b STRING>>"),
        ("STRUCT<a INT64,b STRING>", "STRUCT<a INT64, b STRING>"),
        ("STRUCT<INT64, x STRING>", "STRUCT<INT64, x STRING>"),
        ("STRUCT<`my col` INT64>", "STRUCT<`my col` INT64>"),
        ("ARRAY<ARRAY<INT64>>", "ARRAY<ARRAY<INT64>>"),
        ("RANGE<DATE>", "RANGE<DATE>"),
    ],
)
def test_parse_type_reads_graph_spellings_and_prints_googlesql(text, printed):
    t = parse_type(text)
    assert t is not None and t.sql() == printed == str(t) and t.complete


@pytest.mark.parametrize("text", [None, "", "nonsense", "ARRAY<>", "ARRAY<INT64", "STRUCT", "INT64 junk", "RECORD"])
def test_parse_type_gives_none_for_text_that_is_not_a_type(text):
    assert parse_type(text) is None


def test_gtype_constructors_field_lookup_and_completeness():
    s = GType.struct([("a", INT64), (None, STRING)])
    assert s == GType("STRUCT", fields=(StructField("a", INT64), StructField(None, STRING)))
    assert s.sql() == "STRUCT<a INT64, STRING>"
    assert s.field("A") == INT64  # field names are case-insensitive
    assert s.field("missing") is None
    assert GType.struct([("a", INT64), ("A", STRING)]).field("a") is None  # ambiguous
    assert INT64.field("a") is None
    assert GType.array(INT64).sql() == "ARRAY<INT64>" and GType.range(GType("DATE")).sql() == "RANGE<DATE>"
    # an unknown part prints as ? and makes the type incomplete
    assert GType.array(None).sql() == "ARRAY<?>" and not GType.array(None).complete
    assert GType.struct([("a", None)]).sql() == "STRUCT<a ?>" and not GType.struct([("a", None)]).complete
    assert GType.array(GType.struct([("a", INT64)])).complete


# --- Catalog -----------------------------------------------------------------------------------------------------


def test_catalog_from_types_keeps_column_order_and_accepts_gtypes():
    cat = Catalog.from_types({"p.ds.t": [("b", "STRING"), ("a", INT64), ("w", "NOT A TYPE")]})
    assert cat.lookup("p.ds.t") == (Column("b", STRING), Column("a", INT64), Column("w", None))
    assert [c.name for c in cat.lookup("t")] == ["b", "a", "w"]
    assert list(cat.tables()) == ["p.ds.t"]


def test_catalog_finds_a_table_under_each_suffix_of_its_name_ignoring_case_and_quotes():
    cat = Catalog.from_types({"proj.ds.t": {"a": "INT64"}})
    for spelling in ("proj.ds.t", "ds.t", "t", "`proj.ds.t`", "PROJ.DS.T"):
        assert cat.lookup(spelling) == (Column("a", INT64),), spelling
    assert cat.lookup("other.t") is None
    assert cat.lookup("proj.ds") is None
    for sql in ("SELECT a FROM `proj.ds.t`", "SELECT a FROM proj.ds.t", "SELECT a FROM ds.t", "SELECT a FROM T"):
        assert types(sql, cat) == ["INT64"], sql


def test_catalog_spelling_two_tables_share_finds_neither():
    cat = Catalog.from_types({"ds1.u": {"x": "INT64"}, "ds2.u": {"y": "INT64"}})
    assert cat.lookup("u") is None
    assert cat.lookup("ds1.u") == (Column("x", INT64),)
    assert columns("SELECT * FROM ds1.u, ds2.u AS v", cat) == [("x", "INT64"), ("y", "INT64")]


def test_catalog_add_and_tables_copy():
    cat = Catalog()
    cat.add("`p.d.t`", [Column("a", INT64, required=True)])
    snapshot = cat.tables()
    snapshot.clear()
    assert cat.lookup("d.t") == (Column("a", INT64, True),)
    assert types("SELECT a FROM d.t", cat) == ["INT64"]


def test_catalog_from_fields_reads_bigquery_schema_modes():
    def field(name, type_, mode=None, fields=()):
        return SimpleNamespace(name=name, type=type_, mode=mode, fields=list(fields))

    cat = Catalog.from_fields({"t": [
        field("id", "INTEGER", "REQUIRED"),
        field("tags", "STRING", "REPEATED"),
        field("rec", "RECORD", "NULLABLE", [field("p", "FLOAT"), field("q", "DATE", "REPEATED")]),
        field("recs", "RECORD", "REPEATED", [field("p", "BOOLEAN")]),
        field("odd", "NOT_A_BIGQUERY_TYPE"),
    ]})
    by_name = {c.name: c for c in cat.lookup("t")}
    assert by_name["id"].type == INT64 and by_name["id"].required is True
    assert by_name["tags"].type == GType.array(STRING) and not by_name["tags"].required
    assert by_name["rec"].type.sql() == "STRUCT<p FLOAT64, q ARRAY<DATE>>"
    assert by_name["recs"].type.sql() == "ARRAY<STRUCT<p BOOL>>"
    assert by_name["odd"].type is None
    assert columns("SELECT rec.q, recs[OFFSET(0)].p, odd FROM t", cat) == [
        ("q", "ARRAY<DATE>"), ("p", "BOOL"), ("odd", None)]


def test_a_catalog_column_of_unknown_type_stays_unknown_beside_known_ones():
    cat = Catalog.from_types({"t": {"a": "WEIRD", "b": "INT64"}})
    assert columns("SELECT a, b, * FROM t", cat) == [("a", None), ("b", "INT64")] * 2


def test_user_defined_functions_have_unknown_types_even_with_a_builtin_name():
    cat = Catalog(functions=["MyUdf", "length"])
    cat.add("t", [Column("a", INT64), Column("b", STRING)])
    assert types("SELECT MYUDF(a), LENGTH(b), UPPER(b) FROM t", cat) == [None, None, "STRING"]
    plain = Catalog.from_types({"t": {"b": "STRING"}})
    assert types("SELECT LENGTH(b) FROM t", plain) == ["INT64"]


# --- infer: result object ----------------------------------------------------------------------------------------


def test_infer_accepts_a_parsed_tree_and_never_modifies_it():
    tree = sqlglot.parse_one("SELECT a + 1 AS z, b FROM t WHERE a > 1", read="bigquery")
    before = repr(tree)
    typed = infer(tree, catalog())
    assert repr(tree) == before and typed.tree is tree
    assert [(c.name, str(c.type)) for c in typed.columns] == [("z", "INT64"), ("b", "STRING")]
    assert typed.complete and typed.error is None and typed.findings == ()


def test_type_of_and_relation_answer_for_nodes_of_the_typed_tree():
    tree = sqlglot.parse_one("SELECT a + 1 AS z, b FROM t WHERE a > 1", read="bigquery")
    typed = infer(tree, catalog())
    assert str(typed.type_of(tree.find(exp.Add))) == "INT64"
    assert str(typed.type_of(tree.find(exp.GT))) == "BOOL"
    by_name = {c.name: c for c in tree.find_all(exp.Column)}
    assert str(typed.type_of(by_name["a"])) == "INT64"
    assert str(typed.type_of(by_name["b"])) == "STRING"
    table = tree.find(exp.Table)
    relation = typed.relation(table)
    assert [(c.name, str(c.type)) for c in relation][:2] == [("a", "INT64"), ("b", "STRING")]
    # a node of some other tree has no entry
    assert typed.type_of(exp.column("a")) is None and typed.relation(exp.to_table("t")) is None


def test_complete_means_every_output_column_has_a_complete_type():
    assert infer("SELECT 1 AS a", catalog()).complete
    assert not infer("SELECT foo() AS a", catalog()).complete
    assert not infer("SELECT * FROM nosuch", catalog()).complete


@pytest.mark.parametrize("sql", ["not sql at all ((", "", "INSERT INTO t VALUES (1)"])
def test_a_statement_that_is_not_a_query_has_no_columns_and_an_error(sql):
    typed = infer(sql, catalog())
    assert typed.columns is None and typed.error and not typed.complete


def test_infer_without_a_catalog_types_what_needs_no_table():
    typed = infer("SELECT 1 AS a, 'x' AS b")
    assert [(c.name, str(c.type)) for c in typed.columns] == [("a", "INT64"), ("b", "STRING")]
    missing = infer("SELECT * FROM t")
    assert missing.columns is None and missing.findings == ()  # nothing says the table is absent


def test_every_finding_code_is_declared_and_findings_carry_a_message():
    assert set(FINDING_CODES) == {
        "unknown_table", "unknown_column", "ambiguous_column", "incompatible_operands", "set_operation_width",
        "set_operation_type", "invalid_field_access", "no_matching_signature", "star_except_missing",
    }
    for sql in ("SELECT zz FROM t", "SELECT 1 UNION ALL SELECT 1, 2", "SELECT a + b FROM t", "SELECT * EXCEPT (q) FROM t"):
        for finding in infer(sql, catalog()).findings:
            assert finding.code in FINDING_CODES and finding.message


# --- literals, supertypes and coercion ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "select, expected",
    [
        ("1", "INT64"),
        ("1.5", "FLOAT64"),
        ("1e3", "FLOAT64"),
        ("'x'", "STRING"),
        ("b'x'", "BYTES"),
        ("TRUE", "BOOL"),
        ("NULL", "INT64"),  # an untyped NULL with nothing to coerce it to is INT64
        ("DATE '2020-01-01'", "DATE"),
        ("TIMESTAMP '2020-01-01'", "TIMESTAMP"),
        ("DATETIME '2020-01-01 00:00:00'", "DATETIME"),
        ("TIME '00:00:00'", "TIME"),
        ("NUMERIC '1.5'", "NUMERIC"),
        ("BIGNUMERIC '1'", "BIGNUMERIC"),
        ("INTERVAL 1 DAY", "INTERVAL"),
        ("JSON '{\"a\":1}'", "JSON"),
        ("[]", "ARRAY<INT64>"),  # an empty array literal with nothing to coerce it is ARRAY<INT64>
        ("[NULL]", "ARRAY<INT64>"),
        ("[1, 2]", "ARRAY<INT64>"),
        ("[1, 2.5]", "ARRAY<FLOAT64>"),
        ("[[1]]", "ARRAY<ARRAY<INT64>>"),
        ("ARRAY<STRING>[]", "ARRAY<STRING>"),
        ("STRUCT(1 AS a, 'x')", "STRUCT<a INT64, STRING>"),
        ("STRUCT<a FLOAT64>(1)", "STRUCT<a FLOAT64>"),
        ("CAST(a AS STRING)", "STRING"),
        ("SAFE_CAST(b AS INT64)", "INT64"),
        ("CAST(NULL AS DATE)", "DATE"),
    ],
)
def test_literal_and_cast_types(select, expected):
    assert types(f"SELECT {select} FROM t") == [expected]


@pytest.mark.parametrize(
    "select, expected",
    [
        # supertypes of the numeric types
        ("COALESCE(a, c)", "FLOAT64"),
        ("COALESCE(a, n)", "NUMERIC"),
        ("COALESCE(n, c)", "FLOAT64"),
        ("COALESCE(n, BIGNUMERIC '1')", "BIGNUMERIC"),
        ("COALESCE(i32, a)", "INT64"),
        ("COALESCE(u32, a)", "INT64"),
        ("COALESCE(u32, i32)", "INT64"),
        ("COALESCE(f32, c)", "FLOAT64"),
        ("IF(TRUE, a, c)", "FLOAT64"),
        ("CASE WHEN TRUE THEN a ELSE n END", "NUMERIC"),
        ("[a, c]", "ARRAY<FLOAT64>"),
        ("[a, n]", "ARRAY<NUMERIC>"),
        ("[i32, a]", "ARRAY<INT64>"),
        # an INT64 literal coerces to INT32, UINT32 and UINT64 columns, so the column's type wins
        ("COALESCE(i32, 1)", "INT32"),
        ("COALESCE(1, i32)", "INT32"),
        ("COALESCE(u32, 1)", "UINT32"),
        ("IF(TRUE, i32, 1)", "INT32"),
        ("CASE WHEN TRUE THEN i32 ELSE 1 END", "INT32"),
        ("[i32, 1]", "ARRAY<INT32>"),
        ("[u32, 1]", "ARRAY<UINT32>"),
        ("[i32, i32]", "ARRAY<INT32>"),
        # a floating point literal coerces to NUMERIC and BIGNUMERIC
        ("COALESCE(n, 1.5)", "NUMERIC"),
        ("COALESCE(1.5, n)", "NUMERIC"),
        ("COALESCE(n, 1)", "NUMERIC"),
        ("COALESCE(BIGNUMERIC '1', 1.5)", "BIGNUMERIC"),
        # a STRING literal coerces to DATE, DATETIME, TIME and TIMESTAMP columns; two literals stay STRING
        ("COALESCE(d, '2020-01-01')", "DATE"),
        ("COALESCE('2020-01-01', d)", "DATE"),
        ("COALESCE(ts, '2020-01-01 00:00:00')", "TIMESTAMP"),
        ("COALESCE(dt, '2020-01-01 00:00:00')", "DATETIME"),
        ("CASE WHEN TRUE THEN ts ELSE '2020-01-01' END", "TIMESTAMP"),
        ("[d, '2020-01-01']", "ARRAY<DATE>"),
        ("COALESCE('2020-01-01', '2020-01-02')", "STRING"),
        # NULL and [] take the type of what they meet
        ("COALESCE(NULL, NULL)", "INT64"),
        ("COALESCE(NULL, d)", "DATE"),
        ("COALESCE(NULL, 'x')", "STRING"),
        ("IF(TRUE, a, NULL)", "INT64"),
        ("[NULL, NULL]", "ARRAY<INT64>"),
        ("[NULL, 'x']", "ARRAY<STRING>"),
        ("COALESCE([], [1])", "ARRAY<INT64>"),
        # STRUCTs have a supertype field by field, and the field names must agree
        ("[STRUCT(1 AS a), STRUCT(2.5 AS a)]", "ARRAY<STRUCT<a FLOAT64>>"),
        ("COALESCE(STRUCT(1 AS a), STRUCT(2.5 AS a))", "STRUCT<a FLOAT64>"),
    ],
)
def test_supertypes_and_literal_coercion(select, expected):
    assert types(f"SELECT {select} FROM t") == [expected]


@pytest.mark.parametrize(
    "select",
    [
        "COALESCE(a, b)",  # INT64 and a STRING column have no supertype
        "COALESCE(d, b)",
        "COALESCE(a, 'x')",  # a STRING literal coerces only to the date and time types
        "COALESCE(TRUE, 1)",
        "COALESCE(b'x', 'y')",
        "[1, 'x']",
        "a + b",
        "foo(a)",  # a function the typer does not know
        "SUBSTR(a, 1)",  # no signature takes an INT64 first argument
        "(SELECT a, b FROM s)",  # a scalar subquery of two columns
    ],
)
def test_where_googlesql_fixes_no_type_the_typer_does_not_guess(select):
    assert types(f"SELECT {select} FROM t") == [None]


@pytest.mark.parametrize(
    "select, expected",
    [
        ("a + 1", "INT64"),
        ("a + 1.5", "FLOAT64"),
        ("a + c", "FLOAT64"),
        ("n + 1", "NUMERIC"),
        ("n + c", "FLOAT64"),
        ("i32 + a", "INT64"),
        ("i32 + 1", "INT64"),
        ("a = 1", "BOOL"),
        ("a > 1 AND b = 'x'", "BOOL"),
        ("a IS NULL", "BOOL"),
        ("a IN (1, 2)", "BOOL"),
        ("a BETWEEN 1 AND 2", "BOOL"),
        ("d = '2020-01-01'", "BOOL"),
        ("b LIKE 'x%'", "BOOL"),
        ("NOT TRUE", "BOOL"),
        ("a || 'x'", "STRING"),
        ("LENGTH(b)", "INT64"),
        ("UPPER(b)", "STRING"),
        ("CONCAT(b, 'x')", "STRING"),
        ("ABS(a)", "INT64"),
        ("ABS(c)", "FLOAT64"),
        ("COUNT(*)", "INT64"),
        ("SUM(a)", "INT64"),
        ("SUM(c)", "FLOAT64"),
        ("AVG(a)", "FLOAT64"),
        ("MAX(b)", "STRING"),
        ("ARRAY_AGG(a)", "ARRAY<INT64>"),
        ("ARRAY_AGG(STRUCT(a, b))", "ARRAY<STRUCT<a INT64, b STRING>>"),
        ("ARRAY_LENGTH(arr)", "INT64"),
        ("SPLIT(b, ',')", "ARRAY<STRING>"),
        ("GENERATE_ARRAY(1, 3)", "ARRAY<INT64>"),
        ("DATE_ADD(d, INTERVAL 1 DAY)", "DATE"),
        ("DATE_DIFF(d, d, DAY)", "INT64"),
        ("EXTRACT(YEAR FROM d)", "INT64"),
        ("d - d", "INTERVAL"),
        ("ROW_NUMBER() OVER (ORDER BY a)", "INT64"),
        ("LAG(b) OVER (ORDER BY a)", "STRING"),
        ("JSON_VALUE(JSON '{\"a\":1}', '$.a')", "STRING"),
        ("TO_JSON_STRING(a)", "STRING"),
    ],
)
def test_operator_and_function_result_types(select, expected):
    assert types(f"SELECT {select} FROM t") == [expected]


# --- names, scopes and star --------------------------------------------------------------------------------------


def test_output_column_names_are_the_alias_else_the_last_name_of_a_path():
    assert columns("SELECT a, t.b, st.x, 1 + 1, a + 1 AS z, sa[OFFSET(0)].k FROM t") == [
        ("a", "INT64"), ("b", "STRING"), ("x", "INT64"), (None, "INT64"), ("z", "INT64"), ("k", "STRING")]


def test_star_expands_in_table_order_and_except_replace_edit_it():
    assert [n for n, _ in columns("SELECT * FROM s")] == ["a", "b"]
    assert columns("SELECT * EXCEPT (a) FROM s") == [("b", "STRING")]
    assert columns("SELECT * EXCEPT (A) FROM s") == [("b", "STRING")]  # names match without regard to case
    assert columns("SELECT * REPLACE (CAST(a AS STRING) AS a) FROM s") == [("a", "STRING"), ("b", "STRING")]
    assert columns("SELECT * EXCEPT (a) REPLACE (1.5 AS b) FROM s") == [("b", "FLOAT64")]
    assert columns("SELECT s.* EXCEPT (a), u.* FROM s, u") == [("b", "STRING"), ("a", "INT64"), ("e", "STRING")]
    assert columns("SELECT * FROM s JOIN u ON s.a = u.a") == [
        ("a", "INT64"), ("b", "STRING"), ("a", "INT64"), ("e", "STRING")]


def test_a_range_variable_is_a_struct_of_its_columns_and_is_looked_up_before_columns():
    assert types("SELECT s FROM s") == ["STRUCT<a INT64, b STRING>"]
    assert types("SELECT x FROM s AS x") == ["STRUCT<a INT64, b STRING>"]
    # the table's alias hides its name, but a column named like the range variable loses to the range variable
    assert types("SELECT a FROM s AS a") == ["STRUCT<a INT64, b STRING>"]
    assert types("SELECT a.a FROM s AS a") == ["INT64"]
    assert types("SELECT t FROM (SELECT 1 AS t) AS t") == ["STRUCT<t INT64>"]
    assert types("SELECT t.t FROM (SELECT 1 AS t) AS t") == ["INT64"]
    assert types("SELECT c FROM (SELECT STRUCT(1 AS a) AS c)") == ["STRUCT<a INT64>"]
    assert types("SELECT c.a FROM (SELECT STRUCT(1 AS a) AS c)") == ["INT64"]
    # st is both a range variable and (inside it) no column: st.x asks the range variable, which has no x
    assert codes("SELECT st.x FROM t AS st") == ["unknown_column"]
    assert types("SELECT st.x FROM t") == ["INT64"]  # t is the range variable here; st is a STRUCT column


def test_a_table_alias_hides_the_table_name():
    assert codes("SELECT t.a FROM t AS t1") == ["unknown_column"]
    assert types("SELECT t1.a, t2.b FROM t AS t1 JOIN t AS t2 ON t1.a = t2.a") == ["INT64", "STRING"]


def test_select_list_aliases_are_not_visible_to_the_select_list_or_where():
    assert codes("SELECT a AS z, z FROM s") == ["unknown_column"]
    assert codes("SELECT a AS x FROM s WHERE x > 1") == ["unknown_column"]
    assert codes("SELECT a AS x FROM s ORDER BY x") == []
    assert codes("SELECT a AS x FROM s GROUP BY x") == []


def test_duplicate_output_names_are_allowed_but_ambiguous_to_reference():
    assert columns("SELECT 1 AS a, 'x' AS a") == [("a", "INT64"), ("a", "STRING")]
    assert columns("SELECT * FROM (SELECT 1 AS a, 2 AS a)") == [("a", "INT64"), ("a", "INT64")]
    assert codes("SELECT a FROM (SELECT 1 AS a, 2 AS a)") == ["ambiguous_column"]


def test_using_merges_the_column_and_keeps_both_qualified_names():
    assert types("SELECT a FROM s JOIN u USING (a)") == ["INT64"]
    assert codes("SELECT a FROM s JOIN u USING (a)") == []
    assert types("SELECT s.a, u.a FROM s JOIN u USING (a)") == ["INT64", "INT64"]
    # the USING column comes first in SELECT *, then each side's remaining columns
    assert columns("SELECT * FROM s JOIN u USING (a)") == [("a", "INT64"), ("b", "STRING"), ("e", "STRING")]
    assert columns("SELECT * FROM u FULL JOIN s USING (a)") == [("a", "INT64"), ("e", "STRING"), ("b", "STRING")]
    assert columns("SELECT * EXCEPT (e) FROM s JOIN u USING (a)") == [("a", "INT64"), ("b", "STRING")]
    assert columns("SELECT s.* FROM s JOIN u USING (a)") == [("a", "INT64"), ("b", "STRING")]
    # ON does not merge: the column is ambiguous
    assert codes("SELECT a FROM s JOIN u ON s.a = u.a") == ["ambiguous_column"]


def test_correlated_subqueries_see_the_outer_query_and_inner_names_shadow_it():
    assert types("SELECT (SELECT e FROM u WHERE u.a = s.a LIMIT 1) FROM s") == ["STRING"]
    assert types("SELECT (SELECT b) FROM s") == ["STRING"]  # b is the outer column
    assert types("SELECT (SELECT MAX(e) FROM u WHERE u.a = s.a) FROM s") == ["STRING"]
    assert types("SELECT ARRAY(SELECT AS STRUCT e, a FROM u WHERE u.a = s.a) FROM s") == ["ARRAY<STRUCT<e STRING, a INT64>>"]
    assert types("SELECT a FROM s WHERE EXISTS (SELECT 1 FROM u WHERE u.a = s.a AND u.e = s.b)") == ["INT64"]
    assert codes("SELECT a FROM s WHERE EXISTS (SELECT 1 FROM u WHERE u.a = s.a AND u.e = s.b)") == []
    # an inner range variable hides the outer one of the same name: u.b does not exist
    assert codes("SELECT 1 FROM s AS u WHERE EXISTS (SELECT 1 FROM u WHERE u.b = 'x')") == ["unknown_column"]
    # a name only the outer query has resolves outward, a name nobody has is unknown
    assert codes("SELECT (SELECT s.zz) FROM s") == ["unknown_column"]


def test_with_clauses_name_row_sources_for_later_ctes_and_the_query():
    assert columns("WITH c AS (SELECT 1 AS a, 'x' AS b) SELECT * FROM c") == [("a", "INT64"), ("b", "STRING")]
    assert columns("WITH c (p, q) AS (SELECT 1, 'x') SELECT * FROM c") == [("p", "INT64"), ("q", "STRING")]
    assert columns("WITH c AS (SELECT 1 AS a), d AS (SELECT a + 0.5 AS b FROM c) SELECT * FROM d") == [("b", "FLOAT64")]
    assert types("WITH c AS (SELECT 1 AS a) SELECT c FROM c") == ["STRUCT<a INT64>"]
    assert columns("WITH RECURSIVE r AS (SELECT 1 AS n UNION ALL SELECT n + 1 FROM r WHERE n < 3) SELECT * FROM r") == [
        ("n", "INT64")]


def test_unnest_with_offset_and_value_tables():
    assert columns("SELECT x FROM UNNEST([1, 2, 3]) AS x") == [("x", "INT64")]
    assert columns("SELECT x, off FROM UNNEST(['a', 'b']) AS x WITH OFFSET AS off") == [("x", "STRING"), ("off", "INT64")]
    assert columns("SELECT x, offset FROM UNNEST(['a']) AS x WITH OFFSET") == [("x", "STRING"), ("offset", "INT64")]
    assert columns("SELECT * FROM UNNEST([1, 2]) AS x WITH OFFSET AS pos") == [("x", "INT64"), ("pos", "INT64")]
    assert columns("SELECT x FROM UNNEST([1, 2.5]) AS x") == [("x", "FLOAT64")]
    assert columns("SELECT e FROM s, UNNEST([a, 2]) AS e") == [("e", "INT64")]
    # an array column of a range variable to the left
    assert columns("SELECT e FROM t, UNNEST(t.arr) AS e") == [("e", "INT64")]
    assert columns("SELECT e FROM t CROSS JOIN UNNEST(arr) e") == [("e", "INT64")]
    assert columns("SELECT e FROM t, t.arr AS e") == [("e", "INT64")]
    # an unnested array of structs: the range variable is the struct and its fields are reachable by name
    assert columns("SELECT e, e.k, k, v FROM t, UNNEST(sa) AS e") == [
        ("e", "STRUCT<k STRING, v INT64>"), ("k", "STRING"), ("k", "STRING"), ("v", "INT64")]
    assert columns("SELECT * FROM UNNEST([STRUCT(1 AS p, 'a' AS q)]) AS s") == [("p", "INT64"), ("q", "STRING")]
    assert columns("SELECT s, s.p, p FROM UNNEST([STRUCT(1 AS p, 'a' AS q)]) AS s") == [
        ("s", "STRUCT<p INT64, q STRING>"), ("p", "INT64"), ("p", "INT64")]


def test_a_type_keyword_used_as_a_field_name_is_not_typed():
    """sqlglot 26.0.0 reads ``j.array[0]`` as an array literal and drops ``j.``; the typer must not take that for ARRAY<INT64>."""

    typed = infer("SELECT j.array[0]['f'] FROM (SELECT JSON '{}' AS j)", catalog())
    assert typed.columns is None or typed.columns[0].type is None


def test_unnest_of_something_the_typer_cannot_type_has_unknown_columns():
    assert columns("SELECT * FROM UNNEST(NULL)") is None
    assert columns("SELECT * FROM UNNEST([])") == [(None, "INT64")]  # an empty array literal defaults to ARRAY<INT64>


def test_select_as_value_and_select_as_struct_make_value_tables():
    assert types("SELECT AS VALUE 1") == ["INT64"]
    assert types("SELECT AS VALUE STRUCT(1 AS a, 'x' AS b)") == ["STRUCT<a INT64, b STRING>"]
    assert types("SELECT (SELECT AS STRUCT 1 AS a, 'x' AS b)") == ["STRUCT<a INT64, b STRING>"]
    assert types("SELECT s FROM (SELECT AS STRUCT 1 AS a) AS s") == ["STRUCT<a INT64>"]
    # a value table of STRUCT: SELECT * gives its fields, the range variable its whole struct, and fields are reachable
    sub = "(SELECT AS VALUE STRUCT(1 AS a, 'x' AS b))"
    assert columns(f"SELECT * FROM {sub}") == [("a", "INT64"), ("b", "STRING")]
    assert columns(f"SELECT v FROM {sub} AS v") == [("v", "STRUCT<a INT64, b STRING>")]
    assert columns(f"SELECT v.a, b FROM {sub} AS v") == [("a", "INT64"), ("b", "STRING")]
    # a value table of a non-struct is one column named by its alias
    assert columns("SELECT v FROM (SELECT AS VALUE 1) AS v") == [("v", "INT64")]


# --- STRUCT and ARRAY access -------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "select, expected",
    [
        ("st.x", "INT64"),
        ("st.y", "STRING"),
        ("t.st.y", "STRING"),
        ("st.X", "INT64"),  # field names are case-insensitive
        ("arr[OFFSET(0)]", "INT64"),
        ("arr[SAFE_OFFSET(0)]", "INT64"),
        ("arr[ORDINAL(1)]", "INT64"),
        ("sa[OFFSET(0)].k", "STRING"),
        ("t.sa[SAFE_OFFSET(1)].v", "INT64"),
        ("[1, 2][OFFSET(0)]", "INT64"),
        ("STRUCT(1 AS q, 'x' AS r).r", "STRING"),
        ("STRUCT(STRUCT(1 AS z) AS q).q.z", "INT64"),
        ("(SELECT AS STRUCT 1 AS q).q", "INT64"),
    ],
)
def test_struct_and_array_access(select, expected):
    assert types(f"SELECT {select} FROM t") == [expected]
    assert codes(f"SELECT {select} FROM t") == []


# --- set operations ----------------------------------------------------------------------------------------------


def test_positional_set_operations_take_names_from_the_first_branch_and_the_supertype_of_each_position():
    assert columns("SELECT a, b FROM s UNION ALL SELECT a, b FROM s") == [("a", "INT64"), ("b", "STRING")]
    assert columns("SELECT a AS x FROM s UNION ALL SELECT a AS y FROM s") == [("x", "INT64")]
    assert columns("SELECT 1 AS x UNION DISTINCT SELECT 2.5") == [("x", "FLOAT64")]
    assert columns("SELECT 1 EXCEPT DISTINCT SELECT 1.5") == [(None, "FLOAT64")]
    assert columns("SELECT 1 INTERSECT DISTINCT SELECT 1.5") == [(None, "FLOAT64")]
    assert columns("SELECT 1 AS x, 'a' AS y UNION ALL SELECT 2.5, 'b'") == [("x", "FLOAT64"), ("y", "STRING")]
    assert columns("SELECT a FROM s UNION ALL (SELECT a FROM u INTERSECT DISTINCT SELECT a FROM s)") == [("a", "INT64")]
    assert columns("(SELECT a FROM s) UNION ALL (SELECT a FROM u)") == [("a", "INT64")]
    assert columns("WITH c AS (SELECT 1 AS x) SELECT x FROM c UNION ALL SELECT a FROM s") == [("x", "INT64")]


def test_n_ary_set_operations_have_one_supertype_over_every_branch():
    assert types("SELECT 1 UNION ALL SELECT 2 UNION ALL SELECT 3.5 UNION ALL SELECT 4") == ["FLOAT64"]
    assert types("SELECT 3.5 UNION ALL SELECT 1 UNION ALL SELECT 2") == ["FLOAT64"]
    assert types("SELECT a FROM s UNION ALL SELECT a FROM u UNION ALL SELECT a FROM s") == ["INT64"]
    assert types("SELECT NULL UNION ALL SELECT NULL UNION ALL SELECT 'x'") == ["STRING"]
    assert types("SELECT NULL UNION ALL SELECT NULL") == ["INT64"]
    assert types("SELECT [] UNION ALL SELECT ['x']") == ["ARRAY<STRING>"]
    assert types("SELECT STRUCT(1 AS a) AS x UNION ALL SELECT STRUCT(2.5 AS a)") == ["STRUCT<a FLOAT64>"]
    assert types("SELECT 1 AS x UNION ALL SELECT 2 AS x UNION ALL SELECT 'a'") == [None]


def test_set_operation_literals_coerce_to_the_other_branchs_column_type():
    assert types("SELECT i32 FROM t UNION ALL SELECT 1") == ["INT32"]
    assert types("SELECT 1 UNION ALL SELECT i32 FROM t") == ["INT32"]
    assert types("SELECT i32 FROM t UNION ALL SELECT a FROM t") == ["INT64"]
    assert types("SELECT d FROM t UNION ALL SELECT '2020-01-01'") == ["DATE"]
    assert types("SELECT '2020-01-01' UNION ALL SELECT d FROM t") == ["DATE"]
    assert types("SELECT n FROM t UNION ALL SELECT 1.5") == ["NUMERIC"]
    assert types("SELECT NULL UNION ALL SELECT 'x'") == ["STRING"]


def test_by_name_matches_columns_by_name_not_position_and_takes_the_first_branchs_order():
    assert columns("SELECT a, b FROM s UNION ALL BY NAME SELECT b, a FROM s") == [("a", "INT64"), ("b", "STRING")]
    assert columns("SELECT 1 AS x, 'a' AS y UNION ALL BY NAME SELECT 'b' AS y, 2.5 AS x") == [
        ("x", "FLOAT64"), ("y", "STRING")]
    assert columns("SELECT 1 AS b, 2 AS a UNION ALL BY NAME SELECT 2.5 AS A, 3 AS B") == [("b", "INT64"), ("a", "FLOAT64")]
    assert columns("SELECT 1 AS x INTERSECT DISTINCT BY NAME SELECT 1.5 AS x") == [("x", "FLOAT64")]
    assert columns("SELECT 1 AS x EXCEPT DISTINCT BY NAME SELECT 1.5 AS x") == [("x", "FLOAT64")]
    assert columns("SELECT 1 AS x UNION ALL BY NAME SELECT 2.5 AS x UNION ALL BY NAME SELECT 3 AS x") == [("x", "FLOAT64")]


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT a, b FROM s UNION ALL BY NAME SELECT b FROM s",  # BY NAME is strict: both need every name
        "SELECT 1 AS x UNION ALL BY NAME SELECT 2 AS y",
        "SELECT 1, 2 UNION ALL BY NAME SELECT 3, 4",  # unnamed columns cannot be matched
        pytest.param("SELECT a, b FROM s UNION ALL STRICT CORRESPONDING SELECT b FROM s", marks=needs_set_operation_syntax),
        pytest.param("SELECT 1 AS x, 'a' AS y UNION ALL STRICT CORRESPONDING SELECT 2.5 AS x", marks=needs_set_operation_syntax),
    ],
)
def test_by_name_with_columns_that_cannot_be_matched_is_unknown_not_guessed(sql):
    assert columns(sql) is None


@needs_set_operation_syntax
def test_corresponding_strict_matches_like_by_name():
    assert columns("SELECT a, b FROM s UNION ALL STRICT CORRESPONDING SELECT b, a FROM s") == [
        ("a", "INT64"), ("b", "STRING")]
    assert columns("SELECT 1 AS x, 'a' AS y UNION ALL STRICT CORRESPONDING SELECT 2.5 AS x, 'b' AS y") == [
        ("x", "FLOAT64"), ("y", "STRING")]


@needs_set_operation_syntax
def test_corresponding_without_a_mode_keeps_the_columns_every_branch_has():
    assert columns("SELECT a, b FROM s UNION ALL CORRESPONDING SELECT b, a FROM s") == [("a", "INT64"), ("b", "STRING")]
    assert columns("SELECT 1 AS x, 'a' AS y UNION ALL CORRESPONDING SELECT 2.5 AS x, 1 AS z") == [("x", "FLOAT64")]


@needs_set_operation_syntax
def test_set_operation_modes_inner_left_and_full():
    first, second = "SELECT 1 AS x, 'a' AS y", "SELECT 2.5 AS x, 1 AS z"
    assert columns(f"{first} INNER UNION ALL CORRESPONDING {second}") == [("x", "FLOAT64")]
    assert columns(f"{first} INNER UNION ALL BY NAME {second}") == [("x", "FLOAT64")]
    assert columns(f"{first} LEFT UNION ALL CORRESPONDING {second}") == [("x", "FLOAT64"), ("y", "STRING")]
    assert columns(f"{first} LEFT OUTER UNION ALL BY NAME {second}") == [("x", "FLOAT64"), ("y", "STRING")]
    # FULL keeps every name, the first branch's first; a column a branch lacks is NULL there
    assert columns(f"{first} FULL UNION ALL CORRESPONDING {second}") == [
        ("x", "FLOAT64"), ("y", "STRING"), ("z", "INT64")]
    assert columns(f"{first} FULL OUTER UNION ALL BY NAME {second}") == [
        ("x", "FLOAT64"), ("y", "STRING"), ("z", "INT64")]


@needs_set_operation_syntax
def test_mode_set_operations_chain_n_ary():
    assert columns("SELECT 1 AS x FULL UNION ALL BY NAME SELECT 2 AS y FULL UNION ALL BY NAME SELECT 3.5 AS x") == [
        ("x", "FLOAT64"), ("y", "INT64")]
    assert columns(
        "SELECT 1 AS x, 'a' AS y FULL UNION ALL CORRESPONDING SELECT 2.5 AS x, 1 AS z "
        "FULL UNION ALL CORRESPONDING SELECT 'q' AS w") == [
        ("x", "FLOAT64"), ("y", "STRING"), ("z", "INT64"), ("w", "STRING")]
    assert columns(
        "SELECT 1 AS x, 2 AS y INNER UNION ALL BY NAME SELECT 2.5 AS y, 3 AS x INNER UNION ALL BY NAME SELECT 3 AS x"
    ) == [("x", "INT64")]


@needs_set_operation_syntax
def test_on_and_by_lists_choose_the_output_columns_their_order_and_their_spelling():
    assert columns("SELECT 1 AS x, 'a' AS y UNION ALL CORRESPONDING BY (y) SELECT 2.5 AS x, 'b' AS y") == [
        ("y", "STRING")]
    assert columns("SELECT 1 AS x, 'a' AS y UNION ALL BY NAME ON (y, x) SELECT 2.5 AS x, 'b' AS y") == [
        ("y", "STRING"), ("x", "FLOAT64")]
    # the output name is spelled as in the list, though the match ignores case
    assert columns("SELECT 1 AS x, 'a' AS y FULL UNION ALL BY NAME ON (Y) SELECT 2.5 AS x, 'b' AS y") == [
        ("Y", "STRING")]
    assert columns("SELECT 1 AS b, 2 AS a, 5 AS z INNER UNION ALL BY NAME ON (B, a) SELECT 6 AS y, 3 AS b, 4 AS a") == [
        ("B", "INT64"), ("a", "INT64")]
    # in LEFT and FULL modes a name only some branches have is allowed in the list
    assert columns("SELECT 1 AS x FULL UNION ALL BY NAME ON (X, z) SELECT 2 AS x, 'a' AS z") == [
        ("X", "INT64"), ("z", "STRING")]
    assert columns("SELECT 1 AS x, 'b' AS y LEFT UNION ALL BY NAME ON (y, x) SELECT DATE '2020-01-01' AS y") == [
        ("y", "DATE"), ("x", "INT64")]


@needs_set_operation_syntax
def test_a_name_in_an_on_list_that_a_strict_branch_lacks_is_unknown_not_guessed():
    assert columns("SELECT 1 AS x UNION ALL BY NAME ON (x, z) SELECT 2 AS x") is None


# --- PIVOT and UNPIVOT -------------------------------------------------------------------------------------------

SRC = "(SELECT 'x' AS k, 1 AS m, 2 AS g)"


def test_pivot_keeps_the_ungrouped_columns_then_one_column_per_value():
    assert columns(f"SELECT * FROM {SRC} PIVOT(SUM(m) FOR k IN ('x', 'y'))") == [
        ("g", "INT64"), ("x", "INT64"), ("y", "INT64")]
    assert columns(f"SELECT * FROM {SRC} AS src PIVOT(SUM(m) FOR k IN ('x', 'y')) AS p") == [
        ("g", "INT64"), ("x", "INT64"), ("y", "INT64")]
    assert columns(f"SELECT p.x FROM {SRC} AS src PIVOT(SUM(m) FOR k IN ('x', 'y')) AS p") == [("x", "INT64")]
    assert types("SELECT * FROM (SELECT 'x' AS k, 1.5 AS m, 2 AS g) PIVOT(AVG(m) FOR k IN ('x'))") == ["INT64", "FLOAT64"]


def test_pivot_names_columns_by_aggregate_alias_then_value_alias_value_major():
    assert columns(f"SELECT * FROM {SRC} PIVOT(SUM(m) AS s, COUNT(*) AS c FOR k IN ('x', 'y' AS why))") == [
        ("g", "INT64"), ("s_x", "INT64"), ("c_x", "INT64"), ("s_why", "INT64"), ("c_why", "INT64")]


def test_unpivot_gives_the_value_and_name_columns_after_the_untouched_ones():
    src = "(SELECT 1 AS id, 10 AS q1, 20 AS q2)"
    assert columns(f"SELECT * FROM {src} UNPIVOT(v FOR q IN (q1, q2))") == [
        ("id", "INT64"), ("v", "INT64"), ("q", "STRING")]
    assert columns(f"SELECT * FROM {src} UNPIVOT(v FOR q IN (q1 AS 'first', q2 AS 'second'))") == [
        ("id", "INT64"), ("v", "INT64"), ("q", "STRING")]
    assert columns(f"SELECT * FROM {src} UNPIVOT INCLUDE NULLS (v FOR q IN (q1, q2))") == [
        ("id", "INT64"), ("v", "INT64"), ("q", "STRING")]


def test_multi_column_unpivot_has_one_value_column_per_name():
    src = "(SELECT 1 AS id, 10 AS a1, 20 AS b1, 30 AS a2, 40 AS b2)"
    assert columns(f"SELECT * FROM {src} UNPIVOT((x, y) FOR n IN ((a1, b1) AS 'one', (a2, b2) AS 'two'))") == [
        ("id", "INT64"), ("x", "INT64"), ("y", "INT64"), ("n", "STRING")]


def test_unpivot_over_columns_of_different_types_has_an_unknown_value_type():
    src = "(SELECT 1 AS id, 10 AS a1, 20 AS b1, 'x' AS a2, 'y' AS b2)"
    assert columns(f"SELECT * FROM {src} UNPIVOT((x, y) FOR n IN ((a1, b1) AS 'one', (a2, b2) AS 'two'))") == [
        ("id", "INT64"), ("x", None), ("y", None), ("n", "STRING")]
    assert columns("SELECT * FROM (SELECT 1 AS a, 'x' AS b) UNPIVOT(v FOR q IN (a, b))") == [
        ("v", None), ("q", "STRING")]


def test_pivot_over_an_unknown_table_has_unknown_columns():
    assert columns("SELECT * FROM nosuch PIVOT(SUM(m) FOR k IN ('x'))") is None


# --- findings ----------------------------------------------------------------------------------------------------


def test_unknown_table_only_when_the_catalog_lacks_it():
    typed = infer("SELECT a FROM nosuch", catalog())
    assert [f.code for f in typed.findings] == ["unknown_table"]
    assert typed.columns is not None and typed.columns[0].type is None  # the column's type is not guessed
    assert codes("SELECT a FROM s") == []
    assert codes("SELECT * FROM nosuch1 JOIN nosuch2 USING (a)") == ["unknown_table", "unknown_table"]
    assert codes("WITH nosuch AS (SELECT 1 AS a) SELECT a FROM nosuch") == []  # a CTE is not a catalog table


def test_columns_of_a_table_that_is_not_in_the_catalog_are_not_reported_missing():
    assert codes("SELECT zz FROM nosuch") == ["unknown_table"]
    assert codes("SELECT zz FROM s, nosuch") == ["unknown_table"]
    assert columns("SELECT * FROM nosuch") is None
    assert columns("SELECT x.* FROM nosuch AS x") is None
    assert columns("SELECT * EXCEPT (a) FROM nosuch") is None
    assert columns("SELECT a FROM s, nosuch") == [("a", None)]  # a may be nosuch's too: unknown, not INT64


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT zz FROM s",
        "SELECT s.zz FROM s",
        "SELECT zz.a FROM s",
        "SELECT a FROM s WHERE zz = 1",
        "SELECT s.a FROM s JOIN u ON s.a = u.zz",
        "SELECT a FROM s WHERE a IN (SELECT zz FROM u)",
        "SELECT x.a FROM (SELECT a FROM s) AS x WHERE x.b = 'x'",
        "SELECT u.a FROM s",
        "SELECT t.a FROM t AS t1",
        "SELECT s.a FROM s, UNNEST(s.zz) AS q",
        "SELECT (SELECT zz) FROM s",
    ],
)
def test_unknown_column(sql):
    assert "unknown_column" in codes(sql)


def test_unknown_column_leaves_the_type_unknown():
    assert columns("SELECT zz FROM s") == [("zz", None)]
    assert columns("SELECT a, zz FROM s") == [("a", "INT64"), ("zz", None)]


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT a FROM s, u",
        "SELECT a FROM s AS x JOIN s AS y ON x.a = y.a",
        "SELECT a FROM s JOIN u ON s.a = u.a",
        "SELECT a FROM (SELECT 1 AS a, 2 AS a)",
        "SELECT a.b FROM (SELECT STRUCT(1 AS b) AS a) AS q, t",  # q has a column a, t has a column a
    ],
)
def test_ambiguous_column(sql):
    typed = infer(sql, catalog())
    assert "ambiguous_column" in [f.code for f in typed.findings]
    assert typed.columns is not None and typed.columns[0].type is None


def test_a_name_with_a_single_owner_is_not_ambiguous():
    assert codes("SELECT a, b, e FROM s, u") == ["ambiguous_column"]  # a is in both
    assert codes("SELECT b, e FROM s, u") == []
    assert codes("SELECT s.a, u.a FROM s, u") == []
    assert codes("SELECT a FROM s WHERE EXISTS (SELECT 1 FROM u WHERE u.a = s.a)") == []


def test_set_operation_width():
    for sql in (
        "SELECT 1 UNION ALL SELECT 1, 2",
        "SELECT 1 INTERSECT DISTINCT SELECT 2, 3",
        "SELECT 1, 2 UNION ALL SELECT 1, 2 UNION ALL SELECT 1",
        "SELECT a FROM s UNION ALL SELECT a, b FROM s",
        "SELECT 1 AS a UNION ALL SELECT 2 AS b UNION ALL SELECT 3 AS c, 4",
    ):
        typed = infer(sql, catalog())
        assert [f.code for f in typed.findings] == ["set_operation_width"], sql
        assert typed.columns is None, sql
    assert codes("SELECT 1, 2 UNION ALL SELECT 3, 4") == []


def test_set_operation_type_when_the_branches_have_no_common_type():
    for sql in (
        "SELECT 1 AS x UNION ALL SELECT DATE '2020-01-01'",
        "SELECT TRUE UNION ALL SELECT 1",
        "SELECT b FROM s UNION ALL SELECT a FROM s",
        "SELECT 1 UNION ALL SELECT DATE '2020-01-01' UNION ALL SELECT 2",
        "SELECT 1 AS x UNION ALL BY NAME SELECT DATE '2020-01-01' AS x",
    ):
        typed = infer(sql, catalog())
        assert "set_operation_type" in [f.code for f in typed.findings], sql
        assert typed.columns[0].type is None, sql  # the column exists; its type is not guessed
    assert codes("SELECT 1 UNION ALL SELECT 2.5") == []


@needs_set_operation_syntax
def test_set_operation_type_for_corresponding():
    typed = infer("SELECT 1 AS x UNION ALL CORRESPONDING SELECT TRUE AS x", catalog())
    assert [f.code for f in typed.findings] == ["set_operation_type"]
    assert typed.columns[0].type is None


@pytest.mark.parametrize("sql", list(SET_OPERATION_SYNTAX))
def test_unparsed_set_operation_syntax_is_unknown(sql):
    """A sqlglot that cannot parse a mode or list gives a parse error and unknown columns, never a guess."""

    typed = infer(sql, catalog())
    if _parses(sql):
        assert typed.columns is not None and typed.error is None
    else:
        assert typed.columns is None and typed.error.startswith("parse error")


def test_set_operation_branches_that_may_be_coercible_are_not_reported():
    assert codes("SELECT a FROM s UNION ALL SELECT 1.5") == []
    assert codes("SELECT d FROM t UNION ALL SELECT '2020-01-01'") == []
    assert codes("SELECT NULL UNION ALL SELECT 'x'") == []
    assert codes("SELECT i32 FROM t UNION ALL SELECT 1") == []


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT st.z FROM t",  # no such field
        "SELECT STRUCT(1 AS q).z",
        "SELECT STRUCT(1, 'x').f",
        "SELECT STRUCT(STRUCT(1 AS z) AS q).q.y",
        "SELECT st.x.y FROM t",  # INT64 has no fields
        "SELECT b.z FROM t",
        "SELECT d.z FROM t",
        "SELECT sa[OFFSET(0)].zz FROM t",
        "SELECT sa[OFFSET(0)].k.z FROM t",
        "SELECT t.b.zz FROM t",
    ],
)
def test_invalid_field_access(sql):
    typed = infer(sql, catalog())
    assert [f.code for f in typed.findings] == ["invalid_field_access"], sql
    assert typed.columns[0].type is None


def test_invalid_field_access_is_found_outside_the_select_list_too():
    assert codes("SELECT x FROM (SELECT 1 AS x) WHERE x.y = 1") == ["invalid_field_access"]
    assert types("SELECT x FROM (SELECT 1 AS x) WHERE x.y = 1") == ["INT64"]


def test_field_access_on_json_and_on_values_of_unknown_type_is_not_reported():
    assert codes("SELECT JSON '{}'.a, PARSE_JSON('{}').a.b") == []
    assert codes("SELECT foo(a).anything FROM t") == []
    assert codes("SELECT arr.x FROM t") in ([], ["invalid_field_access"])  # an array has no fields; either is sound
    assert types("SELECT arr.x FROM t") == [None]


def test_star_except_missing():
    for sql in (
        "SELECT * EXCEPT (zz) FROM s",
        "SELECT * EXCEPT (a, zz) FROM s",
        "SELECT s.* EXCEPT (zz) FROM s",
        "SELECT * REPLACE (1 AS zz) FROM s",
    ):
        typed = infer(sql, catalog())
        assert [f.code for f in typed.findings] == ["star_except_missing"], sql
        assert typed.columns is None, sql
    assert codes("SELECT * EXCEPT (a) FROM s") == []
    assert codes("SELECT * REPLACE ('x' AS B) FROM s") == []


def test_incompatible_operands_and_no_matching_signature_are_never_given_a_type():
    # GoogleSQL rejects these (no signature takes an INT64 and a STRING); whether the typer reports them with a
    # finding or not, it must leave the type unknown rather than pick one.
    for sql in ("SELECT a + b FROM t", "SELECT a - b FROM t", "SELECT SUBSTR(a, 1) FROM t", "SELECT ABS(b) FROM t"):
        typed = infer(sql, catalog())
        assert typed.columns is not None and typed.columns[0].type is None, sql
        assert {f.code for f in typed.findings} <= {"incompatible_operands", "no_matching_signature"}, sql


def test_valid_queries_have_no_findings():
    for sql in (
        "SELECT a, b FROM s",
        "SELECT t.* FROM t",
        "SELECT a + 1, b || 'x' FROM s ORDER BY 1",
        "SELECT e FROM t, UNNEST(sa) AS e WHERE e.v > 1",
        "SELECT a, COUNT(*) AS n FROM s GROUP BY a HAVING COUNT(*) > 1",
        "SELECT * FROM s JOIN u USING (a) WHERE e = 'x'",
        "WITH c AS (SELECT a FROM s) SELECT c.a FROM c JOIN u USING (a)",
    ):
        assert codes(sql) == [], sql


# --- uncertain constructs stay unknown ---------------------------------------------------------------------------


def test_constructs_the_typer_does_not_model_give_unknown_columns_or_types_never_a_guess():
    assert columns("SELECT * FROM nosuch") is None
    assert columns("SELECT * FROM s, nosuch") is None
    assert columns("SELECT foo(a) FROM s") == [(None, None)]
    assert columns("SELECT (SELECT a, b FROM s)") == [(None, None)]
    # a table function's columns are not known
    assert columns("SELECT * FROM my_table_function(1)") is None
    # STRUCT/ARRAY typed partly: an unknown field makes the whole type unknown rather than a partial one
    assert columns("SELECT STRUCT(1 AS a, foo(a) AS b) FROM s") == [(None, None)]
    assert columns("SELECT [foo(a)] FROM s") == [(None, None)]
    assert columns("SELECT ARRAY(SELECT foo(a) FROM s)") == [(None, None)]


def test_struct_supertype_takes_field_names_from_the_first_argument():
    assert types("SELECT COALESCE(STRUCT(1 AS a), STRUCT(2.5 AS b)) FROM t") == ["STRUCT<a FLOAT64>"]
    assert types("SELECT COALESCE(STRUCT(1 AS a), STRUCT(2.5)) FROM t") == ["STRUCT<a FLOAT64>"]
