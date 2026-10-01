import pytest


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    """Keep saved UI, scope and formatting state out of the real user directory."""

    monkeypatch.setenv("KUMOSQL_HOME", str(tmp_path / "kumosql-home"))


@pytest.fixture(autouse=True)
def fresh_redactor(monkeypatch):
    """Placeholders are per session; keep names one test registered from changing another test's log text."""

    from kumosql import redact

    monkeypatch.setattr(redact, "GLOBAL", redact.Redactor(enabled=True))
