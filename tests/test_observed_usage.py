import json

from kumosql import Scope, load_compiled_graph
from kumosql.observed_usage import MIN_READERS, observed_usage, observed_usage_report

EV = "`p.raw.events`"
EN = "`p.raw.entity`"


def build():
    graph = {
        "tables": [],
        "declarations": [{"target": {"database": "p", "schema": "raw", "name": n}} for n in ("events", "entity")],
    }
    return load_compiled_graph(graph)


def rec(i, text, reader=None, **extra):
    row = {
        "job_id": f"job-{i}",
        "creation_time": "2026-01-01T00:00:00Z",
        "destination": None,
        "referenced_tables": ["p.raw.events", "p.raw.entity"],
        "query": text,
        "reader": reader if reader is not None else f"reader-{i}",
    }
    row.update(extra)
    return row


LOOKUP = f"SELECT e.entity_key, d.label FROM {EV} e JOIN {EN} d ON e.entity_key = d.entity_key"


def test_join_sides_and_columns():
    result = observed_usage(build(), [rec(i, LOOKUP) for i in range(4)])
    entity, events = result.tables["p.raw.entity"], result.tables["p.raw.events"]
    assert entity.joined_as == {"lookup_side": 4, "driving_side": 0, "not_joined": 0}
    assert events.joined_as == {"lookup_side": 0, "driving_side": 4, "not_joined": 0}
    assert entity.join_columns == {"entity_key": 4}
    assert entity.columns_selected == {"label": 4}
    assert events.columns_selected == {"entity_key": 4}
    assert entity.confidence == "medium"


def test_group_aggregate_select_not_joined():
    text = f"SELECT group_id, region, SUM(amount) AS total FROM {EV} GROUP BY 1, region HAVING MAX(quantity) > 1"
    usage = observed_usage(build(), [rec(i, text) for i in range(3)]).tables["p.raw.events"]
    assert usage.joined_as["not_joined"] == 3
    assert set(usage.columns_grouped) == {"group_id", "region"}
    assert set(usage.columns_aggregated) == {"amount", "quantity"}
    assert set(usage.columns_selected) == {"group_id", "region"}


def test_right_join_flips_sides_and_using():
    text = f"SELECT d.label FROM {EV} e RIGHT JOIN {EN} d USING (entity_key)"
    result = observed_usage(build(), [rec(1, text)])
    assert result.tables["p.raw.entity"].joined_as["driving_side"] == 1
    assert result.tables["p.raw.events"].joined_as["lookup_side"] == 1
    assert result.tables["p.raw.events"].join_columns == {"entity_key": 1}


def test_counted_by_readers_not_runs():
    same_job = [rec(i, LOOKUP, reader="scheduler") for i in range(50)]
    many_people = [rec(100 + i, LOOKUP) for i in range(5)]
    result = observed_usage(build(), same_job + many_people)
    assert result.tables["p.raw.entity"].readers_examined == 6
    assert result.tables["p.raw.entity"].joined_as["lookup_side"] == 6
    assert result.records_examined == 55


def test_reader_falls_back_to_destination_then_text():
    rows = [rec(i, LOOKUP, destination="p.out.t") for i in range(5)]
    for row in rows:
        row.pop("reader")
    assert observed_usage(build(), rows).tables["p.raw.entity"].readers_examined == 1
    for row in rows:
        row["destination"] = None
    assert observed_usage(build(), rows).tables["p.raw.entity"].readers_examined == 1


def test_small_sample_is_low_and_large_is_high():
    small = observed_usage(build(), [rec(i, LOOKUP) for i in range(MIN_READERS - 1)])
    assert small.tables["p.raw.entity"].confidence == "low"
    assert "small sample" in small.tables["p.raw.entity"].reason
    large = observed_usage(build(), [rec(i, LOOKUP) for i in range(12)])
    assert large.tables["p.raw.entity"].confidence == "high"


def test_unexamined_records_are_counted_with_reasons():
    rows = [rec(i, LOOKUP) for i in range(4)]
    rows += [rec(10, None), rec(11, "   "), rec(12, LOOKUP, query_truncated=True), rec(13, "SELEC FROM ((")]
    rows += [rec(14, "SELECT 1 FROM `p.other.thing`", referenced_tables=["p.other.thing"])]
    rows += ["not a record"]
    result = observed_usage(build(), rows)
    assert result.records_total == 10
    assert result.records_examined == 4
    assert result.records_unexamined == {
        "invalid_record": 1,
        "no_query_text": 2,
        "truncated": 1,
        "parse_error": 1,
        "no_known_tables": 1,
    }
    entity = result.tables["p.raw.entity"]
    assert entity.readers_examined == 4
    assert entity.readers_unexamined == 4
    assert entity.unexamined_reasons == {"no_query_text": 2, "parse_error": 1, "truncated": 1}
    assert entity.confidence == "medium"
    assert "p.other.thing" not in result.tables


def test_scope_filters_records_and_counts_them():
    rows = [rec(i, LOOKUP, team="a") for i in range(3)] + [rec(9, LOOKUP, team="b")]
    result = observed_usage(build(), rows, scope=Scope("s", {"team": ("a",)}))
    assert result.records_unexamined == {"out_of_scope": 1}
    assert result.tables["p.raw.entity"].readers_examined == 3


def test_mostly_unexamined_is_low():
    rows = [rec(i, LOOKUP) for i in range(3)] + [rec(10 + i, None) for i in range(8)]
    assert observed_usage(build(), rows).tables["p.raw.entity"].confidence == "low"


def test_records_without_text_still_count_as_reads():
    result = observed_usage(build(), [rec(i, None) for i in range(4)])
    usage = result.tables["p.raw.entity"]
    assert usage.readers_examined == 0 and usage.readers_unexamined == 4
    assert usage.columns_selected == {} and usage.confidence == "low"
    assert result.readers_unexamined == 4


def test_wildcard_sharded_temporary_and_ctes():
    shard = "SELECT COUNT(1) FROM `p.raw.events_*`"
    dated = "SELECT x FROM `p.raw.events_20260101`"
    temp = (
        f"CREATE TEMP TABLE scratch AS SELECT entity_key FROM {EV}; "
        f"SELECT s.entity_key FROM scratch s JOIN {EN} d ON s.entity_key = d.entity_key"
    )
    cte = "WITH entity AS (SELECT 1 AS entity_key) SELECT entity_key FROM entity"
    result = observed_usage(build(), [rec(1, shard), rec(2, dated), rec(3, temp), rec(4, cte)])
    assert result.tables["p.raw.events"].readers_examined == 3
    assert result.sharded_references_folded == 2
    assert result.temporary_references_ignored >= 1
    assert result.records_unexamined == {"no_known_tables": 1}
    assert result.tables["p.raw.entity"].readers_examined == 1


def test_multi_statement_scripts_are_examined():
    script = f"SELECT label FROM {EN}; SELECT SUM(amount) FROM {EV}"
    result = observed_usage(build(), [rec(1, script)])
    assert result.tables["p.raw.entity"].columns_selected == {"label": 1}
    assert result.tables["p.raw.events"].columns_aggregated == {"amount": 1}


def test_star_and_unqualified_ambiguous_columns():
    result = observed_usage(build(), [rec(1, f"SELECT * FROM {EV} e JOIN {EN} d ON e.k = d.k")])
    assert result.tables["p.raw.events"].columns_selected == {"*": 1}
    ambiguous = observed_usage(build(), [rec(1, f"SELECT label FROM {EV} e JOIN {EN} d ON e.k = d.k")])
    assert ambiguous.tables["p.raw.entity"].columns_selected == {}


def test_report_has_no_text_or_identities():
    text = f"SELECT d.label FROM {EV} e JOIN {EN} d ON e.entity_key = d.entity_key WHERE e.note = 'secret-literal'"
    rows = [rec(i, text, reader=f"identifying-{i}") for i in range(4)]
    report = observed_usage_report(build(), rows)
    blob = json.dumps(report)
    for banned in ("identifying-", "secret-literal", "SELECT", "job-"):
        assert banned not in blob
    assert report["tables"]["p.raw.entity"]["readers_examined"] == 4


def test_never_raises_on_garbage():
    result = observed_usage(build(), [None, 5, {}, {"query": 3, "referenced_tables": 7}, rec(1, LOOKUP)])
    assert result.records_total == 5
    assert observed_usage(build(), []).tables == {}
