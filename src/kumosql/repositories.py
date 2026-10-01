"""Connected Dataform repositories: a saved setting, not a one-time page action.

The list lives in the ``repositories`` section of the local state file, so it
survives restarts and is shared by the UI and the CLI. One connected
repository is *active*: it is the project the graph, cost and change pages
show. Loading goes through :func:`kumosql.git_repo.load_into_graph`, the same
path as the one-off loads, so credentials, caching and error messages are
identical.

On server start :func:`autoload` reloads the active repository in the
background: it asks git for the latest commit and, if the remote cannot be
reached, falls back to the cached clone and records why.
"""

from __future__ import annotations

import threading
import uuid
from datetime import datetime, timezone

from . import state, storage, workflow_configs
from .git_repo import GitRepoError, load_into_graph, parse_branch, parse_remote

SECTION = "repositories"
MAX_REPOSITORIES = 20
_LOCK = threading.RLock()
_LOADING: set[str] = set()  # ids being loaded right now; in memory only


class RepositoryError(ValueError):
    """A connected-repository request was invalid."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _read() -> dict:
    saved = state.get_section(SECTION, {})
    items = saved.get("items") if isinstance(saved, dict) else None
    clean = []
    for item in items if isinstance(items, list) else []:
        if isinstance(item, dict) and isinstance(item.get("id"), str) and isinstance(item.get("url"), str):
            clean.append(item)
    active = saved.get("active") if isinstance(saved, dict) else None
    if not any(item["id"] == active for item in clean):
        active = clean[0]["id"] if clean else None
    return {"items": clean, "active": active}


def _write(data: dict) -> None:
    state.set_section(SECTION, data)


def listing() -> dict:
    """``{"repositories": [...], "active": id | None}`` for the UI."""

    with _LOCK:
        data = _read()
        items = [{**item, "loading": True} if item["id"] in _LOADING else item for item in data["items"]]
    return {"repositories": items, "active": data["active"]}


def replace(entries: object, active: object = None) -> dict:
    """Save the list of ``{url, branch}`` entries; existing entries keep their load status."""

    if not isinstance(entries, list) or len(entries) > MAX_REPOSITORIES:
        raise RepositoryError(f"repositories must be a list of at most {MAX_REPOSITORIES}")
    with _LOCK:
        current = {(item["url"], item.get("branch") or ""): item for item in _read()["items"]}
        items, seen = [], set()
        for entry in entries:
            if not isinstance(entry, dict):
                raise RepositoryError("each repository must be an object with a url")
            try:
                url, branch = parse_remote(entry.get("url")), parse_branch(entry.get("branch"))
            except GitRepoError as exc:
                raise RepositoryError(str(exc)) from exc
            key = (url, branch or "")
            if key in seen:
                continue
            seen.add(key)
            items.append(current.get(key) or {"id": uuid.uuid4().hex[:12], "url": url, "branch": branch})
        if any((item["url"], item.get("branch") or "") not in current for item in items) and not storage.configured():
            raise RepositoryError(
                "Choose a local data folder first (Settings > Storage). KumoSQL keeps repository clones there.")
        ids = {item["id"] for item in items}
        data = {"items": items, "active": active if active in ids else (items[0]["id"] if items else None)}
        _write(data)
    return {"repositories": data["items"], "active": data["active"]}


def activate(repo_id: object) -> dict:
    with _LOCK:
        data = _read()
        if not any(item["id"] == repo_id for item in data["items"]):
            raise RepositoryError("unknown repository")
        data["active"] = repo_id
        _write(data)
    return load(repo_id)


def load(repo_id: object, refresh: bool = False) -> dict:
    """Load one connected repository into the graph and record the outcome.

    Raises :class:`GitRepoError` with git's own message when git fails; the
    message is also saved on the entry so the Settings page can show it.
    """

    with _LOCK:
        item = next((i for i in _read()["items"] if i["id"] == repo_id), None)
        if item is None:
            raise RepositoryError("unknown repository")
        url, branch = item["url"], item.get("branch")
        _LOADING.add(repo_id)
    # git runs without _LOCK held: Settings and every other request only need the saved list.
    try:
        try:
            result = load_into_graph(url, branch, refresh)
        except (GitRepoError, ValueError) as exc:
            _update(repo_id, error=str(exc), error_at=_now())
            raise
        _update(repo_id, drop=("error", "error_at", "stale_reason"),
                last_loaded=_now(), label=result["label"], files=result["files"], activate=True)
    finally:
        with _LOCK:
            _LOADING.discard(repo_id)
    return {**result, "id": repo_id, "last_loaded": _read_item(repo_id).get("last_loaded")}


def _read_item(repo_id: str) -> dict:
    with _LOCK:
        return next((i for i in _read()["items"] if i["id"] == repo_id), {})


def _update(repo_id: str, drop: tuple = (), activate: bool = False, **fields: object) -> None:
    """Change one saved entry (re-read first: the list may have been edited while git ran)."""

    with _LOCK:
        data = _read()
        for item in data["items"]:
            if item["id"] == repo_id:
                for key in drop:
                    item.pop(key, None)
                item.update(fields)
                if activate:
                    data["active"] = repo_id
                _write(data)
                return


def autoload(background: bool = True) -> threading.Thread | None:
    """Reload the active repository at startup; never raises and never blocks the server."""

    def run() -> None:
        with _LOCK:
            active = _read()["active"]
        if not active:
            return
        try:
            load(active, refresh=True)
        except (GitRepoError, ValueError) as refresh_error:
            try:  # remote unreachable or auth expired: the cached clone still works
                load(active, refresh=False)
                _record_stale(active, str(refresh_error))
            except (GitRepoError, ValueError):
                pass  # the error is already saved on the entry

    if not background:
        run()
        return None
    thread = threading.Thread(target=run, name="kumosql-repo-autoload", daemon=True)
    thread.start()
    return thread


def _record_stale(repo_id: str, reason: str) -> None:
    """After a fallback to the cached clone, keep the refresh error visible."""

    _update(repo_id, stale_reason=reason)
