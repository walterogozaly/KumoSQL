"""The local data folder: where KumoSQL keeps all its files, such as settings, caches, logs and repository clones.

It is a saved setting, chosen by the user, because the default location can be
unusable for git: the Microsoft Store build of Python redirects writes under
AppData into a private folder that ``git.exe`` cannot see. Choosing a folder
checks that it exists, is writable, and that git can see what Python writes there.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

from . import state

SECTION = "storage"


class StorageError(ValueError):
    """The chosen folder cannot be used."""


def saved_folder() -> Path | None:
    return state.chosen_folder()


def configured() -> bool:
    """True when the user chose a folder (or an environment override pins one)."""

    return bool(os.environ.get("KUMOSQL_GIT_CACHE")) or saved_folder() is not None


def suggested() -> str:
    return str(Path.home() / "KumoSQL")


def describe() -> dict:
    folder = saved_folder()
    return {"folder": str(folder) if folder else None, "suggested": suggested(),
            "configured": configured(), "pointer": str(state.pointer_path()), "files": str(state.data_dir()), "override": os.environ.get("KUMOSQL_GIT_CACHE") or None}


def _probe(folder: Path) -> None:
    """Prove git and Python see the same folder: Python writes a file, git reads it and inits a repo."""

    from .git_repo import GitRepoError, _run

    probe = Path(tempfile.mkdtemp(prefix=".kumosql-probe-", dir=folder))
    try:
        marker = probe / "marker.txt"
        marker.write_text("kumosql", encoding="utf-8")
        _run(["init", "-q"], cwd=probe)
        if not (probe / ".git").is_dir():
            raise StorageError(
                "git ran but its files are not visible to Python in this folder. "
                "Choose a folder outside AppData, such as one in your home folder.")
        _run(["hash-object", "marker.txt"], cwd=probe)
    except GitRepoError as exc:
        raise StorageError(
            f"git cannot use this folder ({exc}). Choose a folder outside AppData, such as one in your home folder.") from exc
    finally:
        from .git_repo import _rmtree

        try:
            _rmtree(probe)
        except OSError:
            pass


def validate(value: object) -> Path:
    """Expand, create if needed and check a folder; returns its absolute path."""

    if not isinstance(value, str) or not value.strip() or len(value) > 1024 or "\0" in value:
        raise StorageError("Enter a folder path")
    folder = Path(os.path.expandvars(os.path.expanduser(value.strip())))
    if not folder.is_absolute():
        raise StorageError("Enter a full path, such as C:\\Users\\you\\KumoSQL or /home/you/KumoSQL")
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise StorageError(f"Could not create {folder}: {exc}") from exc
    if not os.access(folder, os.W_OK):
        raise StorageError(f"{folder} is not writable")
    _probe(folder)
    return folder.resolve()


def save(value: object) -> dict:
    """Validate and save the folder, moving the settings and caches already kept elsewhere into it.

    Only a tiny pointer file stays in the default location. Clones are not moved (they are
    fetched again on their next load). The result's ``migrated`` says what moved.
    """

    folder = validate(value)
    with state._MIGRATE_LOCK:
        previous = state.data_dir()
        report = state.migrate(previous, folder) if previous.resolve() != folder else {"moved": [], "merged": [], "kept": []}
        state.set_pointer(folder)
    result = describe()
    result["migrated"] = {**report, "from": str(previous)} if any(report.values()) else None
    if previous.resolve() != folder:
        result["previous"] = str(previous)
    return result
