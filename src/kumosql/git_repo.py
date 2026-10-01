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
import tempfile
import threading
from pathlib import Path

from .live_graph import MAX_FILES, MAX_TOTAL_BYTES, ProjectError
from .state import data_dir

# One git load at a time: the UI can start several (a double click, the startup
# reload plus a manual one), and they must not clone into or delete the same cache.
_LOAD_LOCK = threading.RLock()
_TIMEOUT_SECONDS = 180
_AUTH_FAILURES = (
    "authentication failed", "could not read username", "could not read password",
    "terminal prompts disabled", "permission denied (publickey", "repository not found",
    "http basic: access denied", "invalid username or password", "host key verification failed",
)
_AUTH_HINT = (
    "Git could not sign in to this remote. Sign in to git on this computer first, for example "
    "with Git Credential Manager (installed with Git for Windows), `gh auth login`, or an SSH key "
    "added to your account, then try again. If the repository URL is wrong, GitHub reports it the same way."
)
_ALLOWED_PROTOCOLS = "https:http:ssh:git:file"
_REMOTE = re.compile(r"^(?:https?://|ssh://|git://|file://|[A-Za-z0-9._-]+@[A-Za-z0-9._-]+:|/|[A-Za-z]:[\\/]|\\\\)")
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
    return _run(args, cwd).decode("utf-8", "replace")


def _run(args: list[str], cwd: Path | None = None, stdin: bytes | None = None) -> bytes:
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"  # fail with git's message instead of hanging on a prompt
    env["GIT_ALLOW_PROTOCOL"] = _ALLOWED_PROTOCOLS  # blocks ext:: and other command-running transports
    env.setdefault("GIT_SSH_COMMAND", "ssh -o BatchMode=yes")
    env["LC_ALL"] = "C"
    # Never inherit the server's working directory: if it was deleted (a temporary or
    # extracted folder), git fails with "Unable to read current working directory".
    if cwd is None:
        cwd = cache_dir()
        cwd.mkdir(parents=True, exist_ok=True)
    try:
        done = subprocess.run(
            ["git", "-c", "core.longpaths=true", *args], cwd=cwd, env=env, capture_output=True,
            input=stdin, timeout=_TIMEOUT_SECONDS, check=False,
        )
    except FileNotFoundError as exc:
        raise GitRepoError("git was not found on PATH; install git to load a repository") from exc
    except subprocess.TimeoutExpired as exc:
        raise GitRepoError(f"git {args[0]} timed out after {_TIMEOUT_SECONDS} seconds") from exc
    if done.returncode != 0:
        message = (done.stderr or done.stdout).decode("utf-8", "replace").strip() or f"exit status {done.returncode}"
        hint = f"\n{_AUTH_HINT}" if any(text in message.lower() for text in _AUTH_FAILURES) else ""
        raise GitRepoError(f"git {args[0]} failed: {message}{hint}")
    return done.stdout


def _cache_path(remote: str, branch: str | None) -> Path:
    digest = hashlib.sha256(f"{remote}\0{branch or ''}".encode()).hexdigest()[:20]
    return cache_dir() / digest


def sync(remote: str, branch: str | None = None, refresh: bool = False) -> Path:
    """Clone (shallow) or update the cached checkout and return its path."""

    with _LOAD_LOCK:
        return _sync(remote, branch, refresh)


def _sync(remote: str, branch: str | None, refresh: bool) -> Path:
    path = _cache_path(remote, branch)
    if (path / ".git").is_dir() and not refresh:
        return path
    if (path / ".git").is_dir():
        ref = branch or "HEAD"
        try:
            _git(["fetch", "--depth", "1", "--", "origin", ref], cwd=path)
            _git(["update-ref", "HEAD", "FETCH_HEAD"], cwd=path)
        except GitRepoError:
            # A cache that can no longer be updated is rebuilt only if the clone below succeeds.
            _clone(remote, branch, path)
        return path
    _clone(remote, branch, path)
    return path


def _clone(remote: str, branch: str | None, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = Path(tempfile.mkdtemp(prefix=path.name + ".partial-", dir=path.parent))
    # No working tree: files are read from git objects, so path length and
    # reserved names on the local filesystem (Windows' 260-character limit) cannot break a load.
    args = ["clone", "--depth", "1", "--no-tags", "--no-checkout", "--quiet"]
    if branch:
        args += ["--branch", branch]
    try:
        _git([*args, "--", remote, str(partial)])
    except GitRepoError:
        shutil.rmtree(partial, ignore_errors=True)
        raise
    shutil.rmtree(path, ignore_errors=True)
    partial.rename(path)


def _tree_blobs(checkout: Path) -> dict[str, str]:
    """``{path: blob id}`` of the SQL and config files in HEAD (symlinks are skipped)."""

    listing = _run(["ls-tree", "-r", "-z", "HEAD"], cwd=checkout).decode("utf-8", "surrogateescape")
    blobs: dict[str, str] = {}
    for entry in listing.split("\0"):
        meta, _, path = entry.partition("\t")
        parts = meta.split()
        if len(parts) != 3 or parts[1] != "blob" or parts[0] == "120000":
            continue
        if path.lower().endswith(_SUFFIXES) or path in _CONFIG_FILES:
            blobs[path] = parts[2]
    return blobs


def _read_blobs(checkout: Path, blobs: dict[str, str]) -> dict[str, bytes]:
    names = sorted(blobs)
    out = _run(["cat-file", "--batch"], cwd=checkout, stdin="".join(f"{blobs[n]}\n" for n in names).encode())
    result: dict[str, bytes] = {}
    position = 0
    for name in names:
        end = out.index(b"\n", position)
        header = out[position:end].split()
        if len(header) != 3 or header[1] != b"blob":
            raise GitRepoError(f"Could not read {name} from the repository")
        size = int(header[2])
        result[name] = out[end + 1:end + 1 + size]
        position = end + 1 + size + 1
    return result


def fetch_project(value: object, branch: object = None, refresh: bool = False) -> dict:
    """Return ``{"repository", "branch", "commit", "files": {path: text}}`` for a remote."""

    remote = parse_remote(value)
    wanted = parse_branch(branch)
    with _LOAD_LOCK:  # the cache must not change between the sync and the reads below
        return _fetch(remote, wanted, bool(refresh))


def _fetch(remote: str, wanted: str | None, refresh: bool) -> dict:
    checkout = sync(remote, wanted, refresh)
    actual = _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=checkout).strip()
    commit = _git(["rev-parse", "--short", "HEAD"], cwd=checkout).strip()
    blobs = _tree_blobs(checkout)
    selected = sorted(blobs)
    if not any(p in _CONFIG_FILES for p in selected) and not any(p.lower().endswith(".sqlx") for p in selected):
        raise GitRepoError("This repository does not appear to contain a Dataform project (no .sqlx files or workflow_settings.yaml)")
    if len(selected) > MAX_FILES:
        raise GitRepoError(f"Project has {len(selected)} SQL files; loading is limited to {MAX_FILES}")
    files: dict[str, str] = {}
    total = 0
    for name, data in _read_blobs(checkout, blobs).items():
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            continue
        total += len(data)
        if total > MAX_TOTAL_BYTES:
            raise GitRepoError("Project is too large to load")
        files[name] = text
    if not files:
        raise GitRepoError("The repository has no readable SQL files")
    name = re.split(r"[/\\:]", re.sub(r"(?:\.git)?[/\\]?$", "", remote))[-1] or remote
    return {"repository": name, "branch": actual, "commit": commit, "files": files}


def load_into_graph(value: object, branch: object = None, refresh: bool = False) -> dict:
    """Fetch a remote and make it the project the graph page shows."""

    from . import live_graph

    fetched = fetch_project(value, branch, refresh)
    label = f"{fetched['repository']} ({fetched['branch']} @ {fetched['commit']})"
    try:
        live_graph.load_files(
            fetched["files"], label,
            remote={"url": parse_remote(value), "branch": parse_branch(branch), "actual": fetched["branch"]})
    except ProjectError as exc:
        raise GitRepoError(str(exc)) from exc
    return {"loaded": True, "label": label, "files": len(fetched["files"])}
