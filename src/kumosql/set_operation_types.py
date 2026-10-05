"""Find set operations whose branches give one output different declared types.

A set operation converts every branch's value to the outputs' common type before it compares
rows, and the algebraic rules read it as if no conversion happened: they move a filter, a
projection or a join into each branch, where it runs on the unconverted value. Over ``a.x BIGINT``
and ``b.x VARCHAR`` DuckDB makes the union ``VARCHAR``, so

    SELECT x FROM (SELECT x FROM a UNION ALL SELECT x FROM b) d WHERE x = '01'

drops the integer 1 (it becomes ``'1'``), while ``SELECT x FROM a WHERE x = '01' UNION ALL ...``
keeps it (``1 = '01'`` compares as integers). An ``INT64`` above 2**53 compared before it
becomes ``FLOAT64`` can differ the same way. :func:`mixed_types` names such an output so the
prover declines instead of proving through the conversion, and :func:`unchecked_types` tells it when a proof
must carry :data:`ASSUMPTION` because some branch's type is not known.

Types come from the declared column types, casts and literals, followed through derived tables,
CTEs and nested set operations; an output whose type is not known on some branch is not reported.

Where that reading leaves a branch's type unknown, the GoogleSQL type checker (:mod:`kumosql.googlesql_types`) is asked
for the types of the branches' select-list expressions (BigQuery only; a ``SELECT *`` over one table follows that
table). When it knows every branch's type and they are all the same, the output needs no assumption; when it knows
them and they differ, or does not know one, the proof still carries :data:`ASSUMPTION` as before. The checker never
makes :func:`mixed_types` decline anything it did not decline before.
"""

from __future__ import annotations

import re

import sqlglot
from sqlglot import exp

from .googlesql_types import Catalog, GType, infer

_FAMILIES = {
    "int": ("TINYINT", "SMALLINT", "MEDIUMINT", "INT", "INTEGER", "BIGINT", "INT64", "BYTEINT", "HUGEINT", "UTINYINT", "USMALLINT", "UINTEGER", "UBIGINT", "INT2", "INT4", "INT8"),
    "float": ("FLOAT", "FLOAT64", "DOUBLE", "REAL", "FLOAT4", "FLOAT8"),
    "string": ("STRING", "VARCHAR", "TEXT", "CHAR", "NVARCHAR", "NCHAR", "BPCHAR"),
}
_MAX_DEPTH = 12
_EXACT_IN_FLOAT = 2**53
# an integer literal below 2**53 has the same value as a FLOAT64 or NUMERIC (``SELECT price ... UNION ALL SELECT 0``)


def _family(type_sql: str) -> str:
    base = re.split(r"[\s(<]", type_sql.strip().upper(), maxsplit=1)[0]
    for family, names in _FAMILIES.items():
        if base in names:
            return family
    return re.sub(r"\s+", "", type_sql.upper())  # NUMERIC(10, 2) and NUMERIC(12, 4) are different types too


def _conflict(column: tuple[str | None, ...]) -> list[str]:
    """The different known types in one output's branches; a small integer literal fits any number type."""

    known = set(column) - {None, "null"}
    if "small int" in known:
        known.discard("small int")
        if any(not _is_number(k) for k in known):
            known.add("int")
    return sorted(known) if len(known) > 1 else []


def _is_number(family: str) -> bool:
    return family in ("int", "float") or family.startswith(("NUMERIC", "DECIMAL", "BIGNUMERIC", "BIGDECIMAL"))


def _leaves(node: exp.Expression) -> list[exp.Expression]:
    while isinstance(node, (exp.Paren, exp.Subquery)) and not (isinstance(node, exp.Subquery) and node.alias):
        node = node.this
    if isinstance(node, exp.SetOperation):
        return _leaves(node.this) + _leaves(node.expression)
    return [node]


class _Types:
    def __init__(self, tree: exp.Expression, types: dict[str, dict[str, str]]):
        self.types = {k.lower(): {c.lower(): t for c, t in v.items()} for k, v in types.items()}
        self.ctes = {cte.alias_or_name.lower(): cte.this for cte in tree.find_all(exp.CTE)}
        self._outputs: dict[tuple[int, int], list[str | None] | None] = {}  # (node id, depth) -> outputs

    def outputs(self, query: exp.Expression, depth: int) -> list[str | None] | None:
        """The type family of each output of ``query``, ``None`` per unknown one; ``None`` if unreadable.

        Each column of a derived table asks for the outputs of the whole query below it, so a chain of nested
        set operations over wide selects would repeat that work once per column per level (exponentially);
        the answer for a node at a depth is kept.
        """

        key = (id(query), depth)
        if key not in self._outputs:
            self._outputs[key] = self._read_outputs(query, depth)
        result = self._outputs[key]
        return None if result is None else list(result)

    def _read_outputs(self, query: exp.Expression, depth: int) -> list[str | None] | None:
        leaves = _leaves(query)
        if len(leaves) > 1:
            per_leaf = [self.outputs(leaf, depth + 1) for leaf in leaves]
            if any(p is None for p in per_leaf) or len({len(p) for p in per_leaf}) != 1:
                return None
            merged = []
            for column in zip(*per_leaf):
                known = set(column) - {None, "null"}
                if len(known) > 1:
                    merged.append(None)  # reported where that operation is visited
                elif known:
                    merged.append(known.pop())
                else:
                    merged.append("null" if "null" in column else None)
            return merged
        select = leaves[0]
        if not isinstance(select, exp.Select) or any(isinstance(e, exp.Star) or isinstance(e.unalias(), exp.Star) for e in select.expressions):
            return None
        return [self.value(e.unalias(), select, depth) for e in select.expressions]

    def value(self, node: exp.Expression, select: exp.Select, depth: int) -> str | None:
        if depth > _MAX_DEPTH:
            return None
        if isinstance(node, exp.Paren):
            return self.value(node.this, select, depth + 1)
        if isinstance(node, exp.Null):
            return "null"
        if isinstance(node, exp.Boolean):
            return "bool"
        if isinstance(node, exp.Literal):
            if node.is_string:
                return "string"
            if re.fullmatch(r"\d+", node.name or ""):
                return "small int" if int(node.name) < _EXACT_IN_FLOAT else "int"
            return "float"
        if isinstance(node, exp.Neg):
            return self.value(node.this, select, depth + 1)
        if isinstance(node, exp.Cast) and isinstance(node.args.get("to"), exp.DataType):
            return _family(node.args["to"].sql())
        if isinstance(node, exp.Column) and isinstance(node.this, exp.Identifier):
            return self.column(node, select, depth)
        return None

    def column(self, column: exp.Column, select: exp.Select, depth: int) -> str | None:
        from_ = select.args.get("from_") or select.args.get("from")
        sources = ([from_.this] if from_ is not None else []) + [j.this for j in select.args.get("joins") or []]
        name = column.name.lower()
        found: list[str | None] = []
        for source in sources:
            if column.table and (source.alias_or_name or "").lower() != column.table.lower():
                continue
            if isinstance(source, exp.Table) and not source.args.get("db") and source.name.lower() in self.ctes:
                source = exp.Subquery(this=self.ctes[source.name.lower()])
            if isinstance(source, exp.Table):
                parts = [p.name for p in (source.args.get("catalog"), source.args.get("db"), source.this) if p is not None]
                declared = self.types.get(".".join(parts).lower())
                if declared is None:
                    return None  # an unknown table may hold the column
                if name in declared:
                    found.append(_family(declared[name]))
            elif isinstance(source, exp.Subquery):
                leaves = _leaves(source.this)
                first = leaves[0]
                if not isinstance(first, exp.Select):
                    return None
                names = [e.alias_or_name.lower() for e in first.expressions]
                if names.count(name) > 1:
                    return None
                if name in names:
                    outputs = self.outputs(source.this, depth + 1)
                    found.append(None if outputs is None else outputs[names.index(name)])
            else:
                return None  # VALUES, UNNEST, table functions: not followed
        return found[0] if len(found) == 1 else None


ASSUMPTION = "the branches of each set operation give each output the same type (no implicit conversion)"


def _per_operation(tree: exp.Expression, types: dict[str, dict[str, str]] | None):
    """``(leaves, per-leaf output types)`` for each set operation; the types are ``None`` when unreadable."""

    reader = _Types(tree, types or {})
    for operation in tree.find_all(exp.SetOperation):
        leaves = _leaves(operation)
        try:
            per_leaf = [reader.outputs(leaf, 0) for leaf in leaves]
        except Exception:  # noqa: BLE001 - a shape this reader does not know is an unknown type
            yield leaves, None
            continue
        if any(p is None for p in per_leaf) or len({len(p) for p in per_leaf}) != 1:
            yield leaves, None
        else:
            yield leaves, per_leaf


def _parse(sql: str, dialect: str) -> exp.Expression | None:
    try:
        return sqlglot.parse_one(sql, read=dialect)
    except Exception:  # noqa: BLE001 - the prover reports the parse error itself
        return None


class _Checker:
    """The type checker's view of the branches of one query; built only when the first unknown type needs it."""

    def __init__(self, tree: exp.Expression, types: dict[str, dict[str, str]] | None, dialect: str):
        self.tree = tree
        self.types = types or {}
        self.dialect = dialect
        self._typed = None

    @property
    def typed(self):
        if self._typed is None:
            self._typed = False
            if self.dialect == "bigquery":
                try:
                    result = infer(self.tree, Catalog.from_types(self.types))
                except Exception:  # noqa: BLE001 - the checker only ever removes an assumption
                    result = None
                if result is not None and result.error is None:
                    self._typed = result
        return self._typed or None

    def outputs(self, leaf: exp.Expression) -> list[GType | str | None] | None:
        """The checked type of each output of one branch (``"null"`` for a bare NULL), ``None`` per unknown one,
        ``None`` when the branch's outputs cannot be listed."""

        typed = self.typed
        if typed is None or not isinstance(leaf, exp.Select) or leaf.args.get("kind"):
            return None
        out: list[GType | str | None] = []
        for item in leaf.expressions:
            value = item.unalias()
            while isinstance(value, exp.Paren):
                value = value.this
            if isinstance(value, exp.Null):
                out.append("null")
            elif isinstance(value, exp.Star) or (isinstance(value, exp.Column) and isinstance(value.this, exp.Star)):
                columns = self._star(leaf, value, typed)
                if columns is None:
                    return None
                out.extend(columns)
            else:
                known = typed.type_of(item.unalias())
                out.append(known if known is not None and known.complete else None)
        return out

    @staticmethod
    def _star(leaf: exp.Select, star: exp.Expression, typed) -> list[GType | None] | None:
        """``SELECT *`` over a single table (or CTE) with no modifier: that relation's columns."""

        from_ = leaf.args.get("from_") or leaf.args.get("from")
        bare = star if isinstance(star, exp.Star) else star.this
        if (
            from_ is None or leaf.args.get("joins") or leaf.args.get("laterals") or not isinstance(from_.this, exp.Table)
            or from_.this.args.get("pivots") or from_.this.args.get("joins") or not isinstance(from_.this.this, exp.Identifier)
            or any(bare.args.get(k) for k in ("except_", "except", "replace", "rename"))
            or (isinstance(star, exp.Column) and (star.table or "").lower() != (from_.this.alias_or_name or "").lower())
        ):
            return None
        columns = typed.relation(from_.this)
        if columns is None or any(c.name is None for c in columns):
            return None
        return [c.type if c.type is not None and c.type.complete else None for c in columns]

    def resolved(self, leaves: list[exp.Expression], position: int | None) -> bool:
        """Whether the checker knows one output's type on every branch and they are all the same (a bare NULL branch
        fits any type). ``position`` is None for every output at once, for a set operation whose branches the old
        reading could not list."""

        per_leaf = [self.outputs(leaf) for leaf in leaves]
        if any(p is None for p in per_leaf) or len({len(p) for p in per_leaf}) != 1:
            return False
        for column in (zip(*per_leaf) if position is None else [[p[position] for p in per_leaf]]):
            kinds = [c for c in column if c != "null"]
            if any(not isinstance(c, GType) for c in kinds) or len(set(kinds)) > 1:
                return False
        return True


def unchecked_types(sql: str, types: dict[str, dict[str, str]] | None, dialect: str = "bigquery") -> bool:
    """Whether some set operation has an output whose type is not known on every branch (so a proof assumes them equal).

    Without types DuckDB, MySQL and others convert ``1`` and ``'01'`` to one type silently; a proof that moved a
    filter into the branches then holds only under :data:`ASSUMPTION`.
    """

    tree = _parse(sql, dialect)
    if tree is None:
        return False
    checker = _Checker(tree, types, dialect)
    for leaves, per_leaf in _per_operation(tree, types):
        if per_leaf is None:
            if not checker.resolved(leaves, None):
                return True
            continue
        for position, column in enumerate(zip(*per_leaf)):
            if None in column and len([c for c in column if c != "null"]) > 1 and not checker.resolved(leaves, position):
                return True
    return False


def mixed_types(sql: str, types: dict[str, dict[str, str]] | None, dialect: str = "bigquery") -> str | None:
    """A description of the first set-operation output whose branches have different known types, else None."""

    if not types:
        return None
    tree = _parse(sql, dialect)
    if tree is None:
        return None
    for leaves, per_leaf in _per_operation(tree, types):
        if per_leaf is None:
            continue
        first = leaves[0]
        names = [e.alias_or_name for e in first.expressions] if isinstance(first, exp.Select) else []
        for position, column in enumerate(zip(*per_leaf)):
            known = _conflict(column)
            if known:
                label = names[position] if position < len(names) and names[position] else f"#{position + 1}"
                return f"set operation output {label} is {' and '.join(known)} in different branches"
    return None
