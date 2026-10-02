"""Loading Dataform/SQLX projects and compiled graphs into a Pipeline."""

from __future__ import annotations

from collections import defaultdict
import json
from pathlib import Path
import re
from typing import TYPE_CHECKING, Callable, Iterable, Mapping

from sqlglot import exp

from .identity import NodeIdentity
from .pipeline_types import Model, PipelineDiagnostic, Target
from .resilience import (
    PipelineLoadError,
    describe_os_error,
    extended_path,
    find_assets,
    parse_json_or_raise,
    read_text_or_reason,
)
from .sqlx import (
    _SQLX_BLOCK_RE as _SQLX_CONFIG_RE,
    mask_sqlx_interpolations as _mask_sqlx_interpolations,
    split_sqlx_sections as _split_sqlx_sections,
)

if TYPE_CHECKING:
    from .pipeline import Pipeline


# --------------------------------------------------------------------- loading


def _reference_label(reference: object) -> str:
    if isinstance(reference, str):
        return reference.strip()
    if isinstance(reference, NodeIdentity):
        return reference.key
    if isinstance(reference, Mapping):
        nested = reference.get("tableReference")
        if isinstance(nested, Mapping):
            reference = nested
        parts = [
            str(reference.get(key, ""))
            for key in ("projectId", "datasetId", "tableId")
            if reference.get(key)
        ]
        if not parts:
            parts = [
                str(reference.get(key, ""))
                for key in ("database", "schema", "name")
                if reference.get(key)
            ]
        return ".".join(parts) if parts else str(reference)
    if all(hasattr(reference, key) for key in ("database", "schema", "name")):
        return ".".join(
            str(getattr(reference, key))
            for key in ("database", "schema", "name")
            if getattr(reference, key)
        )
    if getattr(reference, "parts", None) is not None:
        return ".".join(part.name for part in reference.parts)
    return str(reference)


def _is_asset_reference(reference: object) -> bool:
    if isinstance(reference, NodeIdentity):
        return reference.kind == "asset"
    if not isinstance(reference, str):
        return False
    normalized = reference.replace("\\", "/")
    return "/" in normalized or normalized.lower().endswith((".sql", ".sqlx"))


_REF_RE = re.compile(r"\$\{\s*(?:ctx\.)?ref\(\s*(?P<args>[^()]*?)\s*\)\s*\}")
_RESOLVE_RE = re.compile(r"\$\{\s*(?:ctx\.)?resolve\(\s*(?P<args>[^()]*?)\s*\)\s*\}")
_SELF_RE = re.compile(r"\$\{\s*(?:ctx\.)?self\(\s*\)\s*\}")
_CONFIG_DEPENDENCIES_RE = re.compile(r"\bdependencies\s*:\s*(?:\[(?P<list>[^\]]*)\]|(?P<one>\{[^}]*\}|['\"`][^'\"`]*['\"`]))")
_DEPENDENCY_ITEM_RE = re.compile(r"\{[^}]*\}|['\"`][^'\"`]*['\"`]")
_STRING_RE = re.compile(r"""(['"`])((?:\\.|(?!\1).)*)\1""")


def _top_level(config: str) -> str:
    """The text directly inside a config's outer braces, with nested braces, brackets and strings blanked.

    A ``name:`` inside ``columns: {...}`` or a ``dependencies`` object must not be taken
    for the action's own ``name``.
    """

    start = config.find("{")
    if start < 0:
        return config
    out: list[str] = []
    depth = 0
    quote: str | None = None
    index = start
    while index < len(config):
        char = config[index]
        if quote:
            if char == "\\" and index + 1 < len(config):
                out.append(config[index : index + 2] if depth <= 1 else "  ")
                index += 2
                continue
            if char == quote:
                quote = None
            out.append(char if depth <= 1 else " ")
        elif char in "'\"`":
            quote = char
            out.append(char if depth <= 1 else " ")
        elif char in "{[(":
            out.append(char if depth == 0 else " ")
            depth += 1
        elif char in "}])":
            depth -= 1
            out.append(char if depth == 0 else " ")
        else:
            out.append(char if depth <= 1 else " ")
        index += 1
    return "".join(out)


def _config_value(config: str, key: str) -> str | None:
    match = re.search(rf"\b{key}\s*:\s*(['\"`])((?:\\.|(?!\1).)*)\1", _top_level(config))
    return match.group(2) if match else None


def _config_tags(config: str) -> tuple[str, ...]:
    """Tags from a Dataform config block: ``tags: ["a", "b"]`` or ``tags: "a"``."""

    match = re.search(r"\btags\s*:\s*(?:\[(?P<list>[^\]]*)\]|(?P<one>['\"`][^'\"`]*['\"`]))", config)
    if not match:
        return ()
    # ``tags: []`` matches the list branch with an empty string; ``or`` would fall through to the unmatched ``one`` (None).
    listed, single = match.group("list"), match.group("one")
    return tuple(dict.fromkeys(re.findall(r"['\"`]([^'\"`]+)['\"`]", listed if listed is not None else single or "")))


def _braced(text: str, start: int) -> str:
    """The text of the ``{...}`` block opening at ``start`` (strings skipped); empty if unbalanced."""

    depth, index, quote = 0, start, ""
    while index < len(text):
        char = text[index]
        if quote:
            if char == "\\":
                index += 1
            elif char == quote:
                quote = ""
        elif char in "'\"`":
            quote = char
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
        index += 1
    return ""


def _name_list(text: str) -> tuple[str, ...]:
    return tuple(re.findall(r"['\"`]([^'\"`]+)['\"`]", text))


def _config_assertions(config: str) -> tuple[tuple[str, ...], tuple[tuple[str, ...], ...]]:
    """``(non_null columns, unique keys)`` from ``assertions: {nonNull, uniqueKey, uniqueKeys}``."""

    match = re.search(r"\bassertions\s*:\s*\{", config)
    if not match:
        return (), ()
    block = _braced(config, match.end() - 1)
    non_null: tuple[str, ...] = ()
    keys: list[tuple[str, ...]] = []
    found = re.search(r"\bnonNull\s*:\s*(\[[^\]]*\]|['\"`][^'\"`]*['\"`])", block)
    if found:
        non_null = _name_list(found.group(1))
    found = re.search(r"\buniqueKey\s*:\s*(\[[^\]]*\]|['\"`][^'\"`]*['\"`])", block)
    if found and _name_list(found.group(1)):
        keys.append(_name_list(found.group(1)))
    found = re.search(r"\buniqueKeys\s*:\s*\[((?:\s*\[[^\]]*\]\s*,?)+)\]", block)
    if found:
        keys.extend(_name_list(group) for group in re.findall(r"\[([^\]]*)\]", found.group(1)) if _name_list(group))
    return non_null, tuple(dict.fromkeys(keys))


def _config_dependencies(config: str) -> tuple[str, ...]:
    """Arguments of each entry in a config block's ``dependencies`` (a name or a ``{name, schema}`` object)."""

    match = _CONFIG_DEPENDENCIES_RE.search(config)
    if not match:
        return ()
    listed, single = match.group("list"), match.group("one")
    return tuple(_DEPENDENCY_ITEM_RE.findall(listed if listed is not None else single or ""))


def _read_project_defaults(
    root: Path, diagnostics: list[PipelineDiagnostic] | None = None
) -> tuple[str, str]:
    """Return (default project, default dataset) from Dataform settings, if any.

    An unreadable or malformed settings file yields empty defaults and a
    ``settings_unreadable`` diagnostic when ``diagnostics`` is given.
    """

    try:
        return _read_project_defaults_strict(root)
    except (OSError, UnicodeError, ValueError, AttributeError) as exc:
        if diagnostics is None:
            raise
        reason = (
            describe_os_error(exc)
            if isinstance(exc, (OSError, UnicodeError))
            else "settings file is not valid JSON"
        )
        diagnostics.append(
            PipelineDiagnostic("", "settings_unreadable", f"{reason}; project defaults were not applied")
        )
        return "", ""


def _assertion_dataset(root: Path) -> str:
    """Where assertions without their own schema live: the project's assertion dataset, else Dataform's default."""

    try:
        settings = root / "workflow_settings.yaml"
        if settings.is_file():
            match = re.search(r"(?m)^\s*defaultAssertionDataset\s*:\s*['\"]?([^'\"\s#]+)", settings.read_text(encoding="utf-8-sig"))
            return match.group(1) if match else "dataform_assertions"
        legacy = root / "dataform.json"
        if legacy.is_file():
            value = json.loads(legacy.read_text(encoding="utf-8-sig")).get("assertionSchema")
            return value if isinstance(value, str) and value else "dataform_assertions"
    except (OSError, UnicodeError, ValueError, AttributeError):
        pass
    return "dataform_assertions"


def _read_project_defaults_strict(root: Path) -> tuple[str, str]:
    settings = root / "workflow_settings.yaml"
    if settings.is_file():
        text = settings.read_text(encoding="utf-8-sig")

        def value(key: str) -> str:
            match = re.search(rf"(?m)^\s*{key}\s*:\s*['\"]?([^'\"\s#]+)", text)
            return match.group(1) if match else ""

        return value("defaultProject"), value("defaultDataset")
    legacy = root / "dataform.json"
    if legacy.is_file():
        data = json.loads(legacy.read_text(encoding="utf-8-sig"))
        return data.get("defaultDatabase", ""), data.get("defaultSchema", "")
    return "", ""


# ------------------------------------------------------- JavaScript declarations

_JS_CALL_RE = re.compile(r"(?<![\w.$])(declare|publish)\s*\(")
_JS_IDENT_RE = re.compile(r"^[A-Za-z_$][\w$]*$")


def _call_arguments(text: str, opening: int) -> str:
    """The text between the parenthesis at ``opening`` and its match (strings skipped); empty if unbalanced."""

    depth, quote, index = 0, "", opening
    while index < len(text):
        char = text[index]
        if quote:
            if char == "\\":
                index += 1
            elif char == quote:
                quote = ""
        elif char in "'\"`":
            quote = char
        elif char in "([{":
            depth += 1
        elif char in ")]}":
            depth -= 1
            if depth == 0:
                return text[opening + 1:index]
        index += 1
    return ""


def _split_top_level(text: str) -> list[str]:
    """``text`` split on commas that are not inside brackets or strings."""

    parts, depth, quote, start, index = [], 0, "", 0, 0
    while index < len(text):
        char = text[index]
        if quote:
            if char == "\\":
                index += 1
            elif char == quote:
                quote = ""
        elif char in "'\"`":
            quote = char
        elif char in "([{":
            depth += 1
        elif char in ")]}":
            depth -= 1
        elif char == "," and depth == 0:
            parts.append(text[start:index])
            start = index + 1
        index += 1
    parts.append(text[start:])
    return [part.strip() for part in parts if part.strip()]


def _js_string(expression: str) -> str | None:
    match = re.fullmatch(r"""\s*(['"`])((?:\\.|(?!\1)[^$])*)\1\s*""", expression)
    return match.group(2) if match and "${" not in match.group(2) else None


def _js_bindings(text: str, name: str) -> list[str] | None:
    """The literal strings the identifier ``name`` can hold in this file, or None when it cannot be known."""

    found: list[list[str]] = []
    for match in re.finditer(rf"\bconst\s+{re.escape(name)}\s*=\s*(?P<value>['\"`][^'\"`$]*['\"`])\s*[;\n]", text):
        found.append([match.group("value")[1:-1]])
    loops = (
        rf"\[(?P<a>[^\[\]]*)\]\s*\.forEach\(\s*\(?\s*{re.escape(name)}\b",
        rf"\bfor\s*\(\s*(?:const|let|var)\s+{re.escape(name)}\s+of\s*\[(?P<a>[^\[\]]*)\]",
    )
    for pattern in loops:
        for match in re.finditer(pattern, text):
            items = _split_top_level(match.group("a"))
            values = [_js_string(item) for item in items]
            if not items or any(value is None for value in values):
                return None
            found.append([value for value in values if value is not None])
    # Any other binding of the same identifier (a parameter, a computed value) makes it unknowable.
    others = len(re.findall(rf"\b(?:const|let|var)\s+{re.escape(name)}\b", text))
    others += len(re.findall(rf"[(,]\s*{re.escape(name)}\s*(?:,[^)]*)?\)\s*=>", text))
    others += len(re.findall(rf"\bfunction\s*\w*\s*\([^)]*\b{re.escape(name)}\b", text))
    if len(found) != 1 or others > 1:
        return None
    return found[0]


def _js_field(config: str, text: str, key: str) -> list[str] | None | str:
    """Possible values of ``key`` in a config object: a list, ``""`` when absent, None when not knowable."""

    inner = config.strip()
    inner = inner[1:-1] if inner.startswith("{") and inner.endswith("}") else inner
    for entry in _split_top_level(inner):
        pair = re.match(r"""^(?:['"`]?)([\w$]+)(?:['"`]?)\s*(?::\s*(.*))?$""", entry, re.S)
        if not pair or pair.group(1) != key:
            continue
        value = pair.group(2)
        if value is None:  # shorthand ``{ name }``
            return _js_bindings(text, key)
        literal = _js_string(value)
        if literal is not None:
            return [literal]
        if _JS_IDENT_RE.match(value.strip()):
            return _js_bindings(text, value.strip())
        return None
    return ""


def js_declared_targets(text: str, default: Target) -> tuple[list[Target], list[Target], bool]:
    """Declarations and named actions found in one Dataform JavaScript file.

    Returns ``(declared, actions, complete)``. ``declared`` are ``declare({...})`` targets and ``actions`` the
    ``publish("name", {...})`` ones, both with project defaults filled in. ``complete`` is False when a call's
    name, schema or database cannot be read without running the code (computed in a function, a loop over a
    list that is not literal): the file then may declare tables that are not listed.
    """

    declared: list[Target] = []
    actions: list[Target] = []
    complete = True
    for match in _JS_CALL_RE.finditer(text):
        kind = match.group(1)
        args = _split_top_level(_call_arguments(text, match.end() - 1))
        if kind == "declare":
            config = args[0] if args else ""
            names = _js_field(config, text, "name") if config.startswith("{") else None
        else:
            if not args:
                continue
            config = args[1] if len(args) > 1 and args[1].startswith("{") else "{}"
            first = _js_string(args[0])
            names = [first] if first is not None else (_js_bindings(text, args[0]) if _JS_IDENT_RE.match(args[0]) else None)
        schemas = _js_field(config, text, "schema") if config.startswith("{") else ""
        databases = _js_field(config, text, "database") if config.startswith("{") else ""
        if not names or schemas is None or databases is None:
            complete = False
            continue
        for name in names:
            for database in (databases or [default.database]):
                for schema in (schemas or [default.schema]):
                    (declared if kind == "declare" else actions).append(Target(database, schema, name))
    return declared, actions, complete


def _parse_ref_args(
    args: str,
    default: Target,
    known: dict[str, list[Target]] | None = None,
    *,
    names_may_be_missing: bool = False,
) -> Target:
    """The target a ``ref()`` names.

    Dataform finds ``ref("name")`` by the action's name, wherever its config put
    it, so a name that ``known`` (every action and declaration by name) holds
    once resolves to that target; otherwise the project defaults apply. A name that
    several targets hold, or one that no action names while ``names_may_be_missing``
    (JavaScript declarations that could not be read), is not guessed: it raises.
    """

    def unresolved(name: str, schema: str | None = None) -> None:
        matches = [t for t in (known or {}).get(name, []) if schema is None or t.schema == schema]
        if len(matches) > 1:
            raise ValueError("ref() names several tables; it was left unresolved")
        if not matches and names_may_be_missing:
            raise ValueError("ref() names a table that a declaration may define; it was left unresolved")

    def by_name(name: str, schema: str | None = None) -> Target | None:
        matches = [
            target for target in (known or {}).get(name, [])
            if schema is None or target.schema == schema
        ]
        return matches[0] if len(matches) == 1 else None

    if args.lstrip().startswith("{"):
        name = _config_value(args, "name") or ""
        schema = _config_value(args, "schema")
        database = _config_value(args, "database")
        if not database:
            found = by_name(name, schema)
            if found is not None:
                return found
            unresolved(name, schema)
        return Target(database or default.database, schema or default.schema, name)
    parts = [match.group(2) for match in _STRING_RE.finditer(args)]
    if len(parts) == 1:
        found = by_name(parts[0])
        if found is None:
            unresolved(parts[0])
        return found or Target(default.database, default.schema, parts[0])
    if len(parts) == 2:
        found = by_name(parts[1], parts[0])
        if found is None:
            unresolved(parts[1], parts[0])
        return found or Target(default.database, parts[0], parts[1])
    if len(parts) >= 3:
        return Target(parts[0], parts[1], parts[2])
    raise ValueError("unsupported ref() arguments")


def load_sqlx_project(
    root: str | Path,
    *,
    source_schema: dict[str, dict[str, str]] | None = None,
    compiled_targets: Callable[[], Iterable[tuple[tuple[str, str, str], bool]]] | None = None,
) -> Pipeline:
    """Load a Dataform project (``definitions/**.sqlx``) or a folder of ``.sql`` files.

    ``${ref(...)}`` and ``${self()}`` are resolved to table names using the
    project defaults; other interpolations are masked so the SQL still
    parses. For exact compiled SQL, prefer :func:`load_compiled_graph` with
    the output of ``dataform compile --json``.
    """

    root = extended_path(root)
    if not root.is_dir():
        raise PipelineLoadError("project folder was not found or is not a directory")
    diagnostics: list[PipelineDiagnostic] = []
    database, dataset = _read_project_defaults(root, diagnostics)
    assertion_dataset = _assertion_dataset(root)
    search_root = root / "definitions" if (root / "definitions").is_dir() else root
    models: dict[str, Model] = {}
    sources: dict[str, Target] = {}

    def unlistable(directory: Path, reason: str) -> None:
        try:
            label = str(directory.relative_to(root))
        except ValueError:
            label = "."
        diagnostics.append(
            PipelineDiagnostic(label, "unreadable_directory", f"{reason}; files inside were not analyzed")
        )

    def add_model(model: Model) -> None:
        if model.key in models:
            diagnostics.append(
                PipelineDiagnostic(
                    model.path or model.key,
                    "duplicate_model",
                    f"defines the same table as {models[model.key].path or model.key}; the earlier definition was replaced",
                )
            )
        models[model.key] = model

    # Read every asset first: a ref() names an action wherever its config put it.
    known: dict[str, list[Target]] = {}

    def read_asset(path: Path):
        relative = str(path.relative_to(root))
        text, reason = read_text_or_reason(path)
        if text is None:
            diagnostics.append(PipelineDiagnostic(relative, "read_error", f"{reason}; asset was skipped"))
            return None
        if path.suffix == ".sql" and not _SQLX_CONFIG_RE.search(text):
            target = Target(name=path.stem)
            add_model(Model(target, "sql", text, relative))
            return None
        try:
            sections = _split_sqlx_sections(text)
        except ValueError as exc:
            diagnostics.append(PipelineDiagnostic(relative, "sqlx_parse_error", str(exc)))
            return None
        config = next(
            (body for kind, body in sections if kind == "block" and body.lstrip().startswith("config")),
            "",
        )
        kind = _config_value(config, "type") or "table"
        target = Target(
            _config_value(config, "database") or database,
            _config_value(config, "schema") or (assertion_dataset if kind == "assertion" else dataset),
            _config_value(config, "name") or path.stem,
        )
        known.setdefault(target.name, []).append(target)
        if kind == "declaration":
            sources[target.key] = target
            return None
        return relative, sections, config, kind, target

    def load_asset(relative: str, sections, config: str, kind: str, target: Target) -> None:
        default = Target(database, dataset, "")
        body = "".join(section for kind_, section in sections if kind_ == "sql")
        dependencies: list[Target] = []

        def substitute(match: re.Match[str]) -> str:
            try:
                ref = _parse_ref_args(match.group("args"), default, known, names_may_be_missing=incomplete_js)
            except ValueError as exc:
                diagnostics.append(PipelineDiagnostic(target.key, "unsupported_ref", str(exc)))
                return match.group(0)  # left masked like any other interpolation
            dependencies.append(ref)
            return ref.sql()

        body = _REF_RE.sub(substitute, body)
        body = _SELF_RE.sub(target.sql(), body)
        # Dataform evaluates ref() in pre_operations and post_operations too, so what they ref is a dependency.
        for kind_, section in sections:
            if kind_ == "block" and re.match(r"\s*(?:pre|post)_operations\b", section):
                for match in _REF_RE.finditer(section):
                    try:
                        ref = _parse_ref_args(match.group("args"), default, known, names_may_be_missing=incomplete_js)
                    except ValueError as exc:
                        diagnostics.append(PipelineDiagnostic(target.key, "unsupported_ref", str(exc)))
                        continue
                    if ref not in dependencies:
                        dependencies.append(ref)

        def resolve(match: re.Match[str]) -> str:
            try:
                return _parse_ref_args(match.group("args"), default, known, names_may_be_missing=incomplete_js).sql()
            except ValueError:
                return match.group(0)  # computed argument: left masked like any other interpolation

        body = _RESOLVE_RE.sub(resolve, body)
        for entry in _config_dependencies(config):
            try:
                declared = _parse_ref_args(entry, default, known, names_may_be_missing=incomplete_js)
            except ValueError as exc:
                diagnostics.append(PipelineDiagnostic(target.key, "unsupported_ref", f"config dependencies: {exc}"))
                continue
            if declared not in dependencies:
                dependencies.append(declared)
        masked: tuple[str, ...] = ()
        if "${" in body:
            body, restorations = _mask_sqlx_interpolations(body)
            masked = tuple(item.original for item in restorations)
        try:
            tags = _config_tags(config)
        except Exception as exc:  # noqa: BLE001 - tags are optional; never lose the model over them
            diagnostics.append(PipelineDiagnostic(relative, "config_tags_unreadable", f"could not read tags ({type(exc).__name__}: {exc}); the model was kept without tags"))
            tags = ()
        try:
            non_null, unique_keys = _config_assertions(config)
        except Exception:  # noqa: BLE001 - assertions are optional evidence
            non_null, unique_keys = (), ()
        add_model(Model(target, kind, body, relative, tuple(dependencies), masked, tags, non_null, unique_keys))

    def unreadable(relative: str, exc: Exception) -> None:
        diagnostics.append(PipelineDiagnostic(
            relative, "asset_unreadable",
            f"could not be analyzed ({type(exc).__name__}: {exc}); asset was skipped"))

    incomplete_js = False
    for path in find_assets(root, (".js",), unlistable):
        relative_parts = path.relative_to(root).parts
        if "node_modules" in relative_parts or ".git" in relative_parts:
            continue
        text, _reason = read_text_or_reason(path)
        if text is None or not _JS_CALL_RE.search(text):
            continue
        declared, actions, complete = js_declared_targets(text, Target(database, dataset, ""))
        for target in declared:
            known.setdefault(target.name, []).append(target)
            sources[target.key] = target
        for target in actions:
            known.setdefault(target.name, []).append(target)
        if not complete:
            incomplete_js = True
            diagnostics.append(PipelineDiagnostic(
                str(path.relative_to(root)), "js_declaration_dynamic",
                "declares or names tables that cannot be read without running the code; refs to unlisted names were left unresolved"))

    if incomplete_js and compiled_targets is not None:
        # Dataform's own compilation lists every action, which settles what the JavaScript could not.
        try:
            fetched = list(compiled_targets())
        except Exception as exc:  # noqa: BLE001 - no credentials, offline, no matching repository: stay unresolved
            diagnostics.append(PipelineDiagnostic(
                "", "compiled_graph_unavailable",
                f"the Dataform compilation could not be read ({type(exc).__name__}); refs to unlisted names stay unresolved"))
        else:
            for (target_db, target_schema, target_name), is_declaration in fetched:
                target = Target(target_db, target_schema, target_name)
                if target not in known.setdefault(target.name, []):
                    known[target.name].append(target)
                if is_declaration:
                    sources[target.key] = target
            incomplete_js = False

    pending = []
    for path in find_assets(search_root, (".sqlx", ".sql"), unlistable):
        try:
            asset = read_asset(path)
        except Exception as exc:  # noqa: BLE001 - one odd file must not fail the whole project
            unreadable(str(path.relative_to(root)), exc)
            continue
        if asset is not None:
            pending.append(asset)
    for asset in pending:
        try:
            load_asset(*asset)
        except Exception as exc:  # noqa: BLE001 - one odd file must not fail the whole project
            unreadable(asset[0], exc)

    from .pipeline import Pipeline  # deferred: pipeline imports this module

    return Pipeline(
        models,
        sources,
        dict(source_schema or {}),
        diagnostics,
        default_project=database,
        default_dataset=dataset,
    )


def load_compiled_graph(
    graph: str | Path | dict,
    *,
    source_schema: dict[str, dict[str, str]] | None = None,
) -> Pipeline:
    """Load the JSON printed by ``dataform compile --json``."""

    if isinstance(graph, (str, Path)):
        graph = parse_json_or_raise(Path(graph), "compiled graph")
    if not isinstance(graph, dict):
        raise PipelineLoadError("compiled graph must be a JSON object")

    diagnostics: list[PipelineDiagnostic] = []

    def target_of(raw: object) -> Target:
        if not isinstance(raw, dict):
            raise ValueError("target is not an object")
        return Target(*(str(raw.get(field) or "") for field in ("database", "schema", "name")))

    def list_of(kind_key: str) -> list:
        value = graph.get(kind_key, [])
        if not isinstance(value, list):
            diagnostics.append(
                PipelineDiagnostic(kind_key, "invalid_entry", f"{kind_key!r} is not a list; its entries were skipped")
            )
            return []
        return value

    models: dict[str, Model] = {}
    for kind_key, default_kind in (("tables", "table"), ("assertions", "assertion"), ("operations", "operations")):
        for index, item in enumerate(list_of(kind_key)):
            label = f"{kind_key}[{index}]"
            try:
                target = target_of(item.get("target", {}))
                label = target.key or label
                sql = item.get("query")
                if sql is None:
                    sql = ";\n".join(item.get("queries", []))
                if not isinstance(sql, str):
                    raise ValueError("query is not text")
                kind = item.get("type", default_kind) if kind_key == "tables" else default_kind
                file_name = item.get("fileName")
                asserted = item.get("assertions") if isinstance(item.get("assertions"), dict) else {}
                non_null = asserted.get("nonNull", [])
                non_null = (non_null,) if isinstance(non_null, str) else tuple(c for c in non_null if isinstance(c, str))
                unique = []
                if isinstance(asserted.get("uniqueKey"), list):
                    unique.append(tuple(c for c in asserted["uniqueKey"] if isinstance(c, str)))
                if isinstance(asserted.get("uniqueKeys"), list):
                    for entry in asserted["uniqueKeys"]:
                        columns = entry.get("uniqueKey") if isinstance(entry, dict) else entry
                        if isinstance(columns, list):
                            unique.append(tuple(c for c in columns if isinstance(c, str)))
                model = Model(
                    target,
                    str(kind),
                    sql,
                    file_name if isinstance(file_name, str) else None,
                    tuple(target_of(dep) for dep in item.get("dependencyTargets", [])),
                    tags=tuple(tag for tag in item.get("tags", []) if isinstance(tag, str)),
                    non_null=non_null,
                    unique_keys=tuple(dict.fromkeys(k for k in unique if k)),
                )
            except (AttributeError, TypeError, ValueError):
                diagnostics.append(
                    PipelineDiagnostic(label, "invalid_entry", "entry is malformed; asset was skipped")
                )
                continue
            key = target.key or (model.identity.key if model.identity else "")
            if key:
                if key in models:
                    diagnostics.append(
                        PipelineDiagnostic(key, "duplicate_model", "defined more than once; the earlier definition was replaced")
                    )
                models[key] = model
    sources: dict[str, Target] = {}
    for index, item in enumerate(list_of("declarations")):
        try:
            target = target_of(item.get("target", {}))
        except (AttributeError, TypeError, ValueError):
            diagnostics.append(
                PipelineDiagnostic(f"declarations[{index}]", "invalid_entry", "entry is malformed; asset was skipped")
            )
            continue
        sources[target.key] = target
    from .pipeline import Pipeline  # deferred: pipeline imports this module

    return Pipeline(
        models,
        sources,
        dict(source_schema or {}),
        diagnostics,
        default_project=str(graph.get("defaultDatabase", graph.get("defaultProject", "")) or ""),
        default_dataset=str(graph.get("defaultSchema", graph.get("defaultDataset", "")) or ""),
    )

if TYPE_CHECKING:
    from .pipeline import Pipeline


# -------------------------------------------------------------------- analysis


class _TargetResolver:
    """Resolve full, dataset-qualified or bare table names to known keys."""

    def __init__(self, keys: list[str]):
        self._by_suffix: dict[str, set[str]] = defaultdict(set)
        for key in keys:
            parts = key.split(".")
            for start in range(len(parts)):
                self._by_suffix[".".join(parts[start:])].add(key)

    def _matches(self, parts: list[str], start: int) -> set[str]:
        found = self._by_suffix.get(".".join(parts[start:]), set())
        if start:
            found = {key for key in found if key.count(".") + 1 <= len(parts) - start}
        return found

    def resolve(self, table: exp.Table | str) -> str | None:
        if isinstance(table, exp.Table):
            name = ".".join(part.name for part in table.parts)
        else:
            name = table.strip("`")
        parts = name.split(".")
        # Try the most specific spelling first, then drop leading qualifiers. A dropped qualifier only
        # matches a model keyed with no more qualifiers than are left: ``raw.orders`` is not the model
        # ``p.staging.orders``, but ``p.ds.orders`` finds a model keyed ``ds.orders`` or ``orders``.
        for start in range(len(parts)):
            matches = self._matches(parts, start)
            if matches and len(matches) == 1:
                return next(iter(matches))
            if matches:
                return None
        return None

    def is_ambiguous(self, table: exp.Table | str) -> bool:
        """True when the name matches several known tables, not none."""

        if isinstance(table, exp.Table):
            name = ".".join(part.name for part in table.parts)
        else:
            name = table.strip("`")
        parts = name.split(".")
        for start in range(len(parts)):
            matches = self._matches(parts, start)
            if matches:
                return len(matches) > 1
        return False
