"""Read-only access to public Dataform projects hosted on GitHub."""

from __future__ import annotations

import base64
import json
import re
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen


class GitHubRepoError(ValueError):
    """A repository URL or GitHub API request could not be handled."""


_MAX_RESPONSE_BYTES = 12 * 1024 * 1024
_OWNER_REPO = re.compile(r"^/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?$")


def parse_repo_url(value: str) -> tuple[str, str]:
    """Accept a GitHub repository root URL; reject other hosts and URL forms."""

    if not isinstance(value, str) or len(value) > 2048:
        raise GitHubRepoError("Enter a GitHub repository URL")
    parsed = urlparse(value.strip())
    try:
        valid_port = parsed.port in (None, 443)
    except ValueError:
        valid_port = False
    match = _OWNER_REPO.fullmatch(parsed.path)
    if (parsed.scheme != "https" or parsed.hostname != "github.com"
            or parsed.username or parsed.password or not valid_port
            or parsed.query or parsed.fragment or not match):
        raise GitHubRepoError("Use a repository URL like https://github.com/owner/repository")
    return match.group(1), match.group(2)


def _get_json(url: str) -> dict:
    request = Request(url, headers={
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "KumoSQL",
    })
    try:
        with urlopen(request, timeout=15) as response:
            raw = response.read(_MAX_RESPONSE_BYTES + 1)
    except HTTPError as exc:
        if exc.code == 404:
            raise GitHubRepoError("Repository or file was not found, or the repository is private") from exc
        if exc.code in (403, 429):
            raise GitHubRepoError("GitHub API rate limit reached; try again later") from exc
        raise GitHubRepoError(f"GitHub returned HTTP {exc.code}") from exc
    except (TimeoutError, URLError, OSError) as exc:
        raise GitHubRepoError("Could not reach GitHub; check your connection and try again") from exc
    if len(raw) > _MAX_RESPONSE_BYTES:
        raise GitHubRepoError("GitHub response is too large to browse")
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GitHubRepoError("GitHub returned an invalid response") from exc
    if not isinstance(payload, dict):
        raise GitHubRepoError("GitHub returned an unexpected response")
    return payload


def connect(value: str) -> dict:
    """List Dataform SQLX files in a public repository's default branch."""

    owner, repo = parse_repo_url(value)
    root = f"https://api.github.com/repos/{quote(owner)}/{quote(repo)}"
    details = _get_json(root)
    branch = details.get("default_branch")
    if not isinstance(branch, str) or not branch:
        raise GitHubRepoError("GitHub repository has no default branch")
    tree = _get_json(f"{root}/git/trees/{quote(branch, safe='') }?recursive=1")
    if tree.get("truncated"):
        raise GitHubRepoError("Repository is too large to browse in one pass")
    entries = tree.get("tree")
    if not isinstance(entries, list):
        raise GitHubRepoError("GitHub returned an invalid repository tree")
    paths = [
        item["path"] for item in entries
        if isinstance(item, dict) and item.get("type") == "blob"
        and isinstance(item.get("path"), str) and item["path"].lower().endswith(".sqlx")
    ]
    paths.sort(key=str.casefold)
    if not any(path in {"workflow_settings.yaml", "workflow_settings.yml", "dataform.json"}
               for path in (item.get("path") for item in entries if isinstance(item, dict))):
        raise GitHubRepoError("This repository does not appear to be a Dataform project (no workflow_settings.yaml or dataform.json at its root)")
    if not paths:
        raise GitHubRepoError("Dataform project found, but it contains no .sqlx files")
    return {
        "repository": details.get("full_name", f"{owner}/{repo}"),
        "branch": branch,
        "files": paths,
    }


def read_file(value: str, branch: str, path: str) -> dict:
    """Fetch one tracked SQLX file from a connected public repository."""

    owner, repo = parse_repo_url(value)
    if not isinstance(branch, str) or not branch or len(branch) > 255:
        raise GitHubRepoError("Invalid branch")
    if not isinstance(path, str) or len(path) > 2048 or not path.lower().endswith(".sqlx"):
        raise GitHubRepoError("Choose a .sqlx file from the repository")
    if path.startswith("/") or ".." in path.split("/") or "\\" in path:
        raise GitHubRepoError("Invalid repository file path")
    url = (f"https://api.github.com/repos/{quote(owner)}/{quote(repo)}"
           f"/contents/{quote(path, safe='/')}?ref={quote(branch, safe='')}")
    payload = _get_json(url)
    if payload.get("type") != "file":
        raise GitHubRepoError("GitHub did not return a file")
    if not isinstance(payload.get("content"), str):
        raise GitHubRepoError("GitHub only returns file contents up to 1 MB through this endpoint")
    try:
        content = base64.b64decode(payload["content"], validate=False).decode("utf-8")
    except (ValueError, UnicodeDecodeError) as exc:
        raise GitHubRepoError("The selected file is not valid UTF-8 text") from exc
    if len(content.encode("utf-8")) > 5 * 1024 * 1024:
        raise GitHubRepoError("The selected file is larger than 5 MB")
    return {"path": path, "content": content}
