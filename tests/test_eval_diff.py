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
