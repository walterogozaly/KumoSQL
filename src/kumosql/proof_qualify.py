"""Independent check of column qualification: ``qualify_columns`` rewrites ``x`` into ``a.x``.

The rule decides which FROM item owns a bare column, and the prover's own column resolution (the algebraic
and SMT provers qualify bare columns with a schema) makes a similar decision. A mistake in how either picks the
owner, and so in what counts as a select alias, a struct field, a correlated reference or a name two sources
share, would be repeated on both sides of a proof. This module re-derives the claim from the step's before
and after statements alone and imports no rule, normalizer, lifter or prover (only the record types of
``proof_steps`` and the re-parse helper of ``proof_syntax``).

A step is accepted only when both hold.

* **Only qualifiers were added.** Walking the two trees together, they are identical node for node except
  that some columns, bare before, carry a table qualifier after. Nothing else changed (not a name, a
  projection, an alias, a clause or a source), so a change hidden inside the step is a refusal.
* **Each added qualifier names the one source that has the column.** For every column that gained a qualifier,
  re-derived from the before statement:

  - its select reads one relation per FROM item, each with a known set of columns: a CTE or derived table that
    lists names (not ``*``, an unnamed expression, a duplicate name, ``SELECT AS VALUE`` or ``AS STRUCT``), a
    physical table whose columns the acceptance layer supplied, or an aliased ``UNNEST``. Anything else
    (a table function, PIVOT, a snapshot, a NATURAL join, a recursive CTE, a path such as ``a.b``) refuses the
    step, as do two sources with one name;
  - exactly one source the column can read has the name, and the qualifier is that source's alias (or its table
    name when it has none). A JOIN ... ON reads the sources up to and including its own join, and an ``UNNEST``
    argument reads only the sources before it: a later source's column of the same name is an outer column
    there (checked on BigQuery), so a qualifier naming the later source changes the query;
  - the name is not a USING column (the bare name is the merged value), not the name of a source or an
    ``UNNEST`` element or offset, and not an output name that GROUP BY, HAVING, QUALIFY, ORDER BY or a named
    window would read first (a select alias, a ``* REPLACE`` name, the field of ``s.f``: BigQuery prefers them
    there, checked on BigQuery; in WHERE, ON and the select list they are not visible);
  - it is not inside a star's ``EXCEPT``, ``REPLACE`` or ``RENAME``, a lambda, a PIVOT, a set operation's own ORDER
    BY or LIMIT or a parenthesized query's, and not the argument of a function while spelled like a date part
    (sqlglot reads ``MONTH`` in ``FOO(d, MONTH)`` as a column).

A correlated reference to an outer select has no owner among the inner sources, so it is refused, and so is a
struct field with no source column of that name. Both statements are read again from the step's text, never from
the caller's trees. A failed check, including an error in the checker, refuses the step: it never guesses.

Assumptions the check does not establish: the physical table's column lists are complete and current (they come from
the loaded project and saved catalog, not from this module), and the original statement runs on BigQuery (an
ambiguous name the engine would reject, such as a field of an ``UNNEST`` element that a source also has, is not
looked for).
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlglot import exp

from .proof_steps import RewriteStep, StepCheck
from .proof_syntax import _reparse

QUALIFY_FAMILY = "qualify_columns"
QUALIFY_ASSUMPTIONS = (
    "only_qualifiers_added",
    "qualifier_names_the_only_readable_source_with_the_column",
    "source_columns_known_exactly",
    "output_names_and_value_names_stay_bare",
    "supplied_table_columns_complete_and_input_valid",
)

_DATE_PARTS = frozenset({
    "MICROSECOND", "MILLISECOND", "NANOSECOND", "SECOND", "MINUTE", "HOUR", "DAY", "DAYOFWEEK", "DAYOFYEAR", "WEEK",
    "ISOWEEK", "MONTH", "QUARTER", "YEAR", "ISOYEAR", "DATE", "TIME", "DATETIME", "DECADE", "CENTURY", "MILLENNIUM",
})
_NAME_CLAUSES = ("group", "having", "qualify", "order", "windows")
_PLAIN_CLAUSES = ("expressions", "where")
_HARMLESS_TABLE_ARGS = frozenset({"this", "alias", "db", "catalog", "comments", "version", "sample", "hints"})


class _Rejected(ValueError):
    pass


# --- 1. only qualifiers were added ----------------------------------------------------------------------

def _empty(value) -> bool:
    return value is None or value == []


def _walk(old, new, gained: list, path: str, skip: str = "") -> None:
    """Compare two trees node for node; collect the columns that only gained a qualifier."""

    if isinstance(old, exp.Expression):
        if not isinstance(new, exp.Expression) or type(old) is not type(new):
            raise _Rejected(f"the statements differ at {path}")
        if isinstance(old, exp.Column) and _empty(old.args.get("table")) and not _empty(new.args.get("table")):
            if not isinstance(new.args["table"], exp.Identifier) or not isinstance(old.args.get("this"), exp.Identifier):
                raise _Rejected(f"a qualifier was added to something other than a plain column at {path}")
            if any(not _empty(column.args.get(part)) for column in (old, new) for part in ("db", "catalog")):
                raise _Rejected(f"a qualified column gained a database or catalog part at {path}")
            gained.append((old, new))
            skip = "table"
        for name in sorted(set(old.args) | set(new.args)):
            if name == "comments" or name == skip:
                continue
            _walk(old.args.get(name), new.args.get(name), gained, f"{path}.{name}")
        return
    if isinstance(old, list) or isinstance(new, list):
        a, b = old or [], new or []
        if not isinstance(a, list) or not isinstance(b, list) or len(a) != len(b):
            raise _Rejected(f"the statements differ at {path}")
        for index, (x, y) in enumerate(zip(a, b)):
            _walk(x, y, gained, f"{path}[{index}]")
        return
    if _empty(old) and _empty(new):
        return
    if old != new or type(old) is not type(new):
        raise _Rejected(f"the statements differ at {path}")


# --- 2. what each select reads ---------------------------------------------------------------------------

@dataclass(frozen=True)
class _Source:
    name: str
    columns: frozenset[str]
    reserved: frozenset[str] = frozenset()


def _names_of_query(query) -> list[str] | None:
    """The output column names of a CTE or derived table, or ``None`` when they are not all certain."""

    while isinstance(query, exp.Subquery):
        query = query.this
    if isinstance(query, exp.SetOperation):
        if any(query.args.get(key) for key in ("by_name", "on", "side", "kind")):
            return None
        return _names_of_query(query.this)
    if not isinstance(query, exp.Select) or query.args.get("kind"):
        return None
    names: list[str] = []
    for item in query.expressions:
        if isinstance(item, exp.Alias):
            name = item.alias
        elif isinstance(item, exp.Column) and not isinstance(item.this, exp.Star):
            name = item.name
        else:
            return None
        if not name:
            return None
        names.append(name.lower())
    return names if len(set(names)) == len(names) else None


def _cte_named(table: exp.Table) -> exp.CTE | None:
    """The CTE a name in FROM means; ``None`` when it is a physical table. Refuses what it cannot settle."""

    first = table.args.get("catalog") or table.args.get("db") or table.this
    wanted = first.name.lower()
    multipart = table.args.get("db") is not None or table.args.get("catalog") is not None
    child, parent = table, table.parent
    while parent is not None:
        if isinstance(parent, exp.With):
            clause, siblings = parent, list(parent.expressions)
            position = next((i for i, cte in enumerate(siblings) if cte is child), len(siblings))
            visible = siblings[:position]
        else:
            clause = next((v for v in parent.args.values() if isinstance(v, exp.With) and v is not child), None)
            siblings = list(clause.expressions) if clause is not None else []
            visible = siblings
        if clause is not None:
            named = [cte for cte in siblings if cte.alias_or_name.lower() == wanted]
            if clause.args.get("recursive") and named:
                raise _Rejected(f"{wanted!r} is defined by a recursive WITH")
            found = [cte for cte in visible if cte.alias_or_name.lower() == wanted]
            if len(named) > 1:
                raise _Rejected(f"the CTE name {wanted!r} is defined twice")
            if found:
                if multipart:
                    raise _Rejected(f"{wanted!r} is a CTE read as a path")
                return found[0]
        child, parent = parent, parent.parent
    return None


def _table_source(table: exp.Table, known: dict[str, frozenset[str]]) -> _Source:
    carried = {key for key, value in table.args.items() if not _empty(value) and value is not False}
    if carried - _HARMLESS_TABLE_ARGS:
        raise _Rejected(f"a table carries {sorted(carried - _HARMLESS_TABLE_ARGS)[0]}, which can change its columns")
    if not isinstance(table.this, exp.Identifier):
        raise _Rejected("a FROM item is a table function")
    alias = table.args.get("alias")
    if alias is not None and (alias.args.get("columns") or alias.this is None):
        raise _Rejected("a table alias lists columns")
    name = (alias.this.name if alias is not None else table.this.name).lower()
    cte = _cte_named(table)
    if cte is not None:
        listed = cte.args["alias"].args.get("columns") if cte.args.get("alias") is not None else None
        names = [c.name.lower() for c in listed] if listed else _names_of_query(cte.this)
        if names is None:
            raise _Rejected(f"the columns of the CTE {table.name!r} are not all named")
        return _Source(name, frozenset(names))
    parts = [p.name.lower() for p in (table.args.get("catalog"), table.args.get("db"), table.this) if p is not None]
    columns = known.get(".".join(parts))
    if columns is None:
        raise _Rejected(f"the columns of the table {'.'.join(parts)!r} are not known")
    return _Source(name, columns)


def _relation_source(relation, known: dict[str, frozenset[str]]) -> _Source:
    if isinstance(relation, exp.Table):
        return _table_source(relation, known)
    if isinstance(relation, exp.Subquery):
        extra = {key for key, value in relation.args.items() if not _empty(value)} - {"this", "alias", "comments"}
        alias = relation.args.get("alias")
        if extra or alias is None or alias.this is None or alias.args.get("columns"):
            raise _Rejected("a derived table has no plain alias or carries a clause")
        names = _names_of_query(relation.this)
        if names is None:
            raise _Rejected("the columns of a derived table are not all named")
        return _Source(alias.this.name.lower(), frozenset(names))
    if isinstance(relation, exp.Unnest):
        extra = {key for key, value in relation.args.items() if not _empty(value)} - {"expressions", "alias", "offset", "comments"}
        alias = relation.args.get("alias")
        columns = alias.args.get("columns") if alias is not None else None
        if extra or not columns or len(columns) != 1 or alias.this is not None:
            raise _Rejected("an UNNEST has no single plain alias")
        offset = relation.args.get("offset")
        reserved = {columns[0].name.lower()}
        if offset is not None:
            reserved.add(offset.name.lower() if isinstance(offset, exp.Identifier) else "offset")
        # The element is a value; its fields (if it is a struct) are readable bare, so it is not an owner.
        return _Source(columns[0].name.lower(), frozenset(), frozenset(reserved))
    raise _Rejected(f"a FROM item of kind {type(relation).__name__} is not understood")


def _sources(select: exp.Select, known: dict[str, frozenset[str]]) -> list[_Source]:
    from_ = select.args.get("from_") or select.args.get("from")
    if from_ is None:
        raise _Rejected("the select has no FROM")
    items = [from_.this] + [join.this for join in select.args.get("joins") or []]
    for join in select.args.get("joins") or []:
        if str(join.args.get("method") or "").upper() == "NATURAL":
            raise _Rejected("the select has a NATURAL join")
        if str(join.args.get("kind") or "").upper() in ("SEMI", "ANTI"):
            raise _Rejected("the select has a SEMI or ANTI join, whose right side's columns are not readable")
    sources = [_relation_source(item, known) for item in items]
    names = [source.name for source in sources]
    if len(set(names)) != len(names):
        raise _Rejected("two sources of the select share a name")
    return sources


# --- 3. each added qualifier ------------------------------------------------------------------------------

def _locate(column: exp.Column) -> tuple[exp.Select, exp.Expression, exp.Expression | None]:
    """The select that reads ``column``, its child that holds it, and the node just below that child."""

    below: exp.Expression | None = None
    node = column
    while True:
        parent = node.parent
        if parent is None:
            raise _Rejected("the column is not inside a SELECT")
        if isinstance(parent, exp.Select):
            return parent, node, below
        if isinstance(parent, exp.SetOperation):
            raise _Rejected("the column belongs to a set operation's own ORDER BY or LIMIT")
        if isinstance(parent, exp.Subquery) and node.arg_key != "this":
            raise _Rejected("the column belongs to a parenthesized query's own ORDER BY or LIMIT")
        if isinstance(parent, exp.Star):
            raise _Rejected("the column is inside a star's EXCEPT, REPLACE or RENAME")
        if isinstance(parent, (exp.Lambda, exp.Pivot)):
            raise _Rejected("the column is a lambda parameter or inside a PIVOT")
        below, node = node, parent


def _readable(select: exp.Select, top: exp.Expression, below: exp.Expression | None, count: int) -> tuple[str, int]:
    """The clause of ``select`` that holds the column, and how many leading sources that place can read."""

    key = top.arg_key
    if key in _PLAIN_CLAUSES or key in _NAME_CLAUSES:
        return key, count
    if key == "joins" and below is not None:
        index = next((i for i, join in enumerate(select.args.get("joins") or []) if join is top), None)
        if index is not None and below.arg_key == "on":
            return "on", index + 2
        if index is not None and below.arg_key == "this" and isinstance(below, exp.Unnest):
            return "from", index + 1
    raise _Rejected(f"the column sits in a clause ({key}) that reads no source we can name")


def _output_names(select: exp.Select) -> set[str]:
    """Names GROUP BY, HAVING, QUALIFY, ORDER BY and a named window may read before a source column."""

    names: set[str] = set()
    for item in select.expressions:
        value = item
        while isinstance(value, exp.Paren):
            value = value.this
        if isinstance(item, exp.Alias):
            names.add(item.alias.lower())
        elif isinstance(value, exp.Column) and not isinstance(value.this, exp.Star):
            if not _empty(value.args.get("table")):
                names.add(value.name.lower())
        elif isinstance(value, exp.Dot):
            names.add(value.text("expression").lower())
        for star in value.find_all(exp.Star):
            for part in (*(star.args.get("replace") or []), *(star.args.get("rename") or [])):
                names.update(identifier.name.lower() for identifier in part.find_all(exp.Identifier))
    return names


def _justify(old: exp.Column, new: exp.Column, known: dict[str, frozenset[str]], cache: dict) -> None:
    select, top, below = _locate(old)
    if id(select) not in cache:
        cache[id(select)] = _sources(select, known)
    sources: list[_Source] = cache[id(select)]
    clause, readable = _readable(select, top, below, len(sources))
    name = old.name.lower()
    using = {i.name.lower() for join in select.args.get("joins") or [] for i in join.args.get("using") or []}
    if name in using:
        raise _Rejected(f"{old.name!r} is a USING column, so the bare name is the merged value")
    if name in {source.name for source in sources} or any(name in source.reserved for source in sources):
        raise _Rejected(f"{old.name!r} is the name of a source or an UNNEST element or offset")
    if clause in _NAME_CLAUSES and name in _output_names(select):
        raise _Rejected(f"{old.name!r} names an output of the select, which {clause.upper()} reads first")
    if isinstance(old.parent, exp.Func) and name.upper() in _DATE_PARTS:
        raise _Rejected(f"{old.name!r} is a date part spelled as a function argument")
    owners = [index for index, source in enumerate(sources[:readable]) if name in source.columns]
    if len(owners) != 1:
        raise _Rejected(
            f"{old.name!r} is " + ("not a column of any source it can read (a correlated or unknown name)" if not owners
                                   else "a column of more than one source it can read")
        )
    owner = sources[owners[0]]
    qualifier = new.args["table"].name.lower()
    if qualifier != owner.name:
        raise _Rejected(f"{old.name!r} was qualified with {new.args['table'].name!r} but its source is {owner.name!r}")


def _check(step: RewriteStep) -> tuple[bool, str, int]:
    before, after = _reparse(step.before_sql), _reparse(step.after_sql)
    known = {
        str(table).lower(): frozenset(str(column).lower() for column in columns)
        for table, columns in step.known_columns
    }
    gained: list[tuple[exp.Column, exp.Column]] = []
    _walk(before, after, gained, "statement")
    if not gained:
        return False, "no qualifier was added", 0
    cache: dict = {}
    for old, new in gained:
        try:
            _justify(old, new, known, cache)
        except _Rejected as exc:
            raise _Rejected(f"the qualifier on {old.sql(dialect='bigquery')!r} is not justified: {exc}") from None
    return True, (
        f"identical once the {len(gained)} added qualifier(s) are ignored; each names the only source it can read that has its column"
    ), len(gained)


def check_qualify_transition(step: RewriteStep, before: exp.Expression, after: exp.Expression) -> StepCheck:
    """Check a qualification step from its SQL text (the caller's trees are not read)."""

    if step.family != QUALIFY_FAMILY:
        return StepCheck(step, False, f"no independent checker for the {step.family!r} family")
    if step.assumptions != QUALIFY_ASSUMPTIONS:
        return StepCheck(step, False, "the step's assumptions are not the assumptions of its family")
    try:
        accepted, reason, cases = _check(step)
    except Exception as exc:  # noqa: BLE001 - an error in the checker is a rejection, never an acceptance
        return StepCheck(step, False, str(exc) or type(exc).__name__)
    return StepCheck(step, accepted, reason, cases)


__all__ = ["QUALIFY_ASSUMPTIONS", "QUALIFY_FAMILY", "check_qualify_transition"]
