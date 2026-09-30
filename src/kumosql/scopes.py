"""Saved scopes: named labels of associations on any field.

A scope is a name plus a field and the values that field may take, for example
``My Team`` = ``author`` in ``{ana@x.com, bo@x.com}`` or ``My Team's Projects``
= ``project`` in ``{proj-a, proj-b}``. Use one to silo a task such as a
refactor or an inefficient-job search to an actionable slice. A scope may
constrain several fields at once; a record matches when every field matches.
Matching is case-insensitive, and a value ending in ``*`` is a prefix match.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping

from . import state

SECTION = "scopes"
MAX_SCOPES = 200
MAX_VALUES = 5000


@dataclass(frozen=True)
class Scope:
    name: str
    #: field name -> allowed values (``*`` suffix means prefix match)
    fields: Mapping[str, tuple[str, ...]]

    def matches(self, record: Mapping[str, object]) -> bool:
        """Whether ``record`` satisfies every field constraint of the scope."""

        for field, values in self.fields.items():
            actual = record.get(field)
            if actual is None:
                return False
            text = str(actual).casefold()
            if not any(_value_matches(text, value.casefold()) for value in values):
                return False
        return True

    def filter(self, records: Iterable[Mapping[str, object]]) -> list[Mapping[str, object]]:
        return [record for record in records if self.matches(record)]

    def to_json(self) -> dict:
        return {"name": self.name, "fields": {k: list(v) for k, v in self.fields.items()}}


def _value_matches(text: str, pattern: str) -> bool:
    return text.startswith(pattern[:-1]) if pattern.endswith("*") else text == pattern


def parse_scope(data: object) -> Scope:
    """Validate a JSON-shaped scope, raising ``ValueError`` when it is malformed."""

    if not isinstance(data, dict):
        raise ValueError("a scope must be an object")
    name = data.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ValueError("a scope needs a name")
    fields = data.get("fields")
    if not isinstance(fields, dict) or not fields:
        raise ValueError("a scope needs at least one field")
    parsed: dict[str, tuple[str, ...]] = {}
    for field, values in fields.items():
        if not isinstance(field, str) or not field.strip():
            raise ValueError("field names must be non-empty text")
        if not isinstance(values, list) or len(values) > MAX_VALUES or any(not isinstance(v, str) for v in values):
            raise ValueError(f"values for {field!r} must be a list of text")
        cleaned = tuple(dict.fromkeys(v.strip() for v in values if v.strip()))
        if not cleaned:
            raise ValueError(f"field {field!r} needs at least one value")
        parsed[field.strip()] = cleaned
    return Scope(name.strip(), parsed)


def list_scopes() -> list[Scope]:
    """All saved scopes, in the order they were saved."""

    stored = state.get_section(SECTION, [])
    scopes = []
    for item in stored if isinstance(stored, list) else []:
        try:
            scopes.append(parse_scope(item))
        except ValueError:
            continue
    return scopes


def get_scope(name: str) -> Scope | None:
    key = name.strip().casefold()
    return next((scope for scope in list_scopes() if scope.name.casefold() == key), None)


def save_scopes(scopes: Iterable[Scope]) -> None:
    scopes = list(scopes)
    if len(scopes) > MAX_SCOPES:
        raise ValueError(f"at most {MAX_SCOPES} scopes can be saved")
    names = [scope.name.casefold() for scope in scopes]
    if len(set(names)) != len(names):
        raise ValueError("scope names must be unique")
    state.set_section(SECTION, [scope.to_json() for scope in scopes])


def save_scope(scope: Scope) -> None:
    """Add a scope, or replace the saved one with the same name."""

    key = scope.name.casefold()
    scopes = [s for s in list_scopes() if s.name.casefold() != key]
    scopes.append(scope)
    save_scopes(scopes)


def delete_scope(name: str) -> bool:
    key = name.strip().casefold()
    scopes = list_scopes()
    kept = [s for s in scopes if s.name.casefold() != key]
    if len(kept) == len(scopes):
        return False
    save_scopes(kept)
    return True
