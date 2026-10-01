"""Which KumoSQL build is running, so an update can be confirmed."""

from __future__ import annotations

import json
import subprocess
from functools import lru_cache
from importlib import metadata
from pathlib import Path


@lru_cache(maxsize=1)
def info() -> dict:
    """``{"version", "commit"}``; ``commit`` is None when it cannot be determined.

    A ``pip install git+https://...`` records the commit pip checked out; a
    development checkout is asked directly.
    """

    try:
        dist = metadata.distribution("kumosql")
        version = dist.version
    except metadata.PackageNotFoundError:
        dist, version = None, "unknown"
    commit = None
    if dist is not None:
        try:
            direct = json.loads(dist.read_text("direct_url.json") or "{}")
            commit = (direct.get("vcs_info") or {}).get("commit_id")
        except (ValueError, OSError):
            commit = None
    if not commit:
        root = Path(__file__).resolve().parents[2]
        if (root / ".git").exists():
            try:
                done = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True,
                                      text=True, timeout=5, check=False)
                commit = done.stdout.strip() or None
            except (OSError, subprocess.SubprocessError):
                commit = None
    return {"version": version, "commit": commit[:7] if commit else None}


def describe() -> str:
    data = info()
    return f"{data['version']} ({data['commit']})" if data["commit"] else data["version"]
