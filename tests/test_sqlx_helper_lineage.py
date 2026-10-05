import json

import pytest

from kumosql import ColumnRef, load_sqlx_project
from kumosql.cli import pipeline_main
from kumosql.resilience import PipelineLoadError
from kumosql.sqlx import expand_sqlx_interpolations

CALL = '${helpers.normalize("o.amount")}'


def project(root, sql=None):
    (root / 'workflow_settings.yaml').write_text('defaultProject: p\ndefaultDataset: d\n')
    definitions = root / 'definitions'
    definitions.mkdir()
    (definitions / 'raw.sqlx').write_text('config { type: "declaration" }\n')
    (definitions / 'orders.sqlx').write_text('SELECT id, amount, extra FROM ${ref("raw")}')
    (definitions / 'result.sqlx').write_text(sql or (
        'WITH c AS (SELECT o.id, ' + CALL + ' + o.id AS value FROM ${ref("orders")} o) '
        'SELECT id, value FROM c'
    ))
    (definitions / 'final.sqlx').write_text('SELECT value FROM ${ref("result")}')
    return root


def load(root, **kwargs):
    return load_sqlx_project(root, source_schema={'p.d.raw': {'id': 'INT64', 'amount': 'FLOAT64', 'extra': 'STRING'}}, **kwargs)


def test_unresolved_helper_retains_visible_edges_and_unknown_through_cte_and_model(tmp_path):
    pipeline = load(project(tmp_path))
    value = ColumnRef('p.d.result', 'value')
    record = pipeline.explain_lineage()[value]
    assert record.status == 'unknown'
    assert record.reason == 'unresolved_template'
    assert record.sources == {ColumnRef('p.d.orders', 'id')}
    assert pipeline.explain_lineage()[ColumnRef('p.d.result', 'id')].status == 'traced'
    assert not pipeline.trace_column(ColumnRef('p.d.final', 'value')).complete
    assert "p.d.orders" not in pipeline.dead_columns()
    assert any(d.code == 'unresolved_template' and 'generated SQL' in d.message for d in pipeline.all_diagnostics())
    assert all('__sqlx_token_' not in str(ref) for refs in pipeline.column_lineage().values() for ref in refs)
    assert all('__sqlx_token_' not in str(ref) for refs in pipeline.consumed_columns().values() for ref in refs)


def test_supplied_sql_traces_helper_through_cte_and_model_chain(tmp_path):
    pipeline = load(project(tmp_path), interpolation_sql={CALL: 'ROUND(o.amount, 2)'})
    value = ColumnRef('p.d.result', 'value')
    assert pipeline.column_lineage()[value] == {ColumnRef('p.d.orders', 'amount'), ColumnRef('p.d.orders', 'id')}
    trace = pipeline.trace_column(ColumnRef('p.d.final', 'value'))
    assert trace.complete
    assert trace.sources == {ColumnRef('p.d.raw', 'amount'), ColumnRef('p.d.raw', 'id')}
    assert pipeline.dead_columns()['p.d.orders'] == ('extra',)
    assert not pipeline.models['p.d.result'].masked_expressions


@pytest.mark.parametrize('sql', [
    'SELECT ${helper()} AS value FROM ${ref("orders")}',
    "SELECT '${helper()}' AS value FROM ${ref(\"orders\")}",
    'SELECT 1 AS value UNION ALL SELECT ${helper()} AS value FROM ${ref("orders")}',
    'SELECT o.id AS value FROM ${ref("orders")} o UNION ALL SELECT ${helper()} AS value FROM ${ref("orders")}',
])
def test_opaque_helper_is_never_constant_or_fake_source(sql, tmp_path):
    pipeline = load(project(tmp_path, sql))
    record = pipeline.explain_lineage()[ColumnRef('p.d.result', 'value')]
    assert record.status == 'unknown'
    assert record.reason == 'unresolved_template'
    assert all('__sqlx_token_' not in str(ref) for ref in record.sources)
    assert pipeline.dead_columns() == {}


def test_expansion_can_emit_projection_list_and_ref(tmp_path):
    sql = 'SELECT ${helper()} FROM ${ref("orders")} o'
    expansion = 'o.amount AS value, (SELECT MAX(extra) FROM ${ref("raw")}) AS label'
    pipeline = load(project(tmp_path, sql), interpolation_sql={'${helper()}': expansion})
    assert pipeline.output_columns('p.d.result') == ('value', 'label')
    assert pipeline.column_lineage()[ColumnRef('p.d.result', 'value')] == {ColumnRef('p.d.orders', 'amount')}
    assert pipeline.column_lineage()[ColumnRef('p.d.result', 'label')] == {ColumnRef('p.d.raw', 'extra')}


def test_exact_expansions_preserve_comments_and_do_not_recurse():
    sql = '-- ${helper()}\nSELECT ${helper()}, ${helper(1)} /* ${helper()} */'
    assert expand_sqlx_interpolations(sql, {'${helper()}': '${other()}', '${other()}': '42'}) == (
        '-- ${helper()}\nSELECT ${other()}, ${helper(1)} /* ${helper()} */'
    )


@pytest.mark.parametrize('expansion', ['1', "'constant'", ''])
def test_supplied_sql_can_be_constant_or_empty_fragment(expansion, tmp_path):
    sql = 'SELECT ${helper()} 1 AS value FROM ${ref("orders")}' if not expansion else 'SELECT ${helper()} AS value FROM ${ref("orders")}'
    pipeline = load(project(tmp_path, sql), interpolation_sql={'${helper()}': expansion})
    assert pipeline.explain_lineage()[ColumnRef('p.d.result', 'value')].status == 'constant'


@pytest.mark.parametrize('bad', [[], {'${helper()}': 42}, {42: 'id'}])
def test_invalid_expansion_mapping_is_actionable(bad, tmp_path):
    with pytest.raises(PipelineLoadError, match='mapping expression text to SQL strings'):
        load(project(tmp_path), interpolation_sql=bad)


def test_cli_expansions_show_complete_lineage(tmp_path, capsys):
    project(tmp_path)
    expansions = tmp_path / 'expansions.json'
    expansions.write_text(json.dumps({CALL: 'o.amount'}))
    assert pipeline_main([str(tmp_path), '--interpolation-sql', str(expansions)]) == 0
    report = json.loads(capsys.readouterr().out)
    row = next(row for row in report['column_lineage'] if row['node'] == 'p.d.result' and row['column'] == 'value')
    assert row['status'] == 'traced'
    assert row['complete']
    assert {'node': 'p.d.orders', 'column': 'amount'} in row['sources']


def test_cli_invalid_mapping_returns_load_error(tmp_path, capsys):
    project(tmp_path)
    expansions = tmp_path / 'expansions.json'
    expansions.write_text('[1]')
    assert pipeline_main([str(tmp_path), '--interpolation-sql', str(expansions)]) == 2
    assert 'interpolation SQL' in capsys.readouterr().err


def test_unaliased_helper_has_unknown_outputs_instead_of_sentinel_column(tmp_path):
    pipeline = load(project(tmp_path, 'SELECT o.id, ${helper()} FROM ${ref("orders")} o'))
    assert pipeline.output_columns('p.d.result') == ('id', '*')
    record = pipeline.explain_lineage()[ColumnRef('p.d.result', '*')]
    assert record.status == 'unknown'
    assert record.reason == 'unresolved_template'
    assert 'p.d.orders' not in pipeline.dead_columns()
    assert all('__sqlx_token_' not in str(ref) for ref in pipeline.explain_lineage())


def test_unknown_helper_keeps_hidden_column_changes_unknown(tmp_path):
    pipeline = load(project(tmp_path, 'SELECT ${helper()} AS value FROM ${ref("orders")}'))
    impact = pipeline.assess_change('drop_column', 'p.d.orders', 'extra')
    assert not impact.complete
    assert 'p.d.result' in {item.model for item in impact.unknown}


def test_expansion_qualifies_join_aliases_without_cross_wiring(tmp_path):
    sql = 'SELECT ${helper()} AS value FROM ${ref("orders")} o JOIN ${ref("raw")} r ON o.id = r.id'
    pipeline = load(project(tmp_path, sql), interpolation_sql={'${helper()}': 'o.amount + r.amount'})
    assert pipeline.column_lineage()[ColumnRef('p.d.result', 'value')] == {
        ColumnRef('p.d.orders', 'amount'), ColumnRef('p.d.raw', 'amount'),
    }


def test_unknown_source_schema_does_not_make_helper_a_fake_column(tmp_path):
    pipeline = load_sqlx_project(project(tmp_path))
    record = pipeline.explain_lineage()[ColumnRef('p.d.result', 'value')]
    assert record.status == 'unknown'
    assert record.reason == 'unresolved_template'
    assert all('__sqlx_token_' not in str(ref) for ref in record.sources)


def test_invalid_generated_sql_is_reported_as_unknown(tmp_path):
    pipeline = load(project(tmp_path), interpolation_sql={CALL: 'ROUND('})
    assert not pipeline.trace_column(ColumnRef('p.d.result', 'value')).complete
    assert any(d.code == 'parse_error' for d in pipeline.all_diagnostics())


def test_cli_rejects_expansions_for_compiled_graph(tmp_path, capsys):
    graph = tmp_path / 'compiled.json'
    graph.write_text('{"tables": []}')
    expansions = tmp_path / 'expansions.json'
    expansions.write_text('{}')
    assert pipeline_main([str(graph), '--interpolation-sql', str(expansions)]) == 2
    assert 'requires a SQLX project folder' in capsys.readouterr().err


def test_real_identifier_that_resembles_token_is_kept(tmp_path):
    project(tmp_path, 'SELECT o.__sqlx_token_000__ AS real, ${helper()} AS value FROM ${ref("orders")} o')
    (tmp_path / 'definitions' / 'orders.sqlx').write_text('SELECT id, 1 AS __sqlx_token_000__ FROM ${ref("raw")}')
    pipeline = load(tmp_path)
    assert pipeline.models['p.d.result'].masked_tokens == ('__sqlx_token_001__',)
    assert pipeline.column_lineage()[ColumnRef('p.d.result', 'real')] == {ColumnRef('p.d.orders', '__sqlx_token_000__')}
    assert pipeline.explain_lineage()[ColumnRef('p.d.result', 'real')].status == 'traced'
    assert pipeline.explain_lineage()[ColumnRef('p.d.result', 'value')].status == 'unknown'


def test_token_like_literal_without_helper_is_constant(tmp_path):
    pipeline = load(project(tmp_path, "SELECT '__sqlx_token_000__' AS value FROM ${ref(\"orders\")}"))
    assert pipeline.explain_lineage()[ColumnRef('p.d.result', 'value')].status == 'constant'


def test_helper_token_metadata_survives_saved_snapshot(tmp_path):
    from kumosql.storage import pipeline_from_snapshot, pipeline_snapshot

    pipeline = load(project(tmp_path))
    restored = pipeline_from_snapshot(json.loads(json.dumps(pipeline_snapshot(pipeline, 'helpers'))), 'helpers')
    assert restored.models == pipeline.models
    assert restored.models['p.d.result'].masked_tokens == ('__sqlx_token_000__',)
    assert restored.explain_lineage()[ColumnRef('p.d.result', 'value')].reason == 'unresolved_template'


@pytest.mark.parametrize('contents', ['null', '{"${helper()}": 1}'])
def test_cli_rejects_invalid_expansion_values(contents, tmp_path, capsys):
    project(tmp_path)
    expansions = tmp_path / 'expansions.json'
    expansions.write_text(contents)
    assert pipeline_main([str(tmp_path), '--interpolation-sql', str(expansions)]) == 2
    assert 'interpolation SQL' in capsys.readouterr().err
