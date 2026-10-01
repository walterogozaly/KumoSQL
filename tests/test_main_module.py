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


def test_user_facing_text_never_tells_people_to_run_a_launcher():
    """Launchers (kumosql-ui.exe) are blocked or off PATH on locked-down machines: messages use python -m."""

    launchers = [name for name in COMMANDS if name != "kumosql-ui"] + ["kumosql-ui"]
    pattern = re.compile(r"(?<![\w.\-/<!\"'])(" + "|".join(re.escape(n) for n in launchers) + r")(?![\w\-\"'])")
    offenders = []
    for path in (ROOT / "src" / "kumosql").rglob("*"):
        if path.suffix not in (".py", ".js", ".html") or path.name == "__main__.py" or "vendor" in path.parts:
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if pattern.search(line) and not any(ok in line for ok in ("STORAGE_KEY", "python -m", "<!--")):
                offenders.append(f"{path.name}:{number}: {line.strip()[:80]}")
    assert not offenders, offenders
