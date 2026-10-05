"""Random compositions of derived tables, CTEs and star forms, aimed at the structural rewrites of
``algebraic_equivalence`` (flattening, unwrapping, inlining, pruning, pushing filters, lifting a LIMIT, star expansion,
CTE inlining, USING). The templates of ``derived_tables`` and ``derived_stress`` fix each shape by hand; this module
nests them: every relation is a base table or a wrapper (rename / reorder / swap names, filter, DISTINCT, GROUP BY,
ORDER BY .. LIMIT over all columns, computed column, join, UNION ALL, USING join, SELECT * with EXCEPT / REPLACE, CTE
that shadows a table) around another relation, to a depth of two or three, and the whole is read by a projection, an
aggregate, an IN / EXISTS test or a join. Output names are drawn from the names of the base columns so that aliases
shadow and swap them. Duplicate output names are left to the hand-written templates: a name read twice is ambiguous in
BigQuery, so DuckDB resolving it either way is not a difference between a query and its rewrite."""

from __future__ import annotations

import random

from rule_fuzz_gen import make_schema

BASES = {
    "t": ["id", "x", "y"],
    "u": ["k", "w"],
    "p": ["id", "tid"],
}
NAMES = ["id", "x", "y", "k", "w", "a", "b", "c", "tid", "t", "u"]


class Relation:
    """SQL text of a query plus its output column names (all integers)."""

    def __init__(self, sql: str, columns: list[str]):
        self.sql = sql
        self.columns = columns


class Builder:
    def __init__(self, rng: random.Random):
        self.rng = rng
        self.counter = 0
        self.ctes: list[tuple[str, str]] = []

    def alias(self) -> str:
        self.counter += 1
        return self.rng.choice(["d", "e", "q", "t", "u", "p", "r"]) + (str(self.counter) if self.rng.random() < 0.6 else "")

    def base(self) -> Relation:
        name = self.rng.choice(list(BASES))
        cols = BASES[name]
        if self.rng.random() < 0.5:
            return Relation(f"SELECT {', '.join(cols)} FROM {name}", list(cols))
        keep = self.rng.sample(cols, k=self.rng.randint(1, len(cols)))
        return Relation(f"SELECT {', '.join(f'{name}.{c}' for c in keep)} FROM {name}", keep)

    def names(self, count: int, avoid: list[str] | None = None) -> list[str]:
        pool = [n for n in NAMES if n not in (avoid or [])] or NAMES
        return self.rng.sample(pool, k=min(count, len(pool))) + [f"c{i}" for i in range(max(0, count - len(pool)))]

    def relation(self, depth: int) -> Relation:
        if depth <= 0:
            return self.base()
        inner = self.relation(depth - 1)
        kind = self.rng.choice(
            ["project", "project", "filter", "distinct", "group", "limit", "compute", "join", "union", "using", "star", "star_except", "star_replace", "cte", "pass"]
        )
        return getattr(self, "_" + kind)(inner, depth)

    # -- wrappers ------------------------------------------------------------

    def _from(self, inner: Relation) -> tuple[str, str]:
        alias = self.alias()
        return f"({inner.sql}) AS {alias}", alias

    def _pick(self, inner: Relation, low: int = 1) -> list[str]:
        columns = list(dict.fromkeys(inner.columns))
        return self.rng.sample(columns, k=self.rng.randint(min(low, len(columns)), len(columns)))

    def _pass(self, inner: Relation, depth: int) -> Relation:
        source, alias = self._from(inner)
        columns = list(dict.fromkeys(inner.columns))
        return Relation(f"SELECT {', '.join(f'{alias}.{c}' for c in columns)} FROM {source}", columns)

    def _project(self, inner: Relation, depth: int) -> Relation:
        source, alias = self._from(inner)
        picked = self._pick(inner)
        if self.rng.random() < 0.6:
            names = self.names(len(picked), avoid=[] if self.rng.random() < 0.5 else picked)
            items = [f"{alias}.{c} AS {n}" for c, n in zip(picked, names)]
            return Relation(f"SELECT {', '.join(items)} FROM {source}", names)
        return Relation(f"SELECT {', '.join(f'{alias}.{c}' for c in picked)} FROM {source}", picked)

    def _filter(self, inner: Relation, depth: int) -> Relation:
        source, alias = self._from(inner)
        column = self.rng.choice(inner.columns)
        condition = self.rng.choice([f"{alias}.{column} > 0", f"{alias}.{column} IS NOT NULL", f"{alias}.{column} < 3 OR {alias}.{column} IS NULL"])
        columns = list(dict.fromkeys(inner.columns))
        return Relation(f"SELECT {', '.join(f'{alias}.{c}' for c in columns)} FROM {source} WHERE {condition}", columns)

    def _distinct(self, inner: Relation, depth: int) -> Relation:
        source, alias = self._from(inner)
        picked = self._pick(inner)
        return Relation(f"SELECT DISTINCT {', '.join(f'{alias}.{c}' for c in picked)} FROM {source}", picked)

    def _group(self, inner: Relation, depth: int) -> Relation:
        source, alias = self._from(inner)
        key = self.rng.choice(inner.columns)
        value = self.rng.choice(inner.columns)
        aggregate = self.rng.choice([f"SUM({alias}.{value})", f"COUNT(*)", f"MAX({alias}.{value})", f"COUNT({alias}.{value})"])
        names = self.names(2, avoid=[])
        if self.rng.random() < 0.15:
            return Relation(f"SELECT {aggregate} AS {names[0]} FROM {source}", [names[0]])
        having = self.rng.choice(["", "", f" HAVING COUNT(*) > 1"])
        return Relation(f"SELECT {alias}.{key} AS {names[0]}, {aggregate} AS {names[1]} FROM {source} GROUP BY {alias}.{key}{having}", names)

    def _limit(self, inner: Relation, depth: int) -> Relation:
        source, alias = self._from(inner)
        columns = list(dict.fromkeys(inner.columns))
        order = ", ".join(f"{alias}.{c}" for c in self.rng.sample(columns, k=len(columns)))
        offset = self.rng.choice(["", "", " OFFSET 1"])
        return Relation(f"SELECT {', '.join(f'{alias}.{c}' for c in columns)} FROM {source} ORDER BY {order} LIMIT {self.rng.choice([1, 2, 4])}{offset}", columns)

    def _compute(self, inner: Relation, depth: int) -> Relation:
        source, alias = self._from(inner)
        column = self.rng.choice(inner.columns)
        other = self.rng.choice(inner.columns)
        expression = self.rng.choice([f"{alias}.{column} + 1", f"COALESCE({alias}.{column}, 0)", f"{alias}.{column} * {alias}.{other}", f"CASE WHEN {alias}.{column} > 0 THEN 1 ELSE 0 END"])
        name = self.rng.choice(NAMES)
        columns = [c for c in dict.fromkeys(inner.columns) if c != name]
        keep = self.rng.sample(columns, k=self.rng.randint(0, len(columns))) if columns else []
        items = [f"{expression} AS {name}"] + [f"{alias}.{c}" for c in keep]
        self.rng.shuffle(items)
        return Relation(f"SELECT {', '.join(items)} FROM {source}", [name] + keep)

    def _join(self, inner: Relation, depth: int) -> Relation:
        other = self.relation(depth - 1)
        left, a = self._from(inner)
        right, b = self._from(other)
        join = self.rng.choice(["JOIN", "JOIN", "LEFT JOIN", "RIGHT JOIN", "FULL JOIN"])
        on = f"{a}.{self.rng.choice(inner.columns)} = {b}.{self.rng.choice(other.columns)}"
        picked = [f"{a}.{c}" for c in self._pick(inner)] + [f"{b}.{c}" for c in self._pick(other)]
        names = self.names(len(picked))
        items = [f"{p} AS {n}" for p, n in zip(picked, names)]
        return Relation(f"SELECT {', '.join(items)} FROM {left} {join} {right} ON {on}", names)

    def _using(self, inner: Relation, depth: int) -> Relation:
        other = self.relation(depth - 1)
        shared = [c for c in dict.fromkeys(inner.columns) if c in other.columns]
        if not shared:
            return self._project(inner, depth)
        left, a = self._from(inner)
        right, b = self._from(other)
        join = self.rng.choice(["JOIN", "LEFT JOIN", "RIGHT JOIN", "FULL JOIN"])
        key = self.rng.choice(shared)
        rest = [c for c in dict.fromkeys(inner.columns + other.columns) if c != key]
        picked = self.rng.sample(rest, k=self.rng.randint(0, len(rest))) if rest else []
        if any(c in inner.columns and c in other.columns for c in picked):
            picked = [c for c in picked if not (c in inner.columns and c in other.columns)]
        # the other shared columns would be ambiguous bare; USING one key only when nothing else is shared
        if any(c != key for c in shared):
            return self._project(inner, depth)
        return Relation(f"SELECT {', '.join([key] + picked)} FROM {left} {join} {right} USING ({key})", [key] + picked)

    def _union(self, inner: Relation, depth: int) -> Relation:
        other = self.relation(depth - 1)
        width = min(len(dict.fromkeys(inner.columns)), len(dict.fromkeys(other.columns)), 2)
        names = self.names(width)
        left = list(dict.fromkeys(inner.columns))[:width]
        right = list(dict.fromkeys(other.columns))[:width]
        l, a = self._from(inner)
        r, b = self._from(other)
        first = ", ".join(f"{a}.{c} AS {n}" for c, n in zip(left, names))
        second = ", ".join(f"{b}.{c}" for c in right)
        return Relation(f"SELECT {first} FROM {l} UNION ALL SELECT {second} FROM {r}", names)

    def _star(self, inner: Relation, depth: int) -> Relation:
        source, alias = self._from(inner)
        columns = inner.columns
        if len(set(columns)) != len(columns):
            return self._pass(inner, depth)
        return Relation(f"SELECT {self.rng.choice(['*', alias + '.*'])} FROM {source}", columns)

    def _star_except(self, inner: Relation, depth: int) -> Relation:
        source, alias = self._from(inner)
        columns = inner.columns
        if len(set(columns)) != len(columns) or len(columns) < 2:
            return self._pass(inner, depth)
        dropped = self.rng.choice(columns)
        return Relation(f"SELECT * EXCEPT ({dropped}) FROM {source}", [c for c in columns if c != dropped])

    def _star_replace(self, inner: Relation, depth: int) -> Relation:
        source, alias = self._from(inner)
        columns = inner.columns
        if len(set(columns)) != len(columns):
            return self._pass(inner, depth)
        changed = self.rng.choice(columns)
        return Relation(f"SELECT * REPLACE ({changed} + 1 AS {changed}) FROM {source}", columns)

    def _cte(self, inner: Relation, depth: int) -> Relation:
        """The inner relation becomes a CTE (named like a base table half the time) read by the next wrapper."""

        name = self.rng.choice(["t", "u", "p", "c", "c", "w"])
        if any(existing == name for existing, _ in self.ctes) or (name in BASES and not set(BASES[name]) <= set(inner.columns)):
            name = "c" if not any(existing == "c" for existing, _ in self.ctes) else "w"
            if any(existing == name for existing, _ in self.ctes):
                return self._pass(inner, depth)
        self.ctes.append((name, inner.sql))
        columns = list(dict.fromkeys(inner.columns))
        return Relation(f"SELECT {', '.join(f'{name}.{c}' for c in columns)} FROM {name}", columns)

    # -- the reader ----------------------------------------------------------------

    def reader(self, inner: Relation) -> str:
        source, alias = self._from(inner)
        columns = list(dict.fromkeys(inner.columns))
        column = self.rng.choice(columns)
        kind = self.rng.choice(["project", "project", "aggregate", "group", "filter", "in", "exists", "join", "star", "order"])
        if kind == "aggregate":
            return f"SELECT {self.rng.choice(['COUNT(*)', f'SUM({alias}.{column})', f'MAX({alias}.{column})'])} AS n FROM {source}"
        if kind == "group":
            return f"SELECT {alias}.{column} AS g, COUNT(*) AS n FROM {source} GROUP BY {alias}.{column}"
        if kind == "filter":
            return f"SELECT {', '.join(f'{alias}.{c}' for c in columns)} FROM {source} WHERE {alias}.{column} {self.rng.choice(['> 1', 'IS NULL', 'IS NOT NULL'])}"
        if kind == "in":
            return f"SELECT t.id FROM t WHERE t.y {self.rng.choice(['IN', 'NOT IN'])} (SELECT {alias}.{column} FROM {source})"
        if kind == "exists":
            return f"SELECT t.id FROM t WHERE {self.rng.choice(['EXISTS', 'NOT EXISTS'])} (SELECT 1 FROM {source} WHERE {alias}.{column} = t.x)"
        if kind == "join":
            return f"SELECT {alias}.{column}, u.w FROM {source} {self.rng.choice(['JOIN', 'LEFT JOIN'])} u ON u.k = {alias}.{column}"
        if kind == "star":
            return f"SELECT * FROM {source}" if len(set(columns)) == len(inner.columns) else f"SELECT {alias}.{column} FROM {source}"
        if kind == "order":
            return f"SELECT {alias}.{column} FROM {source} ORDER BY {', '.join(f'{alias}.{c}' for c in columns)} LIMIT 3"
        picked = self.rng.sample(columns, k=self.rng.randint(1, len(columns)))
        return f"SELECT {', '.join(f'{alias}.{c}' for c in picked)} FROM {source}"

    def query(self) -> str:
        inner = self.relation(self.rng.choice([1, 2, 2, 3]))
        sql = self.reader(inner)
        if self.ctes:
            sql = "WITH " + ", ".join(f"{name} AS ({body})" for name, body in self.ctes) + " " + sql
        return sql


def cases(seed: int, count: int) -> list[dict]:
    rng = random.Random(seed)
    result = []
    for index in range(count):
        schema, constraints = make_schema(rng)
        sql = Builder(rng).query()
        result.append(
            {
                "sql": sql,
                "dialect": "bigquery",
                "schema": schema,
                "constraints": constraints,
                "options": {},
                "source": f"derived_compose:{seed}:{index}",
            }
        )
    return result
