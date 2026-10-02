"""Exact sizes of sub-joins, computed fast by variable elimination.

A COUNT(*) over an equi-join is a sum-product: every set of columns forced equal
is a variable and every relation a factor holding row counts per value of its
join columns. Summing variables out one at a time (min-degree order) gives the
exact count without materialising the join, which matters for the many-to-many
joins in STATS-CEB and JOB whose results reach billions of rows. Runs in DuckDB.
"""

from __future__ import annotations

from typing import Any

from ..query import JoinQuery


def count_sql(query: JoinQuery, subset: frozenset[str], dialect: str = "duckdb",
              prefiltered: dict[str, str] | None = None) -> str:
    """Variable-elimination COUNT(*) SQL for one sub-join.

    ``prefiltered`` maps aliases to tables that already hold the alias' filtered
    rows (see :func:`materialize_filtered`), so filters are not re-applied.
    """
    aliases = sorted(subset)
    parent: dict[tuple[str, str], tuple[str, str]] = {}

    def find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            x = parent[x]
        return x

    for e in query.edges_within(subset):
        a, b = find((e.left, e.left_col)), find((e.right, e.right_col))
        if a != b:
            parent[max(a, b)] = min(a, b)
    var_of = {x: "v%d" % i for i, x in enumerate(sorted({find(x) for x in list(parent)}))}
    ctes: list[str] = []
    factors: list[tuple[str, set[str]]] = []
    for a in aliases:
        cols: dict[str, str] = {}
        where = [] if prefiltered else [f.sql(dialect=dialect, identify=True) for f in query.filters.get(a, [])]
        for (al, col) in sorted(parent):
            if al != a:
                continue
            var = var_of[find((al, col))]
            if var in cols:
                where.append(f'"{a}"."{cols[var]}" = "{a}"."{col}"')
            else:
                cols[var] = col
        for refs, node in query.residual:
            if refs == {a} and not prefiltered:
                where.append(node.sql(dialect=dialect, identify=True))
        where += [f'"{a}"."{c}" IS NOT NULL' for c in cols.values()]
        sel = [f'"{a}"."{c}" AS {v}' for v, c in cols.items()]
        sql = (f"SELECT {', '.join(sel + ['CAST(COUNT(*) AS HUGEINT) AS c'])} "
               f'FROM "{(prefiltered or query.tables)[a]}" AS "{a}"')
        if where:
            sql += " WHERE " + " AND ".join(f"({w})" for w in where)
        if cols:
            sql += " GROUP BY " + ", ".join(f'"{a}"."{c}"' for c in cols.values())
        name = f"f_{a}"
        ctes.append(f"{name} AS ({sql})")
        factors.append((name, set(cols)))
    step = 0
    while True:
        live = {v for _, vs in factors for v in vs}
        if not live:
            break

        def width(v: str) -> int:
            return len(set().union(*[vs for _, vs in factors if v in vs]))
        var = min(sorted(live), key=width)
        group = [(n, vs) for n, vs in factors if var in vs]
        rest = [(n, vs) for n, vs in factors if var not in vs]
        keep = sorted(set().union(*[vs for _, vs in group]) - {var})
        first = group[0][0]
        sources = first
        for n, _ in group[1:]:
            sources += f" JOIN {n} ON {first}.{var} = {n}.{var}"
        owners = {v: next(n for n, vs in group if v in vs) for v in keep}
        conds = []
        for v in keep:
            hold = [n for n, vs in group if v in vs]
            conds += [f"{hold[0]}.{v} = {h}.{v}" for h in hold[1:]]
        prod = " * ".join(f"{n}.c" for n, _ in group)
        sel = [f"{owners[v]}.{v} AS {v}" for v in keep] + [f"SUM({prod}) AS c"]
        sql = f"SELECT {', '.join(sel)} FROM {sources}"
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        if keep:
            sql += " GROUP BY " + ", ".join(f"{owners[v]}.{v}" for v in keep)
        step += 1
        name = f"g{step}"
        ctes.append(f"{name} AS ({sql})")
        factors = rest + [(name, set(keep))]
    final = " * ".join(f"COALESCE((SELECT SUM(c) FROM {n}), 0)" for n, _ in factors)
    return "WITH " + ",\n".join(ctes) + f"\nSELECT CAST({final} AS DOUBLE)"


def exact_count(con: Any, query: JoinQuery, subset: frozenset[str],
                prefiltered: dict[str, str] | None = None) -> float:
    return float(con.execute(count_sql(query, subset, prefiltered=prefiltered)).fetchone()[0])


def materialize_filtered(con: Any, query: JoinQuery, prefix: str = "_kumosql_f_") -> dict[str, str]:
    """Temp tables with each alias' filtered rows, keeping only its join columns."""
    out = {}
    for a, table in query.tables.items():
        cols = sorted({e.col(a) for e in query.edges if a in (e.left, e.right)})
        sel = ", ".join(f'"{a}"."{c}"' for c in cols) or "1 AS one"
        where = [f.sql(dialect="duckdb", identify=True) for f in query.filters.get(a, [])]
        where += [n.sql(dialect="duckdb", identify=True) for refs, n in query.residual if refs == {a}]
        sql = f'SELECT {sel} FROM "{table}" AS "{a}"'
        if where:
            sql += " WHERE " + " AND ".join(f"({w})" for w in where)
        name = prefix + a
        con.execute(f"CREATE OR REPLACE TEMP TABLE {name} AS {sql}")
        out[a] = name
    return out
