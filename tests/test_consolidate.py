"""Folding intermediate tables into one table, with the result proved equal."""

import json
from threading import Thread
from urllib.error import HTTPError
from urllib.request import Request
from ui_http import urlopen

import pytest

pytest.importorskip("z3")

from kumosql import consolidate, load_sqlx_project
from kumosql.prover_schema import from_pipeline
from kumosql.ui import UIHandler, UIServer

TABLE = 'config { type: "table" }\n'


def write(root, relative, text):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def build(root, **overrides):
    """upstream -> a -> (b, c) -> d, the diagram of the feature request."""

    files = {
        "a": TABLE + 'SELECT id, customer_id, amount, status FROM ${ref("raw","upstream")} WHERE amount IS NOT NULL\n',
        "b": TABLE + 'SELECT customer_id, SUM(amount) AS total FROM ${ref("a")} GROUP BY customer_id\n',
        "c": TABLE + "SELECT customer_id, COUNT(*) AS n FROM ${ref(\"a\")} WHERE status = 'paid' GROUP BY customer_id\n",
        "d": TABLE + 'SELECT b.customer_id, b.total, c.n FROM ${ref("b")} AS b JOIN ${ref("c")} AS c ON b.customer_id = c.customer_id\n',
    }
    files.update(overrides)
    write(root, "workflow_settings.yaml", "defaultProject: proj\ndefaultDataset: an\n")
    write(root, "definitions/up.sqlx", 'config { type: "declaration", schema: "raw", name: "upstream" }\n')
    for name, text in files.items():
        write(root, f"definitions/{name}.sqlx", text)
    return load_sqlx_project(root)


@pytest.fixture
def diamond(tmp_path):
    return build(tmp_path)


def test_a_diamond_folds_into_the_final_table_and_is_proved(diamond):
    result = consolidate.consolidate_tables(diamond, ["a", "b", "c"], "d")
    assert result.proven and result.status == "equivalent"
    assert result.target == "proj.an.d"
    assert result.folded == ["proj.an.a", "proj.an.b", "proj.an.c"]  # sources first
    # d reads the upstream table directly; no pipeline table is read any more
    assert "`proj.raw.upstream`" in result.sql
    assert "proj.an." not in result.sql
    assert result.sql.count("proj.raw.upstream") == 1  # the shared table a is written once
    assert result.original_sql != result.sql and result.assumptions


def test_the_original_pipeline_is_left_alone(diamond):
    before = {k: m.sql for k, m in diamond.models.items()}
    consolidate.consolidate_tables(diamond, ["a", "b", "c"], "d")
    assert {k: m.sql for k, m in diamond.models.items()} == before


def test_a_table_read_from_outside_the_set_is_never_folded(tmp_path):
    pipeline = build(tmp_path, e=TABLE + 'SELECT customer_id FROM ${ref("b")}\n')
    with pytest.raises(consolidate.ConsolidationError) as error:
        consolidate.consolidate_tables(pipeline, ["a", "b", "c"], "d")
    assert error.value.readers == {"proj.an.b": ["proj.an.e"]}
    assert "proj.an.e" in str(error.value)
    # leaving a out is also refused: c still reads it
    with pytest.raises(consolidate.ConsolidationError) as error:
        consolidate.consolidate_tables(pipeline, ["b", "c"], "d")
    assert error.value.readers == {"proj.an.b": ["proj.an.e"]}


def test_a_partial_fold_keeps_the_tables_left_out(diamond):
    result = consolidate.consolidate_tables(diamond, ["b", "c"], "d")
    assert result.proven
    assert "`proj.an.a`" in result.sql and "proj.an.b" not in result.sql


def test_names_are_resolved_the_way_the_pipeline_does(diamond):
    result = consolidate.consolidate_tables(diamond, ["proj.an.a", "B", "c", "d"], "an.d")  # the target in the list is ignored
    assert result.proven and result.folded == ["proj.an.a", "proj.an.b", "proj.an.c"]
    with pytest.raises(ValueError, match="not a model"):
        consolidate.consolidate_tables(diamond, ["nope"], "d")
    with pytest.raises(consolidate.ConsolidationError, match="at least one"):
        consolidate.consolidate_tables(diamond, ["d"], "d")


def test_only_tables_that_feed_the_target_are_folded(tmp_path):
    pipeline = build(tmp_path, z=TABLE + 'SELECT id FROM ${ref("raw","upstream")}\n')
    with pytest.raises(consolidate.ConsolidationError, match="nothing reads proj.an.z"):
        consolidate.consolidate_tables(pipeline, ["a", "b", "c", "z"], "d")


def test_incremental_tables_and_sources_are_refused(tmp_path):
    pipeline = build(tmp_path, a='config { type: "incremental" }\nSELECT id, customer_id, amount, status FROM ${ref("raw","upstream")}\n')
    with pytest.raises(consolidate.ConsolidationError, match="incremental"):
        consolidate.consolidate_tables(pipeline, ["a", "b", "c"], "d")
    with pytest.raises(ValueError, match="not a model"):  # a declared source is read, never folded
        consolidate.consolidate_tables(pipeline, ["upstream"], "a")


def test_names_do_not_clash_with_the_targets_own_with_tables(tmp_path):
    pipeline = build(tmp_path, d=TABLE + 'WITH a AS (SELECT customer_id, total FROM ${ref("b")}) SELECT a.customer_id, a.total, c.n FROM a JOIN ${ref("c")} AS c ON a.customer_id = c.customer_id\n')
    result = consolidate.consolidate_tables(pipeline, ["a", "b", "c"], "d")
    assert result.proven
    assert "a_2" in result.sql  # the folded table a gives way to d's own WITH table a


def test_a_fold_that_is_not_proved_is_unknown_and_never_claimed(diamond, monkeypatch):
    monkeypatch.setattr(consolidate, "check_observable", lambda *args, **kwargs: (False, [], "d: the prover could not decide"))
    result = consolidate.consolidate_tables(diamond, ["a", "b", "c"], "d")
    assert result.status == "unknown" and not result.proven and result.assumptions == []
    assert "could not decide" in result.reason and result.sql


def test_the_proof_catches_a_wrong_fold(diamond, monkeypatch):
    # negative control: a fold that drops a table's filter must not come back equivalent
    real = consolidate.fold_sql

    def wrong(pipeline, members, target):
        return real(pipeline, members, target).replace("NOT amount IS NULL", "TRUE")

    monkeypatch.setattr(consolidate, "fold_sql", wrong)
    result = consolidate.consolidate_tables(diamond, ["a", "b", "c"], "d")
    assert result.status == "unknown"


UNION_C = TABLE + 'SELECT customer_id, id AS n FROM ${ref("a")} UNION ALL SELECT customer_id, id FROM ${ref("a")} WHERE id > 5\n'
STAR_A = TABLE + 'WITH x AS (SELECT * FROM ${ref("raw","upstream")}) SELECT id, customer_id, amount, status FROM x WHERE amount > 0\n'


def with_source_columns(pipeline):
    schema = from_pipeline(pipeline)
    schema.columns["proj.raw.upstream"] = ["id", "customer_id", "amount", "status"]
    return schema


def test_a_fold_through_union_all_is_proved(tmp_path):
    pipeline = build(tmp_path, c=UNION_C)
    result = consolidate.consolidate_tables(pipeline, ["a", "b", "c"], "d")
    assert result.proven and result.sql.count("UNION ALL") == 1 and "proj.an." not in result.sql


def test_a_fold_through_union_all_and_a_with_table_inside_a_folded_table_is_proved(tmp_path):
    pipeline = build(tmp_path, a=STAR_A, c=UNION_C)
    result = consolidate.consolidate_tables(pipeline, ["a", "b", "c"], "d", schema=with_source_columns(pipeline))
    assert result.proven and "UNION ALL" in result.sql


def test_select_star_over_undeclared_columns_is_unknown_and_says_why(tmp_path):
    pipeline = build(tmp_path, a=STAR_A, c=UNION_C)
    result = consolidate.consolidate_tables(pipeline, ["a", "b", "c"], "d")
    assert result.status == "unknown" and "proj.raw.upstream" in result.reason and "not declared" in result.reason
    assert "UNION shapes differ" not in result.reason


def _drop_second_branch(sql):
    import sqlglot
    from sqlglot import exp

    tree = sqlglot.parse_one(sql, read="bigquery")
    union = tree.find(exp.Union)
    union.replace(union.this)
    return tree.sql(dialect="bigquery")


def test_a_wrong_union_all_fold_is_not_called_equivalent(tmp_path, monkeypatch):
    pipeline = build(tmp_path, c=UNION_C)
    real = consolidate.fold_sql
    mistakes = {
        "branch filter dropped": lambda sql: sql.replace("id > 5", "TRUE"),
        "UNION ALL became UNION": lambda sql: sql.replace("UNION ALL", "UNION DISTINCT"),
        "second branch lost": _drop_second_branch,
    }
    for label, damage in mistakes.items():
        monkeypatch.setattr(consolidate, "fold_sql", lambda p, m, t, damage=damage: damage(real(p, m, t)))
        result = consolidate.consolidate_tables(pipeline, ["a", "b", "c"], "d")
        assert not result.proven, label


def test_command_line(tmp_path, capsys):
    build(tmp_path)
    assert consolidate.main([str(tmp_path), "d", "a", "b", "c"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "equivalent" and out["folded"] == ["proj.an.a", "proj.an.b", "proj.an.c"]
    assert consolidate.main([str(tmp_path), "d", "a"]) == 2  # b and c still read it
    refused = json.loads(capsys.readouterr().out)
    assert refused["status"] == "refused" and refused["readers"] == {"proj.an.a": ["proj.an.b", "proj.an.c"]}


def _snapshot(root):
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


def test_command_line_never_changes_the_project_or_writes_anything(tmp_path, monkeypatch, capsys):
    """A bare run, a refused run and --help leave the project byte-identical and create no file anywhere."""

    project, cwd, home = tmp_path / "project", tmp_path / "cwd", tmp_path / "home"
    cwd.mkdir(), home.mkdir()
    build(project)
    before = _snapshot(project)
    monkeypatch.chdir(cwd)
    monkeypatch.setenv("KUMOSQL_HOME", str(home))
    assert consolidate.main([str(project), "d", "a", "b", "c"]) == 0
    assert consolidate.main([str(project), "d", "a"]) == 2
    with pytest.raises(SystemExit) as stop:
        consolidate.main(["--help"])
    assert stop.value.code == 0
    capsys.readouterr()
    assert _snapshot(project) == before
    assert list(cwd.iterdir()) == [] and list(home.iterdir()) == []


def test_help_says_the_command_is_a_read_only_preview_and_has_no_write_option(capsys):
    with pytest.raises(SystemExit):
        consolidate.main(["--help"])
    text = capsys.readouterr().out
    assert "READ-ONLY PREVIEW" in text and "changes nothing" in text
    assert "never writes, moves, renames or deletes" in text
    assert "python -m kumosql consolidate-tables path/to/project D A B C" in text
    for option in ("--write", "--apply", "--force", "--output", "--in-place", "--delete"):
        assert option not in text


def test_the_command_is_registered():
    from kumosql import __main__

    assert __main__.resolve("consolidate-tables") == "kumosql.consolidate:main"


@pytest.fixture
def ui_server():
    server = UIServer(("127.0.0.1", 0), UIHandler)
    thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_route_needs_a_loaded_project_and_names(ui_server, monkeypatch):
    from kumosql import live_graph

    monkeypatch.setattr(live_graph, "loaded", lambda: None)
    for body in (b"{}", b'{"tables": ["a"], "target": "d"}'):
        request = Request(ui_server + "/api/consolidate-tables", method="POST", data=body, headers={"Content-Type": "application/json"})
        with pytest.raises(HTTPError) as error:
            urlopen(request)
        assert error.value.code == 400


def test_route_folds_the_loaded_project(ui_server, diamond, monkeypatch):
    from kumosql import live_graph

    monkeypatch.setattr(live_graph, "loaded", lambda: {"pipeline": diamond})
    request = Request(
        ui_server + "/api/consolidate-tables", method="POST",
        data=json.dumps({"tables": ["a", "b", "c"], "target": "d"}).encode(), headers={"Content-Type": "application/json"},
    )
    with urlopen(request) as response:
        assert json.load(response)["status"] == "equivalent"
