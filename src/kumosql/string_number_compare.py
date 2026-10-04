"""Decline to prove queries that compare a string with a number.

The provers read a value as a number, a string or a Boolean and call values of different kinds
unequal, so ``WHERE '2' <> 2`` filters nothing out and the query is proven equal to one without the
filter. No engine reads it that way:

- MySQL compares a string with a number as numbers (``'2' = 2`` is true, ``'abc' = 0`` is true);
- DuckDB and PostgreSQL cast the string to the number's type (``'2' = 2`` is true, ``'abc' = 2`` is an error);
- BigQuery rejects the comparison as a type error, so the query cannot run.

:func:`problem` names the first such comparison so the prover returns "not proven". It looks at
equality, ordering, ``BETWEEN``, ``IN`` lists, simple ``CASE`` operands and ``NULLIF``, comparing

- a string literal with a number literal, arithmetic, a numeric ``CAST``, a ``COUNT`` or a column declared numeric;
- a number literal (or the other numeric forms) with a column declared as a string;
- one column compared with a string literal in one place and a number literal in another (``a = 'abc' AND a = 0``
  is not empty on MySQL when ``a`` is an integer column, since ``'abc'`` reads as 0).

The SMT prover reads a plain comparison of that kind as an opaque predicate of its two values, true or false whatever the
engine's conversion rule is, so a pair that uses one the same way on both sides can still be proven; :func:`problem` with
``plain_ok`` reports only the other forms and the same-column case. Nothing is folded, even where the engine's reading is exact (MySQL ``'2' = 2``).

The kind of a column the schema does not declare is inferred from the query (:class:`_Kinds`): a column compared with a
string literal reads as a string and one compared with a number as a number, columns equated by a join, ``USING``, ``IN``
or a derived table's projection share a kind, and a value that mixes kinds (``COALESCE(n, 'x')``, ``CASE`` branches,
``GREATEST``) is a string and a number at once. A comparison that sets one kind against the other, a column that
gathers both kinds, or a mixed value used as a comparison or arithmetic operand is declined, since the SMT prover
cannot tell the kinds apart for a column it has no type for and calls a string and a number unequal.
"""

from __future__ import annotations

import re

import sqlglot
from sqlglot import exp

_INTEGER = ("TINYINT", "SMALLINT", "MEDIUMINT", "INT", "INTEGER", "BIGINT", "INT64", "BYTEINT", "HUGEINT", "UTINYINT", "USMALLINT", "UINTEGER", "UBIGINT", "INT2", "INT4", "INT8")
_FLOAT = ("FLOAT", "FLOAT64", "DOUBLE", "REAL", "FLOAT4", "FLOAT8")
_DECIMAL = ("NUMERIC", "DECIMAL", "BIGNUMERIC", "BIGDECIMAL", "DEC", "NUMBER")
_STRING = ("STRING", "VARCHAR", "TEXT", "CHAR", "NVARCHAR", "NCHAR", "BPCHAR")
_ARITHMETIC = (exp.Add, exp.Sub, exp.Mul, exp.Div, exp.Mod, exp.IntDiv)
_NUMBER_NODES = _ARITHMETIC + (exp.Count, exp.Sum, exp.Avg, exp.Abs, exp.Round, exp.Floor, exp.Ceil, exp.Length, exp.Sqrt, exp.Pow, exp.Stddev, exp.Variance)
_STRING_NODES = tuple(getattr(exp, name) for name in ("Lower", "Upper", "Concat", "ConcatWs", "Substring", "Trim", "Initcap", "Replace", "Repeat", "Reverse"))
_MIXING = (exp.Coalesce, exp.Greatest, exp.Least, exp.If, exp.Case, exp.Nullif)
_COMPARISONS = (exp.EQ, exp.NEQ, exp.LT, exp.LTE, exp.GT, exp.GTE, exp.NullSafeEQ, exp.NullSafeNEQ)


def _type_kind(type_sql: str) -> str | None:
    base = re.split(r"[\s(<]", type_sql.strip().upper(), maxsplit=1)[0]
    if base in _INTEGER + _FLOAT + _DECIMAL:
        return "number"
    if base in _STRING:
        return "string"
    return None


def is_integer_type(type_sql: str) -> bool:
    """Whether a declared type is one of the integer types."""

    return re.split(r"[\s(<]", type_sql.strip().upper(), maxsplit=1)[0] in _INTEGER


def _data_type_kind(data_type: object) -> str | None:
    return _type_kind(data_type.sql()) if isinstance(data_type, exp.DataType) else None


def _column_kinds(types: dict[str, dict[str, str]] | None) -> dict[str, str]:
    """Declared kind of each column name that has one kind in every declared table."""

    kinds: dict[str, set[str | None]] = {}
    for columns in (types or {}).values():
        for name, type_sql in columns.items():
            kinds.setdefault(name.lower(), set()).add(_type_kind(type_sql))
    return {name: found.pop() for name, found in kinds.items() if len(found) == 1 and None not in found}


def _kind(node: exp.Expression, columns: dict[str, str]) -> str | None:
    """``"number"``, ``"string"`` or None (not known) for an operand."""

    while isinstance(node, (exp.Paren, exp.Neg, exp.Alias)):
        node = node.this
    if isinstance(node, exp.Literal):
        return "string" if node.is_string else "number"
    if isinstance(node, exp.Cast):
        return _data_type_kind(node.args.get("to"))
    if isinstance(node, _NUMBER_NODES):
        return "number"
    if isinstance(node, exp.Column):
        return columns.get(node.name.lower())
    return None


def _pairs(tree: exp.Expression):
    """``(left, right)`` operand pairs of every comparison in the tree."""

    for node in tree.walk():
        if isinstance(node, _COMPARISONS):
            yield node.this, node.expression
        elif isinstance(node, exp.Between):
            yield node.this, node.args["low"]
            yield node.this, node.args["high"]
        elif isinstance(node, exp.In):
            if node.args.get("query") is None:
                for item in node.expressions:
                    yield node.this, item
            else:
                inner = node.args["query"]
                inner = inner.this if isinstance(inner, exp.Subquery) else inner
                if isinstance(inner, exp.Query) and inner.selects:
                    yield node.this, inner.selects[0]
        elif isinstance(node, exp.Case) and node.args.get("this") is not None:
            for branch in node.args.get("ifs") or []:
                yield node.args["this"], branch.this
        elif isinstance(node, exp.Nullif):
            yield node.this, node.expression


def mismatched(node: exp.Expression, types: dict[str, dict[str, str]] | None = None) -> bool:
    """Whether a plain comparison (``=``, ``<>``, ``<``, ``<=``, ``>``, ``>=``, ``<=>``) sets a string against a number."""

    return isinstance(node, _COMPARISONS) and {_kind(node.this, _column_kinds(types)), _kind(node.expression, _column_kinds(types))} == {"string", "number"}


def _unwrap(node: exp.Expression) -> exp.Expression:
    while isinstance(node, (exp.Paren, exp.Alias)):
        node = node.this
    return node


class _Kinds:
    """The kinds (``"string"``, ``"number"``) each column of a query can hold, from the query's own comparisons.

    Columns are keyed by ``(qualifier, name)``. A comparison between two columns, a ``USING`` list, an ``IN`` subquery
    and the projection of a derived table or CTE join their keys into one class; a comparison with a literal or another
    expression adds that expression's kinds to the class. Declared kinds join in only for a class with a column the
    schema leaves undeclared, since the SMT prover reads a comparison between two declared columns of different kinds
    as an unknown function and needs no help there.
    """

    def __init__(self, tree: exp.Expression, types: dict[str, dict[str, str]] | None):
        self.tree = tree
        self.types = {name.lower().split(".")[-1]: {c.lower(): t for c, t in cols.items()} for name, cols in (types or {}).items()}
        self.by_name = _column_kinds(types)
        self.parent: dict[tuple[str, str], tuple[str, str]] = {}
        self.evidence: dict[tuple[str, str], set[str]] = {}
        self.tables: dict[str, set[str]] = {}
        self.derived: dict[str, exp.Query] = {}
        self._collect_sources()
        self._build()

    def _collect_sources(self) -> None:
        for node in self.tree.walk():
            if isinstance(node, exp.Table) and node.name:
                self.tables.setdefault((node.alias or node.name).lower(), set()).add(node.name.lower())
            elif isinstance(node, exp.CTE) and node.alias and isinstance(node.this, exp.Query):
                self.derived[node.alias.lower()] = node.this
            elif isinstance(node, exp.Subquery) and node.alias and isinstance(node.this, exp.Query):
                self.derived[node.alias.lower()] = node.this

    def key(self, column: exp.Column) -> tuple[str, str]:
        return (column.table.lower(), column.name.lower())

    def find(self, key: tuple[str, str]) -> tuple[str, str]:
        self.parent.setdefault(key, key)
        while self.parent[key] != key:
            self.parent[key] = self.parent[self.parent[key]]
            key = self.parent[key]
        return key

    def union(self, a: tuple[str, str], b: tuple[str, str]) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[ra] = rb
            self.evidence.setdefault(rb, set()).update(self.evidence.pop(ra, set()))

    def add(self, key: tuple[str, str], kinds: set[str]) -> None:
        self.evidence.setdefault(self.find(key), set()).update(kinds)

    def declared(self, key: tuple[str, str]) -> str | None:
        qualifier, name = key
        found = self.tables.get(qualifier) if qualifier else None
        if found and all(t in self.types for t in found):
            kinds = {_type_kind(self.types[t][name]) for t in found if name in self.types[t]}
            return kinds.pop() if len(kinds) == 1 else None
        return None if qualifier in self.derived else self.by_name.get(name)

    def kinds(self, node: exp.Expression, depth: int = 0) -> set[str]:
        """Kinds ``node`` can hold; empty when nothing is known."""

        node = _unwrap(node)
        while isinstance(node, exp.Neg):
            node = _unwrap(node.this)
        if depth > 6:
            return set()
        if isinstance(node, exp.Literal):
            return {"string" if node.is_string else "number"}
        if isinstance(node, exp.Cast):
            kind = _data_type_kind(node.args.get("to"))
            return {kind} if kind else set()
        if isinstance(node, _NUMBER_NODES):
            return {"number"}
        if isinstance(node, _STRING_NODES):
            return {"string"}
        if isinstance(node, exp.Column):
            key = self.key(node)
            found = set(self.evidence.get(self.find(key), set()))
            kind = self.declared(key)
            return found | ({kind} if kind else set())
        if isinstance(node, (exp.Coalesce, exp.Greatest, exp.Least)):
            return set().union(*(self.kinds(a, depth + 1) for a in [node.this, *node.expressions]))
        if isinstance(node, exp.If) and node.args.get("true") is not None:
            return self.kinds(node.args["true"], depth + 1) | (self.kinds(node.args["false"], depth + 1) if node.args.get("false") is not None else set())
        if isinstance(node, exp.Case):
            branches = [branch.args["true"] for branch in node.args.get("ifs") or [] if branch.args.get("true") is not None]
            if node.args.get("default") is not None:
                branches.append(node.args["default"])
            return set().union(*(self.kinds(b, depth + 1) for b in branches))
        if isinstance(node, (exp.Nullif, exp.Max, exp.Min, exp.AnyValue)):
            return self.kinds(node.this, depth + 1)
        if isinstance(node, (exp.Subquery, exp.Select)):
            inner = node.this if isinstance(node, exp.Subquery) else node
            return self.kinds(inner.selects[0], depth + 1) if isinstance(inner, exp.Query) and len(inner.selects) == 1 else set()
        return set()

    def _build(self) -> None:
        for _ in range(3):  # a kind found late flows to the expressions built on it
            self._projections()
            self._comparisons()
            self._using()

    def _projections(self) -> None:
        for alias, query in self.derived.items():
            for name, expr in zip(query.named_selects, query.selects):
                inner = _unwrap(expr)
                if isinstance(inner, exp.Column):
                    self.union((alias, name.lower()), self.key(inner))
                else:
                    self.add((alias, name.lower()), self.kinds(inner))

    def _comparisons(self) -> None:
        for left, right in _pairs(self.tree):
            self._relate(_unwrap(left), _unwrap(right))
            self._relate(_unwrap(right), _unwrap(left))

    def _relate(self, column: exp.Expression, other: exp.Expression) -> None:
        if not isinstance(column, exp.Column):
            return
        key = self.key(column)
        if isinstance(other, exp.Column):
            # two declared columns of different kinds are the SMT prover's opaque comparison, not an inferred one
            if not (self.declared(key) and self.declared(self.key(other))):
                self.union(key, self.key(other))
        else:
            self.add(key, self.kinds(other))

    def _using(self) -> None:
        for select in self.tree.find_all(exp.Select):
            joins = select.args.get("joins") or []
            source = select.args.get("from_") or select.args.get("from")
            names = [source.this if source else None] + [j.this for j in joins]
            aliases = [(n.alias or n.name).lower() for n in names if isinstance(n, (exp.Table, exp.Subquery))]
            for join in joins:
                for ident in join.args.get("using") or []:
                    name = ident.name.lower()
                    for other in aliases[1:]:
                        self.union((aliases[0], name), (other, name))


def problem(sql: str, dialect: str = "bigquery", types: dict[str, dict[str, str]] | None = None, plain_ok: bool = False) -> str | None:
    """A description of the first string-versus-number comparison in ``sql``, else None.

    With ``plain_ok`` a plain two-operand comparison of operands whose kinds the schema or the text settles is not reported:
    the SMT prover reads it as an opaque predicate of its operands (:meth:`kumosql.smt_equivalence._Compiler._compare`),
    which is right for any conversion rule.
    """

    try:
        tree = sqlglot.parse_one(sql, read=dialect)
    except Exception:  # noqa: BLE001 - the prover reports the parse error itself
        return None
    columns = _column_kinds(types)
    kinds = _Kinds(tree, types)
    literal_kinds: dict[str, set[str]] = {}
    for left, right in _pairs(tree):
        sides = {_kind(left, columns), _kind(right, columns)}
        plain = plain_ok and isinstance(left.parent, _COMPARISONS)
        if sides == {"string", "number"} and not plain:
            return "a string is compared with a number (the engines convert the string; the prover does not model it)"
        found = kinds.kinds(left) | kinds.kinds(right)
        if found == {"string", "number"} and not (plain and None not in sides):
            return "a string is compared with a number (the engines convert the string; the prover does not model it)"
        for column, other in ((left, right), (right, left)):
            if isinstance(column, exp.Column) and isinstance(other, exp.Literal):
                literal_kinds.setdefault(column.sql().lower(), set()).add("string" if other.is_string else "number")
    for name, found in literal_kinds.items():
        if len(found) > 1:
            return f"column {name} is compared with both a string and a number (the engines convert the string; the prover does not model it)"
    for node in tree.walk():
        if isinstance(node, _MIXING):
            if kinds.kinds(node) == {"string", "number"} and _is_operand(node):
                return "a value of mixed kinds (a string in one branch, a number in another) is compared or used in arithmetic (the engines convert it; the prover does not model it)"
    return None


def _is_operand(node: exp.Expression) -> bool:
    """Whether ``node`` is read as a value by a comparison, arithmetic or an aggregate that adds."""

    parent = node.parent
    while isinstance(parent, (exp.Paren, exp.Neg)):
        node, parent = parent, parent.parent
    if isinstance(parent, exp.Case):
        return parent.this is node
    return isinstance(parent, _COMPARISONS + _ARITHMETIC + (exp.Between, exp.In, exp.Sum, exp.Avg))
