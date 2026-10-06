"""Script variables, data-neutral statements and statement splitting in incremental models."""

import pytest

pytest.importorskip("duckdb")

import sqlglot  # noqa: E402

from kumosql.incremental import (  # noqa: E402
    IncrementalError,
    SourceTable,
    _script,
    _strip_old_bounds,
    check_incremental,
    effective_model,
    modelled_exactly,
    neutral_statement,
    parse_incremental_sqlx,
    replay,
    split_statements,
    variable_statement,
)

EVENTS = {
    "events": SourceTable(
        {"id": "INT64", "ts": "TIMESTAMP", "v": "INT64"}, ("id",), "ts"
    )
}
EPOCH = "TIMESTAMP '1970-01-01'"
QUERY = 'SELECT id, ts, v FROM ${ref("events")}'


def sqlx(
    pre: str, where: str = "WHERE ts > wm", config: str = "", post: str = ""
) -> str:
    after = f"post_operations {{\n{post}\n}}\n" if post else ""
    return f'config {{ type: "incremental"{config} }}\npre_operations {{\n{pre}\n}}\n{after}{QUERY}\n{where}'


def model(pre: str, **options):
    return parse_incremental_sqlx(sqlx(pre, **options), "m")


def insert(i, hour):
    return f"INSERT INTO events (id, ts, v) VALUES ({i}, TIMESTAMP '2024-01-01 {hour:02d}:00:00', 1)"


# --- splitting statements -----------------------------------------------------------------------------


SPLITS = {
    "semicolons": ("SELECT 1; SELECT 2;", ["SELECT 1", "SELECT 2"]),
    "no trailing semicolon": ("SELECT 1; SELECT 2", ["SELECT 1", "SELECT 2"]),
    "SQLX --- on its own line": (
        "SELECT 1\n---\nSELECT 2",
        ["SELECT 1", "SELECT 2"],
    ),
    "--- with indentation": (
        "SELECT 1\n  ---  \nSELECT 2",
        ["SELECT 1", "SELECT 2"],
    ),
    "both separators": (
        "SELECT 1;\n---\nSELECT 2; SELECT 3",
        ["SELECT 1", "SELECT 2", "SELECT 3"],
    ),
    "a semicolon in a string": (
        "SELECT ';' AS s; SELECT 2",
        ["SELECT ';' AS s", "SELECT 2"],
    ),
    "a semicolon in quotes and backticks": (
        'SELECT "a;b", `c;d`; SELECT 2',
        ['SELECT "a;b", `c;d`', "SELECT 2"],
    ),
    "an escaped quote": (
        "SELECT 'it\\'s; ok'; SELECT 2",
        ["SELECT 'it\\'s; ok'", "SELECT 2"],
    ),
    "a semicolon in brackets": (
        "SELECT [1, 2]; SELECT (1; 2)",
        ["SELECT [1, 2]", "SELECT (1; 2)"],
    ),
    "a semicolon in a line comment": (
        "SELECT 1 -- a; b\n; SELECT 2",
        ["SELECT 1 -- a; b", "SELECT 2"],
    ),
    "a semicolon in a block comment": (
        "SELECT 1 /* a; b */; SELECT 2",
        ["SELECT 1 /* a; b */", "SELECT 2"],
    ),
    "comment-only statements are dropped": (
        "-- nothing\n; /* nothing */ ; SELECT 1;",
        ["SELECT 1"],
    ),
    "empty": ("  \n ", []),
}


@pytest.mark.parametrize("text, expected", SPLITS.values(), ids=list(SPLITS))
def test_split_statements(text, expected):
    assert split_statements(text) == expected


def test_the_sqlx_separator_is_a_line_of_its_own():
    assert split_statements("SELECT 1\n---\nSELECT 2") == ["SELECT 1", "SELECT 2"]
    assert split_statements("SELECT 1 --- a note") == ["SELECT 1 --- a note"]


# --- data-neutral statements --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "statement",
    [
        "GRANT SELECT ON TABLE t TO 'user:a@example.com'",
        "grant `roles/bigquery.dataViewer` on table t to 'group:g@example.com'",
        "REVOKE SELECT ON TABLE t FROM 'user:a@example.com'",
        "ALTER TABLE t SET OPTIONS (description = 'a;b')",
        "ALTER TABLE IF EXISTS d.t SET OPTIONS (labels = [('k', 'v')])",
        "ALTER TABLE t ALTER COLUMN c SET OPTIONS (description = 'x')",
        "ALTER VIEW v SET OPTIONS (expiration_timestamp = NULL)",
        "ALTER SCHEMA s SET OPTIONS (default_table_expiration_days = 3)",
        "-- note\nGRANT SELECT ON TABLE t TO 'x'",
    ],
)
def test_statements_that_cannot_change_rows_are_neutral(statement):
    assert neutral_statement(statement)


@pytest.mark.parametrize(
    "statement",
    [
        "DELETE FROM t WHERE TRUE",
        "INSERT INTO t (a) VALUES (1)",
        "UPDATE t SET a = 1 WHERE TRUE",
        "TRUNCATE TABLE t",
        "DROP TABLE t",
        "ALTER TABLE t ADD COLUMN c INT64",
        "ALTER TABLE t ALTER COLUMN c SET DATA TYPE NUMERIC",
        "ALTER TABLE t SET OPTIONS (a = 1), ALTER COLUMN c SET DATA TYPE INT64",
        "ALTER TABLE t SET OPTIONS (a = 1); DROP TABLE t",
        "CREATE OR REPLACE TABLE t AS SELECT 1",
        "SELECT 1",
        "DECLARE a INT64",
    ],
)
def test_statements_that_can_change_rows_are_not_neutral(statement):
    assert not neutral_statement(statement)


def test_neutral_statements_are_dropped_from_the_models_pre_operations():
    pre = "GRANT SELECT ON ${self()} TO 'x';\nALTER TABLE ${self()} SET OPTIONS (description = 'd');\nDECLARE wm DEFAULT 1"
    m = model(pre, where="")
    assert m.pre_operations == ("DECLARE wm DEFAULT 1",) and m.unmodelled == ()
    assert modelled_exactly(m)


def test_a_data_changing_statement_is_kept_on_incremental_runs_and_unmodelled_on_full_builds():
    m = model("DELETE FROM ${self()} WHERE v > 5", where="")
    assert (
        m.pre_operations == ("DELETE FROM m WHERE v > 5",)
        and m.full_pre_operations == ()
    )
    assert m.unmodelled == ("pre_operations that change data on full builds",)
    only_incremental = model(
        "${when(incremental(), `DELETE FROM ${self()} WHERE v > 5`)}", where=""
    )
    assert (
        only_incremental.pre_operations == ("DELETE FROM m WHERE v > 5",)
        and only_incremental.unmodelled == ()
    )


def test_post_operations_may_grant_or_declare_but_not_change_data():
    assert (
        model(
            "DECLARE a INT64",
            where="",
            post="GRANT SELECT ON ${self()} TO 'x'; DECLARE b INT64",
        ).unmodelled
        == ()
    )
    changing = model(
        "DECLARE a INT64",
        where="",
        post="GRANT SELECT ON ${self()} TO 'x'; DELETE FROM ${self()} WHERE TRUE",
    )
    assert changing.unmodelled == ("post_operations that change data",)
    assert check_incremental(changing, EVENTS, {"insert_new"}).outcome == "unsupported"


def test_pre_operations_split_on_the_sqlx_separator():
    m = model("DECLARE a INT64 DEFAULT 1\n---\nSET a = 2", where="")
    assert m.pre_operations == ("DECLARE a INT64 DEFAULT 1", "SET a = 2")
    assert m.full_pre_operations == m.pre_operations


# --- variable statements ------------------------------------------------------------------------------


def test_variable_statement_reads_declare_and_set():
    declared = variable_statement("DECLARE wm TIMESTAMP DEFAULT TIMESTAMP '1970-01-01'")
    assert (declared.verb, declared.names, declared.kind) == (
        "declare",
        ("wm",),
        "TIMESTAMP",
    )
    assert (
        declared.value.sql() == "CAST('1970-01-01' AS TIMESTAMP)"
        or "1970-01-01" in declared.value.sql()
    )
    several = variable_statement("DECLARE a, b DEFAULT 2")
    assert several.names == ("a", "b") and several.kind is None
    assert variable_statement("DECLARE a INT64").value is None  # declares NULL
    assert variable_statement("SET a = a + 1").verb == "set"
    assert variable_statement("-- c\nDECLARE A DEFAULT 1").names == ("a",)


@pytest.mark.parametrize(
    "statement",
    ["SELECT 1", "DELETE FROM t WHERE TRUE", "SET (a, b) = (1, 2)", "DECLARE ("],
)
def test_other_statements_are_not_variable_statements(statement):
    assert variable_statement(statement) is None


# --- substituting variables: effective_model -----------------------------------------------------------


def test_a_variable_is_replaced_by_its_definition():
    m = model(
        f"DECLARE wm DEFAULT (SELECT COALESCE(MAX(ts), {EPOCH}) FROM ${{self()}})"
    )
    effective = effective_model(m)
    assert "wm" not in effective.incremental_sql.replace("WHERE", "")
    assert "COALESCE((SELECT MAX(ts) FROM m)" in effective.incremental_sql
    assert effective.pre_operations == () and effective.full_pre_operations == ()


def test_the_full_build_reads_its_own_definition():
    pre = f"DECLARE wm DEFAULT (${{when(incremental(), `SELECT COALESCE(MAX(ts), {EPOCH}) FROM ${{self()}}`, `SELECT {EPOCH}`)}})"
    m = model(pre)
    assert m.pre_operations != m.full_pre_operations
    effective = effective_model(m)
    assert "FROM m" in effective.incremental_sql and "FROM m" not in effective.full_sql
    assert "1970-01-01" in effective.full_sql


def test_a_declared_type_becomes_a_cast_and_set_overrides_declare():
    assert (
        "CAST('2000-01-01' AS TIMESTAMP)"
        in effective_model(
            model(
                f"DECLARE wm TIMESTAMP DEFAULT {EPOCH};\nSET wm = TIMESTAMP '2000-01-01'"
            )
        ).full_sql
    )
    chain = effective_model(
        model(
            "DECLARE a INT64 DEFAULT 1;\nDECLARE b DEFAULT a + 1;\nSET a = 10",
            where="WHERE v > b",
        )
    )
    assert (
        "CAST(1 AS INT64) + 1" in chain.incremental_sql
        and "10" not in chain.incremental_sql
    )  # b was read before a changed


UNSUBSTITUTABLE = {
    "a clock value": "DECLARE wm DEFAULT CURRENT_TIMESTAMP()",
    "a random value": "DECLARE wm DEFAULT RAND()",
    "a table the next statement writes": "DECLARE wm DEFAULT (SELECT MAX(ts) FROM ${self()});\nINSERT INTO ${self()} (id) VALUES (1)",
    "a delete of the window it measured": "DECLARE wm DEFAULT (SELECT MAX(ts) FROM ${self()});\nDELETE FROM ${self()} WHERE ts >= wm",
}


@pytest.mark.parametrize("pre", UNSUBSTITUTABLE.values(), ids=list(UNSUBSTITUTABLE))
def test_a_variable_that_is_not_its_definition_is_not_substituted(pre):
    m = model(pre)
    assert effective_model(m) == m  # unchanged, so no proof rule reads past it


def test_a_variable_the_dml_does_not_touch_is_substituted_around_it():
    m = model(
        f"DECLARE wm DEFAULT {EPOCH};\nDELETE FROM ${{self()}} WHERE v > 5",
        where="WHERE ts > wm",
    )
    effective = effective_model(m)
    assert effective.pre_operations == ("DELETE FROM m WHERE v > 5",)
    assert "wm" not in effective.incremental_sql.split("WHERE")[1]


def test_effective_model_is_the_identity_without_variables():
    plain = parse_incremental_sqlx(f'config {{ type: "incremental" }}\n{QUERY}\n', "m")
    assert effective_model(plain) is plain


# --- the liveness pass -----------------------------------------------------------------------------------


def names(statements, query):
    return [s for s, _ in _script(statements, query, "bigquery")]


def test_a_variable_nobody_reads_is_not_run():
    script = ["DECLARE dead DEFAULT (SELECT MAX(ts) FROM m)", "DECLARE wm DEFAULT 1"]
    assert names(script, "SELECT id FROM events WHERE v > wm") == [
        "DECLARE wm DEFAULT 1"
    ]
    assert names(script, "SELECT id FROM events") == []


def test_a_variable_a_later_statement_reads_is_run_with_its_dependencies():
    script = [
        "DECLARE a DEFAULT 1",
        "DECLARE b DEFAULT a",
        "DECLARE dead DEFAULT 2",
        "SET a = 5",
    ]
    assert names(script, "SELECT b") == ["DECLARE a DEFAULT 1", "DECLARE b DEFAULT a"]
    with_dml = ["DECLARE a DEFAULT 1", "DELETE FROM m WHERE v > a"]
    assert (
        names(with_dml, "SELECT 1") == with_dml
    )  # DML is always run, and keeps what it reads alive


def test_a_dead_declaration_that_reads_the_table_does_not_stop_the_first_build():
    dead = model(
        f"DECLARE unused DEFAULT (SELECT MAX(ts) FROM ${{self()}});\nDECLARE wm DEFAULT {EPOCH}"
    )
    results = replay(dead, EVENTS, [insert(1, 1)], [[insert(2, 2)]])
    assert (
        [r.status for r in results]
        == [
            "agree",
            "diverge",
        ]
    )  # it ran: the dead declaration was skipped (wm is the epoch, so the second batch re-appends)
    live = model(
        f"DECLARE unused DEFAULT (SELECT MAX(ts) FROM ${{self()}});\nDECLARE wm DEFAULT {EPOCH}",
        where="WHERE ts > COALESCE(unused, wm)",
    )
    with pytest.raises(IncrementalError, match="reads the table before it is built"):
        replay(live, EVENTS, [insert(1, 1)], [[insert(2, 2)]])


# --- the simulator and the proofs read the same script ---------------------------------------------------

DOCS_COALESCE = (
    "DECLARE wm DEFAULT (${when(incremental(), `SELECT COALESCE(MAX(ts), "
    + EPOCH
    + ") FROM ${self()}`, `SELECT "
    + EPOCH
    + "`)})"
)
DOCS_PLAIN = (
    "DECLARE wm DEFAULT (${when(incremental(), `SELECT MAX(ts) FROM ${self()}`, `SELECT "
    + EPOCH
    + "`)})"
)


def test_the_documented_watermark_pattern_with_a_default_is_proved_by_the_watermark_rules():
    append = model(DOCS_COALESCE)
    verdict = check_incremental(append, EVENTS, {"insert_new", "empty"})
    assert verdict.outcome == "safe" and verdict.rule.startswith("R")
    assert (
        check_incremental(append, EVENTS, {"insert_new", "insert_boundary"}).outcome
        == "diverges"
    )
    merged = model(DOCS_COALESCE, where="WHERE ts >= wm", config=', uniqueKey: ["id"]')
    assert (
        check_incremental(
            merged, EVENTS, {"insert_new", "insert_boundary", "empty"}
        ).outcome
        == "safe"
    )


def test_the_same_pattern_without_a_default_loses_the_first_rows():
    verdict = check_incremental(model(DOCS_PLAIN), EVENTS, {"insert_new", "empty"})
    assert (
        verdict.outcome == "diverges"
    )  # MAX over an empty table is NULL, and `ts > NULL` keeps nothing


def test_an_unsubstitutable_variable_is_never_proved():
    pre = (
        "DECLARE wm DEFAULT (SELECT COALESCE(MAX(ts), "
        + EPOCH
        + ") FROM ${self()});\nDELETE FROM ${self()} WHERE ts >= wm"
    )
    assert (
        check_incremental(
            model(pre, where="WHERE ts >= wm"), EVENTS, {"insert_new", "empty"}
        ).outcome
        != "safe"
    )


def test_a_variable_the_query_ignores_changes_nothing():
    pre = "DECLARE run_started DEFAULT CURRENT_TIMESTAMP();\n---\nSET run_started = CURRENT_TIMESTAMP()"
    unused = model(
        pre,
        where="${when(incremental(), `WHERE ts > COALESCE((SELECT MAX(ts) FROM ${self()}), "
        + EPOCH
        + ")`)}",
    )
    assert check_incremental(unused, EVENTS, {"insert_new", "empty"}).outcome == "safe"


# --- _strip_old_bounds ----------------------------------------------------------------------------------


def strip(sql, column="ts"):
    return _strip_old_bounds(sqlglot.parse_one(sql, read="bigquery"), column).sql()


@pytest.mark.parametrize(
    "sql, kept",
    [
        ("SELECT * FROM e WHERE ts > TIMESTAMP '1970-01-01'", None),
        ("SELECT * FROM e WHERE ts >= TIMESTAMP '1970-01-01'", None),
        ("SELECT * FROM e WHERE ts >= DATE '1990-01-01'", None),
        ("SELECT * FROM e WHERE ts > TIMESTAMP '1970-01-01' AND v > 1", "v > 1"),
        ("SELECT * FROM e WHERE v > 1 AND ts > TIMESTAMP '1970-01-01'", "v > 1"),
        (
            "SELECT * FROM e WHERE ts > TIMESTAMP '1970-01-01' AND ts >= DATE '1980-01-01'",
            None,
        ),
    ],
)
def test_an_old_lower_bound_on_the_event_time_is_dropped(sql, kept):
    stripped = strip(sql)
    assert "1970" not in stripped and "1990" not in stripped and "1980" not in stripped
    assert (kept in stripped) if kept else "WHERE" not in stripped


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM e WHERE ts > TIMESTAMP '2024-01-01'",  # a real bound
        "SELECT * FROM e WHERE ts > TIMESTAMP '2001-01-01'",  # after 2000
        "SELECT * FROM e WHERE ts < TIMESTAMP '1970-01-01'",  # an upper bound
        "SELECT * FROM e WHERE v > TIMESTAMP '1970-01-01'",  # another column
        "SELECT * FROM e WHERE ts > v",  # not a literal
        "SELECT * FROM e WHERE ts > TIMESTAMP '1970-01-01' OR v > 1",  # not a conjunct
        "SELECT * FROM e",
    ],
)
def test_other_conditions_are_kept(sql):
    original = sqlglot.parse_one(sql, read="bigquery")
    assert strip(sql) == original.sql()
