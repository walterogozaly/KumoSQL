"""Saved scopes: named, rule-based slices of models and job history.

A scope is a name plus a *rule*. A record (a model, a job-history row, a table
profile) is in the scope when the rule matches it. Rules are trees::

    {"field": "submitter", "op": "in", "value": ["ana@x.com", "bo@x.com"]}   # a condition
    {"all": [rule, ...]}                                                     # AND
    {"any": [rule, ...]}                                                     # OR
    {"not": rule}                                                            # NOT

Groups nest, so "submitter in [...] AND (dataset starts with raw OR NOT
project = sandbox)" is one scope. Conditions can name any field: which fields
exist is discovered from the data (:func:`discover_fields`), not hardcoded.

Operators (text comparison is case-insensitive unless a condition sets
``"case_sensitive": true``): ``eq``, ``ne``, ``in``, ``not_in``, ``prefix``,
``suffix``, ``contains``, ``glob`` (``*`` and ``?``), ``regex`` (a search),
``gt``, ``gte``, ``lt``, ``lte`` (numbers, ISO timestamps, else text),
``is_null`` and ``not_null``. A field holding a list (for example a table's
columns) matches when any element does. A dotted field such as ``labels.team``
reads inside a nested object.

A rule that names a field the data never has is an error
(:class:`UnknownFieldError`), never a silent "matches nothing"; a record that
merely lacks a known field counts as null for it.

Scopes saved by earlier versions (``{"name", "fields": {field: [values]}}``)
are migrated to rules on load: one condition per field, joined with AND, and a
trailing ``*`` in a value becomes a ``glob`` condition.
"""

from __future__ import annotations

import difflib
import fnmatch
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, Mapping

from . import state

SECTION = "scopes"
MAX_SCOPES = 200
MAX_VALUES = 5000
MAX_NODES = 500
MAX_DEPTH = 8
MAX_PATTERN = 500

OPERATORS = {
    "eq": "equals",
    "ne": "does not equal",
    "in": "is one of",
    "not_in": "is not one of",
    "prefix": "starts with",
    "suffix": "ends with",
    "contains": "contains",
    "glob": "matches pattern",
    "regex": "matches regex",
    "gt": "is greater than",
    "gte": "is at least",
    "lt": "is less than",
    "lte": "is at most",
    "is_null": "is empty",
    "not_null": "is not empty",
}
_ALIASES = {
    "==": "eq", "=": "eq", "!=": "ne", "<>": "ne", ">": "gt", ">=": "gte", "<": "lt", "<=": "lte",
    "starts_with": "prefix", "ends_with": "suffix", "like": "glob", "matches": "regex",
}
_LIST_OPS = {"in", "not_in"}
_NO_VALUE_OPS = {"is_null", "not_null"}
_NEGATED = {"ne", "not_in"}

#: Fields of a pipeline model, and the extra fields a table profile adds.
MODEL_FIELDS = ("project", "dataset", "table", "name", "model", "kind", "path", "depends_on")
PROFILE_FIELDS = ("grain_status", "grain_keys", "columns", "column_count", "profile_complete", "row_filters")
#: Fields every job-history record carries, beside its source-specific ones.
JOB_FIELDS = ("job_id", "creation_time", "destination", "referenced_tables")


class UnknownFieldError(ValueError):
    """A scope rule names a field the data does not have."""


# ---------------------------------------------------------------- rule trees


def parse_rule(data: object, *, _depth: int = 0, _count: list[int] | None = None) -> dict:
    """Validate a JSON-shaped rule and return it normalized; ``ValueError`` when malformed."""

    count = _count if _count is not None else [0]
    count[0] += 1
    if count[0] > MAX_NODES:
        raise ValueError(f"a rule can have at most {MAX_NODES} conditions and groups")
    if _depth > MAX_DEPTH:
        raise ValueError(f"rule groups can nest at most {MAX_DEPTH} levels deep")
    if not isinstance(data, Mapping):
        raise ValueError("a rule must be an object")
    keys = set(data) & {"all", "any", "not", "field"}
    if len(keys) != 1:
        raise ValueError('a rule needs exactly one of "field", "all", "any" or "not"')
    (kind,) = keys
    if kind in ("all", "any"):
        children = data[kind]
        if not isinstance(children, list) or not children:
            raise ValueError(f'"{kind}" needs a non-empty list of rules')
        return {kind: [parse_rule(child, _depth=_depth + 1, _count=count) for child in children]}
    if kind == "not":
        return {"not": parse_rule(data["not"], _depth=_depth + 1, _count=count)}
    return _parse_condition(data)


def _parse_condition(data: Mapping) -> dict:
    name = data.get("field")
    if not isinstance(name, str) or not name.strip():
        raise ValueError("a condition needs a field name")
    name = name.strip()
    op = data.get("op", "eq")
    op = _ALIASES.get(op, op) if isinstance(op, str) else op
    if op not in OPERATORS:
        raise ValueError(f"unknown operator {data.get('op')!r} for {name!r}; use one of {', '.join(OPERATORS)}")
    result: dict = {"field": name, "op": op}
    if op in _NO_VALUE_OPS:
        if data.get("value") not in (None, [], ""):
            raise ValueError(f"{op} on {name!r} takes no value")
    elif op in _LIST_OPS:
        values = data.get("value")
        if isinstance(values, (str, int, float)) and not isinstance(values, bool):
            values = [values]
        if not isinstance(values, list) or not values or len(values) > MAX_VALUES:
            raise ValueError(f"{op} on {name!r} needs a list of values")
        if any(isinstance(v, (list, dict)) or v is None for v in values):
            raise ValueError(f"values for {name!r} must be text, numbers or booleans")
        cleaned = list(dict.fromkeys(v.strip() if isinstance(v, str) else v for v in values))
        cleaned = [v for v in cleaned if v != ""]
        if not cleaned:
            raise ValueError(f"{op} on {name!r} needs at least one value")
        result["value"] = cleaned
    else:
        value = data.get("value")
        if isinstance(value, str):
            value = value.strip()
        if value is None or value == "" or isinstance(value, (list, dict)):
            raise ValueError(f"{op} on {name!r} needs a value")
        result["value"] = value
        if op == "regex":
            if len(str(value)) > MAX_PATTERN:
                raise ValueError(f"regex for {name!r} is longer than {MAX_PATTERN} characters")
            try:
                re.compile(str(value))
            except re.error as exc:
                raise ValueError(f"invalid regex for {name!r}: {exc}") from exc
    if data.get("case_sensitive"):
        result["case_sensitive"] = True
    return result


def rule_fields(rule: Mapping) -> list[str]:
    """Every field a rule names, in first-use order."""

    found: list[str] = []

    def walk(node: Mapping) -> None:
        if "field" in node:
            if node["field"] not in found:
                found.append(node["field"])
        elif "not" in node:
            walk(node["not"])
        else:
            for child in node.get("all") or node.get("any") or ():
                walk(child)

    walk(rule)
    return found


def describe_rule(rule: Mapping, *, _top: bool = True) -> str:
    """A one-line reading of a rule, e.g. ``submitter is one of [a, b] AND NOT project = x``."""

    if "field" in rule:
        op = rule["op"]
        label = OPERATORS[op]
        if op in _NO_VALUE_OPS:
            return f"{rule['field']} {label}"
        value = rule["value"]
        shown = "[" + ", ".join(map(str, value)) + "]" if isinstance(value, list) else str(value)
        return f"{rule['field']} {label} {shown}"
    if "not" in rule:
        return "NOT " + describe_rule(rule["not"], _top=False)
    word, children = ("AND", rule["all"]) if "all" in rule else ("OR", rule["any"])
    text = f" {word} ".join(describe_rule(child, _top=False) for child in children)
    return text if _top or len(children) == 1 else f"({text})"


def _texts(value: object, case_sensitive: bool) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set, frozenset)):
        items = [v for v in value if v is not None]
    else:
        items = [value]
    out = []
    for item in items:
        text = ("true" if item else "false") if isinstance(item, bool) else str(item)
        out.append(text if case_sensitive else text.casefold())
    return out


def _lookup(record: Mapping[str, object], name: str) -> object:
    if name in record:
        return record[name]
    folded = name.casefold()
    for key, value in record.items():
        if isinstance(key, str) and key.casefold() == folded:
            return value
    head, _, rest = name.partition(".")
    if rest and head != name:
        inner = _lookup(record, head)
        if isinstance(inner, Mapping):
            return _lookup(inner, rest)
    return None


def _number(text: str) -> float | None:
    try:
        return float(text)
    except ValueError:
        return None


def _time(text: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=None) if parsed.tzinfo else parsed


def _ordered(actual: str, wanted: str, op: str) -> bool:
    for convert in (_number, _time):
        a, b = convert(actual), convert(wanted)
        if a is not None and b is not None:
            break
    else:
        a, b = actual, wanted
    return {"gt": a > b, "gte": a >= b, "lt": a < b, "lte": a <= b}[op]  # type: ignore[operator]


def _condition_matches(node: Mapping, record: Mapping[str, object]) -> bool:
    op = node["op"]
    actual = _lookup(record, node["field"])
    if op in _NO_VALUE_OPS:
        empty = actual is None or actual == "" or (isinstance(actual, (list, tuple, set)) and not actual)
        return empty if op == "is_null" else not empty
    cs = bool(node.get("case_sensitive"))
    texts = _texts(actual, cs)
    if op in _NEGATED:
        positive = {"ne": "eq", "not_in": "in"}[op]
        return not _condition_matches({**node, "op": positive}, record)
    if not texts:
        return False
    wanted = _texts(node["value"], cs)
    if op == "eq":
        return any(t == wanted[0] for t in texts)
    if op == "in":
        return any(t in set(wanted) for t in texts)
    pattern = wanted[0]
    if op == "prefix":
        return any(t.startswith(pattern) for t in texts)
    if op == "suffix":
        return any(t.endswith(pattern) for t in texts)
    if op == "contains":
        return any(pattern in t for t in texts)
    if op == "glob":
        return any(fnmatch.fnmatchcase(t, pattern) for t in texts)
    if op == "regex":
        compiled = re.compile(str(node["value"]), 0 if cs else re.IGNORECASE)
        return any(compiled.search(t) for t in _texts(actual, True))
    return any(_ordered(t, pattern, op) for t in texts)


def evaluate_rule(rule: Mapping, record: Mapping[str, object]) -> bool:
    if "field" in rule:
        return _condition_matches(rule, record)
    if "not" in rule:
        return not evaluate_rule(rule["not"], record)
    if "all" in rule:
        return all(evaluate_rule(child, record) for child in rule["all"])
    return any(evaluate_rule(child, record) for child in rule["any"])


def _condition(name: str, op: str, value: object = None) -> dict:
    return {"field": name, "op": op, **({} if value is None else {"value": value})}


def rule_from_fields(fields: Mapping[str, Iterable[str]]) -> dict:
    """Migrate the older ``{field: [values]}`` shape to a rule."""

    if not isinstance(fields, Mapping) or not fields:
        raise ValueError("a scope needs at least one field")
    nodes = []
    for name, values in fields.items():
        if not isinstance(name, str) or not name.strip():
            raise ValueError("field names must be non-empty text")
        if not isinstance(values, (list, tuple)) or len(values) > MAX_VALUES or any(
            not isinstance(v, str) for v in values
        ):
            raise ValueError(f"values for {name!r} must be a list of text")
        cleaned = list(dict.fromkeys(v.strip() for v in values if v.strip()))
        if not cleaned:
            raise ValueError(f"field {name!r} needs at least one value")
        exact = [v for v in cleaned if not v.endswith("*")]
        patterns = [v for v in cleaned if v.endswith("*")]
        parts = ([_condition(name.strip(), "in", exact)] if exact else []) + [
            _condition(name.strip(), "prefix", p[:-1]) if p.count("*") == 1 else _condition(name.strip(), "glob", p)
            for p in patterns
        ]
        nodes.append(parts[0] if len(parts) == 1 else {"any": parts})
    return parse_rule(nodes[0] if len(nodes) == 1 else {"all": nodes})


# -------------------------------------------------------------------- scopes


class Scope:
    """A named rule. ``Scope(name, fields)`` still builds one from ``{field: values}``."""

    __slots__ = ("name", "rule")

    def __init__(self, name: str, fields: Mapping[str, Iterable[str]] | None = None, *, rule: Mapping | None = None):
        if (fields is None) == (rule is None):
            raise ValueError("give a scope either a rule or fields")
        object.__setattr__(self, "name", name)
        object.__setattr__(
            self, "rule", parse_rule(rule) if rule is not None else rule_from_fields(fields)  # type: ignore[arg-type]
        )

    def __setattr__(self, key: str, value: object) -> None:
        raise AttributeError("scopes are immutable")

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Scope) and (self.name, self.rule) == (other.name, other.rule)

    def __hash__(self) -> int:
        return hash(self.name)

    def __repr__(self) -> str:
        return f"Scope({self.name!r}, rule={self.rule!r})"

    @property
    def fields(self) -> dict[str, tuple[str, ...]]:
        """The plain ``field in values`` conditions of the rule (older API); richer rules show only those."""

        nodes = self.rule["all"] if "all" in self.rule else [self.rule]
        found: dict[str, tuple[str, ...]] = {}
        for node in nodes:
            if node.get("op") == "in":
                found[node["field"]] = tuple(str(v) for v in node["value"])
        return found

    def fields_used(self) -> list[str]:
        return rule_fields(self.rule)

    def describe(self) -> str:
        return describe_rule(self.rule)

    def matches(self, record: Mapping[str, object]) -> bool:
        """Whether ``record`` satisfies the rule. A missing field is null for that record."""

        return evaluate_rule(self.rule, record)

    def filter(self, records: Iterable[Mapping[str, object]]) -> list[Mapping[str, object]]:
        return [record for record in records if self.matches(record)]

    def unknown_fields(self, available: Iterable[str]) -> list[str]:
        """Rule fields absent from ``available`` (matched ignoring case; ``a.b`` needs ``a``)."""

        known = {name.casefold() for name in available}
        return [
            name for name in self.fields_used()
            if name.casefold() not in known and name.split(".")[0].casefold() not in known
        ]

    def require_fields(self, available: Iterable[str], subject: str) -> None:
        """Raise :class:`UnknownFieldError` naming every rule field ``subject`` lacks."""

        available = sorted(set(available), key=str.casefold)
        missing = self.unknown_fields(available)
        if not missing:
            return
        hints = []
        for name in missing:
            close = difflib.get_close_matches(name.casefold(), [a.casefold() for a in available], n=1)
            if close:
                hints.append(f"{name!r} (did you mean {next(a for a in available if a.casefold() == close[0])!r}?)")
            else:
                hints.append(repr(name))
        raise UnknownFieldError(
            f"scope {self.name!r} uses {'a field' if len(missing) == 1 else 'fields'} that {subject} "
            f"{'does' if len(missing) == 1 else 'do'} not have: {', '.join(hints)}. "
            f"Available: {', '.join(available) if available else 'none'}"
        )

    def to_json(self) -> dict:
        return {"name": self.name, "rule": self.rule}


def parse_scope(data: object) -> Scope:
    """Validate a JSON-shaped scope (a ``rule``, or the older ``fields``); ``ValueError`` when malformed."""

    if not isinstance(data, dict):
        raise ValueError("a scope must be an object")
    name = data.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ValueError("a scope needs a name")
    if ("rule" in data) == ("fields" in data):
        raise ValueError('a scope needs exactly one of "rule" or "fields"')
    if "rule" in data:
        return Scope(name.strip(), rule=data["rule"])
    return Scope(name.strip(), rule=rule_from_fields(data["fields"]))


# ------------------------------------------------------------ field discovery


@dataclass(frozen=True)
class FieldInfo:
    name: str
    #: where the field was found: ``model``, ``profile`` or ``job``
    source: str
    #: ``text``, ``number``, ``time``, ``bool`` or ``list``
    kind: str = "text"
    examples: tuple[str, ...] = ()

    def to_json(self) -> dict:
        return {"name": self.name, "source": self.source, "kind": self.kind, "examples": list(self.examples)}


def _kind(value: object) -> str:
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, (list, tuple, set)):
        return "list"
    if isinstance(value, str) and re.match(r"\d{4}-\d\d-\d\d[T ]", value):
        return "time"
    if isinstance(value, datetime):
        return "time"
    return "text"


def profile_record(profile: object) -> dict[str, object]:
    """The scope fields of a :class:`~kumosql.table_profile.TableProfile`."""

    grain = profile.grain  # type: ignore[attr-defined]
    columns = [a.column for a in profile.attributes]  # type: ignore[attr-defined]
    return {
        "grain_status": grain.status,
        "grain_keys": list(grain.keys),
        "columns": columns,
        "column_count": len(columns),
        "profile_complete": bool(profile.complete),  # type: ignore[attr-defined]
        "row_filters": list(profile.row_scope.filters),  # type: ignore[attr-defined]
    }


def job_record(record: object) -> dict[str, object]:
    """The flat scope record of one job-history row (a mapping or an ``ObservedRead``)."""

    if isinstance(record, Mapping):
        flat = {k: v for k, v in record.items() if k != "attributes"}
        if isinstance(record.get("attributes"), Mapping):
            flat.update(record["attributes"])
        return flat
    scope_record = getattr(record, "scope_record", None)
    return dict(scope_record()) if callable(scope_record) else {}


def discover_fields(
    pipeline: object | None = None,
    observed_reads: Iterable[object] = (),
    profiles: Mapping[str, object] | None = None,
    *,
    max_examples: int = 5,
    max_rows: int = 5000,
) -> list[FieldInfo]:
    """Fields a rule can use, found in the loaded models, table profiles and job history.

    Nothing is hardcoded beyond the model and job shapes: a source-specific job
    column (a submitter, a label, a cost) appears the moment a record carries it.
    """

    seen: dict[tuple[str, str], tuple[str, list[str]]] = {}

    def note(source: str, record: Mapping[str, object]) -> None:
        for key, value in record.items():
            if not isinstance(key, str):
                continue
            if value is None:
                seen.setdefault((source, key), ("text", []))
                continue
            kind, examples = seen.setdefault((source, key), (_kind(value), []))
            if isinstance(value, Mapping):
                for inner, inner_value in value.items():
                    note(source, {f"{key}.{inner}": inner_value})
                continue
            for item in value if isinstance(value, (list, tuple, set)) else [value]:
                text = str(item)
                if text and len(examples) < max_examples and text not in examples and len(text) <= 80:
                    examples.append(text)

    if pipeline is not None:
        for key in pipeline.models:  # type: ignore[attr-defined]
            note("model", pipeline.model_record(key))  # type: ignore[attr-defined]
        for key, profile in (profiles or {}).items():
            note("profile", profile_record(profile))
    for index, row in enumerate(observed_reads):
        if index >= max_rows:
            break
        note("job", job_record(row))
    if pipeline is not None and not profiles:
        for name in PROFILE_FIELDS:
            seen.setdefault(("profile", name), ("list" if name in ("grain_keys", "columns", "row_filters") else "text", []))
    order = {"model": 0, "profile": 1, "job": 2}
    return [
        FieldInfo(name, source, kind, tuple(examples))
        for (source, name), (kind, examples) in sorted(seen.items(), key=lambda kv: (order[kv[0][0]], kv[0][1]))
    ]


@dataclass(frozen=True)
class ScopePlan:
    """Where a scope can be applied: models, job history, or both."""

    scope: Scope
    models: Scope | None
    jobs: Scope | None
    note: str | None = None

    def to_json(self) -> dict:
        applied = [name for name, part in (("models", self.models), ("job history", self.jobs)) if part is not None]
        return {"name": self.scope.name, "rule": self.scope.describe(), "applied_to": applied, "note": self.note}


def plan_scope(scope: Scope, observed_reads: Iterable[object] = ()) -> ScopePlan:
    """Decide which data a saved scope applies to, for pages that show models and job history together.

    A rule on model fields limits the models, a rule on job fields limits the
    job history, and each is reported in ``note`` when it cannot apply to the
    other. A rule no data source can evaluate raises :class:`UnknownFieldError`.
    """

    reads = list(observed_reads)
    job_fields: set[str] = set()
    for row in reads:
        job_fields.update(job_record(row))
    model_fields = (*MODEL_FIELDS, *PROFILE_FIELDS)
    model_missing = scope.unknown_fields(model_fields)
    job_missing = scope.unknown_fields(job_fields)
    models_ok = not model_missing
    jobs_ok = bool(reads) and not job_missing
    if not models_ok and not jobs_ok:
        if not reads:
            scope.require_fields(model_fields, "pipeline models (no job history is loaded)")
        raise UnknownFieldError(
            f"scope {scope.name!r} cannot be applied: pipeline models have no "
            f"{', '.join(map(repr, model_missing))}, and job history has no {', '.join(map(repr, job_missing))}. "
            f"Model fields: {', '.join(model_fields)}. Job fields: {', '.join(sorted(job_fields, key=str.casefold)) or 'none loaded'}"
        )
    note = None
    if models_ok and not jobs_ok and reads:
        note = f"Job history was not filtered: it has no {', '.join(map(repr, job_missing))}."
    elif jobs_ok and not models_ok:
        note = f"Applied to job history only: models have no {', '.join(map(repr, model_missing))}."
    return ScopePlan(scope, scope if models_ok else None, scope if jobs_ok else None, note)


# ------------------------------------------------------------------- storage


def list_scopes() -> list[Scope]:
    """All saved scopes, in the order they were saved. Older ones are migrated to rules."""

    stored = state.get_section(SECTION, [])
    scopes = []
    migrated = False
    for item in stored if isinstance(stored, list) else []:
        try:
            scopes.append(parse_scope(item))
        except ValueError:
            continue
        migrated = migrated or "rule" not in item
    if migrated:
        try:
            state.set_section(SECTION, [scope.to_json() for scope in scopes])
        except OSError:
            pass  # read-only state: the in-memory migration still applies
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
