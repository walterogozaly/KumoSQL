"""Project scan: every incremental action is checked, unsupported ones are reported, not guessed."""

import pytest

pytest.importorskip("duckdb")

from kumosql.incremental_scan import CONTRACTS, scan_project, summarise  # noqa: E402

COAL = "COALESCE((SELECT MAX(created_at) FROM ${self()}), TIMESTAMP '1970-01-01')"


def write(root, name, text):
    path = root / "definitions" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_scan_finds_proof_counterexample_and_unsupported(tmp_path):
    write(tmp_path, "good.sqlx", f'config {{ type: "incremental" }}\nselect id, created_at, v from ${{ref("raw")}}\n${{when(incremental(), `where created_at > {COAL}`)}}\n')
    write(tmp_path, "late.sqlx", 'config { type: "incremental", uniqueKey: ["id"] }\nselect id, created_at, v from ${ref("raw")}\n${when(incremental(), `where created_at > (select max(created_at) from ${self()})`)}\n')
    write(tmp_path, "js.sqlx", 'config { type: "incremental" }\nselect id from ${ref("raw")} where ${dateFilter("created_at", 3)}\n')
    write(tmp_path, "plain.sqlx", 'config { type: "table" }\nselect 1 as x\n')
    rows = scan_project(tmp_path, seeds=15)
    by = {(r.model, r.contract): r for r in rows}
    assert {r.model for r in rows} == {"good", "late", "js"}  # plain tables are not incremental
    assert by[("good", "append_only")].outcome == "safe"
    assert by[("good", "late_and_duplicate")].outcome == "diverges"
    assert by[("late", "append_only")].outcome == "diverges"  # no default for an empty table
    assert all(by[("js", c)].outcome == "unsupported" for c in CONTRACTS)
    assert summarise(rows)["append_only"]["unsupported"] == 1
