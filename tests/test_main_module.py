import re
import subprocess
import sys
from pathlib import Path

from kumosql.__main__ import COMMANDS, resolve

ROOT = Path(__file__).resolve().parents[1]


def test_commands_match_console_scripts():
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    block = text.split("[project.scripts]")[1].split("[build-system]")[0]
    declared = dict(re.findall(r'^([\w-]+) = "([^"]+)"', block, re.M))
    assert COMMANDS == declared


def test_short_names_resolve():
    assert resolve("ui") == COMMANDS["kumosql-ui"]
    assert resolve("rewrite-sql") == COMMANDS["rewrite-sql"]
    assert resolve("nope") is None


def test_python_dash_m_runs_a_command():
    done = subprocess.run([sys.executable, "-m", "kumosql", "rewrite-sql", "--help"],
                          capture_output=True, text=True, timeout=60)
    assert done.returncode == 0 and "usage" in done.stdout.lower()
    done = subprocess.run([sys.executable, "-m", "kumosql", "bogus"],
                          capture_output=True, text=True, timeout=60)
    assert done.returncode == 2 and "unknown command" in done.stderr
