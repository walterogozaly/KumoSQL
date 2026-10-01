"""Tags: KumoSQL's own labels on anything that lives inside a dataset.

An *object* is a table, view, materialized view, function (UDF), procedure, or a Dataform model or
declaration, named ``project.dataset.name``. A tag is a short text label such as ``Retired``. Tags
come from two places and are kept apart:

* **Manual tags**, added and removed by hand in the BigQuery explorer or on a graph node.
* **Tag rules**, "objects matching <conditions> get tag <X>". A condition tree is the same
  :mod:`kumosql.scopes` rule (nested AND/OR/NOT, "is one of", other saved scopes) over the fields in
  :data:`OBJECT_FIELDS`. Rules are evaluated when asked for, over the objects known right now (the
  loaded Dataform project and the saved BigQuery catalog), so a reloaded repository or refreshed
  catalog is tagged again without any further step.

Tags are metadata of this tool only. They are saved in the ``tags`` and ``tag_rules`` sections of the
local settings file and are never written back to BigQuery as labels. Matching ignores case.
The scope field ``tag`` (a list) holds an object's tags, so a scope can say "tag is Retired".
A tag rule can also use "is returned by SQL query" (for example ``full_name`` is returned by a query),
which runs in the billing project under the same bytes cap and cache lifetime as scopes. A tag rule
itself cannot use ``tag``, its own output.
"""

from __future__ import annotations

import re
from typing import Iterable, Mapping

from . import scopes as scope_store
from . import state

MANUAL_SECTION = "tags"
RULES_SECTION = "tag_rules"
MAX_TAG_LENGTH = 60
MAX_RULES = 200
MAX_TAGS_PER_OBJECT = 50
MAX_KEYS_PER_REQUEST = 5000

#: Fields a tag rule can test. ``schema`` is another name for ``dataset``; ``type`` is BigQuery's
#: object type (TABLE, VIEW, MATERIALIZED_VIEW, EXTERNAL, SNAPSHOT, UDF, TABLE_FUNCTION,
#: AGGREGATE_FUNCTION, PROCEDURE); ``kind`` and ``path`` come from a Dataform model; ``source`` says
#: where the object is known from (``bigquery``, ``dataform`` or both, as a list).
OBJECT_FIELDS = ("project", "dataset", "schema", "name", "table", "full_name", "type", "kind", "source", "path", "model")
RESERVED_FIELDS = ("tag", "tags")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def norm(key: str) -> str:
    return key.strip().casefold()


def parse_tag(value: object) -> str:
    """A tag name, tidied: whitespace collapsed, 1 to 60 characters; ``ValueError`` otherwise."""

    if not isinstance(value, str):
        raise ValueError("a tag must be text")
    text = " ".join(_CONTROL.sub(" ", value).split())
    if not text:
        raise ValueError("a tag cannot be empty")
    if len(text) > MAX_TAG_LENGTH:
        raise ValueError(f"a tag can be at most {MAX_TAG_LENGTH} characters")
    return text


# ------------------------------------------------------------------- objects


def _record(project: str, dataset: str, name: str, **extra: object) -> dict[str, object]:
    return {"project": project, "dataset": dataset, "schema": dataset, "name": name, "table": name,
            "full_name": _join(project, dataset, name), **extra}


def _join(*parts: str) -> str:
    return ".".join(part for part in parts if part)


def collect_objects(pipeline: object | None = None, *, catalog: bool = True) -> dict[str, dict]:
    """Every object tags can apply to, by normalized ``project.dataset.name``.

    Each value is the rule record plus ``_key`` (the display key) and ``_aliases`` (other names the
    object goes by, such as a Dataform model key that omits the project). With no ``pipeline`` the
    loaded project is used. BigQuery objects come only from the saved catalog: nothing here calls
    BigQuery.
    """

    objects: dict[str, dict] = {}

    def add(key: str, record: dict[str, object], aliases: Iterable[str] = ()) -> None:
        slot = objects.get(norm(key))
        if slot is None:
            slot = objects[norm(key)] = {"_key": key, "_aliases": set(), "source": []}
        for field, value in record.items():
            if field == "_bq":
                continue
            if field == "source":
                if value not in slot["source"]:
                    slot["source"].append(value)
            elif value not in (None, "") and (field not in slot or field == "type" and record.get("_bq")):
                slot[field] = value
        slot["_aliases"].update(norm(a) for a in aliases if a and norm(a) != norm(key))

    if pipeline is None:
        from . import live_graph

        loaded = live_graph.loaded()
        pipeline = loaded["pipeline"] if loaded else None
    if pipeline is not None:
        default_project = getattr(pipeline, "default_project", "") or ""
        default_dataset = getattr(pipeline, "default_dataset", "") or ""
        for key, model in pipeline.models.items():  # type: ignore[attr-defined]
            target = model.target
            project, dataset = target.database or default_project, target.schema or default_dataset
            identity = getattr(model.identity, "key", "") if model.identity is not None else ""
            add(_join(project, dataset, target.name),
                _record(project, dataset, target.name, kind=model.kind, path=model.path, model=key,
                        type=str(model.kind).upper(), source="dataform"),
                (key, identity))
        for key, target in pipeline.sources.items():  # type: ignore[attr-defined]
            project, dataset = target.database or default_project, target.schema or default_dataset
            add(_join(project, dataset, target.name),
                _record(project, dataset, target.name, kind="declaration", type="DECLARATION", source="dataform"),
                (key,))
    if catalog:
        from . import bigquery_catalog as bq

        for project in bq.selected_projects():
            datasets = bq.peek(f"datasets\x1fbrowsable\x1f{project}")
            for dataset in (datasets or {}).get("data", []):
                tables = bq.peek(f"tables\x1f{project}\x1f{dataset['id']}")
                for item in (tables or {}).get("data", []):
                    add(_join(project, dataset["id"], item["id"]),
                        _record(project, dataset["id"], item["id"], type=item.get("type", "TABLE"),
                                source="bigquery", _bq=True))
    return _merge_partial_names(objects)


def _merge_partial_names(objects: dict[str, dict]) -> dict[str, dict]:
    """Fold an object named without its project (a Dataform model) into the one BigQuery object it must be.

    ``dataset.name`` and ``name`` match a full name that ends the same way, but only when exactly one
    does; an ambiguous name stays separate rather than guessing.
    """

    full = [key for key in objects if key.count(".") >= 2]
    tails: dict[str, list[str]] = {}
    for key in full:
        parts = key.split(".")
        for size in (1, 2):
            tails.setdefault(".".join(parts[-size:]), []).append(key)
    for key in [k for k in objects if k.count(".") < 2]:
        matches = [m for m in tails.get(key, []) if "bigquery" in objects[m]["source"]]
        if len(matches) != 1:
            continue
        partial, target = objects.pop(key), objects[matches[0]]
        for field, value in partial.items():
            if field == "source":
                target["source"].extend(v for v in value if v not in target["source"])
            elif field == "_aliases":
                target["_aliases"].update(value)
            elif field not in ("_key", "type") and value not in (None, ""):
                target.setdefault(field, value)
        target["_aliases"].add(key)
    return objects


# --------------------------------------------------------------------- rules


def parse_tag_rule(data: object) -> dict:
    """Validate one ``{"tag", "rule"}`` pair and return it normalized; ``ValueError`` when malformed."""

    if not isinstance(data, Mapping):
        raise ValueError("a tag rule must be an object")
    tag = parse_tag(data.get("tag"))
    rule = scope_store.parse_rule(data.get("rule"))
    return {"tag": tag, "rule": rule}


def _check_rule(rule: Mapping, lookup: Mapping[str, scope_store.Scope]) -> dict:
    """The rule with scopes inlined, after checking it can run on objects; ``ValueError`` if it cannot."""

    expanded = scope_store.expand_rule(rule, lookup, ())
    used = scope_store.rule_fields(expanded)
    banned = [name for name in used if name.casefold().split(".")[0] in RESERVED_FIELDS]
    if banned:
        raise ValueError(f"a tag rule cannot use {banned[0]!r}: tags are what the rule produces")
    known = {name.casefold() for name in OBJECT_FIELDS}
    missing = [name for name in used if name.casefold() not in known]
    if missing:
        raise ValueError(
            f"objects have no field {missing[0]!r}. Fields a tag rule can use: {', '.join(OBJECT_FIELDS)}")
    return expanded


def _scope_lookup() -> dict[str, scope_store.Scope]:
    return {scope.name.casefold(): scope for scope in scope_store.list_scopes()}


def list_rules() -> list[dict]:
    """The saved tag rules in order. A stored rule that no longer parses is skipped."""

    stored = state.get_section(RULES_SECTION, [])
    rules = []
    for item in stored if isinstance(stored, list) else []:
        try:
            rules.append(parse_tag_rule(item))
        except ValueError:
            continue
    return rules


def save_rules(items: object) -> list[dict]:
    if not isinstance(items, list):
        raise ValueError("tag rules must be a list")
    if len(items) > MAX_RULES:
        raise ValueError(f"at most {MAX_RULES} tag rules can be saved")
    rules = [parse_tag_rule(item) for item in items]
    lookup = _scope_lookup()
    for index, item in enumerate(rules, 1):
        try:
            _check_rule(item["rule"], lookup)
        except ValueError as exc:
            raise ValueError(f"rule {index} (tag {item['tag']!r}): {exc}") from exc
    state.set_section(RULES_SECTION, rules)
    return rules


def describe(rule: Mapping) -> str:
    return scope_store.describe_rule(rule["rule"])


def evaluate_rule(rule: Mapping, objects: Mapping[str, Mapping], lookup: Mapping[str, scope_store.Scope] | None = None) -> list[str]:
    """Normalized keys of the objects ``rule`` (a ``{"tag", "rule"}`` pair) matches; ``ValueError`` if it cannot run."""

    expanded = _check_rule(rule["rule"], _scope_lookup() if lookup is None else lookup)
    return [key for key, record in objects.items() if scope_store.evaluate_rule(expanded, record)]


# ----------------------------------------------------------------- manual tags


def _manual() -> dict[str, list[str]]:
    stored = state.get_section(MANUAL_SECTION, {})
    result: dict[str, list[str]] = {}
    for key, names in (stored.items() if isinstance(stored, dict) else ()):
        if isinstance(key, str) and isinstance(names, list):
            result[key] = [name for name in names if isinstance(name, str)]
    return result


def _canonical(names: Iterable[str]) -> dict[str, str]:
    """Casing already in use for each tag (rules first, then manual), so ``retired`` reuses ``Retired``."""

    seen: dict[str, str] = {}
    for item in list_rules():
        seen.setdefault(item["tag"].casefold(), item["tag"])
    for tags in _manual().values():
        for name in tags:
            seen.setdefault(name.casefold(), name)
    for name in names:
        seen.setdefault(name.casefold(), name)
    return seen


def resolve_keys(keys: Iterable[object], objects: Mapping[str, Mapping] | None = None) -> list[str]:
    """Normalize requested object names, mapping aliases (a short model key) to the full name."""

    objects = collect_objects() if objects is None else objects
    aliases = {alias: key for key, record in objects.items() for alias in record["_aliases"]}
    resolved: list[str] = []
    for key in keys:
        if not isinstance(key, str) or not key.strip() or len(key) > 1024:
            raise ValueError("every object needs a name")
        name = norm(key)
        name = name if name in objects else aliases.get(name, name)
        if name not in resolved:
            resolved.append(name)
    if not resolved:
        raise ValueError("choose at least one object")
    if len(resolved) > MAX_KEYS_PER_REQUEST:
        raise ValueError(f"tag at most {MAX_KEYS_PER_REQUEST} objects at once")
    return resolved


def change_manual(keys: Iterable[object], add: Iterable[object] = (), remove: Iterable[object] = ()) -> dict:
    """Add and remove manual tags on several objects at once; returns the new :func:`snapshot`."""

    added = [parse_tag(name) for name in add]
    removed = {parse_tag(name).casefold() for name in remove}
    if not added and not removed:
        raise ValueError("name a tag to add or remove")
    targets = resolve_keys(keys)
    manual = _manual()
    names = _canonical(added)
    for key in targets:
        current = [name for name in manual.get(key, []) if name.casefold() not in removed]
        for name in added:
            canonical = names[name.casefold()]
            if canonical.casefold() not in {c.casefold() for c in current}:
                current.append(canonical)
        if len(current) > MAX_TAGS_PER_OBJECT:
            raise ValueError(f"an object can have at most {MAX_TAGS_PER_OBJECT} tags")
        if current:
            manual[key] = current
        else:
            manual.pop(key, None)
    state.set_section(MANUAL_SECTION, manual)
    return snapshot()


# -------------------------------------------------------------------- results


def _assign(objects: Mapping[str, Mapping]) -> tuple[dict[str, dict], list[dict]]:
    """Tags per object (``manual`` and ``rules``) and one status row per saved rule."""

    manual = _manual()
    rules = list_rules()
    lookup = _scope_lookup()
    names = _canonical(())
    assigned: dict[str, dict] = {}
    for key, tags in manual.items():
        assigned[key] = {"manual": [names.get(t.casefold(), t) for t in tags], "rules": []}
    status = []
    for index, item in enumerate(rules):
        row = {"index": index, "tag": item["tag"], "description": describe(item), "matched": 0, "error": None}
        try:
            matches = evaluate_rule(item, objects, lookup)
        except ValueError as exc:
            row["error"] = str(exc)
            matches = []
        row["matched"] = len(matches)
        for key in matches:
            slot = assigned.setdefault(key, {"manual": [], "rules": []})
            if item["tag"] not in slot["rules"]:
                slot["rules"].append(item["tag"])
        status.append(row)
    return assigned, status


def snapshot(pipeline: object | None = None) -> dict:
    """Everything the pages need: tags per object, the tags in use, and how each rule did.

    ``objects`` maps ``project.dataset.name`` (lower case) to ``{"manual": [...], "rules": [...]}``;
    ``aliases`` maps other names of the same object (a Dataform model key) to that name.
    """

    objects = collect_objects(pipeline)
    assigned, status = _assign(objects)
    counts: dict[str, dict] = {}
    for slot in assigned.values():
        for name in dict.fromkeys([*slot["manual"], *slot["rules"]]):
            entry = counts.setdefault(name.casefold(), {"tag": name, "count": 0})
            entry["count"] += 1
    return {
        "objects": {key: slot for key, slot in assigned.items() if slot["manual"] or slot["rules"]},
        "aliases": {alias: key for key, record in objects.items() for alias in record["_aliases"]},
        "tags": sorted(counts.values(), key=lambda item: item["tag"].casefold()),
        "rules": status,
        "known_objects": len(objects),
    }


def tag_lookup(pipeline: object | None = None) -> dict[str, list[str]]:
    """Every tag (manual and from rules) by normalized object name and alias, for the scope field ``tag``."""

    objects = collect_objects(pipeline)
    assigned, _ = _assign(objects)
    result: dict[str, list[str]] = {}
    for key, slot in assigned.items():
        tags = list(dict.fromkeys([*slot["manual"], *slot["rules"]]))
        if not tags:
            continue
        result[key] = tags
        for alias in objects.get(key, {}).get("_aliases", ()):
            result[alias] = tags
    return result


def preview_rule(data: object, limit: int = 12) -> dict:
    """How many objects a rule would tag right now, with a few examples; the rule is not saved."""

    item = parse_tag_rule(data)
    objects = collect_objects()
    matches = evaluate_rule(item, objects)
    return {"matched": len(matches), "of": len(objects),
            "examples": [objects[key]["_key"] for key in sorted(matches)[:limit]]}
