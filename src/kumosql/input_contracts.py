"""Persistent, provenance-aware project input contract baselines.

This module stores evidence; it does not promote a check against one snapshot into a
database-wide guarantee. Consumers must inspect ``proof_eligible`` before using a
contract as a conditional-proof premise.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import threading
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from . import state

EVIDENCE_KINDS = frozenset({"user_assertion", "declared_metadata", "snapshot_check", "sql_guarantee"})
_LOCK = threading.RLock()
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


def fingerprint(value: Any) -> str:
    """Stable SHA-256 fingerprint of JSON-compatible source/schema/scope data."""

    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class InputContract:
    """A contract and the exact evidence recorded for it."""

    evidence_kind: str
    relation: str
    columns: tuple[str, ...]
    predicate: str
    source_identity: str
    scope: Mapping[str, Any]
    schema_fingerprint: str
    definition_fingerprint: str
    checking_sql: str = ""
    result: Any = None
    checked_at: str = ""
    id: str = ""

    def __post_init__(self) -> None:
        if self.evidence_kind not in EVIDENCE_KINDS:
            raise ValueError(f"evidence_kind must be one of {sorted(EVIDENCE_KINDS)}")
        if not self.relation.strip() or not self.columns or not self.predicate.strip():
            raise ValueError("relation, columns, and predicate are required")
        if self.evidence_kind == "snapshot_check" and not self.checked_at:
            object.__setattr__(self, "checked_at", datetime.now(timezone.utc).isoformat())
        if not self.id:
            identity = {
                "evidence_kind": self.evidence_kind,
                "relation": self.relation,
                "columns": self.columns,
                "predicate": self.predicate,
                "source_identity": self.source_identity,
                "scope": self.scope,
                "schema_fingerprint": self.schema_fingerprint,
                "definition_fingerprint": self.definition_fingerprint,
            }
            object.__setattr__(self, "id", "contract_" + fingerprint(identity)[:24])

    @property
    def proof_eligible(self) -> bool:
        """Whether provenance can be offered as a general conditional-proof premise."""

        # SQL-derived entries need a checked derivation object before they can be
        # trusted. This store only records their category; it does not derive them.
        return self.evidence_kind == "user_assertion"

    def current_for(self, *, source_identity: str, scope: Mapping[str, Any], schema_fingerprint: str,
                    definition_fingerprint: str) -> bool:
        return (
            self.source_identity == source_identity
            and dict(self.scope) == dict(scope)
            and self.schema_fingerprint == schema_fingerprint
            and self.definition_fingerprint == definition_fingerprint
        )


@dataclass(frozen=True)
class ContractBaseline:
    name: str
    project_identity: str
    created_at: str
    contracts: tuple[InputContract, ...]
    version: int = 1


def _path(project_identity: str, name: str) -> Path:
    if not _NAME.fullmatch(name):
        raise ValueError("baseline name must contain 1-128 letters, digits, dots, underscores, or hyphens")
    project_key = fingerprint(project_identity)
    return state.data_path("input-contracts", project_key, f"{name}.json")


def save_baseline(name: str, project_identity: str, contracts: list[InputContract]) -> ContractBaseline:
    """Atomically save or replace a named baseline under KumoSQL's chosen data folder."""

    unique = {contract.id: contract for contract in contracts}
    baseline = ContractBaseline(name, project_identity, datetime.now(timezone.utc).isoformat(), tuple(unique[k] for k in sorted(unique)))
    path = _path(project_identity, name)
    payload = asdict(baseline)
    with _LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle, temp_name = tempfile.mkstemp(dir=path.parent, prefix=".baseline-", suffix=".tmp")
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as output:
                json.dump(payload, output, indent=2, sort_keys=True, ensure_ascii=False)
            os.replace(temp_name, path)
        except BaseException:
            try:
                os.unlink(temp_name)
            except OSError:
                pass
            raise
    return baseline


def load_baseline(name: str, project_identity: str) -> ContractBaseline | None:
    """Load a baseline by project and name, or return ``None`` when it does not exist."""

    path = _path(project_identity, name)
    try:
        with _LOCK:
            payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    contracts = tuple(InputContract(**{**item, "columns": tuple(item["columns"])}) for item in payload["contracts"])
    return ContractBaseline(payload["name"], payload["project_identity"], payload["created_at"], contracts, payload["version"])


def proof_premises(baseline: ContractBaseline, *, source_identity: str, scope: Mapping[str, Any],
                   schema_fingerprint: str, definition_fingerprint: str) -> tuple[InputContract, ...]:
    """Return current, explicitly proof-eligible contracts; snapshot checks are never returned."""

    return tuple(
        contract for contract in baseline.contracts
        if contract.proof_eligible and contract.current_for(
            source_identity=source_identity, scope=scope, schema_fingerprint=schema_fingerprint,
            definition_fingerprint=definition_fingerprint,
        )
    )
