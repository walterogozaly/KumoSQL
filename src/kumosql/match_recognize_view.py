"""What a ``MATCH_RECOGNIZE`` clause returns and reads, for the analyses that trace columns.

``bigquery_syntax`` reads the clause into ``SELECT * FROM operand MATCH_RECOGNIZE (...)`` (see :mod:`kumosql.match_recognize`).
sqlglot's optimizer does not know what that star is: it expands it to the columns of the operand, but BigQuery returns the
partition columns followed by the ``MEASURES`` and nothing else. Lineage over the star would attribute columns that are not in the
result to the operand, and every column that is (``fv``) to nothing.

:func:`lineage_form` is the same query as plain SQL for tracing only: ``SELECT p, FIRST(v) AS fv FROM operand`` with the
``DEFINE`` conditions in its ``WHERE`` and the partition and ``ORDER BY`` columns in its ``GROUP BY`` and ``WHERE``, so that the
columns that decide which rows match are read and counted as deciding. It is never printed, proven or run.

The result columns are named as BigQuery names them (checked by dry run): a partition column keeps its name, any other partition
expression is ``f0_``, ``f1_`` ... in the order they stand, and a name that is already taken becomes ``name_1``. Where the names
cannot be told the form is refused (:class:`UnknownOutput`), which every caller already treats as a query it cannot trace.
"""

from __future__ import annotations

from sqlglot import exp

from .match_recognize import is_match_select


class UnknownOutput(Exception):
    """The columns a ``MATCH_RECOGNIZE`` returns cannot be named."""


def _define_items(match: exp.MatchRecognize) -> list[exp.Expression]:
    return list(match.args.get("define") or [])


def pattern_variables(match: exp.MatchRecognize) -> set[str]:
    """The pattern variables the clause defines, lower-cased: a column qualified by one means the column of the matched row."""

    return {item.alias.lower() for item in _define_items(match) if isinstance(item, exp.Alias) and item.alias}


def _measures(match: exp.MatchRecognize) -> list[exp.Expression]:
    items = []
    for measure in match.args.get("measures") or []:
        items.append(measure.this if isinstance(measure, exp.MatchRecognizeMeasure) else measure)
    return items


def output_names(match: exp.MatchRecognize) -> list[str]:
    """The names of the columns the clause returns, partition columns first. Raises :class:`UnknownOutput`."""

    names: list[str] = []
    anonymous = 0
    for item in (match.args.get("partition_by") or []):
        if isinstance(item, exp.Column) and not isinstance(item.this, exp.Star):
            names.append(item.name)
        else:
            names.append(f"f{anonymous}_")
            anonymous += 1
    for measure in _measures(match):
        if not isinstance(measure, exp.Alias) or not measure.alias:
            raise UnknownOutput("a MEASURES item without an alias")
        names.append(measure.alias)
    result: list[str] = []
    taken: set[str] = set()
    for name in names:
        unique = name
        if unique.lower() in taken:
            unique = f"{name}_1"
            if unique.lower() in taken or unique.lower() in {n.lower() for n in names}:
                raise UnknownOutput(f"{name} is used more than twice")
        taken.add(unique.lower())
        result.append(unique)
    return result


def _strip_variables(node: exp.Expression, variables: set[str]) -> exp.Expression:
    """``high.x`` as ``x``: ``high`` is a pattern variable, not a table."""

    for column in list(node.find_all(exp.Column)):
        if column.table and column.table.lower() in variables:
            column.set("table", None)
    return node


def lineage_form(tree: exp.Expression) -> exp.Expression:
    """A copy of ``tree`` with every ``MATCH_RECOGNIZE`` select written as the plain select that traces the same columns."""

    tree = tree.copy()
    # An inner clause is rewritten first: it is inside the operand of the outer one.
    selects = [node for node in tree.find_all(exp.Select) if is_match_select(node)]
    for select in reversed(selects):
        replacement = _plain(select)
        if select is tree:
            tree = replacement
        else:
            select.replace(replacement)
    return tree


def _plain(select: exp.Select) -> exp.Select:
    match = select.args["match"]
    names = output_names(match)
    variables = pattern_variables(match)
    partition = [_strip_variables(item.copy(), variables) for item in match.args.get("partition_by") or []]
    projections: list[exp.Expression] = []
    for name, item in zip(names, partition):
        projections.append(exp.alias_(item, name, quoted=False) if not (isinstance(item, exp.Column) and item.name == name) else item)
    for measure in _measures(match):
        target = names[len(projections)]
        value = _strip_variables(measure.this.copy(), variables)
        projections.append(exp.alias_(value, target, quoted=False))
    deciding: list[exp.Expression] = []
    for item in _define_items(match):
        deciding.append(_strip_variables((item.this if isinstance(item, exp.Alias) else item).copy(), variables))
    order = match.args.get("order")
    for key in (order.expressions if order is not None else []):
        deciding.append(exp.Is(this=_strip_variables(key.this.copy(), variables), expression=exp.Null()).not_())
    plain = select.copy()
    plain.set("match", None)
    plain.set("expressions", projections)
    if deciding:
        plain.set("where", exp.Where(this=exp.and_(*deciding)))
    if partition:
        plain.set("group", exp.Group(expressions=[item.copy() for item in partition]))
    return plain
