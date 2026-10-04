"""Dataform workflow configurations for a connected repository.

A workflow configuration is the schedule Dataform runs in production: which
actions to run (by tag or by name, optionally with everything upstream or
downstream of them), the cron expression, and the release configuration it
compiles. This module finds the Dataform repositories whose git remote matches
a connected KumoSQL repository, lists their workflow configurations through the
Dataform API, and works out which models those schedules run.

Credentials are the ones the BigQuery pages already use (Application Default
Credentials, a service account, or ``BQ_ACCESS_TOKEN``). Answers are cached for
the BigQuery query cache lifetime and saved next to the catalog, so reopening
the graph never waits on the network. Nothing here may fail a repository load:
callers get a plain message saying what could not be found.
"""

from __future__ import annotations

import csv
import io
import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from . import bigquery_catalog, state
from .dryrun import access_token

_API = "https://dataform.googleapis.com/v1beta1"
_SCOPE = "https://www.googleapis.com/auth/cloud-platform"
SECTION = "workflow_configs"
DEFAULT_LOCATION = "us-central1"
_PROJECT_ID = re.compile(r"^[A-Za-z][A-Za-z0-9.:_-]{0,99}$")
_LOCATION = re.compile(r"^[a-z][a-z0-9-]{1,40}$")
COLUMNS = [
    "REPO", "WORKFLOW_CONFIGURATION", "TAGS", "SPECIFIC_ACTIONS", "INCLUDE_DEPENDENCIES",
    "INCLUDE_DEPENDENTS", "CRON_SCHEDULE", "TIME_ZONE", "DISABLED", "RELEASE_CONFIGURATION",
    "ACTIVE_PRODUCTION", "UPDATED_TS",
]


class WorkflowConfigError(RuntimeError):
    """Dataform could not be queried; the message is for the user. ``status`` is the HTTP status when Dataform answered."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class CompiledGraphUnavailable(WorkflowConfigError):
    """The compilation behind a repository could not be read. ``reason`` is one of ``no_projects``, ``no_credentials``,
    ``no_repository``, ``no_compilation`` and ``api_error``; ``attempted`` says whether Dataform was contacted at all.
    The message names no project, repository or action, so it is safe to show and to log."""

    def __init__(self, reason: str, message: str, attempted: bool = True) -> None:
        super().__init__(message)
        self.reason = reason
        self.attempted = attempted


# --- URL matching ---------------------------------------------------------------


def norm_git_url(url: object) -> str:
    """``host/owner/repo`` in lower case: SSH and https forms, ``.git`` and trailing ``/`` all compare equal."""

    text = (url or "").strip().lower() if isinstance(url, str) else ""
    text = re.sub(r"^[a-z][a-z0-9+.-]*://", "", text)
    text = re.sub(r"^[^@/]*@", "", text)  # user@ or token@
    text = re.sub(r"^([^/:]+):(?!\d+/)", r"\1/", text)  # scp-like host:path
    text = re.sub(r"^([^/:]+):\d+/", r"\1/", text)  # host:port/path
    text = text.rstrip("/")
    if text.endswith(".git"):
        text = text[:-4]
    return text.rstrip("/")


def short(resource_name: object) -> str:
    return (resource_name if isinstance(resource_name, str) else "").rsplit("/", 1)[-1]


# --- Dataform API ---------------------------------------------------------------

_token: tuple[str, float] | None = None


def _bearer() -> str:
    global _token
    if _token and time.time() < _token[1]:
        return _token[0]
    value = access_token(_SCOPE)
    _token = (value, time.time() + 1800)
    return value


def _get(url: str) -> dict:
    request = Request(url, headers={"Authorization": f"Bearer {_bearer()}", "Accept": "application/json"})
    try:
        with urlopen(request, timeout=60) as response:
            return json.loads(response.read() or b"{}")
    except HTTPError as exc:
        try:
            message = json.loads(exc.read() or b"{}")["error"]["message"]
        except (ValueError, KeyError, TypeError):
            message = ""
        raise WorkflowConfigError(f"Dataform returned HTTP {exc.code}{': ' + message if message else ''}", exc.code) from exc
    except URLError as exc:
        raise WorkflowConfigError(f"Could not connect to Dataform: {exc.reason}") from exc


def _list_all(url: str, key: str) -> list[dict]:
    """Follow ``nextPageToken`` until the listing is exhausted."""

    items: list[dict] = []
    token = ""
    while True:
        query = {"pageSize": "1000", **({"pageToken": token} if token else {})}
        payload = _get(f"{url}?{urlencode(query)}")
        items.extend(payload.get(key) or [])
        token = payload.get("nextPageToken") or ""
        if not token:
            return items


# Locations a Dataform repository can live in. A repository in a region other than the configured one is found by
# searching these, so the default region does not have to be right.
LOCATIONS = (
    "africa-south1", "asia-east1", "asia-east2", "asia-northeast1", "asia-northeast2", "asia-northeast3", "asia-south1",
    "asia-south2", "asia-southeast1", "asia-southeast2", "australia-southeast1", "australia-southeast2", "europe-central2",
    "europe-north1", "europe-southwest1", "europe-west1", "europe-west2", "europe-west3", "europe-west4", "europe-west6",
    "europe-west8", "europe-west9", "europe-west10", "europe-west12", "me-central1", "me-central2", "me-west1",
    "northamerica-northeast1", "northamerica-northeast2", "southamerica-east1", "southamerica-west1", "us-central1",
    "us-east1", "us-east4", "us-east5", "us-south1", "us-west1", "us-west2", "us-west3", "us-west4",
)
_found_location: dict[str, str] = {}  # normalized remote -> the location its Dataform repository was found in


def _search_other_locations(github_url: str, projects: list[str], skip: str) -> tuple[list[str], int]:
    """Repositories matching ``github_url`` in every known location but ``skip``, and how many locations were searched.
    A location that errors (not offered to the project, no permission) counts as having none."""

    others = [location for location in LOCATIONS if location != skip]
    with ThreadPoolExecutor(max_workers=8) as pool:
        for location, (matches, _failures) in zip(others, pool.map(
                lambda place: _search_repositories(github_url, projects, place), others)):
            if matches:
                _found_location[norm_git_url(github_url)] = location
                return matches, len(others)
    return [], len(others)


def _search_repositories(github_url: str, projects: list[str], location: str) -> tuple[list[str], list[tuple[str, WorkflowConfigError]]]:
    want = norm_git_url(github_url)
    matches: list[str] = []
    failures: list[tuple[str, WorkflowConfigError]] = []
    for project in projects:
        parent = f"{_API}/projects/{quote(project, safe='')}/locations/{quote(location, safe='')}/repositories"
        try:
            repos = _list_all(parent, "repositories")
        except WorkflowConfigError as exc:
            failures.append((project, exc))
            continue
        for repo in repos:
            remote = (repo.get("gitRemoteSettings") or {}).get("url")
            if remote and norm_git_url(remote) == want and isinstance(repo.get("name"), str):
                matches.append(repo["name"])
    return matches, failures


def find_repositories(github_url: str, projects: list[str], location: str) -> tuple[list[str], list[str]]:
    """Dataform repositories whose remote matches ``github_url``, plus a warning per project that failed."""

    matches, failures = _search_repositories(github_url, projects, location)
    return matches, [f"{project}: {exc}" for project, exc in failures]


def config_rows(repo_name: str, updated_ts: str) -> list[dict]:
    """One row per workflow configuration of a Dataform repository."""

    configs = _list_all(f"{_API}/{repo_name}/workflowConfigs", "workflowConfigs")
    rows = []
    for config in sorted(configs, key=lambda item: short(item.get("name"))):
        invocation = config.get("invocationConfig") or {}
        targets = invocation.get("includedTargets") or []
        tags = invocation.get("includedTags") or []
        cron = config.get("cronSchedule") or None
        disabled = bool(config.get("disabled", False))
        release = short(config.get("releaseConfig")) or None
        target_names = [".".join(str(t.get(part)) for part in ("database", "schema", "name") if t.get(part))
                        for t in targets if isinstance(t, dict)]
        rows.append({
            "REPO": short(repo_name),
            "WORKFLOW_CONFIGURATION": short(config.get("name")),
            "TAGS": ", ".join(tags) or None,
            "SPECIFIC_ACTIONS": ", ".join(target_names) or None,
            "INCLUDE_DEPENDENCIES": bool(invocation.get("transitiveDependenciesIncluded", False)),
            "INCLUDE_DEPENDENTS": bool(invocation.get("transitiveDependentsIncluded", False)),
            "CRON_SCHEDULE": cron,
            "TIME_ZONE": config.get("timeZone") or None,
            "DISABLED": disabled,
            "RELEASE_CONFIGURATION": release,
            "ACTIVE_PRODUCTION": bool(cron) and not disabled and release == "production",
            "UPDATED_TS": updated_ts,
            "included_tags": [t for t in tags if isinstance(t, str)],
            "included_targets": target_names,
        })
    return rows


def _failure(exc: WorkflowConfigError) -> str:
    """What went wrong without Dataform's own message, which can name a project."""

    return f"Dataform returned HTTP {exc.status}" if exc.status else "Dataform could not be reached"


def compiled_targets(github_url: str) -> list[tuple[tuple[str, str, str], bool]]:
    """Every action Dataform compiled for a repository: ``((database, schema, name), is_declaration)``.

    Uses the newest release compilation (else the newest compilation) of the first Dataform repository whose
    remote matches ``github_url``. Raises :class:`CompiledGraphUnavailable` (a :class:`WorkflowConfigError`) when none
    can be read, with the reason: no projects to search, no credentials, no matching repository, no compilation, or an API error.
    """

    search = search_for(github_url)
    projects, location = search["projects"], search["location"]
    if not projects:
        raise CompiledGraphUnavailable(
            "no_projects", "no Google Cloud project is selected to search for the Dataform repository (choose projects in "
            "the BigQuery settings, or set an override for this repository)", attempted=False)
    try:
        _bearer()
    except RuntimeError as exc:
        raise CompiledGraphUnavailable("no_credentials", str(exc), attempted=False) from exc
    remembered = _found_location.get(norm_git_url(github_url))
    if remembered:
        location = remembered
    repos, failures = _search_repositories(github_url, projects, location)
    searched_elsewhere = 0
    if not repos and not failures:
        repos, searched_elsewhere = _search_other_locations(github_url, projects, location)
    if not repos:
        if failures:
            raise CompiledGraphUnavailable(
                "api_error", f"{_failure(failures[0][1])} for {len(failures)} of {len(projects)} searched project(s) in {location}")
        raise CompiledGraphUnavailable(
            "no_repository", f"none of the {len(projects)} searched project(s) has a Dataform repository whose git remote "
            f"matches this repository, in {location} or in the {searched_elsewhere} other Dataform locations")
    repo = repos[0]
    try:
        return _compiled_actions(repo)
    except CompiledGraphUnavailable:
        raise
    except WorkflowConfigError as exc:
        raise CompiledGraphUnavailable("api_error", f"{_failure(exc)} while reading the compilation") from exc


def _compiled_actions(repo: str) -> list[tuple[tuple[str, str, str], bool]]:
    result = ""
    for config in _list_all(f"{_API}/{repo}/releaseConfigs", "releaseConfigs"):
        if isinstance(config.get("releaseCompilationResult"), str):
            result = config["releaseCompilationResult"]
            break
    if not result:
        query = f"{_API}/{repo}/compilationResults?{urlencode({'pageSize': '1', 'orderBy': 'create_time desc'})}"
        found = _get(query).get("compilationResults") or []
        result = found[0].get("name", "") if found else ""
    if not result:
        raise CompiledGraphUnavailable("no_compilation", "the matching Dataform repository has no compilation result")
    actions = _list_all(f"{_API}/{result}:query", "compilationResultActions")
    targets: list[tuple[tuple[str, str, str], bool]] = []
    for action in actions:
        target = action.get("target") if isinstance(action, dict) else None
        if isinstance(target, dict) and isinstance(target.get("name"), str):
            targets.append(((str(target.get("database") or ""), str(target.get("schema") or ""), target["name"]),
                            isinstance(action.get("declaration"), dict)))
    return targets


def fetch(github_url: str, projects: list[str], location: str) -> dict:
    """Everything the UI needs about one repository's schedules (this calls the Dataform API)."""

    repos, warnings = find_repositories(github_url, projects, location)
    if not repos and warnings and len(warnings) == len(projects):
        raise WorkflowConfigError("; ".join(warnings))
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    rows: list[dict] = []
    for repo in repos:
        try:
            rows.extend(config_rows(repo, stamp))
        except WorkflowConfigError as exc:
            warnings.append(f"{short(repo)}: {exc}")
    return {"repositories": [short(r) for r in repos], "configs": rows, "projects": projects,
            "location": location, "warnings": warnings}


# --- Settings -------------------------------------------------------------------


def _settings() -> dict:
    saved = state.get_section(SECTION, {})
    return saved if isinstance(saved, dict) else {}


def default_location() -> str:
    value = _settings().get("location")
    return value if isinstance(value, str) and _LOCATION.match(value) else DEFAULT_LOCATION


def search_for(github_url: str) -> dict:
    """Projects and location to search for one repository: its override, else the BigQuery projects and the default location."""

    override = (_settings().get("overrides") or {}).get(norm_git_url(github_url)) or {}
    projects = [p for p in override.get("projects") or [] if isinstance(p, str) and _PROJECT_ID.match(p)]
    location = override.get("location")
    return {
        "projects": projects or bigquery_catalog.selected_projects(),
        "location": location if isinstance(location, str) and _LOCATION.match(location) else default_location(),
        "override": bool(override),
    }


def save_settings(github_url: object, projects: object = None, location: object = None,
                  default: object = None) -> dict:
    """Save a per-repository override of projects/location (empty values clear it) and optionally the default location."""

    if default is not None:
        if not isinstance(default, str) or not _LOCATION.match(default.strip()):
            raise ValueError("location must look like us-central1")
        saved = dict(_settings())
        saved["location"] = default.strip()
        state.set_section(SECTION, saved)
    if github_url is None:
        return {"location": default_location()}
    if not isinstance(github_url, str) or not norm_git_url(github_url):
        raise ValueError("unknown repository")
    items = projects if projects is not None else []
    if isinstance(items, str):
        items = re.split(r"[\s,]+", items)
    if not isinstance(items, list) or not all(isinstance(p, str) for p in items):
        raise ValueError("projects must be a list of project ids")
    items = list(dict.fromkeys(p.strip() for p in items if p.strip()))[:20]
    bad = next((p for p in items if not _PROJECT_ID.match(p)), None)
    if bad:
        raise ValueError(f"not a project id: {bad}")
    if location not in (None, "") and (not isinstance(location, str) or not _LOCATION.match(location.strip())):
        raise ValueError("location must look like us-central1")
    saved = dict(_settings())
    overrides = dict(saved.get("overrides") or {})
    key = norm_git_url(github_url)
    if items or location:
        overrides[key] = {"projects": items, **({"location": location.strip()} if location else {})}
    else:
        overrides.pop(key, None)
    saved["overrides"] = overrides
    state.set_section(SECTION, saved)
    return search_for(github_url)


# --- Cached access ----------------------------------------------------------------

_errors: dict[str, str] = {}
_ERRORS_LOCK = threading.Lock()


def _key(github_url: str, search: dict) -> str:
    return f"dataform-workflows:{norm_git_url(github_url)}:{','.join(search['projects'])}:{search['location']}"


def summary(github_url: str, refresh: bool = False) -> dict:
    """Status of one repository's schedules. Only ``refresh`` (or no saved copy and ``refresh`` ``None``) calls Dataform.

    ``state`` is ``needs_projects``, ``error``, ``not_loaded``, ``no_match`` or ``loaded``.
    """

    search = search_for(github_url)
    base = {"projects": search["projects"], "location": search["location"], "override": search["override"]}
    if not search["projects"]:
        return {**base, "state": "needs_projects", "message":
                "Choose the Google Cloud projects Dataform runs in under Settings, BigQuery projects, "
                "or set them for this repository."}
    key = _key(github_url, search)
    try:
        if refresh:
            answer = bigquery_catalog.cached(
                key, lambda: fetch(github_url, search["projects"], search["location"]), refresh=True)
        else:
            answer = bigquery_catalog.peek(key)
            if answer is None:
                with _ERRORS_LOCK:
                    failed = _errors.get(key)
                if failed:
                    return {**base, "state": "error", "message": failed}
                return {**base, "state": "not_loaded", "message": "Not loaded yet."}
            if answer["refreshing"]:  # past the cache lifetime: serve the saved copy, refresh behind it
                answer = bigquery_catalog.cached(
                    key, lambda: fetch(github_url, search["projects"], search["location"]))
    except (WorkflowConfigError, RuntimeError, ValueError) as exc:
        with _ERRORS_LOCK:
            _errors[key] = str(exc)
        return {**base, "state": "error", "message": str(exc)}
    with _ERRORS_LOCK:
        _errors.pop(key, None)
    data = answer["data"]
    rows = data["configs"]
    active = [row for row in rows if row["ACTIVE_PRODUCTION"]]
    result = {
        **base, "fetched_at": answer["fetchedAt"], "stale": answer.get("stale", False),
        "refreshing": answer.get("refreshing", False), "repositories": data["repositories"],
        "warnings": data.get("warnings", []), "configs": len(rows), "active_production": len(active),
        "rows": [{k: v for k, v in row.items() if k in COLUMNS} for row in rows],
    }
    if not data["repositories"]:
        looked = f"{', '.join(search['projects'])} ({search['location']})"
        result.update(state="no_match", message=f"No Dataform repository in {looked} has this git remote. "
                      "Check the projects and location, or that the repository is linked in Dataform.")
    else:
        result["state"] = "loaded"
    return result


def prefetch(github_url: str) -> threading.Thread:
    """Load schedules in the background after a repository loads; never raises."""

    def work() -> None:
        from . import console

        try:
            search = search_for(github_url)
            if search["projects"]:
                console.register("project", search["projects"])
                with console.task("Dataform schedules lookup", repo=github_url, warn_after=60):
                    summary(github_url, refresh=bigquery_catalog.peek(_key(github_url, search)) is None)
        except Exception as exc:  # noqa: BLE001 - a background nicety must not stop anything
            console.error("Dataform schedules lookup failed (the schedules view stays empty)", exc, code="KS-DATAFORM")

    thread = threading.Thread(target=work, name="kumosql-workflow-prefetch", daemon=True)
    thread.start()
    return thread


def forget_all() -> int:
    """Drop every saved Dataform schedule lookup and remembered error; returns how many lookups."""

    with _ERRORS_LOCK:
        _errors.clear()
    return bigquery_catalog.forget_prefix("dataform-workflows:")


def cached_rows(github_url: str) -> list[dict] | None:
    """Saved rows (with their selectors) without any network call, or ``None``."""

    answer = bigquery_catalog.peek(_key(github_url, search_for(github_url)))
    return answer["data"]["configs"] if answer else None


# --- Which models the schedules run -----------------------------------------------


def _key_matches(model_key: str, target: str) -> bool:
    mine, wanted = model_key.lower().split("."), target.lower().split(".")
    if len(wanted) < 2 or len(mine) < 2:
        return mine == wanted
    if mine[-2:] != wanted[-2:]:
        return False
    return len(mine) < 3 or len(wanted) < 3 or mine[0] == wanted[0]


def scheduled_models(pipeline, rows: list[dict]) -> dict[str, list[dict]]:
    """Map model key to the active production schedules that run it.

    A schedule runs the models carrying one of its tags and the models it names,
    or every model when it selects nothing; it also runs everything upstream
    and/or downstream of those when its dependency flags say so.
    """

    models = pipeline.models
    upstream, downstream = pipeline.upstream, pipeline.downstream
    result: dict[str, list[dict]] = {}
    for row in rows:
        if not row.get("ACTIVE_PRODUCTION"):
            continue
        tags, targets = set(row.get("included_tags") or []), row.get("included_targets") or []
        if tags or targets:
            chosen = {key for key, model in models.items()
                      if tags & set(getattr(model, "tags", ()))
                      or any(_key_matches(key, target) for target in targets)}
        else:
            chosen = set(models)
        for flag, edges in (("INCLUDE_DEPENDENCIES", upstream), ("INCLUDE_DEPENDENTS", downstream)):
            if row.get(flag):
                seen, stack = set(chosen), list(chosen)
                while stack:
                    for neighbour in edges.get(stack.pop(), ()):
                        if neighbour in models and neighbour not in seen:
                            seen.add(neighbour)
                            stack.append(neighbour)
                chosen = seen
        entry = {"config": row["WORKFLOW_CONFIGURATION"], "repo": row["REPO"],
                 "cron": row["CRON_SCHEDULE"], "time_zone": row["TIME_ZONE"]}
        for key in chosen:
            result.setdefault(key, []).append(entry)
    return result


def annotate(payload: dict, pipeline, remote: dict | None) -> dict:
    """Add ``schedules`` to the graph's nodes and a ``workflow`` summary, from saved data only."""

    url = (remote or {}).get("url")
    if not url:
        return payload
    rows = cached_rows(url)
    if rows is None:
        payload["workflow"] = {"state": "not_loaded"}
        return payload
    scheduled = scheduled_models(pipeline, rows)
    for node in payload.get("nodes", []):
        if node["id"] in scheduled:
            node["schedules"] = scheduled[node["id"]]
    payload["workflow"] = {
        "state": "loaded", "configs": len(rows), "scheduled_models": len(scheduled),
        "active_production": sum(1 for row in rows if row["ACTIVE_PRODUCTION"]),
    }
    return payload


# --- Export -------------------------------------------------------------------------


def to_csv(rows: list[dict]) -> str:
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=COLUMNS, extrasaction="ignore", lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return out.getvalue()


# --- Command line -------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    import argparse
    import sys

    parser = argparse.ArgumentParser(
        description="List the Dataform workflow configurations of the Dataform repository linked to a git repository")
    parser.add_argument("github_url", help="e.g. https://github.com/org/repo (SSH and https forms are equal)")
    parser.add_argument("--project", action="append", dest="projects",
                        help="Google Cloud project to search; repeatable (default: the projects chosen in Settings)")
    parser.add_argument("--location", help=f"Dataform location (default: {DEFAULT_LOCATION} or the saved default)")
    parser.add_argument("--csv", help="Write rows to this CSV file instead of printing JSON")
    args = parser.parse_args(argv)
    search = search_for(args.github_url)
    projects = args.projects or search["projects"]
    if not projects:
        parser.error("no projects to search: pass --project or choose projects in Settings")
    try:
        data = fetch(args.github_url, projects, args.location or search["location"])
    except (WorkflowConfigError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    for warning in data["warnings"]:
        print(f"warning: {warning}", file=sys.stderr)
    if not data["repositories"]:
        print(f"No Dataform repository found with git remote {args.github_url}", file=sys.stderr)
        return 1
    print(f"Matched Dataform repositories: {', '.join(data['repositories'])}", file=sys.stderr)
    rows = [{k: v for k, v in row.items() if k in COLUMNS} for row in data["configs"]]
    if args.csv:
        with open(args.csv, "w", newline="", encoding="utf-8") as handle:
            handle.write(to_csv(rows))
        print(f"Wrote {len(rows)} rows to {args.csv}", file=sys.stderr)
    else:
        print(json.dumps(rows, indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
