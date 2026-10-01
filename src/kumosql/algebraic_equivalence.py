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

import itertools

import sqlglot
from sqlglot import exp

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
        if isinstance(expr, exp.Column) and expr.name in key_outputs:
            items.append(exp.alias_(key_outputs[expr.name].copy(), name))
        elif _is_agg(expr) and isinstance(expr.this, exp.Column) and expr.this.name in agg_outputs:
            inner_agg = agg_outputs[expr.this.name]
            ok = isinstance(inner_agg, (exp.Sum, exp.Count)) if isinstance(expr, exp.Sum) else type(expr) is type(inner_agg)
            if not ok or not name:
                return None
            items.append(exp.alias_(inner_agg.copy(), name))
        else:
            return None
    result = inner.copy()
    result.set("expressions", items)
    return result


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

    Only exact results are folded (a division must divide evenly, nothing may
    leave the range where FLOAT64 and INT64 agree), so INT64 and FLOAT64
    readings of the expression coincide.
    """

    def step(node: exp.Expression) -> exp.Expression:
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


def normalize(sql: str, *, schema: dict[str, list[str]] | None = None, dialect: str = "bigquery") -> str:
    """Rewrite ``sql`` with the bag-semantics identities above (``dialect`` in and out)."""

    tree = sqlglot.parse_one(sql, read=dialect)
    tree = _fold_constants(tree)
    tree = _values_to_union(tree)
    if schema:
        tree = _expand_stars(tree, schema)

    def step(node: exp.Expression) -> exp.Expression:
        if isinstance(node, exp.Subquery):
            return _inline_projection(node) or node
        if isinstance(node, exp.Select):
            for rule in (_collapse_aggregate, _split_aggregates, _distribute):
                rewritten = rule(node)
                if rewritten is not None:
                    return rewritten
        return node

    for _ in range(4):
        before = tree.sql(dialect="bigquery")
        tree = tree.transform(step)
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
        left = normalize(left_sql, schema=kwargs.get("schema"), dialect=dialect)
        right = normalize(right_sql, schema=kwargs.get("schema"), dialect=dialect)
    except sqlglot.errors.SqlglotError as error:
        return SmtEquivalenceResult(SmtStatus.NOT_PROVEN, f"parse error: {error}")
    return prove_equivalent_smt(left, right, **kwargs)
