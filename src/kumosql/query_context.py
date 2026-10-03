"""Non-secret cache namespaces for the active BigQuery execution context.

Credential secrets are compared only in process, never hashed or persisted.
Opaque access tokens and ADC without an account identifier get a random process
namespace; they cannot reuse another process's saved results.
"""

import hashlib
import json
import os
import threading
import uuid
from pathlib import Path

from . import state

_lock = threading.Lock()
_signals: tuple | None = None
_generation = ""
_anonymous = uuid.uuid4().hex


def execution_location() -> str:
    settings = state.get_section("bigquery", {}) or {}
    value = settings.get("location", "")
    return value.strip() if isinstance(value, str) else ""


def _credential_signal() -> tuple[tuple, str]:
    token = os.environ.get("BQ_ACCESS_TOKEN")
    if token:
        return ("token", token), ""
    raw = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS_JSON")
    path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    if not raw:
        if not path:
            base = os.environ.get("CLOUDSDK_CONFIG")
            if not base:
                base = str(Path(os.environ.get("APPDATA", str(Path.home() / ".config"))) / "gcloud")
            path = str(Path(base) / "application_default_credentials.json")
        try:
            raw = Path(path).read_text(encoding="utf-8")
        except OSError:
            raw = ""
    try:
        info = json.loads(raw or "{}")
    except ValueError:
        info = {}
    identity = (info.get("client_email") or info.get("account") or "") if isinstance(info, dict) else ""
    if isinstance(identity, str) and identity and isinstance(info, dict):
        identity = json.dumps([identity, info.get("private_key_id"), info.get("client_id")])
    return ("credentials", path, raw, os.environ.get("GOOGLE_CLOUD_PROJECT")), identity if isinstance(identity, str) else ""


def execution_context(project: str) -> str:
    """Hash public context plus a random credential generation, never a token."""
    global _signals, _generation
    signals, principal = _credential_signal()
    with _lock:
        if _signals is not None and signals != _signals:
            _generation = uuid.uuid4().hex
        _signals = signals
        # Unknown identities are deliberately scoped to this process. Known
        # identities also rotate when credential material changes in process.
        identity = f"{principal or _anonymous}:{_generation}"
    return hashlib.sha256(json.dumps([project, execution_location().casefold(), identity]).encode()).hexdigest()
