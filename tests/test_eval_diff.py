import importlib.util
import json
from pathlib import Path

_spec = importlib.util.spec_from_file_location("eval_diff", Path(__file__).resolve().parent.parent / "tools" / "eval_diff.py")
eval_diff = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(eval_diff)


def test_timings_are_masked_but_answers_are_not():
    a = eval_diff.normalize('calcite: 204/232 proved in 61.3 s\n{"proven": 5, "seconds": 9.9}\n02:13:04 done')
    b = eval_diff.normalize('calcite: 204/232 proved in 58 s\n{"proven": 5, "seconds": 10.2}\n03:44:51 done')
    c = eval_diff.normalize('calcite: 205/232 proved in 58 s\n{"proven": 5, "seconds": 10.2}\n03:44:51 done')
    assert a == b
    assert a != c


def test_bare_elapsed_column_is_masked_but_counts_are_not():
    header = "suite     pairs  scored  proved  unknown  wrong  unchecked    sec\n"
    a = eval_diff.normalize(header + "calcite     232     232     224        8      0          7 1245.0")
    b = eval_diff.normalize(header + "calcite     232     232     224        8      0          7   98.4  \n")
    c = eval_diff.normalize(header + "calcite     232     232     225        7      0          7   98.4")
    d = eval_diff.normalize(header + "calcite     232     232     224        8      1          7   98.4")
    assert a == b
    assert a != c and a != d and c != d


def test_python_repr_timing_and_memory_keys_are_masked():
    a = eval_diff.normalize("{'models': 1000, 'seconds': 3.98, 'peak_mb': 106, 'growth_mb': 39}")
    b = eval_diff.normalize("{'models': 1000, 'seconds': 4.01, 'peak_mb': 107, 'growth_mb': 41}")
    c = eval_diff.normalize("{'models': 3000, 'seconds': 4.01, 'peak_mb': 107, 'growth_mb': 41}")
    assert a == b
    assert a != c


def test_seconds_suffix_fields_are_masked_but_result_counts_are_not():
    a = eval_diff.normalize('{"queries": 40, "rewrite_seconds": 12.8, "counterexample_seconds": 0.45}')
    b = eval_diff.normalize('{"queries": 40, "rewrite_seconds": 3.1, "counterexample_seconds": 9.2}')
    c = eval_diff.normalize('{"queries": 41, "rewrite_seconds": 3.1, "counterexample_seconds": 9.2}')
    assert a == b
    assert a != c


def test_sampled_commands_require_a_supported_harness():
    cases = {
        "python tools/targeted_data_bench.py --split all": "python tools/targeted_data_bench.py --split all --limit 25",
        "python tools/bounded_bench.py run leetcode --rows 3": "python tools/bounded_bench.py run leetcode --rows 3 --limit 25",
        "python tools/conditional_bench.py singh": "python tools/conditional_bench.py singh --sample 25",
        "python tools/conditional_bench.py verieql --every 8": "python tools/conditional_bench.py verieql --every 8 --limit 25",
        "python tools/engine_suites.py --suite duckdb-slt": "python tools/engine_suites.py --suite duckdb-slt --limit 25",
        "python tools/singh_bedathur_bench.py": "python tools/singh_bedathur_bench.py --sample 25",
    }
    for original, expected in cases.items():
        command, reason = eval_diff.sampled_command(original, 25)
        assert command == expected
        assert reason is None
    command, reason = eval_diff.sampled_command("python tools/qed_bench.py", 25)
    assert command is None
    assert reason == "no deterministic sample mode for this command"


def test_commands_come_from_results_files(tmp_path):
    results = tmp_path / "benchmarks" / "results"
    results.mkdir(parents=True)
    rows = {
        "a": "python tools/a.py --scale --write-results",
        "b": "python tools/a.py --scale",
        "c": "python tools/c.py --data <SQL-IQ checkout>",
        "d": 'python tools/d.py --repo PATH --psql "psql -d stats"',
    }
    for name, command in rows.items():
        (results / f"{name}.json").write_text(json.dumps({"command": command}), encoding="utf-8")
    assert eval_diff.commands(tmp_path) == {"python tools/a.py --scale": ["a", "b"]}


def test_run_uses_current_python_and_records_failure(tmp_path):
    tools = tmp_path / "tools"
    tools.mkdir()
    (tools / "probe.py").write_text(
        "import os,sys\nprint(sys.executable)\nprint(os.environ['PYTHONHASHSEED'])\nraise SystemExit(7)\n",
        encoding="utf-8",
    )
    result = eval_diff.run(tmp_path, "python tools/probe.py", 10)
    import sys
    assert result.returncode == 7
    assert sys.executable in result.output
    assert result.output.splitlines()[-1] == "0"
    assert result.state == "finished"
    assert result.seconds >= 0


def test_run_timeout_returns_uncomparable_result(tmp_path):
    tools = tmp_path / "tools"
    tools.mkdir()
    (tools / "probe.py").write_text("import time\ntime.sleep(60)\n", encoding="utf-8")
    result = eval_diff.run(tmp_path, "python tools/probe.py", 1)
    assert result.returncode == -1
    assert result.state == "timed out"
    assert "process tree terminated" in result.output
    assert result.seconds < 15


def test_matching_failures_do_not_count_as_agreement(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(eval_diff, "commands", lambda root: {"python tools/probe.py": ["probe"]})
    monkeypatch.setattr(eval_diff.subprocess, "run", lambda *a, **kw: None)
    monkeypatch.setattr(eval_diff, "run", lambda *a: eval_diff.RunResult(2, "same failure", 0.01))
    timings = tmp_path / "timings.json"
    assert eval_diff.main(["--serial", "--timings", str(timings)]) == 1
    output = capsys.readouterr().out
    assert "0 comparisons completed; 0 differ; 1 could not be compared" in output
    row = json.loads(timings.read_text())["runs"][0]
    assert row["base_exit"] == row["checkout_exit"] == 2
