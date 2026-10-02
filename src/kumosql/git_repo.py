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
import sys
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

from .live_graph import MAX_FILES, MAX_TOTAL_BYTES, ProjectError
from . import console, state
from .state import data_dir
from .resilience import decode_text

# One git load at a time: the UI can start several (a double click, the startup
# reload plus a manual one), and they must not clone into or delete the same cache.
_LOAD_LOCK = threading.RLock()
_TIMEOUT_SECONDS = 180
_SLOW_SECONDS = 10.0
_AUTH_FAILURES = (
    "authentication failed", "could not read username", "could not read password",
    "terminal prompts disabled", "permission denied (publickey", "repository not found",
    "http basic: access denied", "invalid username or password", "host key verification failed",
)
_NETWORK_FAILURES = (
    "connection timed out", "connection refused", "network is unreachable", "no route to host",
    "could not resolve hostname", "kex_exchange_identification", "connection reset", "operation timed out",
)
_NETWORK_HINT = (
    "Git could not reach the remote over SSH. SSH (port 22) is often blocked on work networks. "
    "Use the repository's https URL instead (for GitHub: https://github.com/owner/repo.git). "
    "KumoSQL tries https for you when an SSH remote fails, but never turns an https URL into SSH."
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


_TRACE: list[dict] | None = None  # filled by diagnose(); None means no tracing


def is_store_python() -> bool:
    """True for the Microsoft Store build of Python, which redirects writes under AppData."""

    return sys.platform == "win32" and "windowsapps" in (sys.executable or "").lower()


def _is_windows() -> bool:
    return os.name == "nt"


def cache_dir() -> Path:
    """Where clones are cached: ``git-cache`` in the chosen local data folder (Settings > Storage).

    ``KUMOSQL_GIT_CACHE`` overrides it. Before a folder is chosen, the default is under the
    user's home folder on Windows (not AppData: the Microsoft Store build of Python redirects
    AppData writes where ``git.exe`` cannot see them) and in the data directory elsewhere.
    """

    override = os.environ.get("KUMOSQL_GIT_CACHE")
    if override:
        return Path(override)
    from . import storage

    chosen = storage.saved_folder()
    if chosen is not None:
        return chosen / "git-cache"
    if _is_windows() and not os.environ.get("KUMOSQL_HOME"):
        return Path.home() / ".kumosql" / "git-cache"
    return data_dir() / "git-cache"


def _rmtree(path: Path) -> None:
    """Delete a folder tree, including git's read-only object files (plain rmtree leaves them on Windows)."""

    def retry(function, target, *_):
        for item in (target, os.path.dirname(target)):
            try:
                os.chmod(item, 0o700)
            except OSError:
                pass
        function(target)

    if not path.exists():
        return
    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=retry)
    else:
        shutil.rmtree(path, onerror=retry)


_CLONE_NAME = re.compile(r"^[0-9a-f]{20}(?:\.partial-.*)?$")


def delete_clones() -> int:
    """Delete every cached clone (only folders KumoSQL named itself); returns how many."""

    with _LOAD_LOCK:
        folder = cache_dir()
        removed = 0
        if folder.is_dir():
            for item in folder.iterdir():
                if item.is_dir() and _CLONE_NAME.match(item.name):
                    _rmtree(item)
                    removed += 1
    return removed


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
    # Nothing may wait for a person at the console: no credential helper window or askpass program.
    env["GCM_INTERACTIVE"] = "never"
    env["GIT_ASKPASS"] = ""
    env["SSH_ASKPASS"] = ""
    env.pop("SSH_ASKPASS_REQUIRE", None)
    env["GIT_LFS_SKIP_SMUDGE"] = "1"  # only SQL text is read; an LFS pointer file is text, so never download LFS objects
    # Never inherit the server's working directory: if it was deleted (a temporary or
    # extracted folder), git fails with "Unable to read current working directory".
    if cwd is None:
        cwd = cache_dir()
        cwd.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    label = f"git {args[0]}"
    try:
        with console.task(label, quiet=True, warn_after=_SLOW_SECONDS, heartbeat_after=_SLOW_SECONDS) as step:
            done = subprocess.run(
                ["git", "-c", "core.longpaths=true", "-c", "filter.lfs.required=false", *args], cwd=cwd, env=env, capture_output=True,
                input=stdin, stdin=None if stdin is not None else subprocess.DEVNULL,
                timeout=_TIMEOUT_SECONDS, check=False,
            )
            step.note(exit=done.returncode)
    except FileNotFoundError as exc:  # the task has already logged this once, with its code and hint
        raise GitRepoError("git was not found on PATH; install git to load a repository") from exc
    except subprocess.TimeoutExpired as exc:
        raise GitRepoError(f"git {args[0]} timed out after {_TIMEOUT_SECONDS} seconds") from exc
    if done.returncode != 0:
        reason = (done.stderr.decode("utf-8", "replace").strip().splitlines() or ["no message"])[-1][:300]
        console.say(f"{label}: exit {done.returncode} in {time.monotonic() - started:.1f}s: {reason}")
    if _TRACE is not None:
        _TRACE.append({
            "command": ["git", *args], "cwd": str(cwd), "cwd_exists": Path(cwd).is_dir(),
            "returncode": done.returncode, "seconds": round(time.monotonic() - started, 2), "stderr": done.stderr.decode("utf-8", "replace").strip()[:2000],
        })
    if done.returncode != 0:
        message = (done.stderr or done.stdout).decode("utf-8", "replace").strip() or f"exit status {done.returncode}"
        lowered = message.lower()
        if any(text in lowered for text in _NETWORK_FAILURES) and ("ssh" in lowered or "port 22" in lowered):
            hint = f"\n{_NETWORK_HINT}"
        elif any(text in lowered for text in _AUTH_FAILURES):
            hint = f"\n{_AUTH_HINT}"
        else:
            hint = ""
        raise GitRepoError(f"git {args[0]} failed: {message}{hint}")
    return done.stdout


def _cache_path(remote: str, branch: str | None) -> Path:
    digest = hashlib.sha256(f"{remote}\0{branch or ''}".encode()).hexdigest()[:20]
    return cache_dir() / digest


def sync(remote: str, branch: str | None = None, refresh: bool = False) -> Path:
    """Clone (shallow) or update the cached checkout and return its path."""

    with _LOAD_LOCK:
        try:
            return _sync(remote, branch, refresh)
        except GitRepoError as exc:
            if remote in str(exc):
                raise
            raise GitRepoError(f"{exc}\nRemote: {remote}") from exc


def _scheme(url: str) -> str:
    if "://" in url:
        return url.split("://", 1)[0].lower()
    return "ssh" if re.match(r"^[A-Za-z0-9._-]+@[A-Za-z0-9._-]+:", url) else "file"


def _refuse_rewritten_transport(remote: str) -> None:
    """Fail clearly if git's own config (``url.<base>.insteadOf``) would change https to SSH or back."""

    if _scheme(remote) not in ("http", "https", "ssh", "git"):
        return
    effective = _git(["ls-remote", "--get-url", "--", remote]).strip()
    if effective and _scheme(effective) != _scheme(remote):
        raise GitRepoError(
            f"Your git configuration rewrites {remote} to {effective} (a url.<base>.insteadOf setting), "
            "so git would use a different protocol than the one you entered. Remove that setting "
            "(`git config --global --get-regexp url`), or enter the URL you want git to use."
        )


_SCP = re.compile(r"^(?:[A-Za-z0-9._-]+@)?(?P<host>[A-Za-z0-9._-]+):(?!//)(?P<path>.+)$")
_SSH_FAILURES = (
    "could not read from remote repository", "permission denied (publickey", "connection timed out",
    "connection refused", "network is unreachable", "no route to host", "could not resolve hostname",
    "kex_exchange_identification", "connection reset", "operation timed out", "host key verification failed",
    "ssh: connect to host", "banner exchange",
)


def https_equivalent(remote: str) -> str | None:
    """``https://host/owner/repo`` for ``git@host:owner/repo`` and ``ssh://git@host/owner/repo``; else None."""

    if remote.lower().startswith("ssh://"):
        parsed = urlsplit(remote)
        if not parsed.hostname or not parsed.path.strip("/"):
            return None
        return f"https://{parsed.hostname}/{parsed.path.lstrip('/')}"
    if "://" in remote or re.match(r"^[A-Za-z]:[\\/]", remote) or remote.startswith(("/", "\\\\")):
        return None
    match = _SCP.match(remote) if "@" in remote.split(":", 1)[0] else None
    if not match:
        return None
    return f"https://{match.group('host')}/{match.group('path').lstrip('/')}"


def _is_ssh_failure(message: str) -> bool:
    lowered = message.lower()
    return any(text in lowered for text in _SSH_FAILURES)


def _repair_origin(path: Path, remote: str) -> None:
    """A cached clone must point at the URL saved for it (an old clone may carry another one)."""

    try:
        current = _git(["remote", "get-url", "origin"], cwd=path).strip()
    except GitRepoError:
        current = ""
    if current != remote:
        console.say(f"repo load > repair cached clone: origin pointed at {console.ref('repo', current) if current else 'no remote'}; now {console.ref('repo', remote)}")
        _git(["remote", "set-url", "origin", remote], cwd=path)


def _sync(remote: str, branch: str | None, refresh: bool) -> Path:
    _refuse_rewritten_transport(remote)
    path = _cache_path(remote, branch)
    if (path / ".git").is_dir():
        _repair_origin(path, remote)
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
        with console.task("clone", repo=remote, branch=branch or "default"):
            _git([*args, "--", remote, str(partial)])
    except GitRepoError:
        shutil.rmtree(partial, ignore_errors=True)
        raise
    try:
        with console.task("write cached clone", repo=remote):
            _rmtree(path)
            partial.rename(path)
    except OSError as exc:
        shutil.rmtree(partial, ignore_errors=True)
        raise GitRepoError(
            f"Could not replace the cached clone at {path}: {exc}. Close anything using that folder "
            "and delete it, or set KUMOSQL_GIT_CACHE to another folder."
        ) from exc


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


_TRANSPORT_SECTION = "git_transport"


def _remembered_https(remote: str) -> str | None:
    saved = state.get_section(_TRANSPORT_SECTION, {})
    value = saved.get(remote) if isinstance(saved, dict) else None
    return value if isinstance(value, str) and value else None


def _remember_https(remote: str, https: str) -> None:
    saved = state.get_section(_TRANSPORT_SECTION, {})
    saved = dict(saved) if isinstance(saved, dict) else {}
    saved[remote] = https
    state.set_section(_TRANSPORT_SECTION, saved)


def fetch_project(value: object, branch: object = None, refresh: bool = False) -> dict:
    """Return ``{"repository", "branch", "commit", "files": {path: text}}`` for a remote.

    An SSH remote (``git@host:owner/repo``) whose SSH connection fails is retried over the
    equivalent https URL; the result then says so in ``note`` and the choice is remembered, so
    machines without SSH go straight to https. An https URL is never turned into SSH.
    """

    remote = parse_remote(value)
    wanted = parse_branch(branch)
    with _LOAD_LOCK:  # the cache must not change between the sync and the reads below
        https = https_equivalent(remote)
        if https and _remembered_https(remote) == https:
            result = _fetch(https, wanted, bool(refresh))
            return {**result, "note": f"Loaded over https ({https}) because SSH did not work for {remote}."}
        try:
            return _fetch(remote, wanted, bool(refresh))
        except GitRepoError as exc:
            if not https or not _is_ssh_failure(str(exc)):
                raise
            console.warn(f"repo load: SSH failed for {console.ref('repo', remote)} (KS-GIT-NET); trying https {console.ref('repo', https)}")
            try:
                result = _fetch(https, wanted, bool(refresh))
            except GitRepoError as second:
                raise GitRepoError(f"{exc}\n\nAlso tried over https ({https}): {second}") from second
            _remember_https(remote, https)
            return {**result, "note": f"Loaded over https ({https}) because SSH did not work for {remote}."}


def _fetch(remote: str, wanted: str | None, refresh: bool) -> dict:
    with console.task("sync cached clone", repo=remote, mode="fetch latest" if refresh else "use cached copy"):
        checkout = sync(remote, wanted, refresh)
    actual = _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=checkout).strip()
    commit = _git(["rev-parse", "--short", "HEAD"], cwd=checkout).strip()
    console.register("branch", actual)
    with console.task("list SQL files", quiet=True) as step:
        blobs = _tree_blobs(checkout)
        step.note(files=len(blobs))
    selected = sorted(blobs)
    if not any(p in _CONFIG_FILES for p in selected) and not any(p.lower().endswith(".sqlx") for p in selected):
        raise GitRepoError("This repository does not appear to contain a Dataform project (no .sqlx files or workflow_settings.yaml)")
    if len(selected) > MAX_FILES:
        raise GitRepoError(f"Project has {len(selected)} SQL files; loading is limited to {MAX_FILES}")
    files: dict[str, str] = {}
    total = 0
    with console.task("read SQL files", files=len(selected)) as step:
        contents = _read_blobs(checkout, blobs)
        step.note(megabytes=round(sum(len(d) for d in contents.values()) / 1e6, 1))
    console.register("file", selected)
    for name, data in contents.items():
        text = decode_text(data)
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

    with live_graph.activity("Fetching repository"):
        fetched = fetch_project(value, branch, refresh)
    label = f"{fetched['repository']} ({fetched['branch']} @ {fetched['commit']})"
    try:
        pipeline = live_graph.load_files(
            fetched["files"], label,
            remote={"url": parse_remote(value), "branch": parse_branch(branch), "actual": fetched["branch"]})
    except ProjectError as exc:
        raise GitRepoError(str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - name the step and the repository instead of a bare TypeError
        console.error(f"repo load > build graph for {console.ref('repo', fetched['repository'])} failed after a successful fetch", exc,
                      code="KS-GRAPH-BUILD")
        raise GitRepoError(
            f"Reading the SQL files of {label} failed while building the graph "
            f"({type(exc).__name__}: {exc}). The repository was fetched fine; this is a KumoSQL bug, "
            "and the traceback is in ui.log.") from exc
    result = {"loaded": True, "label": label, "files": len(fetched["files"]),
              "content_key": getattr(pipeline, "content_key", None), "actual_branch": fetched["branch"]}
    if fetched.get("note"):
        result["note"] = fetched["note"]
    return result


def diagnose(value: object, branch: object = None) -> str:
    """Run a full refresh load while recording every git call; returns a report to paste into a bug report."""

    import platform
    import traceback

    from . import version

    global _TRACE
    lines = [
        f"KumoSQL: {version.describe()}",
        f"Python: {sys.version.split()[0]} ({sys.executable})",
        f"OS: {platform.platform()}",
    ]
    try:
        lines.append("git: " + _git(["--version"]).strip())
        lines.append("git config url rewrites: " + (_git(["config", "--get-regexp", r"^url\."]).strip() or "none"))
    except GitRepoError as exc:
        lines.append(f"git: {exc}")
    try:
        server_cwd = os.getcwd()
        lines.append(f"Working directory: {server_cwd} (exists: {Path(server_cwd).is_dir()})")
    except OSError as exc:
        lines.append(f"Working directory: unreadable ({exc})")
    if is_store_python():
        lines.append("WARNING: this is the Microsoft Store build of Python. It redirects writes under AppData, "
                     "which git cannot see. KumoSQL caches clones under your home folder for that reason.")
    from . import storage

    lines.append(f"Local data folder setting: {storage.saved_folder() or 'not set'}")
    lines.append(f"Cache directory: {cache_dir()} (exists: {cache_dir().is_dir()})")
    lines.append(f"Temp directory: {tempfile.gettempdir()} (exists: {Path(tempfile.gettempdir()).is_dir()})")
    lines.append(f"State directory: {data_dir()} (exists: {data_dir().is_dir()})")
    lines.append(f"Remote: {value!r}   Branch: {branch!r}")
    _TRACE = []
    outcome = "OK"
    try:
        result = fetch_project(value, branch, refresh=True)
        outcome = f"OK: loaded {len(result['files'])} files from {result['repository']} ({result['branch']} @ {result['commit']})"
    except Exception:  # noqa: BLE001 - the report is the point
        outcome = "FAILED:\n" + traceback.format_exc()
    trace, _TRACE = _TRACE, None
    try:
        sizes = _git(["count-objects", "-vH"], cwd=_cache_path(parse_remote(value), parse_branch(branch)))
        lines.append("Clone size: " + ", ".join(l.strip() for l in sizes.splitlines() if l.startswith(("size-pack", "count", "in-pack"))))
    except (GitRepoError, ValueError, OSError):
        pass
    lines.append("")
    lines.append("git calls, in order:")
    for step, call in enumerate(trace, 1):
        lines.append(f"{step}. {' '.join(call['command'])}")
        lines.append(f"   cwd: {call['cwd']} (exists: {call['cwd_exists']})  exit: {call['returncode']}  took: {call['seconds']}s")
        if call["stderr"]:
            lines.append("   stderr: " + call["stderr"].replace("\n", "\n           "))
    lines.append("")
    lines.append("Result: " + outcome)
    return "\n".join(lines)
