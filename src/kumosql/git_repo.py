"""Load a Dataform project from any git remote, using the local ``git`` CLI.

Private repositories work because ``git`` uses whatever credentials the user
already has (SSH keys, a credential helper, a token in the URL). KumoSQL never
asks for or stores credentials and makes no unauthenticated HTTP requests.

The repository is shallow-cloned into a cache directory and reused on later
runs; ``refresh=True`` fetches the latest commit of the chosen branch. Git
never prompts: a missing credential fails fast with git's own message.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
from pathlib import Path

from .live_graph import MAX_FILES, MAX_TOTAL_BYTES, ProjectError
from .state import data_dir

_TIMEOUT_SECONDS = 180
_ALLOWED_PROTOCOLS = "https:http:ssh:git:file"
_REMOTE = re.compile(r"^(?:https?://|ssh://|git://|file://|[A-Za-z0-9._-]+@[A-Za-z0-9._-]+:|/)")
_BRANCH = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")
_CONFIG_FILES = ("workflow_settings.yaml", "workflow_settings.yml", "dataform.json")
_SUFFIXES = (".sqlx", ".sql")


class GitRepoError(ValueError):
    """The repository could not be cloned, updated or read."""


def cache_dir() -> Path:
    override = os.environ.get("KUMOSQL_GIT_CACHE")
    return Path(override) if override else data_dir() / "git-cache"


def parse_remote(value: object) -> str:
    """Accept HTTPS, SSH (``ssh://`` or ``git@host:path``), git, file and absolute local paths."""

    if not isinstance(value, str) or not value.strip() or len(value) > 2048:
        raise GitRepoError("Enter a git repository URL, such as git@github.com:owner/repository.git")
    remote = value.strip()
    if remote.startswith("-") or not _REMOTE.match(remote) or any(c in remote for c in "\0\n\r"):
        raise GitRepoError(
            "Use an https://, ssh:// or git@host:path remote (or an absolute path to a local repository)"
        )
    return remote


def parse_branch(value: object) -> str | None:
    if value in (None, ""):
        return None
    if not isinstance(value, str) or len(value) > 255 or not _BRANCH.match(value) or ".." in value:
        raise GitRepoError("Invalid branch name")
    return value


def _git(args: list[str], cwd: Path | None = None) -> str:
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"  # fail with git's message instead of hanging on a prompt
    env["GIT_ALLOW_PROTOCOL"] = _ALLOWED_PROTOCOLS  # blocks ext:: and other command-running transports
    env.setdefault("GIT_SSH_COMMAND", "ssh -o BatchMode=yes")
    env["LC_ALL"] = "C"
    try:
        done = subprocess.run(
            ["git", *args], cwd=cwd, env=env, capture_output=True, text=True,
            timeout=_TIMEOUT_SECONDS, check=False,
        )
    except FileNotFoundError as exc:
        raise GitRepoError("git was not found on PATH; install git to load a repository") from exc
    except subprocess.TimeoutExpired as exc:
        raise GitRepoError(f"git {args[0]} timed out after {_TIMEOUT_SECONDS} seconds") from exc
    if done.returncode != 0:
        message = (done.stderr or done.stdout).strip() or f"exit status {done.returncode}"
        raise GitRepoError(f"git {args[0]} failed: {message}")
    return done.stdout


def _cache_path(remote: str, branch: str | None) -> Path:
    digest = hashlib.sha256(f"{remote}\0{branch or ''}".encode()).hexdigest()[:20]
    return cache_dir() / digest


def sync(remote: str, branch: str | None = None, refresh: bool = False) -> Path:
    """Clone (shallow) or update the cached checkout and return its path."""

    path = _cache_path(remote, branch)
    if (path / ".git").is_dir() and not refresh:
        return path
    if (path / ".git").is_dir():
        ref = branch or "HEAD"
        try:
            _git(["fetch", "--depth", "1", "--", "origin", ref], cwd=path)
            _git(["reset", "--hard", "FETCH_HEAD"], cwd=path)
            _git(["clean", "-fdx"], cwd=path)
        except GitRepoError:
            # A cache that can no longer be updated is rebuilt only if the clone below succeeds.
            _clone(remote, branch, path)
        return path
    _clone(remote, branch, path)
    return path


def _clone(remote: str, branch: str | None, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(path.name + ".partial")
    shutil.rmtree(partial, ignore_errors=True)
    args = ["clone", "--depth", "1", "--no-tags", "--quiet"]
    if branch:
        args += ["--branch", branch]
    try:
        _git([*args, "--", remote, str(partial)])
    except GitRepoError:
        shutil.rmtree(partial, ignore_errors=True)
        raise
    shutil.rmtree(path, ignore_errors=True)
    partial.rename(path)


def fetch_project(value: object, branch: object = None, refresh: bool = False) -> dict:
    """Return ``{"repository", "branch", "commit", "files": {path: text}}`` for a remote."""

    remote = parse_remote(value)
    wanted = parse_branch(branch)
    checkout = sync(remote, wanted, bool(refresh))
    actual = _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=checkout).strip()
    commit = _git(["rev-parse", "--short", "HEAD"], cwd=checkout).strip()
    tracked = _git(["ls-files", "-z"], cwd=checkout).split("\0")
    selected = sorted(
        p for p in tracked
        if p and (p.lower().endswith(_SUFFIXES) or p in _CONFIG_FILES)
    )
    if not any(p in _CONFIG_FILES for p in selected) and not any(p.lower().endswith(".sqlx") for p in selected):
        raise GitRepoError("This repository does not appear to contain a Dataform project (no .sqlx files or workflow_settings.yaml)")
    if len(selected) > MAX_FILES:
        raise GitRepoError(f"Project has {len(selected)} SQL files; loading is limited to {MAX_FILES}")
    files: dict[str, str] = {}
    total = 0
    root = checkout.resolve()
    for name in selected:
        file = (checkout / name)
        if file.is_symlink() or not file.resolve().is_relative_to(root):
            continue
        try:
            text = file.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        total += len(text.encode("utf-8"))
        if total > MAX_TOTAL_BYTES:
            raise GitRepoError("Project is too large to load")
        files[name] = text
    if not files:
        raise GitRepoError("The repository has no readable SQL files")
    name = re.sub(r"(?:\.git)?/?$", "", remote).rsplit("/", 1)[-1].rsplit(":", 1)[-1] or remote
    return {"repository": name, "branch": actual, "commit": commit, "files": files}


def load_into_graph(value: object, branch: object = None, refresh: bool = False) -> dict:
    """Fetch a remote and make it the project the graph page shows."""

    from . import live_graph

    fetched = fetch_project(value, branch, refresh)
    label = f"{fetched['repository']} ({fetched['branch']} @ {fetched['commit']})"
    try:
        live_graph.load_files(fetched["files"], label)
    except ProjectError as exc:
        raise GitRepoError(str(exc)) from exc
    return {"loaded": True, "label": label, "files": len(fetched["files"])}
