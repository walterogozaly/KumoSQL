"""The test-history recorder and its report (tools/test_history.py)."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import test_history as th  # noqa: E402


def record(label, failed, *, targets=("tests/test_a.py",), branch="feature", source="changed-tests", commit="c1", files=None, **extra):
    """A run record; ``failed`` is a list of (test id, inside the targets)."""

    return {
        "v": 1, "ts": extra.pop("ts", "2026-10-02T10:00:00Z"), "commit": commit, "branch": branch, "dirty": False, "label": label,
        "targets": list(targets), "targets_source": source, "mode": "full",
        "failed": [{"id": nodeid, "kind": "failed", "msg": "boom", "target": inside} for nodeid, inside in failed],
        "files": files or {"tests/test_a.py": 3, "tests/test_b.py": 3}, "slow": extra.pop("slow", {}), **extra,
    }


def test_targets_match_files_ids_and_parametrisations():
    targets = ["tests/test_a.py", "tests/test_b.py::test_x", "tests/sub/"]
    assert th.is_target("tests/test_a.py::test_one", targets)
    assert th.is_target("tests/test_b.py::test_x[3]", targets)
    assert th.is_target("tests/test_b.py::test_x", targets)
    assert th.is_target("tests/sub/test_c.py::test_y", targets)
    assert not th.is_target("tests/test_b.py::test_xy", targets)
    assert not th.is_target("tests/test_ab.py::test_one", ["tests/test_a.py"])


def test_a_failure_outside_the_targets_of_an_otherwise_passing_change_is_collateral():
    records = [
        record("fix A", [("tests/test_b.py::query_12", False)]),
        record("fix B", [("tests/test_b.py::query_12", False), ("tests/test_a.py::mine", True)]),  # a target failed: not collateral
        record("fix C", [("tests/test_b.py::query_12", False)], branch="other"),
        record("on master", [("tests/test_b.py::query_12", False)], branch="master"),  # master was already red
        record("no targets", [("tests/test_b.py::query_12", False)], targets=(), source="none"),
    ]
    found = th.collateral_counts(records)
    assert found["tests/test_b.py::query_12"]["runs"] == 2
    assert found["tests/test_b.py::query_12"]["labels"] == {"fix A", "fix C"}
    counts = th.failure_counts(records)["tests/test_b.py::query_12"]
    assert (counts["failed"], counts["outside"], counts["master"], counts["unattributed"]) == (5, 3, 1, 1)
    assert counts["runs"] == 5


def test_environment_sized_failures_stay_out_of_the_rankings():
    many = [(f"tests/test_b.py::t{i}", False) for i in range(th.MASS_FAILURE)]
    records = [record("no z3", many), record("fix", [("tests/test_b.py::t0", False)])]
    assert th.collateral_counts(records)["tests/test_b.py::t0"]["runs"] == 1
    assert th.failure_counts(records)["tests/test_b.py::t0"]["failed"] == 1


def test_a_test_that_fails_and_passes_at_one_commit_is_flaky():
    failing = record("one", [("tests/test_b.py::slow", False)], commit="abc12345")
    passing = record("two", [], commit="abc12345")
    other = record("three", [("tests/test_b.py::real", False)], commit="def")
    flaky = th.flaky_candidates([failing, passing, other])
    assert [(n, f, p) for n, f, p, _ in flaky] == [("tests/test_b.py::slow", 1, 1)]


def test_order_lists_slow_tests_with_medians_and_often_failing_tests():
    records = [
        record("a", [("tests/test_b.py::flaky", False)], slow={"tests/test_b.py::big": 40.0}),
        record("b", [("tests/test_b.py::flaky", False), ("tests/test_b.py::other", False)], slow={"tests/test_b.py::big": 60.0}),
    ]
    order = th.build_order(records)
    assert order["slow"] == {"tests/test_b.py::big": 50.0}
    assert order["risky"][0] == "tests/test_b.py::flaky"


def test_a_junit_file_becomes_a_record(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_a.py").write_text("")
    junit = tmp_path / "junit.xml"
    junit.write_text(
        '<testsuites><testsuite timestamp="2026-10-02T20:00:00.5">'
        '<testcase classname="tests.test_a" name="test_ok" time="0.1"/>'
        '<testcase classname="tests.test_a.TestK" name="test_slow[a.b]" time="5.0"/>'
        '<testcase classname="tests.test_a" name="test_bad" time="0.2"><failure message="assert 1 == 2&#10;more"/></testcase>'
        '<testcase classname="tests.test_a" name="test_skip" time="0"><skipped type="pytest.xfail" message="xfail"/></testcase>'
        "</testsuite></testsuites>"
    )
    result = th.record_from_junit(junit, commit="abc", branch="master", label="seed", root=tmp_path)
    assert result["counts"] == {"passed": 2, "failed": 1, "xfailed": 1}
    assert result["failed"][0]["id"] == "tests/test_a.py::test_bad"
    assert result["failed"][0]["msg"] == "assert 1 == 2"
    assert result["slow"] == {"tests/test_a.py::TestK::test_slow[a.b]": 5.0}
    assert result["ts"] == "2026-10-02T20:00:00Z"


@pytest.mark.parametrize("workers", [1, 2])
def test_a_pytest_run_writes_a_record_with_targets_and_collateral(tmp_path, workers):
    project = tmp_path / "project"
    (project / "tests").mkdir(parents=True)
    (project / "pytest.ini").write_text("[pytest]\ntestpaths = tests\n")
    (project / "tests" / "conftest.py").write_text(
        f"import sys\nsys.path.insert(0, {str(ROOT / 'tools')!r})\nimport test_history\n\n\ndef pytest_configure(config):\n    test_history.install(config)\n"
    )
    (project / "tests" / "test_mine.py").write_text("def test_target():\n    assert True\n")
    (project / "tests" / "test_other.py").write_text("def test_query_12():\n    assert 1 == 2\n\n\ndef test_fine():\n    pass\n")
    history = tmp_path / "history"
    env = {**os.environ, "KUMOSQL_TEST_HISTORY": str(history), "KUMOSQL_TEST_TARGETS": "tests/test_mine.py", "KUMOSQL_TASK": "my task"}
    command = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *(["-n", str(workers), "--dist", "loadgroup"] if workers > 1 else [])]
    done = subprocess.run(command, cwd=project, env=env, capture_output=True, text=True)
    assert done.returncode == 1, done.stdout + done.stderr
    assert "outside the targets: tests/test_other.py::test_query_12" in done.stdout
    (path,) = list((history / "runs").glob("*.jsonl"))
    saved = json.loads(path.read_text())
    assert saved["label"] == "my task" and saved["targets"] == ["tests/test_mine.py"] and saved["targets_source"] == "declared"
    assert saved["counts"] == {"passed": 2, "failed": 1}
    assert [(f["id"], f["target"]) for f in saved["failed"]] == [("tests/test_other.py::test_query_12", False)]
    assert saved["files"] == {"tests/test_mine.py": 1, "tests/test_other.py": 2}
    assert th.working_change_run(saved) or saved["branch"] in ("master", "main")  # on a feature branch this is collateral


def test_recording_is_off_without_a_history_folder(tmp_path, monkeypatch):
    monkeypatch.setenv("KUMOSQL_TEST_HISTORY", "off")
    assert th.history_dir() is None
    monkeypatch.setenv("KUMOSQL_TEST_HISTORY", str(tmp_path / "h"))
    assert th.history_dir() == tmp_path / "h"
    assert th.load_records(tmp_path / "missing") == []


def test_the_order_file_is_valid_and_names_real_test_files():
    order = json.loads((ROOT / "tests" / "order.json").read_text())
    assert set(order) >= {"slow", "risky", "runs", "slow_seconds"}
    files = {nodeid.split("::")[0] for nodeid in [*order["slow"], *order["risky"]]}
    assert all((ROOT / name).is_file() for name in files), sorted(f for f in files if not (ROOT / f).is_file())
