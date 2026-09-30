import pytest

from kumosql import GrainMapping, Pipeline, Scope, Target, find_rollups
from kumosql.pipeline import Model

SALES = Target("proj", "raw", "sales")
CITIES = Target("proj", "raw", "cities")
SOURCES = {SALES.key: SALES, CITIES.key: CITIES}
SCHEMA = {
    SALES.key: {
        "city_id": "INT64", "amount": "FLOAT64", "qty": "INT64", "price": "FLOAT64",
        "status": "STRING", "shopper": "INT64", "day": "DATE",
    },
    CITIES.key: {"city_id": "INT64", "state_id": "INT64"},
}  # fmt: skip
CITY_MAP = "proj.core.city_map"


def build(models: dict[str, str]) -> Pipeline:
    built = {}
    for name, sql in models.items():
        target = Target("proj", "core", name)
        built[target.key] = Model(target, "table", sql)
    return Pipeline(built, sources=dict(SOURCES), source_schema=dict(SCHEMA))


BY_CITY = (
    "SELECT city_id, SUM(amount) AS total, COUNT(amount) AS n, COUNT(*) AS rows_n, "
    "MIN(amount) AS lo, MAX(amount) AS hi FROM proj.raw.sales GROUP BY city_id"
)
CITY_MAP_SQL = "SELECT city_id, state_id FROM proj.raw.cities"
DECLARED = {CITY_MAP: ["city_id"]}
SALES_CITY, CITY_STATE = "col:proj.raw.sales.city_id", "col:proj.raw.cities.state_id"


def rollups(models, sql, **kw):
    kw.setdefault("declared_grain", DECLARED)
    result = find_rollups(build(models), sql, **kw)
    return result, {(r.table.split(".")[-1], r.attribute): r for r in result.rollups}


def state_query(expr: str) -> str:
    return (
        f"SELECT c.state_id, {expr} AS v FROM proj.raw.sales s "
        "JOIN proj.raw.cities c ON s.city_id = c.city_id GROUP BY c.state_id"
    )


def test_sum_by_state_is_exact_through_a_known_mapping():
    result, found = rollups({"city_totals": BY_CITY, "city_map": CITY_MAP_SQL}, state_query("SUM(s.amount)"))
    item = found[("city_totals", "v")]
    assert item.derivability == "derivable_exact"
    assert item.aggregate == ("SUM",) and item.columns == ("city_id", "total")
    assert any("does not change over time" in c for c in item.conditions)
    assert item.missing == ()
    assert {c.kind: c.outcome for c in item.checks} == {"lineage": "matched", "grain": "matched", "row_scope": "matched"}
    assert result.compared == 2 and result.summary.startswith("compared 2 of 2 tables; 0 skipped")


@pytest.mark.parametrize("fn,column", [("COUNT(s.amount)", "n"), ("MIN(s.amount)", "lo"), ("MAX(s.amount)", "hi")])
def test_decomposable_aggregates_are_exact(fn, column):
    _, found = rollups({"city_totals": BY_CITY, "city_map": CITY_MAP_SQL}, state_query(fn))
    item = found[("city_totals", "v")]
    assert item.derivability == "derivable_exact" and column in item.columns


def test_sum_of_products_is_exact_when_the_finer_table_holds_it():
    models = {"city_rev": "SELECT city_id, SUM(qty * price) AS rev FROM proj.raw.sales GROUP BY city_id", "city_map": CITY_MAP_SQL}
    _, found = rollups(models, state_query("SUM(s.qty * s.price)"))
    assert found[("city_rev", "v")].derivability == "derivable_exact"


def test_average_needs_sum_and_count_and_names_what_is_missing():
    models = {"avg_only": "SELECT city_id, AVG(amount) AS mean FROM proj.raw.sales GROUP BY city_id", "city_map": CITY_MAP_SQL}
    _, found = rollups(models, state_query("AVG(s.amount)"))
    item = found[("avg_only", "v")]
    assert item.derivability == "derivable_with_conditions"
    assert any(m.startswith("sum of amount") for m in item.missing)
    assert any(m.startswith("count of non-null amount") for m in item.missing)


def test_average_with_sum_but_no_count_names_only_the_count():
    models = {"half": "SELECT city_id, SUM(amount) AS total FROM proj.raw.sales GROUP BY city_id", "city_map": CITY_MAP_SQL}
    _, found = rollups(models, state_query("AVG(s.amount)"))
    item = found[("half", "v")]
    assert item.derivability == "derivable_with_conditions"
    assert [m.split(" ")[0] for m in item.missing] == ["count"]


def test_average_with_both_parts_is_exact():
    _, found = rollups({"city_totals": BY_CITY, "city_map": CITY_MAP_SQL}, state_query("AVG(s.amount)"))
    assert found[("city_totals", "v")].derivability == "derivable_exact"


def test_ratio_needs_both_parts():
    models = {"sums": "SELECT city_id, SUM(amount) AS total FROM proj.raw.sales GROUP BY city_id", "city_map": CITY_MAP_SQL}
    _, found = rollups(models, state_query("SUM(s.amount) / COUNT(*)"))
    item = found[("sums", "v")]
    assert item.derivability == "derivable_with_conditions"
    assert item.aggregate == ("COUNT", "SUM")


def test_distinct_count_is_not_derivable_but_reported():
    models = {
        "shoppers": "SELECT city_id, COUNT(DISTINCT shopper) AS n FROM proj.raw.sales GROUP BY city_id",
        "city_map": CITY_MAP_SQL,
    }
    _, found = rollups(models, state_query("COUNT(DISTINCT s.shopper)"))
    item = found[("shoppers", "v")]
    assert item.derivability == "not_derivable"
    assert item.aggregate == ("COUNT DISTINCT",)
    assert "raw data" in item.reason


def test_median_is_not_derivable():
    models = {
        "medians": "SELECT city_id, APPROX_QUANTILES(amount, 2)[OFFSET(1)] AS med FROM proj.raw.sales GROUP BY city_id",
        "city_map": CITY_MAP_SQL,
    }
    _, found = rollups(models, state_query("APPROX_QUANTILES(s.amount, 2)[OFFSET(1)]"))
    assert found[("medians", "v")].derivability == "not_derivable"


def test_unknown_mapping_names_the_missing_piece():
    _, found = rollups({"city_totals": BY_CITY}, state_query("SUM(s.amount)"))
    item = found[("city_totals", "v")]
    assert item.derivability == "unknown"
    assert any("mapping from the finer table's grain to 'state_id'" in m for m in item.missing)
    assert item.checks[1].outcome == "unknown"


def test_supplied_mapping_that_changes_over_time_needs_conditions():
    mapping = GrainMapping(SALES_CITY, CITY_STATE, changes_over_time=True, complete=True)
    _, found = rollups({"city_totals": BY_CITY}, state_query("SUM(s.amount)"), mappings=[mapping])
    item = found[("city_totals", "v")]
    assert item.derivability == "derivable_with_conditions"
    assert any("mapping that applied when they occurred" in c for c in item.conditions)
    assert not any("every finer-grain value" in c for c in item.conditions)


def test_static_complete_mapping_has_no_mapping_conditions():
    mapping = GrainMapping(SALES_CITY, CITY_STATE, changes_over_time=False, complete=True)
    _, found = rollups({"city_totals": BY_CITY}, state_query("SUM(s.amount)"), mappings=[mapping])
    item = found[("city_totals", "v")]
    assert item.derivability == "derivable_exact" and item.conditions == ()


def test_mapping_that_is_not_many_to_one_is_unknown():
    mapping = GrainMapping(SALES_CITY, CITY_STATE, many_to_one=False)
    _, found = rollups({"city_totals": BY_CITY}, state_query("SUM(s.amount)"), mappings=[mapping])
    assert found[("city_totals", "v")].derivability == "unknown"


def test_unmapped_rows_are_a_condition():
    _, found = rollups({"city_totals": BY_CITY, "city_map": CITY_MAP_SQL}, state_query("SUM(s.amount)"))
    assert any("rows with none would be dropped" in c for c in found[("city_totals", "v")].conditions)


def test_finer_table_with_the_coarser_key_as_a_column_needs_no_mapping():
    models = {"by_status_city": "SELECT city_id, status, SUM(amount) AS total FROM proj.raw.sales GROUP BY city_id, status"}
    _, found = rollups(models, "SELECT status, SUM(amount) AS v FROM proj.raw.sales GROUP BY status")
    item = found[("by_status_city", "v")]
    assert item.derivability == "derivable_exact" and item.conditions == ()


def test_same_grain_and_coarser_tables_are_not_rollups():
    models = {
        "same": "SELECT status, SUM(amount) AS total FROM proj.raw.sales GROUP BY status",
        "coarser": "SELECT SUM(amount) AS total FROM proj.raw.sales",
    }
    result, _ = rollups(models, "SELECT status, SUM(amount) AS v FROM proj.raw.sales GROUP BY status")
    assert result.rollups == () and result.compared == 2
    assert "No finer-grain source" in result.summary


def test_global_total_from_a_grouped_table():
    models = {"by_status": "SELECT status, SUM(amount) AS total FROM proj.raw.sales GROUP BY status"}
    _, found = rollups(models, "SELECT SUM(amount) AS v FROM proj.raw.sales")
    assert found[("by_status", "v")].derivability == "derivable_exact"


def test_differing_row_scope_is_unknown_and_names_it():
    models = {
        "paid": "SELECT status, city_id, SUM(amount) AS total FROM proj.raw.sales WHERE status = 'paid' GROUP BY status, city_id"
    }
    _, found = rollups(models, "SELECT status, SUM(amount) AS v FROM proj.raw.sales GROUP BY status")
    item = found[("paid", "v")]
    assert item.derivability == "unknown"
    assert any("row scope" in m for m in item.missing)


def test_time_window_difference_is_unknown():
    models = {
        "recent": "SELECT status, city_id, SUM(amount) AS total FROM proj.raw.sales WHERE day >= '2024-01-01' GROUP BY status, city_id"
    }
    sql = "SELECT status, SUM(amount) AS v FROM proj.raw.sales WHERE day >= '2023-01-01' GROUP BY status"
    _, found = rollups(models, sql)
    assert found[("recent", "v")].derivability == "unknown"


def test_target_filter_on_a_finer_key_is_applied_before_combining():
    models = {"by_status_city": "SELECT status, city_id, SUM(amount) AS total FROM proj.raw.sales GROUP BY status, city_id"}
    sql = "SELECT city_id, SUM(amount) AS v FROM proj.raw.sales WHERE status = 'paid' GROUP BY city_id"
    _, found = rollups(models, sql)
    item = found[("by_status_city", "v")]
    assert item.derivability == "derivable_exact"
    assert any("extra filters" in c for c in item.conditions)


def test_target_filter_on_a_missing_column_is_unknown():
    models = {"by_status_city": "SELECT status, city_id, SUM(amount) AS total FROM proj.raw.sales GROUP BY status, city_id"}
    sql = "SELECT status, SUM(amount) AS v FROM proj.raw.sales WHERE day > '2024-01-01' GROUP BY status"
    _, found = rollups(models, sql)
    assert found[("by_status_city", "v")].derivability == "unknown"


def test_join_in_the_finer_table_adds_a_double_counting_condition():
    models = {
        "joined": (
            "SELECT s.city_id, s.status, SUM(s.amount) AS total FROM proj.raw.sales s "
            "JOIN proj.raw.cities c ON s.city_id = c.city_id GROUP BY s.city_id, s.status"
        )
    }
    _, found = rollups(models, "SELECT status, SUM(amount) AS v FROM proj.raw.sales GROUP BY status")
    assert any("double counting" in c for c in found[("joined", "v")].conditions)


def test_rounding_adds_a_condition():
    models = {"by_status_city": "SELECT status, city_id, SUM(amount) AS total FROM proj.raw.sales GROUP BY status, city_id"}
    _, found = rollups(models, "SELECT status, ROUND(SUM(amount), 2) AS v FROM proj.raw.sales GROUP BY status")
    item = found[("by_status_city", "v")]
    assert item.derivability == "derivable_exact"
    assert any("rounding" in c for c in item.conditions)


def test_raw_row_table_supports_any_aggregate():
    models = {"lines": "SELECT status, city_id, amount, shopper FROM proj.raw.sales"}
    sql = "SELECT city_id, COUNT(DISTINCT shopper) AS d, SUM(amount) AS s FROM proj.raw.sales GROUP BY city_id"
    _, found = rollups(models, sql)
    assert found[("lines", "d")].derivability == "derivable_exact"
    assert found[("lines", "s")].derivability == "derivable_exact"


def test_unrelated_and_unparseable_tables_are_counted_not_raised():
    models = {"other": "SELECT status, MAX(qty) AS q FROM proj.raw.sales GROUP BY status", "broken": "SELEC oops FROM"}
    result, _ = rollups(models, "SELECT status, SUM(amount) AS v FROM proj.raw.sales GROUP BY status")
    assert [r.attribute for r in result.rollups] == [None]  # the unparseable table is undecided, not a source
    assert result.rollups[0].derivability == "unknown"
    assert result.compared + sum(result.skipped.values()) == 2


def test_unknown_target_grain_is_skipped_with_a_reason():
    result, _ = rollups({"city_totals": BY_CITY}, "SELECT amount AS v FROM proj.raw.sales")
    assert result.skipped.get("grain_unknown") == 1
    assert [r.attribute for r in result.rollups] == [None]
    assert result.rollups[0].missing


def test_model_target_scope_and_argument_errors():
    models = {"city_totals": BY_CITY, "state_view": "SELECT status, SUM(amount) AS v FROM proj.raw.sales GROUP BY status"}
    pipeline = build(models)
    with pytest.raises(ValueError):
        find_rollups(pipeline)
    with pytest.raises(ValueError):
        find_rollups(pipeline, "SELECT 1", model="proj.core.state_view")
    with pytest.raises(ValueError):
        find_rollups(pipeline, model="missing")
    result = find_rollups(pipeline, model="proj.core.state_view")
    assert result.candidates_in_scope == 1
    assert {r.table for r in result.rollups} <= {"proj.core.city_totals"}
    outside = find_rollups(pipeline, model="proj.core.state_view", scope=Scope("s", {"table": ("nothing",)}))
    assert outside.skipped == {"outside_scope": 1} and outside.rollups == ()


def test_to_json_shape_has_no_query_text():
    result, _ = rollups({"city_totals": BY_CITY, "city_map": CITY_MAP_SQL}, state_query("SUM(s.amount)"))
    data = result.to_json()
    assert set(data) == {"summary", "target", "rollups", "compared", "skipped", "candidates_in_scope"}
    assert data["rollups"][0]["derivability"] == "derivable_exact"
    assert "FROM" not in str(data)
