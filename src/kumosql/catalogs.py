"""Catalogs: the tables, views, functions and procedures a team owns.

A Catalog is a name plus a rule, exactly like a scope or a tag rule: the same condition tree
(:mod:`kumosql.scopes`: AND/OR/NOT, "is one of", saved scopes, "is returned by SQL query", the columns
of data sources joined by ``full_name``) over the objects known right now, the loaded Dataform project
and the saved BigQuery catalog (:func:`kumosql.tags.collect_objects`). The rule can use ``tag`` but not
``catalog`` (that is what it produces).

The built-in catalog ``Dataform repository`` holds every model of the loaded repository, which is how
ownership worked before catalogs existed. Catalogs are saved in the ``catalogs`` section of the local
settings file; the *active* ones (``catalogs_active``, default: the built-in one) are what the graph,
impact analysis and cost pages treat as "ours". An affected reader outside the active catalogs is still
listed, flagged as not owned.

Every object record gets a list field ``catalog``, so any rule (a scope, or a Refactor class) can say
``catalog is one of My team``.
"""

from __future__ import annotations

from typing import Iterable, Mapping

from . import scopes as scope_store
from . import state, tags

SECTION = "catalogs"
ACTIVE_SECTION = "catalogs_active"
DEFAULT = "Dataform repository"
MAX_CATALOGS = 100
MAX_NAME = 80
#: Models and tables the repository builds (declarations are tables it only reads).
DEFAULT_RULE = {"all": [
    {"field": "source", "op": "eq", "value": "dataform"},
    {"field": "type", "op": "ne", "value": "DECLARATION"},
]}
RESERVED_FIELDS = ("catalog", "catalogs")


def parse_catalog(data: object) -> dict:
    """Validate one ``{"name", "rule"}`` pair and return it normalized; ``ValueError`` when malformed."""

    if not isinstance(data, Mapping):
        raise ValueError("a catalog must be an object")
    name = data.get("name")
    if not isinstance(name, str) or not " ".join(name.split()):
        raise ValueError("a catalog needs a name")
    name = " ".join(name.split())
    if len(name) > MAX_NAME:
        raise ValueError(f"a catalog name can be at most {MAX_NAME} characters")
    return {"name": name, "rule": scope_store.parse_rule(data.get("rule"))}


def _scope_lookup() -> dict[str, scope_store.Scope]:
    return {scope.name.casefold(): scope for scope in scope_store.list_scopes()}


def _check_rule(rule: Mapping, lookup: Mapping[str, scope_store.Scope]) -> dict:
    """The rule with scopes inlined, after checking it can run on objects; ``ValueError`` if it cannot."""

    expanded = scope_store.expand_rule(rule, lookup, ())
    used = scope_store.rule_fields(expanded)
    banned = [name for name in used if name.casefold().split(".")[0] in RESERVED_FIELDS]
    if banned:
        raise ValueError(f"a catalog rule cannot use {banned[0]!r}: catalogs are what the rule produces")
    from . import data_sources

    fields = (*tags.OBJECT_FIELDS, "tag", *data_sources.tag_fields())
    known = {name.casefold() for name in fields}
    missing = [name for name in used if name.casefold() not in known and name.casefold().split(".")[0] not in known]
    if missing:
        raise ValueError(f"objects have no field {missing[0]!r}. Fields a catalog rule can use: {', '.join(fields)}")
    return expanded


def list_catalogs(*, builtin: bool = True) -> list[dict]:
    """The saved catalogs in order, after the built-in one. A stored catalog that no longer parses is skipped."""

    stored = state.get_section(SECTION, [])
    found: list[dict] = []
    for item in stored if isinstance(stored, list) else []:
        try:
            parsed = parse_catalog(item)
        except ValueError:
            continue
        if parsed["name"].casefold() != DEFAULT.casefold():
            found.append(parsed)
    if builtin:
        found.insert(0, {"name": DEFAULT, "rule": DEFAULT_RULE, "builtin": True})
    return found


def save_catalogs(items: object) -> list[dict]:
    """Replace the saved catalogs (the built-in one is never stored); returns the list as listed."""

    if not isinstance(items, list):
        raise ValueError("catalogs must be a list")
    if len(items) > MAX_CATALOGS:
        raise ValueError(f"at most {MAX_CATALOGS} catalogs can be saved")
    parsed = [parse_catalog(item) for item in items]
    names = [item["name"].casefold() for item in parsed]
    if DEFAULT.casefold() in names:
        raise ValueError(f"“{DEFAULT}” is built in and cannot be redefined")
    if len(set(names)) != len(names):
        raise ValueError("catalog names must be unique")
    lookup = _scope_lookup()
    for item in parsed:
        try:
            _check_rule(item["rule"], lookup)
        except ValueError as exc:
            raise ValueError(f"catalog {item['name']!r}: {exc}") from exc
    state.set_section(SECTION, parsed)
    keep = [name for name in _stored_active() if name.casefold() in set(names) or name.casefold() == DEFAULT.casefold()]
    if keep != _stored_active():
        state.set_section(ACTIVE_SECTION, keep)
    return list_catalogs()


def describe(catalog: Mapping) -> str:
    return scope_store.describe_rule(catalog["rule"])


# -------------------------------------------------------------------- active


def _stored_active() -> list[str]:
    stored = state.get_section(ACTIVE_SECTION, None)
    return [name for name in stored if isinstance(name, str)] if isinstance(stored, list) else [DEFAULT]


def active_names() -> list[str]:
    """Names of the active catalogs, in listed order; the built-in one when none was chosen (or none still exists)."""

    wanted = {name.casefold() for name in _stored_active()}
    names = [item["name"] for item in list_catalogs() if item["name"].casefold() in wanted]
    return names or [DEFAULT]


def set_active(names: object) -> list[str]:
    if not isinstance(names, list) or not all(isinstance(name, str) for name in names):
        raise ValueError("active catalogs must be a list of names")
    known = {item["name"].casefold(): item["name"] for item in list_catalogs()}
    missing = [name for name in names if name.casefold() not in known]
    if missing:
        raise ValueError(f"no catalog named {missing[0]!r}")
    if not names:
        raise ValueError("at least one catalog must be active")
    state.set_section(ACTIVE_SECTION, [known[name.casefold()] for name in names])
    return active_names()


# ---------------------------------------------------------------- evaluation


def _records(pipeline: object | None) -> tuple[dict[str, dict], Mapping[str, Mapping]]:
    """The known objects, and their rule records (data-source columns and ``tag`` added)."""

    from . import data_sources

    objects = tags.collect_objects(pipeline)
    by_tag = tags.tags_by_object(objects)
    records = {key: {**record, "tag": by_tag.get(key, [])} for key, record in data_sources.enrich_objects(objects).items()}
    return objects, records


def evaluate(pipeline: object | None = None) -> tuple[dict[str, dict], list[dict]]:
    """Members of every catalog: ``({catalog name: {"keys", "error"}}`` as rows, and the objects).

    Returns ``(objects, rows)`` where each row is ``{"name", "keys": set of normalized names, "error"}``.
    A catalog whose rule cannot run has no keys and says why."""

    objects, records = _records(pipeline)
    lookup = _scope_lookup()
    rows = []
    for item in list_catalogs():
        row = {"name": item["name"], "keys": set(), "error": None}
        try:
            expanded = _check_rule(item["rule"], lookup)
            row["keys"] = {key for key, record in records.items() if scope_store.evaluate_rule(expanded, record)}
        except ValueError as exc:
            row["error"] = str(exc)
        rows.append(row)
    return objects, rows


def members(names: Iterable[str] | str, pipeline: object | None = None) -> set[str]:
    """Normalized ``project.dataset.name`` of every object in any of the catalogs ``names``; ``ValueError`` if one is unknown."""

    wanted = [names] if isinstance(names, str) else list(names)
    _, rows = evaluate(pipeline)
    by_name = {row["name"].casefold(): row for row in rows}
    found: set[str] = set()
    for name in wanted:
        row = by_name.get(name.casefold())
        if row is None:
            raise ValueError(f"no catalog named {name!r}")
        if row["error"]:
            raise ValueError(row["error"])
        found |= row["keys"]
    return found


class Owned:
    """What the active catalogs own, looked up by any name an object goes by (its full name, a model key)."""

    def __init__(self, names: set[str], catalogs: list[str], everything: bool = False):
        self.names = names
        self.catalogs = catalogs
        self.everything = everything

    def __contains__(self, name: object) -> bool:
        return self.everything or (isinstance(name, str) and tags.norm(name) in self.names)

    def __bool__(self) -> bool:
        return True


def owned(pipeline: object | None = None) -> Owned:
    """The objects the active catalogs own, with their aliases (so a Dataform model key matches too)."""

    objects, rows = evaluate(pipeline)
    active = {name.casefold() for name in active_names()}
    keys: set[str] = set()
    for row in rows:
        if row["name"].casefold() in active:
            keys |= row["keys"]
    names = set(keys)
    for key in keys:
        names |= objects.get(key, {}).get("_aliases", set())
    return Owned(names, active_names())


def model_keys(pipeline: object) -> set[str] | None:
    """Keys of the models the active catalogs own, or ``None`` when that is every model (nothing to filter)."""

    have = owned(pipeline)
    keys = {key for key in pipeline.models if key in have}  # type: ignore[attr-defined]
    return None if len(keys) == len(pipeline.models) else keys  # type: ignore[attr-defined]


def lookup(pipeline: object | None = None) -> dict[str, list[str]]:
    """Catalog names by normalized object name and alias, for the scope field ``catalog``."""

    objects, rows = evaluate(pipeline)
    result: dict[str, list[str]] = {}
    for row in rows:
        for key in row["keys"]:
            for name in (key, *objects.get(key, {}).get("_aliases", ())):
                if row["name"] not in result.setdefault(name, []):
                    result[name].append(row["name"])
    return result


def snapshot(pipeline: object | None = None) -> dict:
    """Everything the pages need: each catalog with its rule, size and error, and which are active."""

    objects, rows = evaluate(pipeline)
    stored = {item["name"].casefold(): item for item in list_catalogs()}
    active = {name.casefold() for name in active_names()}
    return {
        "catalogs": [
            {"name": row["name"], "rule": stored[row["name"].casefold()]["rule"],
             "description": describe(stored[row["name"].casefold()]), "builtin": bool(stored[row["name"].casefold()].get("builtin")),
             "matched": len(row["keys"]), "error": row["error"], "active": row["name"].casefold() in active}
            for row in rows
        ],
        "active": active_names(),
        "known_objects": len(objects),
    }


def preview(data: object, limit: int = 12) -> dict:
    """How many objects a catalog rule would own right now, with examples; nothing is saved."""

    item = parse_catalog(data)
    objects, records = _records(None)
    expanded = _check_rule(item["rule"], _scope_lookup())
    keys = [key for key, record in records.items() if scope_store.evaluate_rule(expanded, record)]
    return {"matched": len(keys), "of": len(objects), "examples": [objects[key]["_key"] for key in sorted(keys)[:limit]]}
