"""Algebraic normalization for bag-semantics equivalence proofs.

SQLSolver (SIGMOD 2024) reasons about queries as arithmetic over tuple
multiplicities: under bag semantics ``UNION ALL`` is addition, a join is
multiplication, and a filter or projection is linear. Addition distributes
over all three, and aggregates over an addition split into per-branch partial
aggregates combined arithmetically (``COUNT(A + B) = COUNT(A) + COUNT(B)``).

This module applies those identities as sound AST rewrites, so queries that
differ only by where a ``UNION ALL`` sits reach the same normal form. The SMT
prover then compares normal forms instead of raw syntax. Rules:

* **distribute**: a plain select-project-join over inner/cross joins whose
  FROM contains a derived ``UNION ALL`` becomes a ``UNION ALL`` with one copy
  of the select per branch (nested unions are flattened first);
* **split aggregates**: ``COUNT``, ``SUM``, ``MIN`` and ``MAX`` over a derived
  ``UNION ALL`` become the same function over per-branch partial aggregates
  (``COUNT`` is combined with ``SUM``; ``SUM`` of all-NULL partials stays NULL,
  which matches ``SUM`` of no rows). ``AVG``, ``DISTINCT`` aggregates and
  ``HAVING`` on unsplit expressions are left alone.

``UNION DISTINCT``, outer/semi joins, ``LIMIT``, ``ORDER BY`` and window
functions are never touched. Normalization only rewrites; it proves nothing.
"""

from __future__ import annotations

import calendar
import dataclasses
import datetime
import itertools
import re

import sqlglot
from sqlglot import exp

from .eager_aggregation import flatten_grouped_join, unnest_grouped_source
from .smt_equivalence import SmtEquivalenceResult, SmtStatus, prove_equivalent_smt

MAX_BRANCHES = 16
_SPLIT_ALIAS = "kumosql_u"

_COMBINE = {exp.Count: "SUM", exp.Sum: "SUM", exp.Min: "MIN", exp.Max: "MAX"}


def _union_all_branches(node: exp.Expression) -> list[exp.Expression] | None:
    """Flatten ``a UNION ALL b UNION ALL c``; ``None`` if not a pure UNION ALL."""

    if isinstance(node, exp.Subquery):
        node = node.this
    if isinstance(node, exp.Union):
        if node.args.get("distinct", True) or node.args.get("with_") or node.args.get("with"):
            return None
        if any(node.args.get(k) for k in ("order", "limit", "offset")):
            return None
        left = _union_all_branches(node.left)
        right = _union_all_branches(node.right)
        if left is None or right is None:
            return None
        return left + right
    if isinstance(node, exp.Select):
        if any(node.args.get(k) for k in ("order", "limit", "offset", "with_", "with")):
            return None
        return [node]
    return None


def _aligned_branches(source: exp.Subquery) -> list[exp.Select] | None:
    """Branches of a derived UNION ALL, each naming its columns like the first.

    A union's column names come from its first branch, so a copy of the
    enclosing select that keeps only one branch must give that branch's
    columns those names, or ``u.k`` could bind to a different column.
    """

    branches = _union_all_branches(source)
    if branches is None:
        return None
    first = branches[0]
    names = [item.alias_or_name for item in first.expressions]
    if "" in names or len({n.lower() for n in names}) != len(names):
        return None
    aligned = []
    for branch in branches:
        if len(branch.expressions) != len(names) or any(isinstance(i, exp.Star) for i in branch.expressions):
            return None
        copy = branch.copy()
        copy.set(
            "expressions",
            [
                i.copy() if i.alias_or_name == name else exp.alias_((i.this if isinstance(i, exp.Alias) else i).copy(), name)
                for i, name in zip(copy.expressions, names)
            ],
        )
        aligned.append(copy)
    return aligned


def _plain_sources(select: exp.Select) -> list[exp.Subquery] | None:
    """The union-valued derived tables in FROM/JOIN, if every join is inner."""

    sources = []
    from_ = select.args.get("from_") or select.args.get("from")
    if from_ is None:
        return None
    relations = [from_.this]
    for join in select.args.get("joins") or []:
        if join.args.get("side") or join.args.get("kind") in {"SEMI", "ANTI", "LEFT", "RIGHT", "FULL"}:
            return None
        if join.args.get("using") is not None or join.args.get("method") or join.args.get("global_"):
            return None
        relations.append(join.this)
    for relation in relations:
        if isinstance(relation, exp.Subquery) and _union_all_branches(relation) is not None:
            if isinstance(relation.this, exp.Union):
                if not relation.alias:
                    return None
                sources.append(relation)
    return sources


def _no_extras(select: exp.Select, *, allow_group: bool) -> bool:
    banned = ["distinct", "order", "limit", "offset", "qualify", "windows", "with_", "with"]
    if not allow_group:
        banned += ["group", "having"]
    if any(select.args.get(k) for k in banned):
        return False
    return not any(select.find_all(exp.Window))


def _is_agg(node: exp.Expression) -> bool:
    return isinstance(node, tuple(_COMBINE)) and not node.args.get("distinct") and not isinstance(
        node.this, exp.Distinct
    )


def _branch_copy(select: exp.Select, source: exp.Subquery, branch: exp.Expression) -> exp.Select:
    copy = select.copy()
    for subquery in copy.find_all(exp.Subquery):
        if subquery.alias == source.alias and subquery.this.sql() == source.this.sql():
            replacement = exp.Subquery(this=branch.copy(), alias=subquery.args.get("alias"))
            subquery.replace(replacement)
            break
    return copy


def _distribute(select: exp.Select) -> exp.Expression | None:
    if not _no_extras(select, allow_group=False) or any(select.find_all(exp.AggFunc)):
        return None
    sources = _plain_sources(select)
    if not sources:
        return None
    branch_lists = [_aligned_branches(s) for s in sources]
    if any(branches is None for branches in branch_lists):
        return None
    total = 1
    for branches in branch_lists:
        total *= len(branches)
    if total > MAX_BRANCHES:
        return None
    copies: list[exp.Select] = []
    for combination in itertools.product(*branch_lists):
        copy = select.copy()
        for source, branch in zip(sources, combination):
            copy = _branch_copy(copy, source, branch)
        copies.append(copy)
    result: exp.Expression = copies[0]
    for copy in copies[1:]:
        result = exp.Union(this=result, expression=copy, distinct=False)
    return result


def _split_aggregates(select: exp.Select) -> exp.Expression | None:
    if not _no_extras(select, allow_group=True) or select.args.get("having"):
        return None
    sources = _plain_sources(select)
    if not sources or len(sources) != 1:
        return None
    source = sources[0]
    if source.alias == _SPLIT_ALIAS:
        return None  # already the output of this rule
    from_ = select.args.get("from_") or select.args.get("from")
    if select.args.get("joins") or from_.this is not source:
        return None
    branches = _aligned_branches(source)
    if branches is None or len(branches) > MAX_BRANCHES:
        return None
    group = select.args.get("group")
    keys = list(group.expressions) if group else []
    if any(not isinstance(k, exp.Column) for k in keys):
        return None
    key_sql = {k.sql() for k in keys}

    partial_items: list[exp.Expression] = []
    outer_items: list[exp.Expression] = []
    count = 0
    for item in select.expressions:
        inner = item.this if isinstance(item, exp.Alias) else item
        alias = item.alias if isinstance(item, exp.Alias) else (item.output_name if isinstance(item, exp.Column) else "")
        if isinstance(inner, exp.Column) and inner.sql() in key_sql:
            partial_items.append(exp.alias_(inner.copy(), alias or inner.name))
            outer_items.append(exp.column(alias or inner.name))
        elif _is_agg(inner):
            if any(isinstance(n, exp.Select) for n in inner.walk()) or any(
                _is_agg(n) for n in inner.this.walk() if n is not inner
            ):
                return None
            name = f"kumosql_p{count}"
            count += 1
            partial_items.append(exp.alias_(inner.copy(), name))
            combine = _COMBINE[type(inner)]
            combined = exp.func(combine, exp.column(name))
            outer_items.append(exp.alias_(combined, alias) if alias else combined)
        else:
            return None
    if not any(_is_agg(i.this if isinstance(i, exp.Alias) else i) for i in select.expressions):
        return None

    partials: list[exp.Select] = []
    for branch in branches:
        partial = select.copy()
        partial.set("expressions", [i.copy() for i in partial_items])
        partials.append(_branch_copy(partial, source, branch))
    union: exp.Expression = partials[0]
    for partial in partials[1:]:
        union = exp.Union(this=union, expression=partial, distinct=False)
    outer = exp.select(*outer_items).from_(exp.Subquery(this=union, alias=exp.TableAlias(this=exp.to_identifier(_SPLIT_ALIAS))))
    if keys:
        outer = outer.group_by(*[exp.column(i.alias) for i in partial_items if not _is_agg(i.this)])
    return outer


def _collapse_aggregate(select: exp.Select) -> exp.Expression | None:
    """Drop a redundant regrouping of an already-grouped subquery.

    ``SELECT k, SUM(p) AS n FROM (SELECT k, COUNT(*) AS p FROM t GROUP BY k)
    GROUP BY k`` is the inner query: every group of the outer select holds one
    inner row, so SUM, MIN or MAX of its one value returns that value (SUM of a
    COUNT included). A global inner aggregate has exactly one row, so the same
    holds with no keys at all.
    """

    if not _no_extras(select, allow_group=True) or select.args.get("having"):
        return None
    if select.args.get("where") or select.args.get("joins"):
        return None
    from_ = select.args.get("from_") or select.args.get("from")
    source = from_.this if from_ else None
    if not isinstance(source, exp.Subquery) or not isinstance(source.this, exp.Select):
        return None
    inner = source.this
    if not _no_extras(inner, allow_group=True):
        return None
    inner_keys = list(inner.args["group"].expressions) if inner.args.get("group") else []
    if any(not isinstance(k, exp.Column) for k in inner_keys):
        return None
    inner_key_sql = {k.sql() for k in inner_keys}
    key_outputs: dict[str, exp.Expression] = {}
    agg_outputs: dict[str, exp.Expression] = {}
    for item in inner.expressions:
        expr = item.this if isinstance(item, exp.Alias) else item
        name = item.alias_or_name
        if not name:
            return None
        if _is_agg(expr):
            agg_outputs[name] = expr
        elif isinstance(expr, exp.Column) and expr.sql() in inner_key_sql:
            key_outputs[name] = expr
        else:
            return None
    outer_keys = list(select.args["group"].expressions) if select.args.get("group") else []
    if any(not isinstance(k, exp.Column) or k.name not in key_outputs for k in outer_keys):
        return None
    if {key_outputs[k.name].sql() for k in outer_keys} != inner_key_sql:
        return None

    items: list[exp.Expression] = []
    for item in select.expressions:
        expr = item.this if isinstance(item, exp.Alias) else item
        name = item.alias or item.output_name
        if (
            isinstance(expr, (exp.Sum, exp.Min, exp.Max))
            and isinstance(expr.this, exp.Distinct)
            and len(expr.this.expressions) == 1
        ):
            # One inner row per group: DISTINCT changes nothing.
            expr = type(expr)(this=expr.this.expressions[0].copy())
        if isinstance(expr, exp.Column) and expr.name in key_outputs:
            items.append(_named(key_outputs[expr.name].copy(), name))
        elif isinstance(expr, (exp.Sum, exp.Min, exp.Max)) and isinstance(expr.this, exp.Column) and expr.this.name in key_outputs:
            items.append(_named(key_outputs[expr.this.name].copy(), name))
        elif _is_agg(expr) and isinstance(expr.this, exp.Column) and expr.this.name in agg_outputs:
            inner_agg = agg_outputs[expr.this.name]
            ok = isinstance(inner_agg, (exp.Sum, exp.Count)) if isinstance(expr, exp.Sum) else type(expr) is type(inner_agg)
            if not ok:
                return None
            items.append(_named(inner_agg.copy(), name))
        else:
            return None
    result = inner.copy()
    result.set("expressions", items)
    return result


def _named(expr: exp.Expression, name: str) -> exp.Expression:
    return exp.alias_(expr, name) if name else expr


def _regroup_distinct(select: exp.Select) -> exp.Expression | None:
    """Fold a regrouping that finishes a DISTINCT aggregate back into one grouped query.

    ``SELECT k, SUM(p), SUM(x) FROM (SELECT k, x, SUM(c) AS p FROM t GROUP BY k, x)
    GROUP BY k`` is ``SELECT k, SUM(c), SUM(DISTINCT x) FROM t GROUP BY k``: the
    inner query has one row per ``(k, x)``, so SUM or COUNT of ``x`` over those
    rows sees each value once (NULLs are ignored by both), and SUM, MIN or MAX of
    a partial result over the groups is that aggregate over all rows. A count that
    is summed (and possibly wrapped in ``COALESCE(.., 0)``) is the count: the
    groups of the outer query are never empty.
    """

    if not _no_extras(select, allow_group=True) or select.args.get("having"):
        return None
    if select.args.get("where") or select.args.get("joins") or not select.args.get("group"):
        return None
    from_ = select.args.get("from_") or select.args.get("from")
    source = from_.this if from_ else None
    if not isinstance(source, exp.Subquery) or not isinstance(source.this, exp.Select):
        return None
    inner = source.this
    if not _no_extras(inner, allow_group=True) or not inner.args.get("group") or inner.args.get("having"):
        return None
    inner_keys = list(inner.args["group"].expressions)
    if any(not isinstance(k, exp.Column) for k in inner_keys):
        return None
    inner_key_sql = {k.sql() for k in inner_keys}
    key_outputs: dict[str, exp.Expression] = {}
    agg_outputs: dict[str, exp.Expression] = {}
    for item in inner.expressions:
        expr = item.this if isinstance(item, exp.Alias) else item
        name = item.alias_or_name
        if not name:
            return None
        if _is_agg(expr):
            agg_outputs[name] = expr
        elif isinstance(expr, exp.Column) and expr.sql() in inner_key_sql:
            key_outputs[name] = expr
        else:
            return None
    outer_keys = list(select.args["group"].expressions)
    if any(not isinstance(k, exp.Column) or k.name not in key_outputs for k in outer_keys):
        return None
    extra = inner_key_sql - {key_outputs[k.name].sql() for k in outer_keys}
    if len(extra) != 1:
        return None
    extra_names = [n for n, e in key_outputs.items() if e.sql() in extra]
    if len(extra_names) != 1:
        return None
    extra_name = extra_names[0]

    items: list[exp.Expression] = []
    for item in select.expressions:
        expr = item.this if isinstance(item, exp.Alias) else item
        name = item.alias or item.output_name
        wrapped = False
        if isinstance(expr, exp.Coalesce) and len(expr.expressions) == 1 and expr.expressions[0].sql() == "0":
            expr, wrapped = expr.this, True
        if isinstance(expr, exp.Column) and not wrapped:
            if expr.name not in key_outputs or key_outputs[expr.name].sql() in extra:
                return None
            items.append(_named(key_outputs[expr.name].copy(), name))
            continue
        if isinstance(expr, exp.Avg) and isinstance(expr.this, exp.Column) and not wrapped:
            pass  # AVG of the extra key column only (below)
        elif not _is_agg(expr) or not isinstance(expr.this, exp.Column):
            return None
        column = expr.this.name
        if column == extra_name and not wrapped:
            x = key_outputs[extra_name].copy()
            if isinstance(expr, (exp.Sum, exp.Count, exp.Avg)):
                items.append(_named(type(expr)(this=exp.Distinct(expressions=[x])), name))
            elif isinstance(expr, (exp.Min, exp.Max)):
                items.append(_named(type(expr)(this=x), name))
            else:
                return None
        elif column in agg_outputs:
            partial = agg_outputs[column]
            if wrapped:
                if not (isinstance(expr, exp.Sum) and isinstance(partial, exp.Count)):
                    return None
            elif not (
                (isinstance(expr, exp.Sum) and isinstance(partial, (exp.Sum, exp.Count)))
                or (isinstance(expr, (exp.Min, exp.Max)) and type(partial) is type(expr))
            ):
                return None
            items.append(_named(partial.copy(), name))
        else:
            return None
    result = inner.copy()
    result.set("expressions", items)
    result.set("group", exp.Group(expressions=[key_outputs[k.name].copy() for k in outer_keys]))
    return result


def _key_aggregates(select: exp.Select) -> exp.Expression | None:
    """``MIN(k)``, ``MAX(k)`` and ``SUM(DISTINCT k)`` of a group key are the key itself.

    Every row of a group holds the same ``k`` (NULLs form one group and the
    aggregates ignore them, so a group of NULLs gives NULL, the key).
    """

    group = select.args.get("group")
    if not group or any(select.args.get(k) for k in ("having", "qualify")):
        return None
    keys = {k.sql() for k in group.expressions if isinstance(k, exp.Column)}
    changed = False
    for node in list(select.find_all(exp.Sum, exp.Min, exp.Max)):
        if node.find_ancestor(exp.Select) is not select:
            continue
        arg = node.this
        distinct = isinstance(arg, exp.Distinct) and len(arg.expressions) == 1
        column = arg.expressions[0] if distinct else arg
        if not isinstance(column, exp.Column) or column.sql() not in keys:
            continue
        if isinstance(node, exp.Sum) and not distinct:
            continue
        node.replace(column.copy())
        changed = True
    return select if changed else None


_DATE_TEXT = re.compile(r"^\s*(\d{4})-(\d{1,2})-(\d{1,2})(?:\s*(?:[+-]\d{2}(?::?\d{2})?|Z))?\s*$")


def _date_of(node: exp.Expression) -> datetime.date | None:
    """The date a constant date expression denotes (a time-zone suffix does not change a DATE)."""

    if isinstance(node, (exp.TsOrDsToDate, exp.Cast)) and isinstance(node.this, exp.Literal) and node.this.is_string:
        if isinstance(node, exp.Cast) and not node.args["to"].is_type(exp.DataType.Type.DATE):
            return None
        match = _DATE_TEXT.match(node.this.this)
        if match:
            try:
                return datetime.date(*(int(g) for g in match.groups()))
            except ValueError:
                return None
    return None


def _shift(day: datetime.date, count: int, unit: str) -> datetime.date | None:
    if unit == "DAY":
        return day + datetime.timedelta(days=count)
    if unit == "WEEK":
        return day + datetime.timedelta(weeks=count)
    if unit in ("MONTH", "YEAR"):
        months = day.year * 12 + day.month - 1 + count * (12 if unit == "YEAR" else 1)
        year, month = divmod(months, 12)
        if not 1 <= year <= 9999:
            return None
        return datetime.date(year, month + 1, min(day.day, calendar.monthrange(year, month + 1)[1]))
    return None


def _fold_count_coalesce(tree: exp.Expression) -> exp.Expression:
    """``COALESCE(COUNT(..), 0)`` is the count: a count is never NULL."""

    def step(node: exp.Expression) -> exp.Expression:
        if (
            isinstance(node, exp.Coalesce)
            and isinstance(node.this, exp.Count)
            and len(node.expressions) == 1
            and node.expressions[0].sql() == "0"
        ):
            return node.this
        return node

    return tree.transform(step)


def _fold_dates(tree: exp.Expression) -> exp.Expression:
    """Constant dates become ``CAST('YYYY-MM-DD' AS DATE)``, including ``DATE('..' )`` with a
    time-zone suffix and ``date + INTERVAL n DAY/MONTH/YEAR``, so equal dates compare equal."""

    def literal(day: datetime.date) -> exp.Expression:
        return exp.Cast(this=exp.Literal.string(day.isoformat()), to=exp.DataType.build("DATE"))

    def step(node: exp.Expression) -> exp.Expression:
        day = _date_of(node)
        if day is not None:
            return literal(day)
        sign = 1
        if isinstance(node, (exp.Add, exp.Sub)):
            if isinstance(node, exp.Sub):
                sign = -1
            base, interval = node.this, node.expression
            if isinstance(interval, exp.Interval) and isinstance(interval.this, exp.Literal):
                count, unit = interval.this.this, interval.unit.name.upper() if interval.unit else ""
            else:
                return node
        elif isinstance(node, (exp.DateAdd, exp.DateSub)):
            if isinstance(node, exp.DateSub):
                sign = -1
            base, amount = node.this, node.expression
            if not isinstance(amount, exp.Literal):
                return node
            count, unit = amount.this, (node.args.get("unit").name.upper() if node.args.get("unit") else "")
        else:
            return node
        base_day = _date_of(base)
        if base_day is None or not re.fullmatch(r"-?\d+", str(count).strip()):
            return node
        shifted = _shift(base_day, sign * int(count), unit)
        return literal(shifted) if shifted is not None else node

    return tree.transform(step)


def _union_all_branches(node: exp.Expression) -> list[exp.Select] | None:
    """The SELECTs of a (nested) UNION ALL, or ``None`` for any other shape."""

    if isinstance(node, exp.Subquery):
        return _union_all_branches(node.this)
    if isinstance(node, exp.Select):
        return [node]
    if isinstance(node, exp.Union) and not node.args.get("distinct") and not any(
        node.args.get(k) for k in ("order", "limit", "offset")
    ):
        left, right = _union_all_branches(node.this), _union_all_branches(node.expression)
        return None if left is None or right is None else left + right
    return None


def _prune_union_all(union: exp.Union, used: set[str]) -> bool:
    """Drop, in every branch of a UNION ALL, the columns (by position) nothing reads."""

    branches = _union_all_branches(union)
    if not branches:
        return False
    first = branches[0].expressions
    width = len(first)
    for branch in branches:
        if len(branch.expressions) != width or not _no_extras(branch, allow_group=True) or branch.args.get("distinct"):
            return False
        if any(isinstance(e, exp.Star) or (isinstance(e, exp.Column) and isinstance(e.this, exp.Star)) for e in branch.expressions):
            return False
    if any(not e.alias_or_name for e in first):
        return False
    names = [e.alias_or_name.lower() for e in first]
    if len(set(names)) != width:
        return False
    keep = [i for i in range(width) if names[i] in used] or [0]
    keep.sort(key=lambda i: names[i])  # the enclosing query reads by name, so the order is free
    if keep == list(range(width)):
        return False
    for branch in branches:
        branch.set("expressions", [branch.expressions[i].copy() for i in keep])
    return True


def _sources_of(select: exp.Select) -> list[exp.Expression]:
    from_ = select.args.get("from_") or select.args.get("from")
    return ([from_.this] if from_ is not None else []) + [j.this for j in select.args.get("joins") or []]


def _inline_expression_projection(select: exp.Select) -> exp.Expression | None:
    """``(SELECT f(a) AS x FROM t) AS d`` joined in: read ``t AS d`` and replace ``d.x`` by ``f(d.a)``.

    A derived table that only computes expressions over one table keeps every row, so it can be
    folded into the query that uses it, for any join type.
    """

    if any(isinstance(star, exp.Star) and not isinstance(star.parent, exp.Count) for star in select.find_all(exp.Star)):
        return None
    changed = False
    for source in _sources_of(select):
        if not isinstance(source, exp.Subquery) or not source.alias or not isinstance(source.this, exp.Select):
            continue
        inner = source.this
        if not _no_extras(inner, allow_group=False) or inner.args.get("where") or inner.args.get("joins"):
            continue
        from_ = inner.args.get("from_") or inner.args.get("from")
        table = from_.this if from_ is not None else None
        if not isinstance(table, exp.Table) or any(
            isinstance(n, (exp.AggFunc, exp.Subquery, exp.Select, exp.Star, exp.Window)) for e in inner.expressions for n in e.walk()
        ):
            continue
        names = [e.alias_or_name.lower() for e in inner.expressions]
        if "" in names or len(set(names)) != len(names):
            continue
        # Plain passthrough projections are handled (and kept) by _inline_projection.
        if all(isinstance(e, exp.Column) and e.alias_or_name.lower() == e.name.lower() for e in inner.expressions):
            continue
        alias = source.alias
        qualifier = table.alias_or_name
        # Columns of the new relation are the table's columns, so every use of ``d.x`` is replaced.
        by_name = {n: (e.this if isinstance(e, exp.Alias) else e) for n, e in zip(names, inner.expressions)}
        uses = [c for c in select.find_all(exp.Column) if c.table.lower() == alias.lower()]
        if any(c.name.lower() not in by_name for c in uses):
            continue
        unqualified = [c for c in select.find_all(exp.Column) if not c.table and c.name.lower() in by_name]
        if unqualified:
            continue
        # Alias clashes with another declared name would capture references.
        if sum(1 for n in select.walk() if isinstance(n, (exp.Table, exp.Subquery)) and (n.alias_or_name or "").lower() == alias.lower()) != 1:
            continue
        for column in uses:
            replacement = by_name[column.name.lower()].copy()
            for inner_column in replacement.find_all(exp.Column):
                if not inner_column.table or inner_column.table.lower() == qualifier.lower():
                    inner_column.set("table", exp.to_identifier(alias))
            column.replace(exp.Paren(this=replacement) if isinstance(replacement, exp.Binary) else replacement)
        replaced = exp.Table(this=table.this.copy(), db=table.args.get("db"), catalog=table.args.get("catalog"), alias=exp.TableAlias(this=exp.to_identifier(alias)))
        source.replace(replaced)
        changed = True
    return select if changed else None


_merge_counter = itertools.count()
_OUTER = {"SEMI", "ANTI", "LEFT", "RIGHT", "FULL"}


def _merge_spj_source(select: exp.Select) -> exp.Expression | None:
    """Fold a filtering or joining derived table into the grouped select that reads it.

    ``SELECT SUM(x) FROM (SELECT k, x FROM t WHERE c) AS d GROUP BY k`` is
    ``SELECT SUM(x) FROM t WHERE c GROUP BY k``: the derived table keeps one row per row of its
    own sources, so the aggregate sees the same rows. Only selects that group or aggregate are
    rewritten (the prover flattens the others itself), and only through inner joins.
    """

    from .eager_aggregation import _own_aggregates

    if not (select.args.get("group") or _own_aggregates(select)) or not _no_extras(select, allow_group=True):
        return None
    if any(isinstance(star, exp.Star) and not isinstance(star.parent, exp.Count) for star in select.find_all(exp.Star)):
        return None
    select = select.copy()
    items = _sources_of(select)
    for join in select.args.get("joins") or []:
        if join.args.get("side") or join.args.get("kind") in _OUTER or join.args.get("using") is not None:
            return None
        if join.args.get("method") or join.args.get("global_"):
            return None
    # A subquery outside FROM could read the alias from outside.
    if any(isinstance(n, exp.Subquery) and n not in items for n in select.find_all(exp.Subquery) if n.find_ancestor(exp.Select) is select) or any(
        e.find_ancestor(exp.Select) is select for e in select.find_all(exp.Exists)
    ):
        return None
    own = [c for c in select.find_all(exp.Column) if c.find_ancestor(exp.Select) is select]
    for position, source in enumerate(items):
        if not isinstance(source, exp.Subquery) or not source.alias or not isinstance(source.this, exp.Select):
            continue
        inner = source.this
        if not _no_extras(inner, allow_group=False):
            continue
        if any(isinstance(n, (exp.AggFunc, exp.Window, exp.Star, exp.Subquery, exp.Select, exp.Exists)) for e in inner.expressions for n in e.walk()):
            continue
        inner_items = _sources_of(inner)
        if not inner_items or any(not (isinstance(i, (exp.Table, exp.Subquery)) and i.alias_or_name) for i in inner_items):
            continue
        if any(isinstance(i, exp.Table) and (i.args.get("joins") or i.args.get("pivots") or i.args.get("laterals")) for i in inner_items):
            continue
        inner_joins = inner.args.get("joins") or []
        if any(j.args.get("side") or j.args.get("kind") in _OUTER or j.args.get("using") is not None or j.args.get("method") for j in inner_joins):
            continue
        if any(
            isinstance(n, (exp.Subquery, exp.Exists)) and n not in inner_items
            for cond in [inner.args.get("where")] + [j.args.get("on") for j in inner_joins]
            if cond is not None
            for n in cond.walk()
        ):
            continue
        names = [e.alias_or_name.lower() for e in inner.expressions]
        inner_aliases = [i.alias_or_name.lower() for i in inner_items]
        if "" in names or len(set(names)) != len(names) or len(set(inner_aliases)) != len(inner_aliases):
            continue
        alias = source.alias.lower()
        uses = [c for c in own if c.table.lower() == alias]
        bare = [c for c in own if not c.table]
        if bare and (len(items) != 1 or select.args.get("order")):
            continue
        if any(c.name.lower() not in names for c in uses + bare):
            continue
        if len(inner_items) > 1 and any(not c.table for c in inner.find_all(exp.Column) if c.find_ancestor(exp.Select) is inner):
            continue
        if any(c.table and c.table.lower() not in inner_aliases for c in inner.find_all(exp.Column) if c.find_ancestor(exp.Select) is inner):
            continue
        mapping = {old: f"kumosql_m{next(_merge_counter)}_{old}" for old in inner_aliases}

        def rename(node: exp.Expression) -> exp.Expression:
            holder = exp.Select(expressions=[node.copy()])
            for column in list(holder.find_all(exp.Column)):
                if column.find_ancestor(exp.Select) is holder:
                    column.set("table", exp.to_identifier(mapping[column.table.lower() or inner_aliases[0]]))
            return holder.expressions[0]

        by_name = {n: (e.this if isinstance(e, exp.Alias) else e) for n, e in zip(names, inner.expressions)}
        # A bare column in the select list keeps its output name once it is replaced.
        for index, item in enumerate(select.expressions):
            if item in uses or item in bare:
                named = exp.alias_(item.copy(), item.name)
                select.expressions[index].replace(named)
        own = [c for c in select.find_all(exp.Column) if c.find_ancestor(exp.Select) is select]
        uses = [c for c in own if c.table.lower() == alias]
        bare = [c for c in own if not c.table]
        for column in uses + bare:
            replacement = rename(by_name[column.name.lower()])
            column.replace(exp.Paren(this=replacement) if isinstance(replacement, exp.Binary) else replacement)
        conditions = []
        if inner.args.get("where") is not None:
            conditions.append(rename(inner.args["where"].this))
        for join in inner_joins:
            if join.args.get("on") is not None:
                conditions.append(rename(join.args["on"]))
        new_items = []
        for old, item in zip(inner_aliases, inner_items):
            item = item.copy()
            item.set("alias", exp.TableAlias(this=exp.to_identifier(mapping[old])))
            new_items.append(item)
        all_items: list[exp.Expression] = []
        for i, item in enumerate(items):
            all_items.extend(new_items if i == position else [item.copy()])
        for join in select.args.get("joins") or []:
            if join.args.get("on") is not None:
                conditions.append(join.args["on"].copy())
        if select.args.get("where") is not None:
            conditions.append(select.args["where"].this.copy())
        select.set("from_", exp.From(this=all_items[0]))
        select.set("joins", [exp.Join(this=i) for i in all_items[1:]] or None)
        where = _and_all([part for condition in conditions for part in _conjuncts(condition)])
        select.set("where", exp.Where(this=where) if where is not None else None)
        return select
    return None


def _wrap_outer_join_aggregate(select: exp.Select) -> exp.Expression | None:
    """An aggregate over an outer join reads the join as one derived relation.

    ``SELECT k, COUNT(*) FROM a LEFT JOIN b ON c GROUP BY k`` becomes
    ``SELECT j.c0, COUNT(*) FROM (SELECT a.k AS c0 FROM a LEFT JOIN b ON c) AS j GROUP BY j.c0``:
    the same rows, with the join stated once and its columns named by position, so two spellings of
    the same join (different aliases, expressions folded in) have the same text.
    """

    from_ = select.args.get("from_") or select.args.get("from")
    joins = select.args.get("joins") or []
    if from_ is None or not joins or not any((j.args.get("side") or "").upper() in ("LEFT", "RIGHT", "FULL") for j in joins):
        return None
    from .eager_aggregation import _own_aggregates

    if not (select.args.get("group") or _own_aggregates(select)) or not _no_extras(select, allow_group=True):
        return None
    if any(isinstance(star, exp.Star) and not isinstance(star.parent, exp.Count) for star in select.find_all(exp.Star)):
        return None
    sources = _sources_of(select)
    aliases = [(s.alias_or_name or "").lower() for s in sources]
    if "" in aliases or len(set(aliases)) != len(aliases):
        return None
    # A subquery in the SELECT, WHERE or HAVING could read the join's columns; leave those shapes alone.
    if any(
        not isinstance(n.parent, (exp.From, exp.Join))
        for n in select.find_all(exp.Subquery, exp.Exists)
        if n.find_ancestor(exp.Join) is None
    ):
        return None
    columns = [
        c for c in select.find_all(exp.Column)
        if not isinstance(c.this, exp.Star) and c.find_ancestor(exp.Join) is None and c.find_ancestor(exp.Select) is select
    ]
    if any(c.table.lower() not in aliases for c in columns):
        return None
    spelled = {(s.alias_or_name or "").lower(): s.alias_or_name for s in sources}
    keys = [(c.table.lower(), c.name.lower()) for c in columns]
    positions: dict[tuple[str, str], int] = {}
    for key in keys:
        positions.setdefault(key, len(positions))
    if not positions:
        return None
    inner = exp.Select(
        expressions=[
            exp.alias_(exp.column(name, table=spelled[table]), f"c{index}") for (table, name), index in positions.items()
        ]
    )
    inner.set("from_" if "from_" in select.args else "from", from_.copy())
    inner.set("joins", [j.copy() for j in joins])
    for column, key in zip(columns, keys):
        column.set("table", exp.to_identifier("kqj"))
        column.set("this", exp.to_identifier(f"c{positions[key]}"))
    select.set("joins", None)
    select.set("from_" if "from_" in select.args else "from", exp.From(this=exp.Subquery(this=inner, alias=exp.TableAlias(this=exp.to_identifier("kqj")))))
    return select


def _prune_derived(select: exp.Select) -> exp.Expression | None:
    """Drop the columns of a derived table that the enclosing query never reads.

    A grouped or plain derived table keeps its rows when an output is removed,
    so ``SELECT 1 FROM (SELECT k, COUNT(*) FROM t GROUP BY k) AS d`` is the same
    as ``SELECT 1 FROM (SELECT k FROM t GROUP BY k) AS d``. Not applied under
    ``DISTINCT`` (removing a column changes which rows collapse) or with a star.
    """

    if any(
        isinstance(star, exp.Star) and not isinstance(star.parent, exp.Count)
        for star in select.find_all(exp.Star)
    ):
        return None
    from_ = select.args.get("from_") or select.args.get("from")
    if from_ is None:
        return None
    changed = False
    for source in [from_.this] + [j.this for j in select.args.get("joins") or []]:
        if isinstance(source, exp.Subquery) and source.alias and isinstance(source.this, exp.Union):
            used = {
                c.name.lower()
                for c in select.find_all(exp.Column)
                if not c.table or c.table.lower() == source.alias.lower()
            }
            if _prune_union_all(source.this, used):
                changed = True
            continue
        if not isinstance(source, exp.Subquery) or not source.alias or not isinstance(source.this, exp.Select):
            continue
        inner = source.this
        if not _no_extras(inner, allow_group=True):
            continue
        if inner.args.get("distinct") or any(isinstance(e, exp.Star) or (isinstance(e, exp.Column) and isinstance(e.this, exp.Star)) for e in inner.expressions):
            continue
        used = {
            c.name.lower()
            for c in select.find_all(exp.Column)
            if not c.table or c.table.lower() == source.alias.lower()
        }
        keep = [e for e in inner.expressions if e.alias_or_name and e.alias_or_name.lower() in used]
        if not keep:
            keep = inner.expressions[:1]
        if len(keep) == len(inner.expressions):
            continue
        inner.set("expressions", [e.copy() for e in keep])
        changed = True
    return select if changed else None


def _inline_projection(node: exp.Subquery) -> exp.Expression | None:
    """``(SELECT a, b FROM t) AS u`` is ``t AS u`` when every column is passed through.

    Only the columns of ``u`` the enclosing query uses matter, so dropping the
    unused ones is invisible unless the enclosing query uses ``*``.
    """

    inner = node.this
    if not isinstance(node.parent, (exp.From, exp.Join)) or not node.alias:
        return None
    if not isinstance(inner, exp.Select) or not _no_extras(inner, allow_group=False):
        return None
    if inner.args.get("where") or inner.args.get("joins") or any(inner.find_all(exp.AggFunc)):
        return None
    from_ = inner.args.get("from_") or inner.args.get("from")
    table = from_.this if from_ else None
    if not isinstance(table, exp.Table):
        return None
    if any(not isinstance(e, exp.Column) or e.name == "*" or e.table not in {"", table.alias_or_name} for e in inner.expressions):
        return None
    outer = node.find_ancestor(exp.Select)
    if outer is None or any(isinstance(star, exp.Star) and not isinstance(star.parent, exp.Count) for star in outer.find_all(exp.Star)):
        return None
    return exp.Table(this=table.this.copy(), db=table.args.get("db"), catalog=table.args.get("catalog"), alias=node.args.get("alias"))


def _canonical_branch(branch: exp.Select) -> exp.Select | None:
    """Positional table aliases and output names for a plain select over tables."""

    if not _no_extras(branch, allow_group=True):
        return None
    from_ = branch.args.get("from_") or branch.args.get("from")
    if from_ is None:
        return None
    sources = [from_.this] + [j.this for j in branch.args.get("joins") or []]
    if any(not isinstance(src, exp.Table) for src in sources):
        return None
    if any(isinstance(n, (exp.Subquery, exp.Select)) for n in branch.walk() if n is not branch):
        return None
    copy = branch.copy()
    from_ = copy.args.get("from_") or copy.args.get("from")
    tables = [from_.this] + [j.this for j in copy.args.get("joins") or []]
    renames = {}
    for index, table in enumerate(tables):
        renames[table.alias_or_name.lower()] = f"t{index}"
    new_tables = []
    for column in copy.find_all(exp.Column):
        if column.table:
            if column.table.lower() not in renames:
                return None
            new_tables.append((column, renames[column.table.lower()]))
        elif len(tables) == 1:
            new_tables.append((column, "t0"))
    for column, name in new_tables:
        column.set("table", exp.to_identifier(name))
    for index, table in enumerate(tables):
        table.set("alias", exp.TableAlias(this=exp.to_identifier(f"t{index}")))
    items = []
    for index, item in enumerate(copy.expressions):
        if isinstance(item, exp.Star) or (isinstance(item, exp.Column) and isinstance(item.this, exp.Star)):
            return None
        items.append(exp.alias_((item.this if isinstance(item, exp.Alias) else item).copy(), f"c{index}"))
    copy.set("expressions", items)
    return copy


def _canonicalize_union_source(node: exp.Subquery) -> exp.Expression | None:
    """Make a derived UNION ALL comparable by text: same shape, same SQL.

    The SMT prover identifies a multi-branch derived table by its SQL, so two
    unions that differ only in aliases or branch order would otherwise never
    match. Output columns become c0, c1, ... (the enclosing select is updated),
    table aliases become t0, t1, ..., and branches are sorted.
    """

    if not isinstance(node.this, exp.Union) or not node.alias or node.alias == _SPLIT_ALIAS + "_done":
        return None
    branches = _union_all_branches(node)
    outer = node.find_ancestor(exp.Select)
    if not branches or len(branches) < 2 or outer is None:
        return None
    first_names = [item.alias_or_name for item in branches[0].expressions]
    if "" in first_names or len(set(first_names)) != len(first_names):
        return None
    canonical = [_canonical_branch(b) for b in branches]
    if any(c is None for c in canonical):
        return None
    if any(len(b.expressions) != len(first_names) for b in branches):
        return None
    alias = node.alias.lower()
    renames = {name.lower(): f"c{i}" for i, name in enumerate(first_names)}
    from_ = outer.args.get("from_") or outer.args.get("from")
    sole_source = from_ is not None and from_.this is node and not outer.args.get("joins")
    targets = []
    for column in outer.find_all(exp.Column):
        if column.table:
            if column.table.lower() == alias:
                if column.name.lower() not in renames:
                    return None
                targets.append(column)
        elif sole_source and column.name.lower() in renames:
            targets.append(column)
        elif column.name.lower() in renames:
            return None  # ambiguous unqualified use: leave it alone
    for column in targets:
        column.set("this", exp.to_identifier(renames[column.name.lower()]))
    canonical.sort(key=lambda b: b.sql(dialect="bigquery"))
    union: exp.Expression = canonical[0]
    for branch in canonical[1:]:
        union = exp.Union(this=union, expression=branch, distinct=False)
    return exp.Subquery(this=union, alias=node.args.get("alias"))


def _values_to_union(tree: exp.Expression) -> exp.Expression:
    """``(VALUES (1, 2), (3, 4)) AS t(a, b)`` is ``SELECT 1 AS a, 2 AS b UNION ALL SELECT 3, 4``.

    A constant relation is a sum of one-row relations. Only done when the
    column names are declared, so the rewrite never invents names.
    """

    def step(node: exp.Expression) -> exp.Expression:
        if not isinstance(node, exp.Values) or not isinstance(node.parent, (exp.From, exp.Join)):
            return node
        alias = node.args.get("alias")
        names = [c.name for c in alias.columns] if alias is not None else []
        rows = node.expressions
        if not names or not rows or any(not isinstance(r, exp.Tuple) or len(r.expressions) != len(names) for r in rows):
            return node
        selects = []
        for index, row in enumerate(rows):
            items = [
                exp.alias_(value.copy(), name) if index == 0 else value.copy()
                for value, name in zip(row.expressions, names)
            ]
            selects.append(exp.select(*items))
        body: exp.Expression = selects[0]
        for select in selects[1:]:
            body = exp.Union(this=body, expression=select, distinct=False)
        return exp.Subquery(this=body, alias=exp.TableAlias(this=exp.to_identifier(alias.name)))

    return tree.transform(step)


_EXACT_LIMIT = 2**53


def _int_value(node: exp.Expression) -> int | None:
    if isinstance(node, exp.Paren):
        return _int_value(node.this)
    if isinstance(node, exp.Literal) and not node.is_string and node.this.lstrip("-").isdigit():
        return int(node.this)
    if isinstance(node, exp.Neg):
        inner = _int_value(node.this)
        return None if inner is None else -inner
    return None


def _fold_constants(tree: exp.Expression) -> exp.Expression:
    """Evaluate integer arithmetic on literals, e.g. ``10 / 2`` to ``5``.

    Decimal literals lose trailing zeros (``1.00`` is ``1``). Only exact results are folded (a division must divide evenly, nothing may
    leave the range where FLOAT64 and INT64 agree), so INT64 and FLOAT64
    readings of the expression coincide.
    """

    def step(node: exp.Expression) -> exp.Expression:
        if isinstance(node, exp.Literal) and not node.is_string and re.fullmatch(r"\d+\.\d*0", node.name):
            # 1.00 and 0.20 name the same numbers as 1 and 0.2.
            text = node.name.rstrip("0").rstrip(".")
            return exp.Literal.number(text)
        if not isinstance(node, (exp.Add, exp.Sub, exp.Mul, exp.Div)):
            return node
        a, b = _int_value(node.this), _int_value(node.expression)
        if a is None or b is None:
            return node
        if isinstance(node, exp.Add):
            value = a + b
        elif isinstance(node, exp.Sub):
            value = a - b
        elif isinstance(node, exp.Mul):
            value = a * b
        elif b != 0 and a % b == 0:
            value = a // b
        else:
            return node
        if abs(value) >= _EXACT_LIMIT:
            return node
        return exp.Neg(this=exp.Literal.number(-value)) if value < 0 else exp.Literal.number(value)

    return tree.transform(step)


def _is_empty_select(node: exp.Expression) -> bool:
    """A SELECT whose WHERE is literally FALSE (or that has LIMIT 0) returns no rows."""

    while isinstance(node, exp.Subquery):
        node = node.this
    if not isinstance(node, exp.Select) or node.args.get("group") or node.args.get("having"):
        return False
    where = node.args.get("where")
    if where is not None and isinstance(where.this, exp.Boolean) and not where.this.this:
        return True
    limit = node.args.get("limit")
    return limit is not None and isinstance(limit.expression, exp.Literal) and limit.expression.name == "0"


def _fold_trivia(tree: exp.Expression) -> exp.Expression:
    """Small exact identities: ``(SELECT 1)`` is ``1``, ``IN``/``EXISTS`` over an empty subquery
    is FALSE, and ``agg(x) FILTER (WHERE c)`` is ``agg(CASE WHEN c THEN x END)``."""

    def step(node: exp.Expression) -> exp.Expression:
        if isinstance(node, exp.Subquery) and isinstance(node.parent, exp.Binary):
            inner = node.this
            if (
                isinstance(inner, exp.Select)
                and len(inner.expressions) == 1
                and not any(inner.args.get(k) for k in ("from_", "from", "where", "group", "having", "joins", "limit", "distinct"))
                and isinstance(inner.expressions[0], (exp.Literal, exp.Boolean))
            ):
                return inner.expressions[0].copy()
        if isinstance(node, exp.Select):
            group = node.args.get("group")
            if (
                group is not None
                and len(group.expressions) == 1
                and isinstance(group.expressions[0], exp.Boolean)
                and not node.args.get("having")
                and not node.args.get("distinct")
                and not any(node.find_all(exp.AggFunc))
                and node.expressions
                and all(isinstance(item.unalias(), exp.Literal) for item in node.expressions)
                and not any(isinstance(item.unalias(), exp.Star) for item in node.expressions)
            ):
                # One group holding every row: a constant row exists iff the source has a row.
                node.set("group", None)
                node.set("distinct", exp.Distinct())
                return node
        if isinstance(node, (exp.Upper, exp.Lower)) and isinstance(node.this, (exp.Upper, exp.Lower)):
            return type(node)(this=node.this.this.copy())  # the outer case wins whatever the inner one did
        if isinstance(node, exp.Anonymous) and node.name.upper() == "POSITIVE" and len(node.expressions) == 1:
            return node.expressions[0].copy()
        if isinstance(node, exp.Concat) and any(isinstance(e, exp.Concat) for e in node.expressions):
            flat = []
            for item in node.expressions:
                if isinstance(item, exp.Concat) and item.args.get("safe") == node.args.get("safe") and item.args.get("coalesce") == node.args.get("coalesce"):
                    flat.extend(i.copy() for i in item.expressions)
                else:
                    flat.append(item.copy())
            rebuilt = node.copy()
            rebuilt.set("expressions", flat)
            return rebuilt
        for kind, part in ((exp.Year, "YEAR"), (exp.Month, "MONTH"), (exp.Day, "DAY")):
            if isinstance(node, kind):
                argument = node.this
                if isinstance(argument, exp.TsOrDsToDate):
                    argument = argument.this
                return exp.Extract(this=exp.var(part), expression=argument.copy())
        if isinstance(node, exp.In) and isinstance(node.args.get("query"), exp.Expression) and _is_empty_select(node.args["query"]):
            return exp.false()
        if isinstance(node, exp.Exists) and _is_empty_select(node.this):
            return exp.false()
        if isinstance(node, exp.Filter) and isinstance(node.this, (exp.Count, exp.Sum, exp.Min, exp.Max, exp.Avg)):
            condition = node.expression.this if isinstance(node.expression, exp.Where) else None
            aggregate = node.this
            if condition is None:
                return node
            argument = aggregate.this
            distinct = isinstance(argument, exp.Distinct)
            if distinct:
                if len(argument.expressions) != 1:
                    return node
                argument = argument.expressions[0]
            if isinstance(argument, exp.Star):
                argument = exp.Literal.number(1)
            guarded = exp.Case(ifs=[exp.If(this=condition.copy(), true=argument.copy())])
            if distinct:
                guarded = exp.Distinct(expressions=[guarded])
            rebuilt = aggregate.copy()
            rebuilt.set("this", guarded)
            return rebuilt
        return node

    return tree.transform(step)


def _select_names(select: exp.Expression) -> list[str] | None:
    first = select
    while isinstance(first, exp.Union):
        first = first.this
    while isinstance(first, exp.Subquery):
        first = first.this
    if not isinstance(first, exp.Select):
        return None
    names = [item.alias_or_name.lower() for item in first.expressions]
    if "" in names or len(set(names)) != len(names) or any(isinstance(i, exp.Star) for i in first.expressions):
        return None
    return names


def _expand_stars(tree: exp.Expression, schema: dict[str, list[str]]) -> exp.Expression:
    """Replace ``*`` and ``t.*`` with explicit columns when every source's columns are known."""

    schema = {key.lower(): [c.lower() for c in cols] for key, cols in schema.items()}

    def columns_of(source: exp.Expression) -> list[str] | None:
        if isinstance(source, exp.Table):
            parts = [p.name for p in (source.args.get("catalog"), source.args.get("db"), source.this) if p is not None]
            return schema.get(".".join(parts).lower())
        if isinstance(source, exp.Subquery):
            return _select_names(source.this)
        return None

    for select in list(tree.find_all(exp.Select))[::-1]:  # innermost first
        if not any(isinstance(i, exp.Star) or (isinstance(i, exp.Column) and isinstance(i.this, exp.Star)) for i in select.expressions):
            continue
        from_ = select.args.get("from_") or select.args.get("from")
        if from_ is None:
            continue
        sources = [from_.this] + [j.this for j in select.args.get("joins") or []]
        known = []
        for source in sources:
            columns = columns_of(source)
            if columns is None or not source.alias_or_name:
                known = None
                break
            known.append((source.alias_or_name, columns))
        if known is None:
            continue
        items = []
        ok = True
        for item in select.expressions:
            if isinstance(item, exp.Star):
                if item.args.get("except_") or item.args.get("except") or item.args.get("replace") or item.args.get("replace_"):
                    ok = False
                    break
                items.extend(exp.column(c, table=a) for a, cols in known for c in cols)
            elif isinstance(item, exp.Column) and isinstance(item.this, exp.Star):
                match = [cols for a, cols in known if a.lower() == item.table.lower()]
                if len(match) != 1:
                    ok = False
                    break
                items.extend(exp.column(c, table=item.table) for c in match[0])
            else:
                items.append(item)
        if ok:
            select.set("expressions", items)
    return tree


_REJECTING = (exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE, exp.Like, exp.ILike)


def _conjuncts(node: exp.Expression) -> list[exp.Expression]:
    """The parts of an AND chain; a parenthesized part that is not itself an AND keeps its parentheses."""

    inner = node
    while isinstance(inner, exp.Paren):
        inner = inner.this
    if isinstance(inner, exp.And):
        return _conjuncts(inner.this) + _conjuncts(inner.expression)
    return [node if isinstance(inner, (exp.Or, exp.Xor)) else inner]


def _and_all(parts: list[exp.Expression]) -> exp.Expression | None:
    result = None
    for part in parts:
        result = part if result is None else exp.And(this=result, expression=part)
    return result


def _guard_of(part: exp.Expression, columns: set[str]) -> bool:
    return (
        isinstance(part, exp.Not)
        and isinstance(part.this, exp.Is)
        and isinstance(part.this.expression, exp.Null)
        and part.this.this.sql() in columns
    )


def _declared_not_null(holder: exp.Expression, not_null: dict[str, set[str]]) -> set[str]:
    """SQL of the columns of ``holder``'s select that are NOT NULL by declaration and always present."""

    select = holder.find_ancestor(exp.Select)
    if select is None or any(
        j.args.get("side") or j.args.get("kind") in _OUTER for j in select.args.get("joins") or []
    ):
        return set()
    found: set[str] = set()
    for source in _sources_of(select):
        if isinstance(source, exp.Table) and not source.db and not source.catalog:
            for name in not_null.get(source.name.lower(), ()):
                for column in (exp.column(name, table=source.alias_or_name), exp.column(name)):
                    found.add(column.sql())
    return found


def _fold_null_guards(tree: exp.Expression, not_null: dict[str, frozenset[str]] | None = None) -> exp.Expression:
    """Drop ``x IS NOT NULL`` from a condition that already compares ``x``.

    In WHERE, ON and HAVING a comparison with NULL rejects the row, so another conjunct
    ``x > 0`` makes the guard redundant, and so does a NOT NULL declaration for a column of a
    table that no outer join can null-extend. Writing the condition with or without it must read alike.
    """

    not_null = {k.lower(): {c.lower() for c in v} for k, v in (not_null or {}).items()}

    for holder in list(tree.find_all(exp.Where, exp.Having)) + [j.args["on"].parent for j in tree.find_all(exp.Join) if j.args.get("on") is not None]:
        condition = holder.this if isinstance(holder, (exp.Where, exp.Having)) else holder.args["on"]
        parts = _conjuncts(condition)
        rejected = {
            side.sql()
            for part in parts
            if isinstance(part, _REJECTING)
            for side in (part.this, part.expression)
            if isinstance(side, exp.Column)
        }
        declared = _declared_not_null(holder, not_null) if not_null else set()
        kept = [part for part in parts if not _guard_of(part, rejected | declared)]
        if not kept or (len(kept) == len(parts) and not any(isinstance(p, exp.Paren) for p in condition.find_all(exp.Paren) if isinstance(p.parent, exp.And) or p is condition)):
            continue
        rebuilt = _and_all(kept)
        if isinstance(holder, (exp.Where, exp.Having)):
            holder.set("this", rebuilt)
        else:
            holder.set("on", rebuilt)
    return tree


def _lowercase_columns(tree: exp.Expression) -> exp.Expression:
    """Column names are case-insensitive: ``t.EMPNO`` and ``t.empno`` are the same column.

    A name that is also an output alias somewhere keeps its spelling, because aliases are
    compared as written when output names matter.
    """

    aliases = {a.alias.lower() for a in tree.find_all(exp.Alias) if a.alias}
    aliases |= {t.alias.lower() for t in tree.find_all(exp.TableAlias) if t.name}
    for column in tree.find_all(exp.Column):
        identifier = column.this
        if isinstance(identifier, exp.Identifier) and identifier.name.lower() not in aliases and identifier.name != identifier.name.lower():
            column.set("this", exp.Identifier(this=identifier.name.lower(), quoted=identifier.args.get("quoted")))
    return tree


def normalize(
    sql: str, *, schema: dict[str, list[str]] | None = None, dialect: str = "bigquery", not_null: dict[str, frozenset[str]] | None = None
) -> str:
    """Rewrite ``sql`` with the bag-semantics identities above (``dialect`` in and out)."""

    tree = sqlglot.parse_one(sql, read=dialect)
    tree = _lowercase_columns(tree)
    tree = _fold_dates(tree)
    tree = _fold_constants(tree)
    tree = _fold_trivia(tree)
    tree = _fold_null_guards(tree, not_null)
    tree = _values_to_union(tree)
    if schema:
        tree = _expand_stars(tree, schema)

    def step(node: exp.Expression) -> exp.Expression:
        if isinstance(node, exp.Subquery):
            return _inline_projection(node) or node
        if isinstance(node, exp.Select):
            for rule in (_inline_expression_projection, _prune_derived, _merge_spj_source, _wrap_outer_join_aggregate, _collapse_aggregate, _regroup_distinct, _split_aggregates, _distribute, unnest_grouped_source, flatten_grouped_join, _key_aggregates):
                rewritten = rule(node)
                if rewritten is not None:
                    return rewritten
        return node

    for _ in range(8):
        before = tree.sql(dialect="bigquery")
        tree = _fold_null_guards(_fold_count_coalesce(tree.transform(step)), not_null)
        if tree.sql(dialect="bigquery") == before:
            break
    for subquery in list(tree.find_all(exp.Subquery)):
        if subquery.find_ancestor(exp.Select) is not None:
            replacement = _canonicalize_union_source(subquery)
            if replacement is not None:
                subquery.replace(replacement)
    return tree.sql(dialect=dialect)


def prove_equivalent_algebraic(left_sql: str, right_sql: str, **kwargs) -> SmtEquivalenceResult:
    """Normalize both queries algebraically, then run the SMT prover on the result."""

    dialect = kwargs.get("dialect", "bigquery")
    try:
        not_null = {t: c.not_null for t, c in (kwargs.get("constraints") or {}).items()}
        left = normalize(left_sql, schema=kwargs.get("schema"), dialect=dialect, not_null=not_null)
        right = normalize(right_sql, schema=kwargs.get("schema"), dialect=dialect, not_null=not_null)
    except sqlglot.errors.SqlglotError as error:
        return SmtEquivalenceResult(SmtStatus.NOT_PROVEN, f"parse error: {error}")
    from . import scalar_subqueries

    replaced = False
    try:
        inner = {k: v for k, v in kwargs.items() if k != "compare_names"}

        def same(a: str, b: str) -> bool:
            return prove_equivalent_algebraic(a, b, compare_names=False, **inner).proven

        left, right, replaced = scalar_subqueries.unify(
            left, right, dialect=dialect, schema=kwargs.get("schema"), prove=same
        )
    except sqlglot.errors.SqlglotError:
        replaced = False
    result = prove_equivalent_smt(left, right, **kwargs)
    if replaced and result.proven:
        result = dataclasses.replace(
            result, assumptions=tuple(result.assumptions) + (scalar_subqueries.ASSUMPTION,)
        )
    return result
