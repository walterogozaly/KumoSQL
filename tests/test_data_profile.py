"""Data profiles: the statistics, both SQL dialects, the saved files and the MCP resources."""

import io
import json
import re
from datetime import datetime, timezone

import duckdb
import pytest
import sqlglot

from kumosql import bigquery_catalog, data_profile, data_profile_cli, data_profile_store as store, data_sources, profile_mcp, scope_queries, state
from kumosql.data_profile import (
    BigQueryExecutor, ByteCapExceeded, DataProfile, DuckDBExecutor, Node, ProfileError, classify, profile_queries, profile_table,
)
from kumosql.sql_validation import validate_readonly_query

NOW = lambda: datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc)


@pytest.fixture()
def db():
    connection = duckdb.connect()
    connection.execute("""
        CREATE TABLE orders AS SELECT
            i AS id,
            'cust' || (i % 5) AS customer,
            CAST(i * 1.5 AS DOUBLE) AS amount,
            (i % 3 = 0) AS paid,
            DATE '2024-01-01' + CAST(i % 10 AS INTEGER) AS placed,
            CASE WHEN i % 4 = 0 THEN NULL ELSE i % 7 END AS items,
            'same' AS channel,
            NULL::VARCHAR AS note,
            [i] AS tags
        FROM range(100) r(i)""")
    return connection


def by_name(profile):
    return {column.name: column for column in profile.columns}


def test_duckdb_profile_matches_the_data(db):
    profile = profile_table("orders", DuckDBExecutor(db), now=NOW)
    assert profile.row_count == 100 and profile.generated_at == "2026-10-07T12:00:00Z"
    columns = by_name(profile)

    ident = columns["id"]
    assert (ident.kind, ident.non_null, ident.null_count, ident.distinct) == ("numeric", 100, 0, 100)
    assert (ident.min, ident.max, ident.mean) == (0, 99, 49.5)
    assert (ident.p25, ident.median, ident.p75) == (24.75, 49.5, 74.25)
    assert ident.flags == ["unique"] and ident.top_values == []  # nothing is "most common" in a key

    customer = columns["customer"]
    assert (customer.kind, customer.distinct, customer.min, customer.max) == ("string", 5, "cust0", "cust4")
    assert (customer.min_length, customer.max_length, customer.mean_length) == (5, 5, 5.0)
    assert [(item.value, item.count, item.fraction) for item in customer.top_values] == [
        (f"cust{n}", 20, 0.2) for n in range(5)]

    items = columns["items"]
    assert (items.non_null, items.null_count, items.null_fraction) == (75, 25, 0.25)
    assert items.top_values[0].fraction == pytest.approx(items.top_values[0].count / 75, abs=1e-6)

    assert (columns["paid"].kind, columns["paid"].distinct) == ("boolean", 2)
    assert {item.value: item.count for item in columns["paid"].top_values} == {"true": 34, "false": 66}
    assert (columns["placed"].kind, columns["placed"].min, columns["placed"].max) == ("temporal", "2024-01-01", "2024-01-10")
    assert columns["channel"].flags == ["constant"]
    assert columns["note"].flags == ["all_null"] and columns["note"].null_fraction == 1.0
    # An array is reported by its length, then its elements (see the nested-field tests).
    assert columns["tags"].kind == "array" and columns["tags"].distinct is None and columns["tags"].min_length == 1
    assert columns["tags[]"].unit == "elements" and columns["tags[]"].non_null == 100


def test_options_narrow_the_profile(db):
    executor = DuckDBExecutor(db)
    only = profile_table("orders", executor, include=["ID", "customer"], exclude=["customer"], now=NOW)
    assert [column.name for column in only.columns] == ["id"]
    with pytest.raises(ProfileError, match="no column 'nope'"):
        profile_table("orders", executor, include=["nope"])

    filtered = profile_table("orders", executor, row_filter="id < 10 -- comment", top_values=0, now=NOW)
    assert filtered.row_count == 10 and filtered.row_filter == "id < 10"
    assert all(column.top_values == [] for column in filtered.columns)

    quiet = profile_table("orders", executor, include_values=False, now=NOW)
    columns = by_name(quiet)
    assert columns["customer"].min is None and columns["customer"].top_values == []
    assert columns["customer"].min_length == 5

    sampled = profile_table("orders", executor, sample_percent=50, now=NOW)
    assert 0 < sampled.row_count < 100 and sampled.sample_percent == 50
    assert any("sampled" in note for note in sampled.notes)
    assert profile_table("orders", executor, sample_percent=100, now=NOW).sample_percent is None


@pytest.mark.parametrize("kwargs", [
    {"sample_percent": 0}, {"sample_percent": 101}, {"sample_percent": True}, {"top_values": -1}, {"top_values": 51},
    {"row_filter": "id IN (SELECT 1)"}, {"row_filter": "1=1; DROP TABLE orders"}, {"row_filter": "DELETE FROM orders"},
    {"row_filter": "id ="},
])
def test_bad_options_are_refused(db, kwargs):
    with pytest.raises(ProfileError):
        profile_table("orders", DuckDBExecutor(db), **kwargs)


def test_table_and_column_names_cannot_inject_sql(db):
    executor = DuckDBExecutor(db)
    for name in ("orders; DROP TABLE orders", 'a"b', "x.y.z.w", ""):
        with pytest.raises(ProfileError):
            profile_table(name, executor)
    db.execute('CREATE TABLE odd AS SELECT 1 AS "a""b", 2 AS "x y"')
    assert [column.name for column in profile_table("odd", executor, now=NOW).columns] == ['a"b', "x y"]
    assert db.execute("SELECT count(*) FROM orders").fetchone() == (100,)


def test_a_chunk_is_split_and_wide_tables_are_chunked(db):
    queries = profile_queries("orders", DuckDBExecutor(db), chunk_size=4)
    assert len(queries) > 2
    whole = profile_table("orders", DuckDBExecutor(db), chunk_size=4, now=NOW)
    assert whole.to_json() == profile_table("orders", DuckDBExecutor(db), now=NOW).to_json()
    limited = profile_table("orders", DuckDBExecutor(db), max_columns=3, now=NOW)
    assert len(limited.columns) == 3 and {item["reason"] for item in limited.skipped} == {"over_column_limit"}


def test_a_column_that_fails_is_skipped_not_fatal(db):
    class Flaky(DuckDBExecutor):
        def run(self, sql):
            if '"amount"' in sql and "rows_total" in sql:
                raise ProfileError("cannot aggregate amount")
            return super().run(sql)

    profile = profile_table("orders", Flaky(db), now=NOW)
    assert "amount" not in by_name(profile) and len(profile.columns) == 9
    assert profile.skipped == [{"name": "amount", "reason": "error", "error": "cannot aggregate amount"}]


def test_classify():
    assert [classify(t) for t in ("INT64", "FLOAT64", "NUMERIC(10,2)", "BIGNUMERIC", "BIGINT", "DECIMAL(18,3)", "DOUBLE")] == ["numeric"] * 7
    assert [classify(t) for t in ("STRING", "VARCHAR", "STRING(10)")] == ["string"] * 3
    assert [classify(t) for t in ("DATE", "TIMESTAMP", "DATETIME", "TIME", "TIMESTAMP WITH TIME ZONE")] == ["temporal"] * 5
    assert [classify(t) for t in ("BOOL", "BOOLEAN")] == ["boolean"] * 2
    assert [classify(t) for t in ("ARRAY<INT64>", "STRUCT", "JSON", "GEOGRAPHY", "BYTES", "INT[]", "UNKNOWN", "")] == ["other"] * 8


def nested_db():
    connection = duckdb.connect()
    connection.execute("""
        CREATE TABLE people AS SELECT * FROM (VALUES
            (1, {'city': 'Oslo', 'geo': {'lat': 59.9}}, [{'sku': 'a', 'qty': 1}, {'sku': 'b', 'qty': 2}], [10, 20]),
            (2, {'city': 'Oslo', 'geo': {'lat': 60.4}}, [{'sku': 'a', 'qty': 3}], [30]),
            (3, {'city': 'Rome', 'geo': {'lat': NULL}}, [], NULL),
            (4, NULL, NULL, [40, 50, 60])
        ) v(id, address, orders, scores)""")
    return connection


def test_struct_fields_and_array_elements_are_profiled(tmp_path):
    profile = profile_table("people", DuckDBExecutor(nested_db()), now=NOW)
    columns = by_name(profile)
    assert [column.name for column in profile.columns] == [
        "id", "address", "address.city", "address.geo", "address.geo.lat", "orders", "orders[]", "orders[].sku",
        "orders[].qty", "scores", "scores[]"]
    assert profile.row_count == 4

    assert (columns["address"].kind, columns["address"].non_null, columns["address"].null_count) == ("other", 3, 1)
    city = columns["address.city"]
    assert (city.unit, city.non_null, city.null_count, city.distinct) == ("rows", 3, 1, 2)
    assert {item.value: item.count for item in city.top_values} == {"Oslo": 2, "Rome": 1}
    lat = columns["address.geo.lat"]
    assert (lat.non_null, lat.min, lat.max) == (2, 59.9, 60.4)

    # An array is reported with its length (a missing or empty array counts as missing), then its elements.
    orders = columns["orders"]
    assert (orders.kind, orders.unit, orders.non_null, orders.null_count) == ("array", "rows", 2, 2)
    assert (orders.min_length, orders.max_length, orders.mean_length) == (0, 2, 0.75)
    assert columns["orders[]"].unit == "elements" and columns["orders[]"].non_null == 3
    sku, qty = columns["orders[].sku"], columns["orders[].qty"]
    assert (sku.unit, sku.non_null, sku.distinct) == ("elements", 3, 2)
    assert {item.value: item.count for item in sku.top_values} == {"a": 2, "b": 1}
    assert (qty.min, qty.max, qty.mean) == (1, 3, 2.0)
    scores = columns["scores[]"]
    assert (scores.non_null, scores.min, scores.max, scores.distinct) == (6, 10, 60, 6)
    assert any("array elements" in note for note in profile.notes)

    summary = store.to_markdown(profile)
    assert "| orders[].sku (elements) |" in summary and "- Elements per row: 0 to 2, average 0.75" in summary
    assert DataProfile.from_json(profile.to_json()).to_json() == profile.to_json()


def test_nested_fields_honour_columns_filter_and_sampling():
    executor = DuckDBExecutor(nested_db())
    only = profile_table("people", executor, include=["ORDERS"], now=NOW)
    assert [column.name for column in only.columns] == ["orders", "orders[]", "orders[].sku", "orders[].qty"]
    assert only.row_count == 4  # the row count comes from the table, not the array elements
    without = profile_table("people", executor, exclude=["address", "orders", "scores"], now=NOW)
    assert [column.name for column in without.columns] == ["id"]
    filtered = profile_table("people", executor, row_filter="id <= 2", now=NOW)
    assert by_name(filtered)["orders[].sku"].non_null == 3 and by_name(filtered)["scores[]"].non_null == 3
    quiet = profile_table("people", executor, include_values=False, now=NOW)
    assert by_name(quiet)["orders[].sku"].top_values == [] and by_name(quiet)["address.city"].min is None
    assert len(profile_table("people", executor, max_columns=4, now=NOW).columns) == 4


def test_deep_and_odd_duckdb_types_degrade_to_one_value():
    connection = duckdb.connect()
    connection.execute("""CREATE TABLE odd AS SELECT [[1, 2], [3]] AS nested, MAP {'a': 1} AS m, [1, 2, 3]::INTEGER[3] AS fixed,
        {'a': {'b': {'c': {'d': {'e': {'f': {'g': 1}}}}}}} AS deep""")
    profile = profile_table("odd", DuckDBExecutor(connection), now=NOW)
    columns = by_name(profile)
    assert columns["nested"].kind == "other" and columns["m"].kind == "other" and columns["fixed"].kind == "other"
    assert "deep.a.b.c.d.e.f" in columns and "deep.a.b.c.d.e.f.g" not in columns
    assert [item["reason"] for item in profile.skipped] == ["too_deeply_nested"]


def test_bigquery_schema_with_records_and_repeated_fields(monkeypatch):
    nodes = data_profile._bigquery_nodes([
        {"name": "id", "type": "INTEGER"},
        {"name": "address", "type": "RECORD", "fields": [{"name": "city", "type": "STRING"}]},
        {"name": "orders", "type": "RECORD", "mode": "REPEATED", "fields": [{"name": "sku", "type": "STRING"}]},
        {"name": "tags", "type": "STRING", "mode": "REPEATED"},
    ])
    assert [(n.name, n.type, n.repeated, n.children is not None) for n in nodes] == [
        ("id", "INT64", False, False), ("address", "STRUCT", False, True), ("orders", "STRUCT", True, True), ("tags", "STRING", True, False)]

    class Plain(BigQueryExecutor):
        def __init__(self):
            pass

        def table_sql(self, table):
            return "`proj.ds.people`"

        def fields(self, table):
            return nodes

    queries = profile_queries("proj.ds.people", Plain(), sample_percent=10, row_filter="id > 1")
    for sql in queries:
        validate_readonly_query(sql)
        sqlglot.parse_one(sql, read="bigquery")
    joined = " ".join(queries)
    assert "`address`.`city` AS k2" in joined and "CROSS JOIN UNNEST(`orders`) AS e1" in joined
    assert "e1.`sku`" in joined and "CROSS JOIN UNNEST(`tags`) AS e1" in joined and "ARRAY_LENGTH(k3)" in joined
    assert "TABLESAMPLE SYSTEM (10 PERCENT) CROSS JOIN UNNEST" in joined and joined.count("WHERE (id > 1)") >= 3


def test_nested_bigquery_sql_gives_the_same_answers_when_run_on_duckdb():
    """The BigQuery SQL for STRUCT fields and UNNEST, translated to DuckDB, matches the DuckDB-native profile."""

    connection = nested_db()
    nodes = data_profile._bigquery_nodes([
        {"name": "id", "type": "INTEGER"},
        {"name": "address", "type": "RECORD", "fields": [
            {"name": "city", "type": "STRING"}, {"name": "geo", "type": "RECORD", "fields": [{"name": "lat", "type": "FLOAT"}]}]},
        {"name": "orders", "type": "RECORD", "mode": "REPEATED", "fields": [
            {"name": "sku", "type": "STRING"}, {"name": "qty", "type": "INTEGER"}]},
        {"name": "scores", "type": "INTEGER", "mode": "REPEATED"},
    ])

    class Translated(BigQueryExecutor):
        def __init__(self):
            self._estimated = self._billed = 0
            self._saw_estimate = self._saw_billed = False

        def table_sql(self, table):
            return "`proj.ds.people`"

        def fields(self, table):
            return nodes

        def run(self, sql):
            validate_readonly_query(sql)
            translated = sqlglot.transpile(sql.replace("`proj.ds.people`", "people"), read="bigquery", write="duckdb")[0]
            cursor = connection.execute(translated)
            names = [item[0] for item in cursor.description]
            return [dict(zip(names, [None if v is None else str(v) for v in row])) for row in cursor.fetchall()]

    bigquery = profile_table("proj.ds.people", Translated(), now=NOW)
    native = profile_table("people", DuckDBExecutor(connection), now=NOW)
    assert bigquery.skipped == [] and bigquery.row_count == native.row_count == 4
    wanted = ("name", "kind", "unit", "non_null", "null_count", "distinct", "min", "max", "min_length", "max_length", "mean_length")
    for left, right in zip(bigquery.columns, native.columns):
        assert [getattr(left, n) for n in wanted] == [getattr(right, n) for n in wanted], left.name
        assert {(v.value, v.count) for v in left.top_values} == {(v.value, v.count) for v in right.top_values}, left.name


# ---------------------------------------------------------------- BigQuery


class FakeBigQuery:
    """Runs the BigQuery SQL KumoSQL sends on DuckDB (translated), answering as the REST API does: text cells."""

    def __init__(self, connection, monkeypatch, fail=lambda sql: None):
        self.connection, self.sent, self.fail = connection, [], fail
        state.set_section("bigquery", {"billingProject": "bill-proj"})
        monkeypatch.setitem(data_sources.RUNNERS, "bigquery_sql", self.runner)
        monkeypatch.setattr(bigquery_catalog, "get_table", self.get_table)

    def get_table(self, project, dataset, table):
        assert (project, dataset, table) == ("proj", "ds", "orders")
        fields = [("id", "INT64"), ("customer", "STRING"), ("amount", "FLOAT64"), ("paid", "BOOL"), ("placed", "DATE"),
                  ("items", "INT64"), ("channel", "STRING"), ("note", "STRING"), ("tags", "INT64")]
        return {"schema": [{"name": n, "type": t, **({"mode": "REPEATED"} if n == "tags" else {})} for n, t in fields]}

    def runner(self, source, project, max_bytes):
        sql = source.query
        self.sent.append((sql, project, max_bytes))
        validate_readonly_query(sql)
        if (error := self.fail(sql)):
            raise error
        translated = sqlglot.transpile(sql.replace("`proj.ds.orders`", "orders"), read="bigquery", write="duckdb")[0]
        cursor = self.connection.execute(translated)
        names = [item[0] for item in cursor.description]
        rows = [[None if value is None else str(value) for value in row] for row in cursor.fetchall()]
        return data_sources.Run(names, rows, estimated_bytes=1000, bytes_billed=10_485_760)


def test_bigquery_profile_runs_valid_read_only_sql_in_the_billing_project(db, monkeypatch):
    fake = FakeBigQuery(db, monkeypatch)
    profile = profile_table("proj.ds.orders", BigQueryExecutor(), now=NOW)
    assert profile.dialect == "bigquery" and profile.row_count == 100
    assert not profile.approximate_distinct and any("distinct counts are exact" in note for note in profile.notes)
    assert not any("APPROX_COUNT_DISTINCT" in sql for sql, _p, _m in fake.sent) and any("COUNT(DISTINCT" in sql for sql, _p, _m in fake.sent)
    assert {call[1] for call in fake.sent} == {"bill-proj"}
    assert {call[2] for call in fake.sent} == {scope_queries.get_settings().max_bytes_billed}
    assert all(sql.count("`proj.ds.orders`") >= 1 for sql, _p, _m in fake.sent)
    assert profile.estimated_bytes == 1000 * len(fake.sent) and profile.bytes_billed == 10_485_760 * len(fake.sent)
    columns = by_name(profile)
    # BigQuery sends every cell as text; numbers come back as numbers, dates stay text.
    assert (columns["id"].min, columns["id"].max, columns["id"].mean) == (0, 99, 49.5)
    assert columns["placed"].min == "2024-01-01" and columns["customer"].top_values[0].count == 20
    assert columns["tags"].kind == "array" and columns["tags[]"].unit == "elements"  # REPEATED


def test_bigquery_sql_parses_as_bigquery_and_uses_its_functions(db):
    class Plain(BigQueryExecutor):
        def __init__(self):
            pass

        def table_sql(self, table):
            return "`proj.ds.orders`"

        def fields(self, table):
            return [Node("a", "INT64"), Node("b", "STRING"), Node("c", "TIMESTAMP"), Node("d", "BOOL"), Node("e", "INT64", True)]

    queries = profile_queries("proj.ds.orders", Plain(), sample_percent=10, row_filter="a > 1 AND b = 'x'")
    for sql in queries:
        validate_readonly_query(sql)
        sqlglot.parse_one(sql, read="bigquery")
    stats = queries[0]
    assert "APPROX_QUANTILES(k0, 4)[OFFSET(2)]" in stats and "CAST(MIN(k2) AS STRING)" in stats
    assert "TABLESAMPLE SYSTEM (10 PERCENT)" in stats and "WHERE (a > 1 AND b = 'x')" in stats
    assert "COALESCE(ARRAY_LENGTH(k4), 0)" in stats and "COUNT(DISTINCT k4)" not in stats
    assert "COUNT(DISTINCT k0)" in stats and "APPROX_COUNT_DISTINCT" not in stats  # exact is the default
    estimate = profile_queries("proj.ds.orders", Plain(), approximate=True)[0]
    assert "APPROX_COUNT_DISTINCT(k0)" in estimate and "COUNT(DISTINCT" not in estimate


def test_bigquery_falls_back_to_exact_distinct_and_then_skips(db, monkeypatch):
    def fail(sql):
        if "APPROX_COUNT_DISTINCT" in sql and "`amount` AS" in sql:
            return scope_queries.QueryError("BigQuery rejected the query: unsupported type")
        if "`note` AS" in sql and "rows_total" in sql:
            return scope_queries.QueryError("BigQuery rejected the query: broken")
        return None

    fake = FakeBigQuery(db, monkeypatch, fail)
    profile = profile_table("proj.ds.orders", BigQueryExecutor(), approximate=True, now=NOW)
    assert by_name(profile)["amount"].distinct == 100
    assert [item["name"] for item in profile.skipped] == ["note"] and "broken" in profile.skipped[0]["error"]
    assert any("COUNT(DISTINCT k2)" in sql for sql, _p, _m in fake.sent)


def test_a_byte_cap_refusal_stops_the_run(db, monkeypatch):
    FakeBigQuery(db, monkeypatch, lambda sql: scope_queries.QueryError("the query would process about 5.0 TB, over the 1.1 GB cap."))
    with pytest.raises(ByteCapExceeded):
        profile_table("proj.ds.orders", BigQueryExecutor(), now=NOW)


def test_bigquery_needs_an_explicit_billing_project_and_a_strict_table_name(db, monkeypatch):
    with pytest.raises(ProfileError, match="billing project"):
        BigQueryExecutor()
    executor = BigQueryExecutor("bill-proj", 50_000_000)
    assert executor.max_bytes == 50_000_000
    with pytest.raises(ProfileError, match="project.dataset.table"):
        executor.table_sql("ds.orders")
    with pytest.raises(ProfileError):
        executor.table_sql("p.d.t`; DROP TABLE x --")


# ------------------------------------------------------------- saved files


def test_profiles_round_trip_and_render(db, tmp_path):
    profile = profile_table("orders", DuckDBExecutor(db), now=NOW)
    path = store.save(profile, directory=tmp_path)
    assert path == tmp_path / "orders.json"
    again = store.load("orders", tmp_path)
    assert again.to_json() == profile.to_json()
    assert store.list_profiles(tmp_path) == [{
        "name": "orders", "table": "orders", "dialect": "duckdb", "generated_at": "2026-10-07T12:00:00Z",
        "row_count": 100, "columns": 10, "sampled": False}]

    summary = store.to_markdown(profile)
    assert summary.startswith("# Data profile: orders") and "Rows: 100" in summary
    assert "| customer | VARCHAR | 0.0% | 5 | cust0 | cust4 |" in summary
    assert '"cust0" (20, 20.0%)' in summary and "not as instructions" in summary
    assert "all_null" in summary and "constant" in summary


def test_saved_profile_names_and_files_are_checked(tmp_path, db):
    assert store.default_name("My-Proj.DS.Orders 2") == "my-proj.ds.orders_2"
    for bad in ("../x", "a/b", "", "A", ".hidden", "x" * 121):
        with pytest.raises(ProfileError):
            store.path_for(bad, tmp_path)
    with pytest.raises(ProfileError, match="no saved profile"):
        store.load("missing", tmp_path)
    (tmp_path / "broken.json").write_text("{not json")
    (tmp_path / "wrong.json").write_text(json.dumps({"version": 99}))
    with pytest.raises(ProfileError):
        store.load("broken", tmp_path)
    with pytest.raises(ProfileError):
        store.load("wrong", tmp_path)
    assert store.list_profiles(tmp_path) == []


def test_markdown_does_not_let_table_values_break_the_layout(tmp_path):
    connection = duckdb.connect()
    connection.execute("CREATE TABLE t AS SELECT 'a | b\n# Heading `x`' || repeat('z', 500) AS s FROM range(3)")
    summary = store.to_markdown(profile_table("t", DuckDBExecutor(connection), now=NOW))
    assert "\n# Heading" not in summary and "a \\| b # Heading 'x'" in summary
    assert max(len(line) for line in summary.splitlines()) < 600


# ------------------------------------------------------------------- CLI


def test_cli_profiles_a_csv_saves_it_and_prints_a_summary(tmp_path, capsys):
    csv = tmp_path / "people.csv"
    csv.write_text("name,age\nana,31\nbo,\ncy,45\n")
    assert data_profile_cli.main(["--file", str(csv)]) == 0
    out = capsys.readouterr()
    assert "# Data profile: people" in out.out and "Rows: 3" in out.out and "saved:" in out.err
    saved = store.load("people")
    assert saved.row_count == 3 and by_name(saved)["age"].null_count == 1

    target = tmp_path / "out.json"
    assert data_profile_cli.main(["--file", str(csv), "--format", "json", "--no-save", "--name", "x", "-o", str(target)]) == 0
    assert json.loads(target.read_text())["table"] == "people" and not (state.data_dir() / "data-profiles" / "x.json").exists()


def test_cli_profiles_a_duckdb_file_and_reports_errors(tmp_path, capsys):
    database = tmp_path / "w.duckdb"
    connection = duckdb.connect(str(database))
    connection.execute("CREATE TABLE t AS SELECT range AS a FROM range(5)")
    connection.close()
    assert data_profile_cli.main(["--duckdb", str(database), "t", "--top-values", "0", "--no-save"]) == 0
    assert "Rows: 5" in capsys.readouterr().out
    assert data_profile_cli.main(["--duckdb", str(database), "missing", "--no-save"]) == 2
    assert "error:" in capsys.readouterr().err
    assert data_profile_cli.main(["--duckdb", str(database), "t", "--sample-percent", "0", "--no-save"]) == 2


def test_cli_dry_run_prints_queries_and_estimates_without_running(db, monkeypatch, capsys):
    fake = FakeBigQuery(db, monkeypatch)
    monkeypatch.setattr(scope_queries, "dry_run", lambda sql, column=None, *, project=None, max_bytes=None: {"estimated_bytes": 123})
    assert data_profile_cli.main(["proj.ds.orders", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "estimated bytes processed: 123" in out and f"total estimated bytes processed: {123 * out.count('-- query')}" in out
    assert fake.sent == []  # nothing ran


def test_cli_exits_3_when_over_the_byte_cap(db, monkeypatch, capsys):
    FakeBigQuery(db, monkeypatch, lambda sql: scope_queries.QueryError("over the 1.0 GB cap"))
    assert data_profile_cli.main(["proj.ds.orders", "--no-save"]) == 3
    assert "cap" in capsys.readouterr().err


# -------------------------------------------------------------------- MCP


def rpc(server, method, params=None, ident=1):
    message = {"jsonrpc": "2.0", "method": method, **({"id": ident} if ident is not None else {})}
    if params is not None:
        message["params"] = params
    return server.handle(message)


def test_mcp_lists_and_reads_saved_profiles(db, tmp_path):
    store.save(profile_table("orders", DuckDBExecutor(db), now=NOW), directory=tmp_path)
    server = profile_mcp.Server(tmp_path)

    init = rpc(server, "initialize", {"protocolVersion": "2025-03-26"})["result"]
    assert init["protocolVersion"] == "2025-03-26" and "resources" in init["capabilities"] and "tools" not in init["capabilities"]
    assert rpc(server, "notifications/initialized", ident=None) is None
    assert rpc(server, "ping")["result"] == {}

    listed = rpc(server, "resources/list")["result"]["resources"]
    assert [item["uri"] for item in listed] == ["kumosql://data-profile/orders/summary.md", "kumosql://data-profile/orders/profile.json"]
    assert [item["mimeType"] for item in listed] == ["text/markdown", "application/json"]

    summary = rpc(server, "resources/read", {"uri": listed[0]["uri"]})["result"]["contents"][0]
    assert summary["mimeType"] == "text/markdown" and "# Data profile: orders" in summary["text"]
    document = json.loads(rpc(server, "resources/read", {"uri": listed[1]["uri"]})["result"]["contents"][0]["text"])
    assert document["row_count"] == 100


def test_mcp_refuses_unknown_requests_and_paths_outside_the_profile_folder(tmp_path):
    (tmp_path / "secret.json").write_text("{}")
    server = profile_mcp.Server(tmp_path)
    assert rpc(server, "tools/call", {"name": "x"})["error"]["code"] == profile_mcp.METHOD_NOT_FOUND
    for uri in ("kumosql://data-profile/../secret/summary.md", "file:///etc/passwd", "kumosql://data-profile/a/b/summary.md", None, 5):
        assert rpc(server, "resources/read", {"uri": uri})["error"]["code"] == profile_mcp.INVALID_PARAMS
    assert rpc(server, "resources/read", {"uri": "kumosql://data-profile/nope/summary.md"})["error"]["code"] == profile_mcp.NOT_FOUND
    assert profile_mcp.Server(tmp_path).handle([])["error"]["code"] == profile_mcp.INVALID_REQUEST


def test_mcp_stdio_loop_answers_one_line_per_request(db, tmp_path):
    store.save(profile_table("orders", DuckDBExecutor(db), now=NOW), directory=tmp_path)
    lines = "\n".join([
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "resources/list"}),
        json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
        "not json",
        "",
    ])
    out = io.StringIO()
    profile_mcp.serve(profile_mcp.Server(tmp_path), io.StringIO(lines), out)
    replies = [json.loads(line) for line in out.getvalue().splitlines()]
    assert len(replies) == 2 and len(replies[0]["result"]["resources"]) == 2
    assert replies[1]["error"]["code"] == profile_mcp.PARSE_ERROR


def test_profile_json_has_no_nan_or_infinity(db):
    db.execute("CREATE TABLE f AS SELECT * FROM (VALUES (1.0::DOUBLE), ('NaN'::DOUBLE), (NULL::DOUBLE)) v(x)")
    profile = profile_table("f", DuckDBExecutor(db), now=NOW)
    text = json.dumps(profile.to_json(), allow_nan=False)
    assert not re.search(r"NaN|Infinity", text)
    assert DataProfile.from_json(json.loads(text)).row_count == 3 and not profile.skipped
