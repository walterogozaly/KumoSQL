import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_readme_scoreboard_is_current():
    result = subprocess.run([sys.executable, str(ROOT / "tools" / "scoreboard.py"), "--check"], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout


def test_every_test_file_that_loads_an_eval_script_is_marked_eval():
    """A test that imports a script named by a results file's ``command`` holds that eval's floors, so it must be
    listed in ``EVAL_FILES`` in tests/conftest.py (``pytest -m eval`` and ``tools/run_tests.py --evals`` select by it)."""

    import importlib.util
    import json
    import re

    spec = importlib.util.spec_from_file_location("eval_conftest", ROOT / "tests" / "conftest.py")
    conftest = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(conftest)
    scripts = set()
    for path in (ROOT / "benchmarks" / "results").glob("*.json"):
        scripts.update(re.findall(r"tools/(\w+)\.py", json.loads(path.read_text(encoding="utf-8"))["command"]))
    missing = []
    for test in sorted((ROOT / "tests").glob("test_*.py")):
        text = test.read_text(encoding="utf-8")
        loads = [s for s in scripts if re.search(rf"(import {s}\b|from tools(\.{s})? import|[\"']{s}(\.py)?[\"'])", text) and re.search(rf"\b{s}\b", text)]
        if loads and test.name not in conftest.EVAL_FILES:
            missing.append(f"{test.name} (loads {', '.join(sorted(loads))})")
    assert not missing, "add to EVAL_FILES in tests/conftest.py: " + "; ".join(missing)
