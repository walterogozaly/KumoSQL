import pytest


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    """Keep saved UI, scope and formatting state out of the real user directory."""

    monkeypatch.setenv("KUMOSQL_HOME", str(tmp_path / "kumosql-home"))
