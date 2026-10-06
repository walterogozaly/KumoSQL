"""Where a difference explanation ("equivalent except when P", issue #512) is shown: API, CLI, Compare page, change report.

The core ``explain_difference`` is replaced by a fake here, so these tests check the wiring only: when the search is asked
for, what is shown, and that nothing changes while the option is off.
"""

import json
import shutil
import subprocess
from pathlib import Path
from threading import Thread
from urllib.request import Request

import pytest
from test_change_report import by_model, project, report
from ui_http import urlopen

from kumosql import difference_explanation
from kumosql.difference_explanation import DifferenceExplanation

SETTINGS = Path(__file__).resolve().parent.parent / "src" / "kumosql" / "static" / "settings.js"
FAKE = DifferenceExplanation(sql="x = 5", atoms=("x = 5",), tables=("t",), exact=True)
FAKE_JSON = {"sql": "x = 5", "atoms": ["x = 5"], "tables": ["t"], "exact": True}
LEFT, RIGHT = "SELECT id FROM t WHERE x > 5", "SELECT id FROM t WHERE x >= 5"


@pytest.fixture
def fake(monkeypatch):
    """Replace the core; the returned list records each (left, right, options) the surfaces ask about."""

    calls = []

    def explain(left, right, **options):
        calls.append((left, right, options))
        return FAKE

    monkeypatch.setattr(difference_explanation, "explain_difference", explain)
    return calls


def test_prove_queries_is_unchanged_unless_asked(fake):
    pytest.importorskip("z3")
    from kumosql import pipeline_equivalence

    off = pipeline_equivalence.prove_queries(LEFT, RIGHT)
    assert off["status"] == "not_equivalent"
    assert "except_when" not in off
    assert fake == []
    on = pipeline_equivalence.prove_queries(LEFT, RIGHT, explain=True)
    assert on["except_when"] == FAKE_JSON
    assert {k: v for k, v in on.items() if k != "except_when"} == off
    assert fake[0][:2] == (LEFT, RIGHT)
    assert isinstance(fake[0][2]["timeout_ms"], int)


def test_prove_queries_asks_only_about_refuted_pairs(fake):
    pytest.importorskip("z3")
    from kumosql import pipeline_equivalence

    same = pipeline_equivalence.prove_queries("SELECT id FROM t WHERE x > 5 AND TRUE", "SELECT id FROM t WHERE x > 5", explain=True)
    assert same["status"] == "proven_equivalent"
    assert "except_when" not in same
    assert fake == []


@pytest.mark.parametrize("outcome", ["none", "raises", "empty"])
def test_no_verified_predicate_shows_nothing(monkeypatch, outcome):
    pytest.importorskip("z3")
    from kumosql import pipeline_equivalence

    def explain(left, right, **options):
        if outcome == "raises":
            raise RuntimeError("search failed")
        return None if outcome == "none" else DifferenceExplanation(sql="", atoms=(), tables=(), exact=False)

    monkeypatch.setattr(difference_explanation, "explain_difference", explain)
    result = pipeline_equivalence.prove_queries(LEFT, RIGHT, explain=True)
    assert result["status"] == "not_equivalent"
    assert "except_when" not in result


def test_api_option_is_the_explain_field(fake):
    pytest.importorskip("z3")
    from kumosql.ui import UIHandler, UIServer

    server = UIServer(("127.0.0.1", 0), UIHandler)
    thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()

    def prove(**extra):
        request = Request(
            f"http://127.0.0.1:{server.server_port}/api/prove-queries",
            data=json.dumps({"left": LEFT, "right": RIGHT, **extra}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with urlopen(request, timeout=30) as response:
            return json.load(response)

    try:
        assert "except_when" not in prove()
        assert "except_when" not in prove(explain="yes")
        assert prove(explain=True)["except_when"] == FAKE_JSON
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def sql_files(tmp_path, left, right):
    a, b = tmp_path / "a.sql", tmp_path / "b.sql"
    a.write_text(left)
    b.write_text(right)
    return str(a), str(b)


def test_prove_sql_equivalent_prints_it_behind_a_flag(fake, tmp_path, capsys):
    from kumosql.cli import prove_main

    files = sql_files(tmp_path, LEFT, RIGHT)
    assert prove_main(list(files)) == 2
    assert "except when" not in capsys.readouterr().out
    assert fake == []
    assert prove_main([*files, "--explain-difference"]) == 2
    out = capsys.readouterr().out.splitlines()
    assert "except when: x = 5" in out
    assert "exact: yes" in out


def test_prove_sql_equivalent_asks_nothing_about_a_proven_pair(fake, tmp_path, capsys):
    from kumosql.cli import prove_main

    files = sql_files(tmp_path, LEFT, LEFT)
    assert prove_main([*files, "--explain-difference"]) == 0
    assert "except when" not in capsys.readouterr().out
    assert fake == []


def test_prove_sql_smt_adds_it_to_the_json_behind_a_flag(fake, tmp_path, capsys):
    pytest.importorskip("z3")
    from kumosql.smt_equivalence import main

    files = sql_files(tmp_path, LEFT, RIGHT)
    main(list(files))
    assert "except_when" not in json.loads(capsys.readouterr().out)
    assert fake == []
    main([*files, "--explain-difference", "--timeout-ms", "3000"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "not_equivalent"
    assert payload["except_when"] == FAKE_JSON
    assert fake[0][2]["timeout_ms"] == 3000


def test_change_report_adds_it_per_unproven_model_when_enabled(fake, tmp_path):
    base = project(tmp_path / "base", stage_where="amount > 0")
    head = project(tmp_path / "head", stage_where="amount >= 0")
    off = by_model(report(base, head))
    assert not any("except_when" in change for change in off.values())
    assert fake == []
    on = by_model(report(base, head, explain_differences=True))
    stage = next(change for name, change in on.items() if name.endswith("stg"))
    assert stage["verification"]["label"] == "unproven"
    assert stage["except_when"] == FAKE_JSON
    assert [change for change in on.values() if "except_when" in change] == [stage]
    assert len(fake) == 1


def test_change_report_cli_flag(fake, tmp_path, capsys):
    from kumosql.change_report import change_report_main

    base = project(tmp_path / "base", stage_where="amount > 0")
    head = project(tmp_path / "head", stage_where="amount >= 0")
    assert change_report_main([str(base), str(head), "--no-overlaps"]) == 0
    assert "except_when" not in capsys.readouterr().out
    assert change_report_main([str(base), str(head), "--no-overlaps", "--explain-differences"]) == 0
    assert '"except_when"' in capsys.readouterr().out


HARNESS = r"""
const fs = require("fs");
const vm = require("vm");

class Node {
  constructor(tag) { this.tag = tag; this.children = []; this.attrs = {}; this.listeners = {}; this._text = ""; this.className = ""; this.value = ""; this.checked = false; this.dataset = {}; }
  append(...kids) { for (const kid of kids) this.children.push(typeof kid === "string" ? Object.assign(new Node("#text"), { _text: kid }) : kid); }
  replaceChildren(...kids) { this.children = []; this.append(...kids); }
  setAttribute(key, value) { this.attrs[key] = value; }
  addEventListener(name, fn) { (this.listeners[name] ||= []).push(fn); }
  set textContent(value) { this._text = String(value); this.children = []; }
  get textContent() { return this._text + this.children.map((kid) => kid.textContent).join(" "); }
  all() { return [this, ...this.children.flatMap((kid) => kid.all())]; }
}

const sent = [];
const replies = {
  "GET /api/prover": { enabled: true, timeout_ms: 1000, bounded_rows: 0, available: true, tables: 0, constrained: 0 },
  "GET /api/equivalences": { equivalences: [] },
  "POST /api/prove-queries": JSON.parse(process.argv[3]),
};
const document = {
  createElement: (tag) => new Node(tag), createElementNS: (_, tag) => new Node(tag),
  addEventListener() {}, getElementById: () => null, documentElement: new Node("html"), body: new Node("body"),
};
const sandbox = {
  document, console, location: {}, localStorage: { getItem: () => null, setItem() {} }, setTimeout, clearTimeout,
  fetch: async (url, options = {}) => {
    if (options.body) sent.push(JSON.parse(options.body));
    const reply = replies[`${options.method || "GET"} ${url}`];
    return { ok: Boolean(reply), json: async () => reply || { error: "no reply" } };
  },
};
sandbox.window = sandbox;
let source = fs.readFileSync(process.argv[2], "utf8");
source = source.replace(/\}\)\(\);\s*$/, "window.__renderSolver = renderSolver;\n})();");
vm.runInNewContext(source, sandbox);

(async () => {
  const body = new Node("div");
  await sandbox.__renderSolver(body);
  const click = body.all().find((node) => node.tag === "button" && node.textContent.trim() === "Prove equivalent");
  await Promise.all(click.listeners.click.map((fn) => fn()));
  const detail = body.all().filter((node) => node.className === "sp-query-detail")[1];
  const lines = detail.all().filter((node) => node.className === "sp-except").map((node) => node.textContent.replace(/\s+/g, " ").trim());
  const pre = detail.all().filter((node) => node.tag === "pre").length;
  console.log(JSON.stringify({ lines, pre, sent }));
})();
"""

COUNTEREXAMPLE = {"tables": {"t": [{"id": 1, "x": 5}]}, "left_rows": [], "right_rows": [[1]]}


def compare_page(tmp_path, reply):
    harness = tmp_path / "harness.js"
    harness.write_text(HARNESS)
    run = subprocess.run(["node", str(harness), str(SETTINGS), json.dumps(reply)], capture_output=True, text=True, timeout=60)
    assert run.returncode == 0, run.stderr[-800:]
    return json.loads(run.stdout.strip().splitlines()[-1])


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_compare_page_shows_the_predicate_next_to_the_counterexample(tmp_path):
    reply = {"status": "not_equivalent", "reason": "different", "assumptions": [], "counterexample": COUNTEREXAMPLE, "except_when": FAKE_JSON}
    shown = compare_page(tmp_path, reply)
    assert shown["lines"] == ["Equivalent except when x = 5"]
    assert shown["pre"] == 1
    assert shown["sent"][-1]["explain"] is True


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_compare_page_without_a_predicate_shows_nothing_extra(tmp_path):
    reply = {"status": "not_equivalent", "reason": "different", "assumptions": [], "counterexample": COUNTEREXAMPLE}
    assert compare_page(tmp_path, reply)["lines"] == []
