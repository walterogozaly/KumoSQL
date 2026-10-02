"""Repairs that make benchmark SQL (printed by Calcite, or written in a benchmark's own notation) mean the
same to KumoSQL's prover and to DuckDB as it did to its authors.

Each repair is a rewrite with one reading; anything ambiguous is left as written, so the pair stays
unknown rather than being scored under a guessed meaning.

* ``rebind_correlation_variables``: Calcite's ``$corN.col`` for a column of a LATERAL subquery.
* ``uniquify_star_columns``: ``SELECT *`` over a join with repeated column names inside a derived table.
* ``expand_row_predicates``: VeriEQL Literature's uninterpreted predicates over whole rows, ``B1(X)``.
* ``fold_table_names``: Cosette's case-insensitive table names (``A`` and ``a`` are one table).
"""

from __future__ import annotations

import sqlglot
import sqlglot.expressions as exp


def _mysql(tree: exp.Expression) -> str:
    """The repaired query as MySQL text.

    sqlglot's MySQL writer spells FULL JOIN as a UNION of one-sided joins; that is another query for the
    prover to read, so a query with one is not repaired at all.
    """

    for join in tree.find_all(exp.Join):
        if (join.side or "").upper() == "FULL":
            raise ValueError("FULL JOIN would be rewritten by the MySQL writer")
    return tree.sql(dialect="mysql")


def _from_items(select) -> list:
    items = [select.args.get(k) for k in ("from", "from_") if select.args.get(k) is not None]
    return [i.this for i in items] + [j.this for j in select.args.get("joins") or []]


def _output_names(query, tables: dict[str, list[str]]) -> list[str] | None:
    """The output column names of a query, expanding ``*``; ``None`` when they cannot be read off."""

    while isinstance(query, (exp.Subquery, exp.Paren)):
        query = query.this
    if isinstance(query, exp.SetOperation):
        return _output_names(query.this, tables)
    if not isinstance(query, exp.Select):
        return None
    names: list[str] = []
    for item in query.expressions:
        if isinstance(item, exp.Star) or (isinstance(item, exp.Column) and isinstance(item.this, exp.Star)):
            wanted = item.table if isinstance(item, exp.Column) else None
            for source in _from_items(query):
                if wanted and source.alias_or_name != wanted:
                    continue
                inner = _source_names(source, tables)
                if inner is None:
                    return None
                names.extend(inner)
        else:
            names.append(item.alias_or_name)
    return names


def _source_names(source, tables: dict[str, list[str]]) -> list[str] | None:
    """The column names one FROM item provides, in order; ``None`` when unknown."""

    if isinstance(source, exp.Lateral):
        source = source.this
    if isinstance(source, exp.Table):
        return tables.get(source.name.lower())
    if isinstance(source, exp.Values):
        alias = source.args.get("alias")
        return [c.name for c in alias.columns] if alias is not None and alias.columns else None
    if isinstance(source, exp.Subquery):
        return _output_names(source.this, tables)
    return None


def rebind_correlation_variables(sql: str, tables: dict[str, list[str]]) -> str:
    """Calcite prints a column of a LATERAL subquery as ``$corN.col`` although ``$corN`` names the outer source.

    ``FROM EMP AS $cor0, LATERAL (SELECT MIN(TRUE) AS $f0 ...) AS t1 WHERE $cor0.$f0 IS NOT NULL`` means
    ``t1.$f0``. A ``$corN.col`` is rebound only when every source of that FROM has known columns, ``col``
    is not one of ``$corN``'s, and exactly one other source has it; anything else is left as written
    (DuckDB then rejects it and the pair stays unknown).
    """

    tree = sqlglot.parse_one(sql, read="mysql")
    changed = False
    for select in tree.find_all(exp.Select):
        sources = {}
        for source in _from_items(select):
            names = _source_names(source, tables)
            sources[source.alias_or_name] = None if names is None else {n.lower() for n in names}
        if not any(alias.startswith("$cor") for alias in sources) or any(v is None for v in sources.values()):
            continue
        for column in select.find_all(exp.Column):
            alias = column.table
            if not alias.startswith("$cor") or alias not in sources or column.name.lower() in sources[alias]:
                continue
            owners = [a for a, names in sources.items() if a != alias and column.name.lower() in names]
            if len(owners) == 1:
                column.set("table", exp.to_identifier(owners[0]))
                changed = True
    return _mysql(tree) if changed else sql


def expand_row_predicates(sql: str, tables: dict[str, list[str]]) -> tuple[str, dict[str, int]]:
    """Literature pairs call uninterpreted predicates on whole rows: ``B1(X)`` where ``X`` names a FROM item.

    Each such argument is spelled out as that row's columns (``B1(X.a, X.b)``), so the prover reads the
    predicate as an uninterpreted function of the row and DuckDB can run it as a macro. Returns the new
    SQL and each predicate's arity (empty when nothing names a row).
    """

    tree = sqlglot.parse_one(sql, read="mysql")
    arities: dict[str, int] = {}
    for call in list(tree.find_all(exp.Anonymous)):
        arguments, expanded = [], False
        for argument in call.expressions:
            row = None
            if isinstance(argument, exp.Column) and not argument.table:
                scope = call.find_ancestor(exp.Select)
                while scope is not None and row is None:
                    for source in _from_items(scope):
                        if source.alias_or_name == argument.name:
                            row = (argument.name, _source_names(source, tables))
                            break
                    scope = scope.find_ancestor(exp.Select)
            if row is None:
                arguments.append(argument)
                continue
            alias, names = row
            if not names:
                return sql, {}
            arguments.extend(exp.column(n, table=alias) for n in names)
            expanded = True
        if expanded:
            call.set("expressions", arguments)
            name = str(call.this)
            if arities.setdefault(name, len(arguments)) != len(arguments):
                return sql, {}  # one name used with two arities: leave the pair as written
    return (_mysql(tree), arities) if arities else (sql, {})


def uniquify_star_columns(sql: str, tables: dict[str, list[str]]) -> str:
    """Spell out ``SELECT *`` inside a derived table when the star repeats a column name.

    ``(SELECT * FROM EMP AS a, EMP AS b) AS t`` has two ``SAL`` columns; ``t.SAL`` means the first
    (Calcite renames later copies, and so does DuckDB, which calls the second ``SAL_1``). The star
    becomes ``a.SAL AS SAL, ..., b.SAL AS SAL_1, ...``, the DuckDB spelling, so the prover reads the
    same columns DuckDB runs. A star whose sources are unnamed or unknown is left alone.
    """

    tree = sqlglot.parse_one(sql, read="mysql")
    changed = False
    for select in reversed(list(tree.find_all(exp.Select))):  # inner derived tables first
        holder = select.parent
        while isinstance(holder, exp.SetOperation):
            holder = holder.parent
        if not isinstance(holder, exp.Subquery) or not any(isinstance(e, exp.Star) for e in select.expressions):
            continue
        sources = _from_items(select)
        aliases = [s.alias_or_name for s in sources]
        if len(set(aliases)) != len(aliases):
            continue
        columns = []
        for source in sources:
            names = _source_names(source, tables)
            if names is None:
                columns = None
                break
            columns.extend((source.alias_or_name, n) for n in names)
        if not columns:
            continue
        lowered = [n.lower() for _, n in columns]
        if len(set(lowered)) == len(lowered):
            continue
        seen: dict[str, int] = {}
        items = []
        for alias, name in columns:
            count = seen.get(name.lower(), 0)
            seen[name.lower()] = count + 1
            output = name if count == 0 else f"{name}_{count}"
            items.append(exp.alias_(exp.column(name, table=alias), output))
        outputs = [item.alias.lower() for item in items]
        if len(set(outputs)) != len(outputs):
            continue  # a renamed copy collides with a real column name: DuckDB's naming would differ
        expanded = []
        for item in select.expressions:
            expanded.extend(items if isinstance(item, exp.Star) else [item])
        select.set("expressions", expanded)
        changed = True
    return _mysql(tree) if changed else sql


def fold_table_names(*queries: str) -> list[str]:
    """Spell each table one way across a pair when the queries spell it in more than one case.

    Cosette, like Calcite, reads ``A`` and ``a`` as one table, but MySQL on Linux and the prover keep
    table names case-sensitive, so ``FROM A`` would be a second, unrelated table. The first spelling
    seen wins; queries that already agree are returned unchanged.
    """

    trees = [sqlglot.parse_one(sql, read="mysql") for sql in queries]
    spellings: dict[str, list[str]] = {}
    for tree in trees:
        for table in tree.find_all(exp.Table):
            seen = spellings.setdefault(table.name.lower(), [])
            if table.name not in seen:
                seen.append(table.name)
    out = []
    for sql, tree in zip(queries, trees):
        changed = False
        for table in tree.find_all(exp.Table):
            first = spellings[table.name.lower()][0]
            if table.name != first:
                table.set("this", exp.to_identifier(first))
                changed = True
        out.append(_mysql(tree) if changed else sql)
    return out
