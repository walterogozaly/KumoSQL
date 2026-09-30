import pytest

from kumosql import Pipeline, Scope, TableRole, Target, find_overlaps
from kumosql.pipeline import Model

ORDERS = Target("proj", "raw", "orders")
SOURCES = {ORDERS.key: ORDERS}
SCHEMA = {ORDERS.key: {"customer_id": "INT64", "amount": "FLOAT64", "status": "STRING", "region_id": "INT64"}}


def build(models: dict[str, str], kinds: dict[str, str] | None = None, datasets: dict[str, str] | None = None) -> Pipeline:
    kinds, datasets = kinds or {}, datasets or {}
    built = {}
    for name, sql in models.items():
        target = Target("proj", datasets.get(name, "core"), name)
        built[target.key] = Model(target, kinds.get(name, "table"), sql)
    return Pipeline(built, sources=dict(SOURCES), source_schema=dict(SCHEMA))


BY_REGION = "SELECT region_id, SUM(amount) AS total FROM proj.raw.orders GROUP BY region_id"
PAID_BY_REGION = "SELECT region_id, SUM(amount) AS total FROM proj.raw.orders WHERE status = 'paid' GROUP BY region_id"
TARGET_BY_REGION = "select REGION_ID as state, sum(Amount) as revenue from `proj.raw.orders` group by 1"


def by_table(result):
    return {m.table.split(".")[-1]: m for m in result.matches}


def test_state_level_aggregate_already_exists():
    result = find_overlaps(build({"state_totals": BY_REGION}), TARGET_BY_REGION)
    match = by_table(result)["state_totals"]
    assert (match.kind, match.confidence) == ("same_meaning", "high")
    assert dict(match.attributes) == {"state": "region_id", "revenue": "total"}
    assert {c.kind: c.outcome for c in match.checks} == {"lineage": "matched", "grain": "matched", "row_scope": "matched"}
    assert result.compared == 1 and not result.skipped


def test_same_meaning_through_a_view_chain_and_ctes():
    models = {
        "clean": "SELECT region_id AS r, amount AS a FROM proj.raw.orders",
        "state_totals": "WITH t AS (SELECT r, a FROM proj.core.clean) SELECT r AS area, SUM(a) AS sales FROM t GROUP BY r",
    }
    result = find_overlaps(build(models, kinds={"clean": "view"}), TARGET_BY_REGION)
    match = by_table(result)["state_totals"]
    assert match.kind == "same_meaning"
    assert dict(match.attributes) == {"state": "area", "revenue": "sales"}


def test_same_source_at_a_different_grain_is_not_a_match():
    models = {"customer_totals": "SELECT customer_id, SUM(amount) AS total FROM proj.raw.orders GROUP BY customer_id"}
    result = find_overlaps(build(models), TARGET_BY_REGION)
    assert result.matches == ()
    assert result.compared == 1


def test_shared_source_columns_alone_are_not_a_full_match():
    models = {"other": "SELECT region_id, MAX(amount) AS biggest FROM proj.raw.orders GROUP BY region_id"}
    result = find_overlaps(build(models), "SELECT region_id, MIN(amount) AS smallest FROM proj.raw.orders GROUP BY region_id")
    assert [m.kind for m in result.matches] == ["partial"]  # only the key matches
    assert dict(result.matches[0].attributes) == {"region_id": "region_id"}
    assert result.matches[0].confidence == "low"


def test_same_grain_narrower_existing_filter_is_partial():
    match = by_table(find_overlaps(build({"paid": PAID_BY_REGION}), BY_REGION))["paid"]
    assert (match.kind, match.confidence) == ("partial", "low")
    assert {c.kind: c.outcome for c in match.checks}["row_scope"] == "differs"


def test_wider_existing_table_that_the_target_can_filter_is_contains():
    models = {"all_rows": "SELECT region_id, status, SUM(amount) AS total FROM proj.raw.orders GROUP BY region_id, status"}
    sql = "SELECT region_id, status, SUM(amount) AS total FROM proj.raw.orders WHERE status = 'paid' GROUP BY region_id, status"
    match = by_table(find_overlaps(build(models), sql))["all_rows"]
    assert (match.kind, match.confidence) == ("contains", "medium")


def test_wider_table_missing_the_filter_column_is_only_partial():
    match = by_table(find_overlaps(build({"all_rows": BY_REGION}), PAID_BY_REGION))["all_rows"]
    assert match.kind == "partial"
    assert "does not provide" in match.checks[2].detail


def test_row_scope_that_cannot_be_compared_is_partial():
    models = {"sample": BY_REGION + " LIMIT 10"}
    match = by_table(find_overlaps(build(models), BY_REGION))["sample"]
    assert match.kind == "partial"
    assert match.checks[2].outcome == "unknown"


def test_unknown_lineage_candidate_is_unknown_with_reason():
    models = {"opaque": "SELECT * FROM proj.raw.not_declared"}
    result = find_overlaps(build(models), TARGET_BY_REGION)
    match = by_table(result)["opaque"]
    assert match.kind == "unknown"
    assert match.reason.startswith("incomplete_lineage")
    assert result.skipped == {"incomplete_lineage": 1}
    assert result.compared == 0
    assert "compared 0 of 1 tables" in result.summary


def test_unknown_grain_candidate_is_unknown_never_a_match():
    models = {"rows": "SELECT region_id, amount FROM proj.raw.orders"}
    match = by_table(find_overlaps(build(models), "SELECT region_id, amount FROM proj.raw.orders"))["rows"]
    assert match.kind == "unknown" and match.reason.startswith("grain_unknown")


def test_declared_grain_makes_a_source_comparable():
    models = {"rows": "SELECT customer_id, amount FROM proj.raw.orders"}
    sql = "SELECT customer_id, amount FROM proj.raw.orders"
    match = by_table(find_overlaps(build(models), sql, declared_grain={"proj.raw.orders": ["customer_id"]}))["rows"]
    assert match.kind == "same_meaning"


def test_target_with_unknown_grain_matches_nothing():
    result = find_overlaps(build({"state_totals": BY_REGION}), "SELECT region_id, amount FROM proj.raw.orders")
    assert [m.kind for m in result.matches] == ["unknown"]
    assert "target" in result.matches[0].reason


def test_outside_scope_candidates_are_skipped_and_counted():
    models = {"state_totals": BY_REGION, "old_totals": BY_REGION}
    pipeline = build(models, datasets={"old_totals": "archive"})
    result = find_overlaps(pipeline, TARGET_BY_REGION, scope=Scope("core", {"dataset": ("core",)}))
    assert [m.table for m in result.matches] == ["proj.core.state_totals"]
    assert result.skipped == {"outside_scope": 1}
    assert result.candidates_in_scope == 1 and result.compared == 1
    assert "outside_scope 1" in result.summary


def test_scope_can_match_on_project_and_table():
    pipeline = build({"state_totals": BY_REGION, "other": BY_REGION})
    scope = Scope("one", {"project": ("PROJ",), "table": ("state_*",)})
    result = find_overlaps(pipeline, TARGET_BY_REGION, scope=scope)
    assert [m.table for m in result.matches] == ["proj.core.state_totals"]
    assert result.skipped == {"outside_scope": 1}


def test_no_match_reports_coverage():
    models = {"customer_totals": "SELECT customer_id, SUM(amount) AS total FROM proj.raw.orders GROUP BY customer_id"}
    data = find_overlaps(build(models), TARGET_BY_REGION).to_json()
    assert data["matches"] == []
    assert data["summary"].startswith("compared 1 of 1 tables; 0 skipped")
    assert "No match among the compared tables" in data["summary"]


def test_existing_model_target_does_not_match_itself():
    pipeline = build({"state_totals": BY_REGION, "copy": BY_REGION.replace("total", "t2")})
    result = find_overlaps(pipeline, model="proj.core.state_totals")
    assert [m.table for m in result.matches] == ["proj.core.copy"]
    assert result.matches[0].kind == "same_meaning"
    assert result.compared == 1
    alone = find_overlaps(build({"state_totals": BY_REGION}), model="proj.core.state_totals")
    assert alone.matches == () and alone.compared == 0
    assert alone.to_json()["summary"].startswith("compared 0 of 0 tables")


def test_mirrored_copy_in_another_dataset_matches():
    models = {"mirror": "SELECT region_id, total FROM proj.core.state_totals", "state_totals": BY_REGION}
    result = find_overlaps(build(models, datasets={"mirror": "mirror"}), model="proj.core.state_totals")
    assert by_table(result)["mirror"].kind in ("same_meaning", "unknown")


def test_matches_are_sorted_by_kind():
    models = {"z_same": BY_REGION, "a_partial": PAID_BY_REGION, "m_unknown": "SELECT * FROM proj.raw.not_declared"}
    result = find_overlaps(build(models), BY_REGION)
    assert [m.kind for m in result.matches] == ["same_meaning", "partial", "unknown"]


def test_roles_are_attached_from_argument_and_computed_by_default():
    pipeline = build({"state_totals": BY_REGION})
    given = {"proj.core.state_totals": TableRole("proj.core.state_totals", "fact", "medium")}
    match = find_overlaps(pipeline, TARGET_BY_REGION, roles=given).matches[0]
    assert (match.role.role, match.role.confidence) == ("fact", "medium")
    assert match.to_json()["role"] == {"role": "fact", "confidence": "medium"}
    computed = find_overlaps(pipeline, TARGET_BY_REGION).matches[0]
    assert computed.role is not None and computed.role.role in ("dimension", "fact", "bridge", "unknown")


def test_json_carries_checks_and_no_query_text():
    data = find_overlaps(build({"state_totals": BY_REGION}), TARGET_BY_REGION).to_json()
    match = data["matches"][0]
    assert {c["kind"] for c in match["checks"]} == {"lineage", "grain", "row_scope"}
    assert "SUM(" not in str(match)


def test_requires_exactly_one_target():
    pipeline = build({"state_totals": BY_REGION})
    with pytest.raises(ValueError):
        find_overlaps(pipeline)
    with pytest.raises(ValueError):
        find_overlaps(pipeline, BY_REGION, model="proj.core.state_totals")
    with pytest.raises(ValueError):
        find_overlaps(pipeline, model="proj.core.missing")
