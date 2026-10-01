"""Loading Dataform/SQLX projects and compiled graphs into a Pipeline."""

from __future__ import annotations

from collections import defaultdict
import json
from pathlib import Path
import re
from typing import TYPE_CHECKING, Mapping

from sqlglot import exp

from .identity import NodeIdentity
from .pipeline_types import Model, PipelineDiagnostic, Target
from .resilience import (
    PipelineLoadError,
    describe_os_error,
    find_assets,
    parse_json_or_raise,
    read_text_or_reason,
)
from .sqlx import (
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


_REF_RE = re.compile(r"\$\{\s*ref\(\s*(?P<args>[^()]*?)\s*\)\s*\}")
_SELF_RE = re.compile(r"\$\{\s*self\(\s*\)\s*\}")
_STRING_RE = re.compile(r"""(['"`])((?:\\.|(?!\1).)*)\1""")


def _config_value(config: str, key: str) -> str | None:
    match = re.search(rf"\b{key}\s*:\s*(['\"`])((?:\\.|(?!\1).)*)\1", config)
    return match.group(2) if match else None


def _config_tags(config: str) -> tuple[str, ...]:
    """Tags from a Dataform config block: ``tags: ["a", "b"]`` or ``tags: "a"``."""

    match = re.search(r"\btags\s*:\s*(?:\[(?P<list>[^\]]*)\]|(?P<one>['\"`][^'\"`]*['\"`]))", config)
    if not match:
        return ()
    # ``tags: []`` matches the list branch with an empty string; ``or`` would fall through to the unmatched ``one`` (None).
    listed, single = match.group("list"), match.group("one")
    return tuple(dict.fromkeys(re.findall(r"['\"`]([^'\"`]+)['\"`]", listed if listed is not None else single or "")))


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


def _read_project_defaults_strict(root: Path) -> tuple[str, str]:
    settings = root / "workflow_settings.yaml"
    if settings.is_file():
        text = settings.read_text(encoding="utf-8")

        def value(key: str) -> str:
            match = re.search(rf"(?m)^\s*{key}\s*:\s*['\"]?([^'\"\s#]+)", text)
            return match.group(1) if match else ""

        return value("defaultProject"), value("defaultDataset")
    legacy = root / "dataform.json"
    if legacy.is_file():
        data = json.loads(legacy.read_text(encoding="utf-8"))
        return data.get("defaultDatabase", ""), data.get("defaultSchema", "")
    return "", ""


def _parse_ref_args(args: str, default: Target, known: dict[str, list[Target]] | None = None) -> Target:
    """The target a ``ref()`` names.

    Dataform finds ``ref("name")`` by the action's name, wherever its config put
    it, so a name that ``known`` (every action and declaration by name) holds
    once resolves to that target; otherwise the project defaults apply.
    """

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
        return Target(database or default.database, schema or default.schema, name)
    parts = [match.group(2) for match in _STRING_RE.finditer(args)]
    if len(parts) == 1:
        return by_name(parts[0]) or Target(default.database, default.schema, parts[0])
    if len(parts) == 2:
        return by_name(parts[1], parts[0]) or Target(default.database, parts[0], parts[1])
    if len(parts) >= 3:
        return Target(parts[0], parts[1], parts[2])
    raise ValueError("unsupported ref() arguments")


def load_sqlx_project(
    root: str | Path,
    *,
    source_schema: dict[str, dict[str, str]] | None = None,
) -> Pipeline:
    """Load a Dataform project (``definitions/**.sqlx``) or a folder of ``.sql`` files.

    ``${ref(...)}`` and ``${self()}`` are resolved to table names using the
    project defaults; other interpolations are masked so the SQL still
    parses. For exact compiled SQL, prefer :func:`load_compiled_graph` with
    the output of ``dataform compile --json``.
    """

    root = Path(root)
    if not root.is_dir():
        raise PipelineLoadError("project folder was not found or is not a directory")
    diagnostics: list[PipelineDiagnostic] = []
    database, dataset = _read_project_defaults(root, diagnostics)
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
        if path.suffix == ".sql":
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
            _config_value(config, "schema") or dataset,
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
            ref = _parse_ref_args(match.group("args"), default, known)
            dependencies.append(ref)
            return ref.sql()

        try:
            body = _REF_RE.sub(substitute, body)
        except ValueError as exc:
            diagnostics.append(PipelineDiagnostic(target.key, "unsupported_ref", str(exc)))
        body = _SELF_RE.sub(target.sql(), body)
        masked: tuple[str, ...] = ()
        if "${" in body:
            body, restorations = _mask_sqlx_interpolations(body)
            masked = tuple(item.original for item in restorations)
        try:
            tags = _config_tags(config)
        except Exception as exc:  # noqa: BLE001 - tags are optional; never lose the model over them
            diagnostics.append(PipelineDiagnostic(relative, "config_tags_unreadable", f"could not read tags ({type(exc).__name__}: {exc}); the model was kept without tags"))
            tags = ()
        add_model(Model(target, kind, body, relative, tuple(dependencies), masked, tags))

    def unreadable(relative: str, exc: Exception) -> None:
        diagnostics.append(PipelineDiagnostic(
            relative, "asset_unreadable",
            f"could not be analyzed ({type(exc).__name__}: {exc}); asset was skipped"))

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
                model = Model(
                    target,
                    str(kind),
                    sql,
                    file_name if isinstance(file_name, str) else None,
                    tuple(target_of(dep) for dep in item.get("dependencyTargets", [])),
                    tags=tuple(tag for tag in item.get("tags", []) if isinstance(tag, str)),
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

    def resolve(self, table: exp.Table | str) -> str | None:
        if isinstance(table, exp.Table):
            name = ".".join(part.name for part in table.parts)
        else:
            name = table.strip("`")
        parts = name.split(".")
        # Try the most specific spelling first, then drop leading qualifiers.
        for start in range(len(parts)):
            matches = self._by_suffix.get(".".join(parts[start:]))
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
            matches = self._by_suffix.get(".".join(parts[start:]))
            if matches:
                return len(matches) > 1
        return False
