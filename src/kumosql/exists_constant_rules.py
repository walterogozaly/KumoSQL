"""Two ``EXISTS`` identities: a test a foreign key witnesses, and a correlation a filter fixes.

1. Witnessed by a foreign key. In ``SELECT .. FROM emp WHERE EXISTS (SELECT 1 FROM dept)`` every row the
   WHERE clause tests carries a row of ``emp``. When ``emp.deptno`` is NOT NULL and references
   ``dept.deptno``, ``dept`` holds the row that value names, so the uncorrelated test is TRUE on every
   tested row and the conjunct can go. The test may filter ``dept`` only by conjuncts that row passes:
   ``c = c`` or ``c IS NOT NULL`` for a NOT NULL column of ``dept`` or a referenced column (equal to the
   child's non-NULL value). Its body must be a plain select of that one table (no grouping, aggregate,
   LIMIT or nested query), every column of it the table's own. The child must not sit under a RIGHT or
   FULL join, or on the NULL side of a LEFT one, where a tested row may hold no real child row.
2. A fixed correlation. In ``.. FROM (SELECT .. FROM emp WHERE deptno = 200) t0 WHERE EXISTS (.. = t0.deptno)``
   every row of ``t0`` has ``deptno = 200``, so the correlated reference reads the literal ``200``. A
   WHERE conjunct ``t0.deptno = 200`` of the select itself fixes it too (a row it rejects is rejected
   whatever the other conjuncts say). Only an integer column equated to an integer literal qualifies:
   equal there means identical, unlike ``'0200' = 200`` or ``2.50 = 2.5``. A derived table fixes a column
   only when it has no grouping or aggregate (an empty global aggregate outputs NULL, not the literal),
   and only when it is not NULL-extended by an outer join.
"""

from __future__ import annotations

from sqlglot import exp

from .ast_utils import conjuncts, declared_key, is_function_table, select_sources
from .fk_rules import _same_table

_INTEGER_TYPES = {"INT", "INTEGER", "BIGINT", "SMALLINT", "TINYINT", "MEDIUMINT", "INT64", "INT32", "INT16", "INT8", "HUGEINT"}
_BLOCKING = ("group", "having", "qualify", "limit", "offset", "laterals", "with")


def exists_constant_rules(select: exp.Select, schema, not_null, foreign_keys, types) -> exp.Select | None:
    return _drop_witnessed_exists(select, schema, not_null, foreign_keys) or _fix_correlations(select, schema, types)


# --- 1. witnessed by a foreign key -------------------------------------------------------------


def _real_sources(select: exp.Select) -> list[exp.Expression] | None:
    """Sources whose every tested row is a real row (no NULL extension), or None under RIGHT/FULL joins."""

    joins = select.args.get("joins") or []
    if select.args.get("laterals") or any((j.args.get("side") or "").upper() in ("RIGHT", "FULL") for j in joins):
        return None
    from_ = select.args.get("from_") or select.args.get("from")
    if from_ is None:
        return None
    real = [from_.this]
    for join in joins:
        kind = (join.args.get("kind") or "").upper()
        if not join.args.get("side") and kind in ("", "INNER", "CROSS"):
            real.append(join.this)
    return real


def _drop_witnessed_exists(select: exp.Select, schema, not_null, foreign_keys) -> exp.Select | None:
    where = select.args.get("where")
    if not foreign_keys or not schema or where is None:
        return None
    children = [s for s in _real_sources(select) or [] if isinstance(s, exp.Table) and not is_function_table(s)]
    if not children:
        return None
    parts = conjuncts(where.this)
    kept = [p for p in parts if not (isinstance(p, exp.Exists) and any(_witnesses(c, p.this, schema, not_null, foreign_keys) for c in children))]
    if len(kept) == len(parts):
        return None
    copy = select.copy()
    copy.set("where", exp.Where(this=exp.and_(*[p.copy() for p in kept])) if kept else None)
    return copy


def _witnesses(child: exp.Table, body: exp.Expression, schema, not_null, foreign_keys) -> bool:
    """Whether every row of ``child`` makes ``EXISTS (body)`` TRUE."""

    if not isinstance(body, exp.Select) or body.args.get("joins") or body.args.get("order"):
        return False
    if any(body.args.get(k) for k in _BLOCKING) or body.find(exp.AggFunc, exp.Window, exp.Subquery, exp.Exists):
        return False
    distinct = body.args.get("distinct")
    if distinct is not None and distinct.args.get("on"):
        return False
    from_ = body.args.get("from_") or body.args.get("from")
    parent = from_.this if from_ is not None else None
    if not isinstance(parent, exp.Table) or is_function_table(parent):
        return False
    columns = {c.lower() for c in (schema.get(declared_key(parent)) or [])}
    alias = parent.alias_or_name.lower()
    if not columns:
        return False
    for column in body.find_all(exp.Column):
        if isinstance(column.this, exp.Star):
            continue
        if (column.table and column.table.lower() != alias) or column.name.lower() not in columns:
            return False  # an outer reference: the test may differ from row to row
    child_not_null = {c.lower() for c in (not_null or {}).get(declared_key(child), ())}
    referenced = set()
    for cols, fk_parent, parent_cols in foreign_keys.get(declared_key(child)) or []:
        if _same_table(declared_key(parent), fk_parent) and cols and all(c.lower() in child_not_null for c in cols):
            referenced = {c.lower() for c in parent_cols}
            break
    else:
        return False
    passing = referenced | {c.lower() for c in (not_null or {}).get(declared_key(parent), ())}
    where = body.args.get("where")
    for part in conjuncts(where.this) if where is not None else []:
        if isinstance(part, exp.EQ):
            a, b = part.this, part.expression
            if not (isinstance(a, exp.Column) and isinstance(b, exp.Column) and a.name.lower() == b.name.lower() and a.name.lower() in passing):
                return False
        elif isinstance(part, exp.Not) and isinstance(part.this, exp.Is) and isinstance(part.this.expression, exp.Null):
            if not (isinstance(part.this.this, exp.Column) and part.this.this.name.lower() in passing):
                return False
        else:
            return False
    return True


# --- 2. a fixed correlation --------------------------------------------------------------------


def _int_literal(node: exp.Expression) -> exp.Literal | None:
    if isinstance(node, exp.Literal) and not node.is_string:
        try:
            int(node.this)
        except ValueError:
            return None
        return node
    return None


def _source_of(select: exp.Select, column: exp.Column, schema) -> exp.Expression | None:
    """The source of ``select`` that ``column`` reads, or None (ambiguous, unknown or an outer reference)."""

    sources = select_sources(select)
    if column.table:
        found = [s for s in sources if (s.alias_or_name or "").lower() == column.table.lower()]
    else:
        found = sources if len(sources) == 1 else []
    if len(found) != 1:
        return None
    source = found[0]
    if isinstance(source, exp.Table):
        if is_function_table(source) or column.name.lower() not in {c.lower() for c in (schema or {}).get(declared_key(source)) or []}:
            return None
        return source
    if isinstance(source, exp.Subquery) and isinstance(source.this, exp.Select) and _projection(source) is not None:
        return source if column.name.lower() in _projection(source) else None
    return None


def _projection(subquery: exp.Subquery) -> dict[str, exp.Expression] | None:
    """Output name -> expression of a derived table, or None when its names are not plain."""

    alias = subquery.args.get("alias")
    if alias is not None and alias.args.get("columns"):
        return None
    out: dict[str, exp.Expression] = {}
    for item in subquery.this.expressions:
        name = (item.output_name or "").lower()
        if not name or name == "*" or name in out:
            return None
        out[name] = item.this if isinstance(item, exp.Alias) else item
    return out


def _base_type(select: exp.Select, column: exp.Column, schema, types, depth: int = 0) -> str | None:
    source = _source_of(select, column, schema)
    if source is None or depth > 8:
        return None
    if isinstance(source, exp.Table):
        return (types.get(declared_key(source)) or {}).get(column.name.lower())
    value = _projection(source)[column.name.lower()]
    return _base_type(source.this, value, schema, types, depth + 1) if isinstance(value, exp.Column) else None


def _is_integer(type_name: str | None) -> bool:
    return bool(type_name) and type_name.split("(")[0].strip().upper() in _INTEGER_TYPES


def _fixed_by_where(select: exp.Select, schema) -> list[tuple[exp.Expression, str, exp.Literal, exp.Expression]]:
    """(source, column, literal, conjunct) for each top-level WHERE conjunct ``column = integer``."""

    where = select.args.get("where")
    out = []
    for part in conjuncts(where.this) if where is not None else []:
        if not isinstance(part, exp.EQ):
            continue
        for column, value in ((part.this, part.expression), (part.expression, part.this)):
            literal = _int_literal(value)
            if isinstance(column, exp.Column) and literal is not None:
                source = _source_of(select, column, schema)
                if source is not None:
                    out.append((source, column.name.lower(), literal, part))
    return out


def _fixed_by_derived(subquery: exp.Subquery, schema) -> dict[str, exp.Literal]:
    """Output columns of a derived table that equal one integer literal in every row."""

    inner = subquery.this
    if any(inner.args.get(k) for k in _BLOCKING) or inner.find(exp.AggFunc) and any(
        a.find_ancestor(exp.Select) is inner for a in inner.find_all(exp.AggFunc)
    ):
        return {}
    distinct = inner.args.get("distinct")
    if distinct is not None and distinct.args.get("on"):
        return {}
    projection = _projection(subquery) or {}
    fixed = _fixed_by_where(inner, schema)
    out = {}
    for name, value in projection.items():
        if not isinstance(value, exp.Column):
            continue
        source = _source_of(inner, value, schema)
        for fixed_source, column, literal, _ in fixed:
            if fixed_source is source and source is not None and column == value.name.lower():
                out[name] = literal
                break
    return out


def _fix_correlations(select: exp.Select, schema, types) -> exp.Select | None:
    where = select.args.get("where")
    if where is None or not schema or not types or not where.find(exp.Select):
        return None
    types = {t.lower(): {c.lower(): v for c, v in cols.items()} for t, cols in types.items()}
    # (alias, column) -> (literal, conjunct that fixes it, or None when every row of the source has it)
    fixed: dict[tuple[str, str], tuple[exp.Literal, exp.Expression | None]] = {}
    for source, column, literal, part in _fixed_by_where(select, schema):
        fixed.setdefault(((source.alias_or_name or "").lower(), column), (literal, part))
    for source in _real_sources(select) or []:
        if isinstance(source, exp.Subquery) and isinstance(source.this, exp.Select):
            for column, literal in _fixed_by_derived(source, schema).items():
                fixed[((source.alias_or_name or "").lower(), column)] = (literal, None)
    fixed = {
        key: value for key, value in fixed.items()
        if _is_integer(_base_type(select, exp.column(key[1], table=key[0]), schema, types))
    }
    if not fixed:
        return None
    copy = select.copy()
    changed = False
    for part in conjuncts(copy.args["where"].this):
        for column in list(part.find_all(exp.Column)):
            key = ((column.table or "").lower(), column.name.lower())
            if key not in fixed or not column.table:
                continue
            literal, fixer = fixed[key]
            if fixer is not None and part.sql() == fixer.sql():
                continue
            if column.find_ancestor(exp.Select) is copy or _shadowed(column, copy, key[0]):
                continue  # only references from a nested query, to this select's own source
            column.replace(literal.copy())
            changed = True
    return copy if changed else None


def _shadowed(column: exp.Column, outer: exp.Select, alias: str) -> bool:
    node = column.parent
    while node is not None and node is not outer:
        if isinstance(node, exp.Select) and any((s.alias_or_name or "").lower() == alias for s in select_sources(node)):
            return True
        if isinstance(node, exp.Select) and node.args.get("with"):
            return True
        node = node.parent
    return False
