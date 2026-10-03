"""Shared sqlglot helpers used by rewrite rules and the equivalence prover."""

from __future__ import annotations

from contextlib import contextmanager
import logging

import sqlglot
from sqlglot import ErrorLevel, exp
from sqlglot.errors import UnsupportedError


class _UnknownSubqueryScope(logging.Filter):
    """sqlglot warns (with the SQL text) for every subquery lineage cannot scope; say it once, at debug, without the SQL."""

    def __init__(self) -> None:
        super().__init__()
        self.count = 0

    def filter(self, record: logging.LogRecord) -> bool:
        if not str(record.msg).startswith("Unknown subquery scope"):
            return True
        self.count += 1
        if self.count == 1:
            logging.getLogger("kumosql.lineage").debug("a subquery had no scope for lineage and was skipped (reported once)")
        return False


_scope_filter = _UnknownSubqueryScope()
logging.getLogger("sqlglot.lineage").addFilter(_scope_filter)


def is_function_table(table: exp.Table) -> bool:
    """``FROM dataset.fn(...)``: a table-valued function call, whose name is not a table."""

    return table.this is not None and isinstance(table.this, exp.Func)


@contextmanager
def quiet_parser():
    logger = logging.getLogger("sqlglot")
    previous = logger.level
    logger.setLevel(logging.CRITICAL)
    try:
        yield
    finally:
        logger.setLevel(previous)


def parse_statements(sql: str, *, recover: bool = False) -> list[exp.Expression]:
    """Parse BigQuery SQL, raising unless ``recover`` asks for recovery mode."""

    error_level = ErrorLevel.IGNORE if recover else ErrorLevel.RAISE
    with quiet_parser():
        parsed = sqlglot.parse(sql, read="bigquery", error_level=error_level)
    return [statement for statement in parsed if statement is not None]


def render_statement(statement: exp.Expression) -> str:
    """Render a transformed statement in the project's output style."""

    return statement.sql(dialect="bigquery", pretty=True, pad=4, identify=False)


_POSITION_META = ("line", "col", "start", "end")


def strip_positions(tree: exp.Expression) -> exp.Expression:
    """Drop the source positions the parser records on tokens, in place.

    A third of the nodes carry them and every ``copy()`` deep-copies them, which was about 30% of the
    prover's run time. Nothing in the equivalence pipeline reads positions; other ``meta`` keys are kept.
    """

    for node in tree.walk():
        meta = node._meta
        if meta:
            for key in _POSITION_META:
                meta.pop(key, None)
            if not meta:
                node._meta = None
    return tree


def canonical_negation(tree: exp.Expression) -> exp.Expression:
    """Spell ``x IS NOT NULL``, ``x NOT LIKE y`` and ``x NOT ILIKE y`` as ``NOT (...)``.

    Some dialects (PostgreSQL in current sqlglot releases) parse these as ``Is``, ``Like`` or
    ``ILike`` carrying ``negate=True``; code that reads only the node type would take them for the
    positive test. One spelling everywhere keeps the provers from reading ``IS NOT NULL`` as ``IS NULL``.
    """

    for node in list(tree.find_all(exp.Is, exp.Like, exp.ILike)):
        if node.args.get("negate"):
            positive = node.copy()
            positive.set("negate", None)
            if node is tree:
                return exp.Not(this=positive)
            node.replace(exp.Not(this=positive))
    return tree


class UnmodeledConstruct(UnsupportedError):
    """The query carries a sqlglot flag or clause that the provers do not model."""


# (node class, arg) pairs that change what a query returns and that neither prover reads. A query
# carrying one is declined, never proved with the flag ignored.
_UNMODELED_ARGS = (
    (exp.Between, "symmetric"),
    (exp.Cast, "format"),
    (exp.TryCast, "format"),
    (exp.Join, "match_condition"),
    (exp.Lateral, "ordinality"),
    (exp.Ordered, "with_fill"),
    (exp.Select, "exclude"),
    (exp.Table, "version"),
    (exp.Table, "system_time"),
    (exp.Table, "when"),
    (exp.Table, "partition"),
    (exp.Table, "changes"),
    (exp.Table, "rows_from"),
    (exp.Table, "only"),
    (exp.Table, "pattern"),
    (exp.Table, "ordinality"),
)


# sqlglot reads ``a = b IS TRUE`` as ``a = (b IS TRUE)``; BigQuery, MySQL, PostgreSQL, DuckDB and Calcite read
# ``(a = b) IS TRUE``. Written without parentheses the query is declined rather than proved under one reading.
_COMPARISONS = (exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE, exp.NullSafeEQ, exp.NullSafeNEQ)


def parenthesize_is_operands(tree: exp.Expression) -> exp.Expression:
    """Wrap an ``IS`` test that is a comparison's right operand in parentheses, as the tree means it."""

    for node in list(tree.find_all(*_COMPARISONS)):
        operand = node.expression
        if isinstance(operand, exp.Is) or (isinstance(operand, exp.Not) and isinstance(operand.this, exp.Is)):
            operand.replace(exp.Paren(this=operand.copy()))
    return tree


def table_function_reads_cte(tree: exp.Expression) -> bool:
    """Whether a table-valued function call is handed a CTE by name.

    DuckDB passes a table to a function by its bare name (``histogram_values(cte, l)``) and BigQuery
    with ``TABLE cte``. Such a read is not a table reference, so code that tracks CTE use by table
    references would think the CTE unused, drop it, and compare queries that read different rows.
    """

    names = {cte.alias.lower() for cte in tree.find_all(exp.CTE) if cte.alias}
    if not names:
        return False
    for table in tree.find_all(exp.Table):
        if isinstance(table.this, exp.Identifier) or table.this is None:
            continue
        for node in table.this.walk():
            if isinstance(node, exp.Column) and not node.table and node.name.lower() in names:
                return True
            if isinstance(node, exp.Table) and not node.db and node.name.lower() in names:
                return True
    return False


# sqlglot parses IS [NOT] DISTINCT FROM (NullSafeEQ, NullSafeNEQ) beside LIKE, IN and BETWEEN: tighter than ``=`` and ``<``,
# left to right, and prints the tree back without parentheses. PostgreSQL and DuckDB read it below all of those
# (``a = b IS NOT DISTINCT FROM c`` is ``(a = b) IS NOT DISTINCT FROM c``, ``a IS DISTINCT FROM b IN (c)`` is
# ``a IS DISTINCT FROM (b IN (c))``) and refuse to chain it with another IS; MySQL puts ``<=>``, ``=``, LIKE and IN on
# one level, left to right. GoogleSQL's grouping was not confirmed; declining holds under any of them.
_COMPARISON_LEVEL = (
    exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE, exp.NullSafeEQ, exp.NullSafeNEQ, exp.Is, exp.Like,
    exp.ILike, exp.SimilarTo, exp.Glob, exp.RegexpLike, exp.RegexpILike, exp.In, exp.Between,
)


def _comparison_operator(node: exp.Expression | None) -> bool:
    if isinstance(node, exp.Not):
        node = node.this
    return isinstance(node, _COMPARISON_LEVEL)


def check_distinct_from_grouping(tree: exp.Expression) -> None:
    """Decline IS [NOT] DISTINCT FROM next to another comparison without parentheses between them.

    Engines group such text differently from sqlglot's tree, so it is not proved under either reading.
    An IN list item is in parentheses already.
    """

    for node in tree.find_all(exp.NullSafeEQ, exp.NullSafeNEQ):
        parent = node.parent
        nested = isinstance(parent, _COMPARISON_LEVEL) and not (isinstance(parent, exp.In) and node.arg_key != "this")
        if nested or _comparison_operator(node.this) or _comparison_operator(node.expression):
            raise UnmodeledConstruct("IS DISTINCT FROM next to another comparison without parentheses reads differently across engines")


def check_modeled(tree: exp.Expression) -> exp.Expression:
    """Raise :class:`UnmodeledConstruct` for a flag the provers would silently ignore.

    Covers ``TABLESAMPLE``, time travel, ``SYMMETRIC`` ranges, ``WITH TIES`` and ``PERCENT``
    limits, ``OUTER APPLY`` and the like. sqlglot versions differ in which of these they parse, so
    anything carried on the node is refused whichever version produced it.
    """

    check_distinct_from_grouping(tree)
    for node in tree.walk():
        for kind, arg in _UNMODELED_ARGS:
            if isinstance(node, kind) and node.args.get(arg):
                raise UnmodeledConstruct(f"{kind.__name__}.{arg} is not modeled")
        if getattr(node, "arg_types", None) and "sample" in node.arg_types and node.args.get("sample"):
            raise UnmodeledConstruct(f"{type(node).__name__}.sample is not modeled")
        if isinstance(node, exp.Lateral) and node.args.get("cross_apply") is False:
            raise UnmodeledConstruct("OUTER APPLY is not modeled")
        if isinstance(node, exp.Fetch) and (node.args.get("percent") or node.args.get("with_ties")):
            raise UnmodeledConstruct("PERCENT and WITH TIES limits are not modeled")
        if type(node).__name__ == "LimitOptions" and (node.args.get("percent") or node.args.get("with_ties")):
            raise UnmodeledConstruct("PERCENT and WITH TIES limits are not modeled")
    for node in tree.find_all(*_COMPARISONS):
        operand = node.expression
        if isinstance(operand, exp.Is) or (isinstance(operand, exp.Not) and isinstance(operand.this, exp.Is)):
            raise UnmodeledConstruct("a comparison followed by IS without parentheses reads differently across engines")
    if table_function_reads_cte(tree):
        raise UnmodeledConstruct("a table function that reads a CTE by name is not modeled")
    return tree


def _output_names(query: exp.Expression) -> list[str] | None:
    """The column names a derived query outputs, or ``None`` when they are not plain and distinct."""

    while isinstance(query, (exp.Subquery, exp.SetOperation)):
        query = query.this
    if not isinstance(query, exp.Select):
        return None
    names = []
    for item in query.expressions:
        if isinstance(item, exp.Star) or (isinstance(item, exp.Column) and isinstance(item.this, exp.Star)):
            return None
        names.append(item.alias_or_name)
    if "" in names or len({n.lower() for n in names}) != len(names):
        return None
    return names


def expand_alias_columns(tree: exp.Expression, schema: dict[str, list[str]] | None) -> exp.Expression:
    """Rewrite ``FROM t AS d(a, b)`` as ``FROM (SELECT c1 AS a, c2 AS b FROM t) AS d``.

    A column list on a table alias renames the source's columns by position, so ``dept AS d(name, x)``
    calls ``deptno`` ``name``. The provers resolve columns by name and would read the original
    ``name``; some sqlglot dialects also drop the list when printing. The list is made explicit
    here, for tables (columns in ``schema`` order), CTEs and derived tables; a list that cannot be
    resolved (unknown table, star select, more names than columns) is declined.
    """

    lowered = {k.lower(): v for k, v in (schema or {}).items()}
    ctes = {}
    for cte in tree.find_all(exp.CTE):
        if cte.alias:
            ctes[cte.alias.lower()] = cte.this
    for alias in list(tree.find_all(exp.TableAlias)):
        renamed = [c.name for c in alias.args.get("columns") or []]
        source = alias.parent
        if not renamed or isinstance(source, exp.CTE):
            continue
        if not isinstance(source, (exp.Table, exp.Subquery)) or not isinstance(source.parent, (exp.From, exp.Join)):
            continue
        if isinstance(source, exp.Table):
            if not isinstance(source.this, exp.Identifier):
                raise UnmodeledConstruct("a column list on a table function alias is not modeled")
            key = ".".join(p.name for p in source.parts).lower()
            if not source.db and key in ctes:
                columns = _output_names(ctes[key])
            else:
                columns = lowered.get(key) or (lowered.get(source.name.lower()) if source.db else None)
        else:
            columns = _output_names(source.this)
        if not columns or len(renamed) > len(columns):
            raise UnmodeledConstruct("a column list on a table alias could not be resolved")
        inner_alias = "kq_renamed"
        items = [
            exp.alias_(exp.column(old, table=inner_alias), new if index < len(renamed) else old)
            for index, (old, new) in enumerate(zip(columns, renamed + list(columns[len(renamed):])))
        ]
        if isinstance(source, exp.Table):
            inner = source.copy()
            inner.set("alias", exp.TableAlias(this=exp.to_identifier(inner_alias)))
        else:
            inner = exp.Subquery(this=source.this.copy(), alias=exp.TableAlias(this=exp.to_identifier(inner_alias)))
        body = exp.select(*items).from_(inner)
        replacement = exp.Subquery(this=body, alias=exp.TableAlias(this=alias.this.copy()))
        if source is tree:
            return replacement
        source.replace(replacement)
    return tree


class LossySql(UnmodeledConstruct):
    """Printing a rewritten query in its dialect would change what it means, so it is declined."""


# Arguments that only record spelling or what the parser inferred about a type, not what is computed.
_IGNORED_ARGS = frozenset({"quoted", "comments", "big_int"})


def _shape(node: object) -> object:
    """A comparable form of a tree that ignores what does not change its meaning.

    Parentheses (the tree's own nesting already says how operators group), identifier quoting,
    comments and the case of names are left out; string literals keep their case.
    """

    if isinstance(node, exp.Paren) or (isinstance(node, exp.Subquery) and set(k for k, v in node.args.items() if v) == {"this"}):
        return _shape(node.this)  # grouping only: the tree's nesting already says how things group
    if isinstance(node, exp.Connector):
        # AND and OR are associative: a AND (b AND c) prints as a AND b AND c
        return (node.key, tuple(_shape(o) for o in _connected(node, type(node))))
    if isinstance(node, (exp.Union, exp.Intersect)) and not _modified(node):
        # UNION ALL, UNION and INTERSECT are associative, so a chain prints without inner parentheses
        return (node.key, bool(node.args.get("distinct")), tuple(_shape(o) for o in _chain(node, type(node), node.args.get("distinct"))))
    if isinstance(node, exp.DataType):
        # the provers read a type by its BigQuery spelling, so INT and SIGNED, or VARCHAR and CHAR, are one type
        return ("datatype", node.sql(dialect="bigquery").upper())
    if isinstance(node, (exp.LogicalAnd, exp.LogicalOr)):
        # MIN and MAX of booleans are LOGICAL_AND and LOGICAL_OR; MySQL spells them that way
        return (exp.Min.key if isinstance(node, exp.LogicalAnd) else exp.Max.key, (("this", _shape(node.this)),))
    if isinstance(node, exp.Fetch):
        return ("limit", (("expression", _shape(node.args.get("count"))),))
    if isinstance(node, exp.Limit) and not node.args.get("offset") and not node.args.get("expressions"):
        return ("limit", (("expression", _shape(node.args.get("expression"))),))
    if isinstance(node, list):
        return tuple(_shape(x) for x in node if x is not None and x is not False)
    if isinstance(node, exp.Expression):
        literal = isinstance(node, exp.Literal)
        items = []
        for key in sorted(node.args):
            if key in _IGNORED_ARGS or (isinstance(node, exp.Identifier) and key == "global_"):
                continue
            if isinstance(node, exp.Concat) and key == "safe":
                continue  # whether CONCAT raises on non-string arguments; errors are not modeled
            if isinstance(node, exp.Join) and key == "kind" and str(node.args[key]).upper() == "CROSS" and not node.args.get("on"):
                continue  # ", t" and "CROSS JOIN t" are one join
            value = node.args[key]
            if value is None or value is False or value == []:
                continue
            if isinstance(value, str) and not literal:
                value = value.lower()
            items.append((key, _shape(value) if isinstance(value, (exp.Expression, list)) else value))
        return (node.key, tuple(items))
    return node


def _modified(node: exp.Expression) -> bool:
    return any(node.args.get(k) for k in ("order", "limit", "offset", "with_", "with", "by_name", "side", "kind", "on"))


def _connected(node: exp.Expression, kind: type) -> list[exp.Expression]:
    while isinstance(node, exp.Paren):
        node = node.this
    if type(node) is kind:
        return _connected(node.this, kind) + _connected(node.expression, kind)
    return [node]


def _chain(node: exp.Expression, kind: type, distinct) -> list[exp.Expression]:
    """The operands of a chain of one associative set operation, through grouping parentheses."""

    while isinstance(node, exp.Subquery) and set(k for k, v in node.args.items() if v) == {"this"}:
        node = node.this
    if type(node) is kind and bool(node.args.get("distinct")) == bool(distinct) and not _modified(node):
        return _chain(node.this, kind, distinct) + _chain(node.expression, kind, distinct)
    return [node]


# Other dialects whose printing a dialect's parser reads back, tried when its own printing does not round
# trip. MySQL's generator, for example, spells FULL JOIN as a LEFT JOIN UNION ALL a RIGHT JOIN (wrong under
# an aggregate), a DIV b as CAST(a / b AS SIGNED) (which rounds where DIV truncates) and CAST(x AS BOOLEAN)
# as an integer cast; Spark's prints all three as written, with the same quoting. (A generator subclass
# would do it directly, but a compiled sqlglot does not allow one.)
_NEIGHBOURS = ("spark", "", "duckdb", "postgres")


def faithful_sql(tree: exp.Expression, dialect: str) -> str:
    """Print ``tree`` as ``dialect`` SQL that parses back to the same query, or raise :class:`LossySql`.

    The provers hand rewritten queries to each other as text. sqlglot's generators rewrite some
    constructs for the target engine, and a few of those rewrites change the result, so the text is
    parsed back and compared with the tree before anyone reasons about it.
    """

    expected = _shape(tree)
    writers = [dialect] + [d for d in _NEIGHBOURS if d != dialect]
    # identify=True quotes every name, e.g. an unquoted BigQuery project name with a hyphen
    for writer, identify in [(dialect, False), (dialect, True)] + [(d, False) for d in writers[1:]]:
        try:
            with quiet_parser():
                text = tree.sql(dialect=writer, identify=identify)
                back = canonical_negation(sqlglot.parse_one(text, read=dialect))
        except sqlglot.errors.SqlglotError:
            continue
        if _shape(back) == expected:
            return text
    raise LossySql(f"the query cannot be printed faithfully in {dialect or 'the default dialect'}")


def identifier_name(node: exp.Expression | None) -> str | None:
    if node is None:
        return None
    value = getattr(node, "name", None)
    if value:
        return value
    value = getattr(node, "this", None)
    return value if isinstance(value, str) else None


def conjuncts(node: exp.Expression) -> list[exp.Expression]:
    """The AND-parts of a predicate, with the parentheses around each part removed."""

    while isinstance(node, exp.Paren):
        node = node.this
    if isinstance(node, exp.And):
        return conjuncts(node.this) + conjuncts(node.expression)
    return [node]


def inside(node: exp.Expression, ancestor: exp.Expression) -> bool:
    """Whether ``ancestor`` is a strict ancestor of ``node``."""

    parent = node.parent
    while parent is not None:
        if parent is ancestor:
            return True
        parent = parent.parent
    return False


def select_sources(select: exp.Select) -> list[exp.Expression]:
    """The relations in a SELECT's FROM clause followed by its JOINs."""

    from_ = select.args.get("from_") or select.args.get("from")
    return ([from_.this] if from_ is not None else []) + [join.this for join in select.args.get("joins") or []]


def table_parts(table: exp.Table) -> list[str]:
    """Lower-case catalog, dataset and table names of a table reference, skipping empty parts."""

    return [p.name.lower() for p in (table.args.get("catalog"), table.args.get("db"), table.this) if p is not None and p.name]


def with_arg_key(node: exp.Expression) -> str:
    """Return the sqlglot WITH argument name across supported versions."""

    args = getattr(node, "args", {})
    arg_types = getattr(node, "arg_types", {})
    if "with_" in args or "with_" in arg_types:
        return "with_"
    if "with" in args or "with" in arg_types:
        return "with"
    # Current sqlglot uses ``with_``. This fallback keeps construction
    # compatible with expression implementations that expose neither key until
    # the first value is assigned.
    return "with_"


def top_level_query(statement: exp.Expression) -> exp.Expression | None:
    if isinstance(statement, (exp.Create, exp.Insert)):
        candidate = statement.expression
        while isinstance(candidate, exp.Subquery):
            candidate = candidate.this
        return candidate
    if isinstance(statement, (exp.Select, exp.Union, exp.Update, exp.Delete, exp.Merge)):
        return statement
    return None


def with_clause(query: exp.Expression) -> exp.With | None:
    return query.args.get(with_arg_key(query)) if hasattr(query, "args") else None


def set_with_clause(query: exp.Expression, clause: exp.With | None) -> None:
    query.set(with_arg_key(query), clause)


def cte_alias_name(cte: exp.CTE) -> str | None:
    return identifier_name(cte.args.get("alias"))


def is_cte_reference_candidate(table: exp.Table) -> bool:
    """Whether a table reference is one-part and can therefore name a CTE."""

    return not table.db and not table.catalog


def nearest_parent_cte(node: exp.Expression) -> exp.CTE | None:
    parent = node.parent
    while parent is not None:
        if isinstance(parent, exp.CTE):
            return parent
        parent = parent.parent
    return None


def nearest_root_cte(node: exp.Expression, root_ctes: list[exp.CTE]) -> exp.CTE | None:
    """Find the direct root CTE containing a node, including nested WITHs."""

    parent = node.parent
    while parent is not None:
        if isinstance(parent, exp.CTE) and any(parent is cte for cte in root_ctes):
            return parent
        parent = parent.parent
    return None


def has_nested_with(query: exp.Expression) -> bool:
    root = with_clause(query)
    return any(isinstance(node, exp.With) and node is not root for node in query.walk())


def ambiguous_unnest_names(query: exp.Expression) -> set[str]:
    """Names of relations that also appear as a bare ``UNNEST`` argument.

    Older sqlglot releases (26.x) parse a repeated relation such as
    ``FROM shared JOIN shared AS s2`` as ``JOIN UNNEST(shared) AS s2``, so the
    AST no longer shows the second reference. A bare column inside UNNEST that
    matches a CTE or table name is therefore treated as a hidden reference.
    """

    names = {
        cte_alias_name(cte).lower()
        for cte in query.find_all(exp.CTE)
        if cte_alias_name(cte)
    }
    for table in query.find_all(exp.Table):
        if is_cte_reference_candidate(table):
            names.add(table.name.lower())
            if table.alias:
                names.add(table.alias.lower())
    hidden: set[str] = set()
    for unnest in query.find_all(exp.Unnest):
        for expression in unnest.expressions:
            if isinstance(expression, exp.Column) and not expression.table:
                if expression.name.lower() in names:
                    hidden.add(expression.name.lower())
    return hidden


def cte_dependency_errors(statement: exp.Expression) -> list[str]:
    """Find undefined or forward references in a statement's root CTE list."""

    query = top_level_query(statement)
    if query is None:
        return []
    clause = with_clause(query)
    if not clause:
        return []

    ctes = list(clause.expressions)
    positions = {
        name: index
        for index, cte in enumerate(ctes)
        if (name := cte_alias_name(cte))
    }
    recursive = bool(clause.args.get("recursive"))
    errors: list[str] = []

    for table in query.find_all(exp.Table):
        name = table.name
        if not is_cte_reference_candidate(table):
            continue
        if name.startswith("__lifted_subquery_") and name not in positions:
            errors.append(f"lifted CTE `{name}` is referenced but not defined")
            continue
        if name not in positions:
            continue

        owner = nearest_parent_cte(table)
        if owner is None:
            continue
        owner_name = cte_alias_name(owner)
        if owner_name is None or (name == owner_name and recursive):
            continue
        if positions[name] >= positions.get(owner_name, len(ctes)):
            errors.append(
                f"CTE `{name}` is referenced before it is defined by `{owner_name}`"
            )

    return list(dict.fromkeys(errors))
