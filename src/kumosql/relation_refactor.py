"""Conservatively migrate project readers across a declared relation equivalence.

This first project-refactor slice handles a declaration whose two sides are plain
projections from one table. It substitutes only direct columns described by the
declaration and preserves a consumer's output names. Every result remains conditional
on the persisted relation declaration and its freshness scope.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import tempfile

import sqlglot
from sqlglot import exp

from . import relation_declarations
from .pipeline import load_sqlx_project
from .project_reduction import (
    FileChange,
    _Project,
    _apply,
    _config_block,
    _config_dependencies,
    _copy_project,
    _dependency_entries,
    _entry_name,
    _load,
    _name_key,
    _with_body,
    _written_sql,
)


class RelationRefactorError(ValueError):
    """A relation declaration cannot safely drive this project migration."""


@dataclass(frozen=True)
class QueryRewrite:
    sql: str
    changed: bool
    reason: str = ""


@dataclass(frozen=True)
class ConsumerChange:
    model: str
    path: str
    output_columns: tuple[str, ...]

    def to_json(self) -> dict:
        return {"model": self.model, "path": self.path, "output_columns": list(self.output_columns)}


@dataclass
class ProjectRefactor:
    declaration_id: str
    preferred_side: str
    scope: dict
    evidence: dict
    provenance: dict
    files: list[FileChange]
    changes: list[ConsumerChange]
    skipped: list[dict] = field(default_factory=list)
    output_names_order_preserved: bool = False

    def patch(self) -> str:
        return "".join(change.diff() for change in self.files)

    def to_json(self) -> dict:
        return {
            "output_names_order_preserved": self.output_names_order_preserved,
            "type_preservation": "direct mapped inputs retain the declaration's equal output types",
            "preservation": {
                "status": "conditional" if self.changes else "unchanged",
                "premises": [{"declaration_id": self.declaration_id, "evidence": self.evidence,
                              "scope": self.scope, "provenance": self.provenance}],
                "consumers": [change.to_json() for change in self.changes],
            },
            "changes": [change.to_json() for change in self.changes],
            "skipped": self.skipped,
            "files": [{"path": file.path, "action": file.action} for file in self.files],
            "diff": self.patch(),
        }

    def apply(self, root: str | Path) -> None:
        root = Path(root)
        for change in self.files:
            path = root / change.path
            if change.action == "add":
                if path.exists():
                    raise RelationRefactorError(f"refusing to overwrite a file added after planning: {change.path}")
                continue
            try:
                with open(path, encoding="utf-8", newline="") as handle:
                    current = handle.read()
            except OSError as error:
                raise RelationRefactorError(f"refusing to apply a stale patch for {change.path}: {error}") from error
            if current != change.before:
                raise RelationRefactorError(f"refusing to overwrite edits made after planning: {change.path}")
        _apply(root, self.files)


@dataclass(frozen=True)
class _Side:
    table_parts: tuple[str, ...]
    columns_by_output: dict[str, str]


def _query(sql: str) -> exp.Select:
    tree = sqlglot.parse_one(sql, read="bigquery")
    if not isinstance(tree, exp.Select) or tree.args.get("with_"):
        raise RelationRefactorError("declared sides must be SELECTs without CTEs")
    return tree


def _table_parts(table: exp.Table) -> tuple[str, ...]:
    return tuple(part.name.casefold() for part in table.parts)


def _direct_side(sql: str) -> _Side:
    """Read the narrow declaration form this rewrite can substitute by congruence."""

    try:
        query = _query(sql)
    except (sqlglot.errors.ParseError, RelationRefactorError) as error:
        raise RelationRefactorError(f"unsupported declaration side: {error}") from error
    from_clause = query.args.get("from_")
    if from_clause is None or not isinstance(from_clause.this, exp.Table) or from_clause.expressions:
        raise RelationRefactorError("declaration sides need exactly one direct table")
    if query.args.get("joins"):
        raise RelationRefactorError("declaration sides with joins are not supported")
    if any(query.args.get(key) for key in ("where", "group", "having", "qualify", "order", "limit", "offset", "distinct")):
        raise RelationRefactorError("declaration sides must preserve the full input row set")
    columns: dict[str, str] = {}
    seen_sources: set[str] = set()
    for projection in query.expressions:
        value = projection.this if isinstance(projection, exp.Alias) else projection
        if not isinstance(value, exp.Column) or value.table:
            raise RelationRefactorError("declaration sides must project unqualified direct columns")
        output_name = projection.alias if isinstance(projection, exp.Alias) else value.name
        output_key = output_name.casefold()
        source_name = value.name
        if output_key in columns or source_name.casefold() in seen_sources:
            raise RelationRefactorError("declaration side has duplicate or ambiguous source columns")
        columns[output_key] = source_name
        seen_sources.add(source_name.casefold())
    if not columns:
        raise RelationRefactorError("declaration side has no direct output columns")
    return _Side(_table_parts(from_clause.this), columns)


def _declaration_mapping(declaration: relation_declarations.RelationDeclaration) -> tuple[_Side, _Side, dict[str, str]]:
    if declaration.preferred_side not in {"left", "right"}:
        raise RelationRefactorError("the relation declaration has no preferred side")
    if declaration.scope.kind != "all_snapshots":
        raise RelationRefactorError("this migration requires an all_snapshots freshness scope")
    left, right = _direct_side(declaration.left_sql), _direct_side(declaration.right_sql)
    old, preferred = (right, left) if declaration.preferred_side == "left" else (left, right)
    mapping: dict[str, str] = {}
    for old_output, new_output in declaration.output_mapping:
        old_source = old.columns_by_output.get(old_output.casefold())
        new_source = preferred.columns_by_output.get(new_output.casefold())
        if old_source is None or new_source is None:
            raise RelationRefactorError("declaration mapping does not cover the direct projections")
        old_key = old_source.casefold()
        if old_key in mapping:
            raise RelationRefactorError("one old input column maps to multiple preferred columns")
        mapping[old_key] = new_source
    return old, preferred, mapping


def _same_relation(actual: tuple[str, ...], declared: tuple[str, ...]) -> bool:
    return bool(actual and declared) and (actual == declared or actual[-len(declared):] == declared or declared[-len(actual):] == actual)


def _set_identifier(node: exp.Expression, arg: str, name: str) -> None:
    previous = node.args.get(arg)
    quoted = bool(previous.args.get("quoted")) if isinstance(previous, exp.Identifier) else False
    node.set(arg, exp.to_identifier(name, quoted=quoted))


def rewrite_consumer_query(sql: str, declaration: relation_declarations.RelationDeclaration) -> QueryRewrite:
    """Substitute one declared source relation in a supported consumer SELECT.

    The declaration sides must be projection-only queries over one table. CTEs and
    nested SELECTs, stars, USING/NATURAL joins, unqualified columns in joins, and
    columns outside the declared mapping are refused.
    """

    try:
        old, _preferred, columns = _declaration_mapping(declaration)
    except RelationRefactorError as error:
        return QueryRewrite(sql, False, str(error))
    try:
        tree = sqlglot.parse_one(sql, read="bigquery")
    except sqlglot.errors.ParseError as error:
        return QueryRewrite(sql, False, f"unsupported consumer query: {error}")
    if not isinstance(tree, exp.Select):
        return QueryRewrite(sql, False, "consumer is not a SELECT")
    from_clause = tree.args.get("from_")
    if from_clause is None:
        return QueryRewrite(sql, False)
    relations = [from_clause.this, *(from_clause.expressions or []),
                 *(join.this for join in tree.args.get("joins") or [])]
    matches = [relation for relation in relations
               if isinstance(relation, exp.Table) and _same_relation(_table_parts(relation), old.table_parts)]
    if not matches:
        if any(isinstance(node, exp.Table) and _same_relation(_table_parts(node), old.table_parts)
               for node in tree.walk()):
            return QueryRewrite(sql, False, "the declared relation occurs inside a nested query")
        return QueryRewrite(sql, False)
    if len(matches) != 1:
        return QueryRewrite(sql, False, "the declared relation occurs more than once")
    if tree.args.get("with_") or any(isinstance(node, exp.Select) and node is not tree for node in tree.walk()):
        return QueryRewrite(sql, False, "CTEs and nested SELECTs are not supported")
    if any(not isinstance(relation, exp.Table) for relation in relations):
        return QueryRewrite(sql, False, "consumer relations must be direct tables")
    source = matches[0]
    for join in tree.args.get("joins") or []:
        if join.args.get("using") or str(join.args.get("method") or "").casefold() == "natural":
            return QueryRewrite(sql, False, "USING and NATURAL joins need a separate column-scope analysis")
    explicit_alias = bool(source.alias)
    source_qualifier = source.alias_or_name.casefold()
    qualifiers = [relation.alias_or_name.casefold() for relation in relations if isinstance(relation, exp.Table)]
    if len(qualifiers) != len(set(qualifiers)):
        return QueryRewrite(sql, False, "relation aliases collide in the consumer scope")
    for star in tree.find_all(exp.Star):
        if isinstance(star.parent, exp.Count):
            continue  # COUNT(*) preserves the declared relation's row multiplicity.
        column_parent = star.parent if isinstance(star.parent, exp.Column) else None
        if column_parent is None or not column_parent.table or column_parent.table.casefold() == source_qualifier:
            return QueryRewrite(sql, False, "a star projection could change the consumer output interface")
    unqualified = [column for column in tree.find_all(exp.Column) if not column.table]
    if unqualified and len(relations) > 1:
        return QueryRewrite(sql, False, "unqualified columns in a join cannot be assigned to the declared relation")
    for column in unqualified:
        mapped = columns.get(column.name.casefold())
        if mapped is None:
            return QueryRewrite(sql, False, f"consumer uses undeclared column {column.name!r}")

    target = sqlglot.parse_one(declaration.left_sql if declaration.preferred_side == "left" else declaration.right_sql,
                               read="bigquery")
    target_table = target.args["from_"].this
    old_alias = source.args.get("alias")
    # Keep SQLGlot's source spelling/quoting from the preferred declaration side.
    source.set("this", target_table.args["this"].copy())
    source.set("db", target_table.args.get("db").copy() if target_table.args.get("db") else None)
    source.set("catalog", target_table.args.get("catalog").copy() if target_table.args.get("catalog") else None)
    if explicit_alias:
        source.set("alias", old_alias)

    # If a renamed direct projection had no explicit alias, retain its old public name.
    for projection in list(tree.expressions):
        value = projection.this if isinstance(projection, exp.Alias) else projection
        if not isinstance(value, exp.Column):
            continue
        qualifier_matches = (not value.table and len(relations) == 1) or (
            value.table and value.table.casefold() == source_qualifier
        )
        new_name = columns.get(value.name.casefold()) if qualifier_matches else None
        if new_name and new_name.casefold() != value.name.casefold() and not isinstance(projection, exp.Alias):
            projection.replace(exp.alias_(projection.copy(), value.name, quoted=False))

    for column in list(tree.find_all(exp.Column)):
        if not column.table:
            new_name = columns.get(column.name.casefold())
            if new_name is None:
                continue
        elif column.table.casefold() == source_qualifier:
            new_name = columns.get(column.name.casefold())
            if new_name is None:
                return QueryRewrite(sql, False, f"consumer uses undeclared column {column.name!r}")
            if not explicit_alias:
                _set_identifier(column, "table", target_table.name)
                if target_table.db:
                    _set_identifier(column, "db", target_table.db)
                if target_table.catalog:
                    _set_identifier(column, "catalog", target_table.catalog)
        else:
            continue
        old_name = column.name
        if old_name.casefold() != new_name.casefold():
            _set_identifier(column, "this", new_name)
    return QueryRewrite(tree.sql(dialect="bigquery", pretty=True), True)


def _names(project: _Project) -> dict[str, list[str]]:
    names: dict[str, list[str]] = {}
    for key in [*project.pipeline.models, *project.pipeline.sources]:
        names.setdefault(key.split(".")[-1].casefold(), []).append(key)
    return names


def _dependency_names_old(value: str, old_names: set[str]) -> bool:
    normalized = value.strip().strip("`\"'").casefold()
    return any(normalized == name or normalized.endswith("." + name) for name in old_names)


def _model_is_stable(project: _Project, table_parts: tuple[str, ...]) -> tuple[str | None, str]:
    table = exp.Table(this=exp.to_identifier(table_parts[-1]))
    if len(table_parts) > 1:
        table.set("db", exp.to_identifier(table_parts[-2]))
    if len(table_parts) > 2:
        table.set("catalog", exp.to_identifier(table_parts[-3]))
    key = project.pipeline.resolve(table)
    model = project.pipeline.models.get(key or "")
    if model is None:
        return key, ""
    if model.kind not in {"table", "view", "sql"} or model.operations_sql or model.incremental_sql:
        return key, f"relation {key} has execution-state or operation semantics"
    return key, ""


def refactor_project(root: str | Path, declaration: relation_declarations.RelationDeclaration) -> ProjectRefactor:
    """Create coordinated SQL/SQLX patches for supported readers of a declaration."""

    old, preferred, _ = _declaration_mapping(declaration)
    project = _load(Path(root), None)
    old_key, why = _model_is_stable(project, old.table_parts)
    if why:
        raise RelationRefactorError(why)
    new_key, why = _model_is_stable(project, preferred.table_parts)
    if why:
        raise RelationRefactorError(why)
    names = _names(project)

    def resolve(table: exp.Table) -> str | None:
        return project.pipeline.resolve(table) or _name_key(project, ".".join(p for p in (table.catalog, table.db, table.name) if p))

    files: list[FileChange] = []
    changes: list[ConsumerChange] = []
    skipped: list[dict] = []
    for key, model in project.pipeline.models.items():
        if not model.path or key in {old_key, new_key}:
            continue
        if model.kind not in {"table", "view", "sql"} or model.operations_sql or model.incremental_sql:
            continue
        try:
            before_columns = tuple(project.pipeline.output_columns(key))
        except Exception:  # noqa: BLE001 - unavailable interface evidence blocks this model only
            before_columns = ()
        if not before_columns:
            skipped.append({"model": key, "path": model.path.replace("\\", "/"),
                            "reason": "consumer output names/order could not be established"})
            continue
        # Explicit action dependencies can encode ordering beyond a read edge. Keep
        # these readers untouched until dependency-list rewrites have their own proof.
        old_names = {old.table_parts[-1], ".".join(old.table_parts)}
        if old_key and old_key in {target for _entry, targets in _dependency_entries(project, key)
                                   for target in targets}:
            skipped.append({"model": key, "path": model.path.replace("\\", "/"),
                            "reason": "explicit action dependency on the old relation needs ordering review"})
            continue
        if any(_dependency_names_old(_entry_name(item), old_names)
               for item in _config_dependencies(_config_block(project.texts.get(key, "")))):
            skipped.append({"model": key, "path": model.path.replace("\\", "/"),
                            "reason": "explicit dependency on the old relation needs ordering review"})
            continue
        query = rewrite_consumer_query(project.sql.get(key, model.sql), declaration)
        if query.reason:
            skipped.append({"model": key, "path": model.path.replace("\\", "/"), "reason": query.reason})
        if not query.changed:
            continue
        text = project.texts.get(key, "")
        after_body = _written_sql(project, query.sql, resolve, names)
        after_text = _with_body(text, after_body)
        if after_text == text:
            continue
        files.append(FileChange(model.path.replace("\\", "/"), "modify", text, after_text))
        changes.append(ConsumerChange(key, model.path.replace("\\", "/"), before_columns))

    files.sort(key=lambda item: item.path)
    output_names_order_preserved = not files
    if files:
        with tempfile.TemporaryDirectory() as temporary:
            copy = Path(temporary) / "project"
            copy.mkdir()
            _copy_project(project.root, copy)
            _apply(copy, files)
            try:
                after = load_sqlx_project(copy)
            except Exception as error:  # noqa: BLE001 - invalid project patch is never returned as verified
                skipped.append({"reason": f"patched project failed to load: {type(error).__name__}: {error}"})
                files, changes = [], []
            else:
                invalid = []
                before_diagnostics = {(item.code, item.model) for item in project.pipeline.diagnostics}
                new_diagnostics = [item for item in after.diagnostics
                                   if (item.code, item.model) not in before_diagnostics
                                   and item.code != "duplicate_model"]
                for change in changes:
                    if change.model not in after.models:
                        invalid.append(change.model)
                        continue
                    if tuple(after.output_columns(change.model)) != change.output_columns:
                        invalid.append(change.model)
                if invalid or new_diagnostics:
                    if invalid:
                        skipped.append({"reason": "consumer output names/order changed", "models": invalid})
                    if new_diagnostics:
                        skipped.append({"reason": "patched project has new load diagnostics",
                                        "diagnostics": [{"code": item.code, "model": item.model}
                                                        for item in new_diagnostics[:10]]})
                    files = []
                    changes = []
                else:
                    output_names_order_preserved = True

    return ProjectRefactor(
        declaration.id,
        declaration.preferred_side or "",
        declaration.scope.to_json(),
        declaration.evidence.to_json(),
        dict(declaration.provenance),
        files,
        changes,
        skipped,
        output_names_order_preserved,
    )


def refactor_project_by_id(root: str | Path, declaration_id: str) -> ProjectRefactor:
    """Load a saved relation declaration and plan the supported project migration."""

    declaration = relation_declarations.get(declaration_id)
    if declaration is None:
        raise RelationRefactorError(f"relation declaration {declaration_id!r} was not found")
    return refactor_project(root, declaration)


def main(argv: list[str] | None = None) -> int:
    import argparse
    import json

    parser = argparse.ArgumentParser(
        prog="python -m kumosql refactor-project",
        description="Preview a project migration to a preferred declared relation.",
    )
    parser.add_argument("project", type=Path)
    parser.add_argument("--declaration", required=True, help="saved relation declaration ID")
    parser.add_argument("--patch", type=Path, help="write the proposed unified diff to this path")
    parser.add_argument("--write", action="store_true", help="apply the patch after output names/order are checked")
    args = parser.parse_args(argv)
    try:
        result = refactor_project_by_id(args.project, args.declaration)
    except (OSError, RelationRefactorError, relation_declarations.DeclarationError, ValueError) as error:
        parser.error(str(error))
    if args.write:
        if not result.output_names_order_preserved:
            parser.error("project output names/order could not be checked; patch was not applied")
        result.apply(args.project)
    if args.patch:
        args.patch.write_text(result.patch(), encoding="utf-8", newline="")
    print(json.dumps(result.to_json(), indent=2))
    return 0
