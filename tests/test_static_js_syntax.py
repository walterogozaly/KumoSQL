"""Every script the pages load must parse: one stray parenthesis blanks the Graph, Cost and Changes pages."""

import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parent.parent / "src" / "kumosql" / "static"
SCRIPTS = sorted(STATIC.glob("*.js"))


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
@pytest.mark.parametrize("script", SCRIPTS, ids=lambda path: path.name)
def test_static_script_parses(script):
    result = subprocess.run(["node", "--check", str(script)], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr[-600:]
