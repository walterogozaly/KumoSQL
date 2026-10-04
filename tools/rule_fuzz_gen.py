"""Typed random BigQuery queries for ``tools/rule_fuzz.py``.

The generator keeps a scope stack while it writes a query, so every column it names resolves (innermost scope
first, as BigQuery and DuckDB do) to the source it meant, and it deliberately reuses aliases and output names
across scopes. It leans toward the shapes behind past false proofs:

* empty and one-row inputs, NULLs, duplicates and ties come from the databases, so queries read nullable
  columns, non-key columns and duplicate-producing joins;
* global aggregates (one row even over no rows), COUNT/SUM/MIN/MAX/AVG with and without DISTINCT, COUNTIF,
  HAVING, GROUP BY expressions, ROLLUP, CUBE and GROUPING SETS with GROUPING();
* DISTINCT, DISTINCT ON, set operations (ALL and DISTINCT, INTERSECT and EXCEPT) with ORDER BY/LIMIT tails on a
  branch or on the whole operation;
* correlated EXISTS, IN, NOT IN, ANY/ALL and scalar subqueries, shadowed aliases, CTEs that shadow a table;
* outer joins (LEFT, RIGHT, FULL), USING, CROSS and comma joins, ON conditions that read one side only;
* INT64, FLOAT64 and NUMERIC mixed in arithmetic, CASE, COALESCE and set operations; window functions and
  QUALIFY.
"""

from __future__ import annotations

import random

INT, FLOAT, NUM, STR, BOOL, DATE = "INT64", "FLOAT64", "NUMERIC", "STRING", "BOOL", "DATE"
NUMERIC_TYPES = (INT, FLOAT, NUM)

BASE_SCHEMA = {
    "t": [["id", INT], ["x", INT], ["y", INT], ["s", STR], ["f", FLOAT], ["d", DATE]],
    "u": [["k", INT], ["v", STR], ["w", INT]],
    "p": [["id", INT], ["tid", INT], ["n", NUM], ["b", BOOL]],
}


def make_schema(rng: random.Random) -> tuple[dict, dict]:
    schema = {t: [list(c) for c in cols] for t, cols in BASE_SCHEMA.items()}
    constraints: dict = {"t": {"not_null": ["id"], "keys": [["id"]]}, "p": {"not_null": ["id"], "keys": [["id"]]}}
    if rng.random() < 0.3:
        constraints["t"]["not_null"].append("x")
    if rng.random() < 0.5:
        constraints["u"] = {"not_null": ["k"], "keys": [["k"]]}
        if rng.random() < 0.4:
            constraints["t"].setdefault("foreign_keys", []).append([["y"], "u", ["k"]])
    if rng.random() < 0.4:
        constraints["p"]["foreign_keys"] = [[["tid"], "t", ["id"]]]
    if rng.random() < 0.2:
        constraints["p"]["not_null"].append("tid")
    return schema, constraints


class Source:
    __slots__ = ("alias", "cols")

    def __init__(self, alias: str, cols: list[tuple[str, str]]):
        self.alias = alias
        self.cols = cols


class Scope:
    def __init__(self, sources: list[Source] | None = None):
        self.sources = sources or []


class Gen:
    def __init__(self, rng: random.Random, schema: dict, constraints: dict):
        self.rng = rng
        self.schema = schema
        self.constraints = constraints
        self.ctes: dict[str, list[tuple[str, str]]] = {}
        self.fresh = 0

    # -- helpers ------------------------------------------------------------

    def chance(self, p: float) -> bool:
        return self.rng.random() < p

    def pick(self, items):
        return self.rng.choice(list(items))

    def name(self, prefix: str = "c") -> str:
        self.fresh += 1
        return f"{prefix}{self.fresh}"

    def visible(self, stack: list[Scope], kind: str | None = None, outer: bool = True) -> list[tuple[str, str, int]]:
        """``(reference, type, depth)`` for every column the innermost scope can name (outer scopes if ``outer``)."""

        refs = []
        hidden: set = set()
        scopes = stack[::-1] if outer else stack[-1:]
        for depth, scope in enumerate(scopes):
            for source in scope.sources:
                if source.alias in hidden:
                    continue
                for col, typ in source.cols:
                    if kind is None or typ == kind or (kind == "num" and typ in NUMERIC_TYPES):
                        refs.append((f"{source.alias}.{col}", typ, depth))
            hidden |= {s.alias for s in scope.sources}
        return refs

    def unqualified(self, stack: list[Scope], ref: str) -> str:
        """``ref`` without its alias when that still resolves to the same column."""

        alias, col = ref.split(".", 1)
        for scope in stack[::-1]:
            owners = [s for s in scope.sources if any(c == col for c, _ in s.cols)]
            if owners:
                return col if len(owners) == 1 and owners[0].alias == alias and col != alias else ref
        return ref

    def column(self, stack: list[Scope], kind: str | None = None, outer: float = 0.15) -> tuple[str, str] | None:
        refs = self.visible(stack, kind)
        if not refs:
            return None
        inner = [r for r in refs if r[2] == 0]
        pool = refs if (not inner or self.chance(outer)) else inner
        ref, typ, _ = self.pick(pool)
        if self.chance(0.25):
            ref = self.unqualified(stack, ref)
        return ref, typ

    # -- literals -----------------------------------------------------------

    def literal(self, kind: str) -> str:
        if kind == INT:
            return self.pick(["0", "1", "2", "-1", "3"]) if not self.chance(0.06) else "CAST(NULL AS INT64)"
        if kind == FLOAT:
            return self.pick(["0.5", "1.5", "-1.0", "2.0", "0.1"]) if not self.chance(0.06) else "CAST(NULL AS FLOAT64)"
        if kind == NUM:
            return self.pick(["NUMERIC '1.5'", "NUMERIC '0'", "NUMERIC '0.1'", "CAST(2 AS NUMERIC)"])
        if kind == STR:
            return self.pick(["''", "'a'", "'b'", "'ab'"]) if not self.chance(0.06) else "CAST(NULL AS STRING)"
        if kind == BOOL:
            return self.pick(["TRUE", "FALSE"]) if not self.chance(0.08) else "CAST(NULL AS BOOL)"
        if kind == DATE:
            return self.pick(["DATE '2024-01-01'", "DATE '2024-02-29'", "DATE '2023-12-31'"])
        raise ValueError(kind)

    # -- scalar expressions ---------------------------------------------------

    def expr(self, stack: list[Scope], kind: str, depth: int = 2, agg=False) -> str:
        """An expression of type ``kind``. ``agg`` (the group keys, a list of ``(sql, type)``, possibly empty) makes
        it a grouped select's output: aggregates, group keys and constants only."""

        if agg is not False and self.chance(0.55):
            return self.aggregate(stack, kind, depth)
        r = self.rng.random()
        if depth <= 0 or r < 0.45:
            if agg is not False:
                keys = [k for k in agg if k[1] == kind]
                if keys and self.chance(0.8):
                    return self.pick(keys)[0]
                return self.aggregate(stack, kind, depth) if self.chance(0.6) else self.literal(kind)
            if self.chance(0.85):
                found = self.column(stack, kind)
                if found:
                    return found[0]
            return self.literal(kind)
        d = depth - 1
        if kind == BOOL:
            return f"({self.pred(stack, d, agg=agg)})"
        if r < 0.55:
            return f"CASE WHEN {self.pred(stack, d, agg=agg)} THEN {self.expr(stack, kind, d, agg)} ELSE {self.expr(stack, kind, d, agg)} END"
        if r < 0.62:
            return f"IF({self.pred(stack, d, agg=agg)}, {self.expr(stack, kind, d, agg)}, {self.expr(stack, kind, d, agg)})"
        if r < 0.70:
            return f"COALESCE({self.expr(stack, kind, d, agg)}, {self.expr(stack, kind, d, agg)})"
        if r < 0.74:
            return f"NULLIF({self.expr(stack, kind, d, agg)}, {self.expr(stack, kind, d, agg)})"
        if r < 0.78 and agg is False:
            return self.scalar_subquery(stack, kind, d)
        if kind == INT:
            op = self.rng.random()
            if op < 0.4:
                return f"({self.expr(stack, INT, d, agg)} {self.pick(['+', '-', '*'])} {self.expr(stack, INT, d, agg)})"
            if op < 0.5:
                return f"-{self.expr(stack, INT, d, agg)}"
            if op < 0.6:
                return f"ABS({self.expr(stack, INT, d, agg)})"
            if op < 0.7:
                return f"MOD({self.expr(stack, INT, d, agg)}, {self.pick(['2', '3', '-2'])})"
            if op < 0.8:
                return f"DIV({self.expr(stack, INT, d, agg)}, {self.pick(['2', '3'])})"
            if op < 0.9:
                return f"LENGTH({self.expr(stack, STR, d, agg)})"
            return f"EXTRACT({self.pick(['YEAR', 'MONTH', 'DAY'])} FROM {self.expr(stack, DATE, d, agg)})"
        if kind == FLOAT:
            op = self.rng.random()
            if op < 0.3:
                return f"CAST({self.expr(stack, INT, d, agg)} AS FLOAT64)"
            if op < 0.5:
                return f"({self.expr(stack, self.pick(NUMERIC_TYPES), d, agg)} / NULLIF({self.expr(stack, INT, d, agg)}, 0))"
            if op < 0.65:
                return f"SAFE_DIVIDE({self.expr(stack, INT, d, agg)}, {self.expr(stack, INT, d, agg)})"
            if op < 0.85:
                return f"({self.expr(stack, FLOAT, d, agg)} {self.pick(['+', '-', '*'])} {self.expr(stack, self.pick([INT, FLOAT]), d, agg)})"
            return f"({self.expr(stack, INT, d, agg)} * 1.5)"
        if kind == NUM:
            op = self.rng.random()
            if op < 0.4:
                return f"CAST({self.expr(stack, INT, d, agg)} AS NUMERIC)"
            return f"({self.expr(stack, NUM, d, agg)} {self.pick(['+', '-'])} {self.expr(stack, self.pick([NUM, INT]), d, agg)})"
        if kind == STR:
            op = self.rng.random()
            if op < 0.3:
                return f"{self.pick(['UPPER', 'LOWER'])}({self.expr(stack, STR, d, agg)})"
            if op < 0.5:
                return f"SUBSTR({self.expr(stack, STR, d, agg)}, 1, 1)"
            if op < 0.7:
                return f"CAST({self.expr(stack, INT, d, agg)} AS STRING)"
            return f"({self.expr(stack, STR, d, agg)} || {self.literal(STR)})"
        if kind == DATE:
            if self.chance(0.5):
                return f"DATE_ADD({self.expr(stack, DATE, d, agg)}, INTERVAL {self.pick(['1', '-1', '30'])} DAY)"
            return f"DATE_TRUNC({self.expr(stack, DATE, d, agg)}, MONTH)"
        return self.literal(kind)

    def aggregate(self, stack: list[Scope], kind: str, depth: int) -> str:
        d = max(0, depth - 1)
        r = self.rng.random()
        if kind == INT:
            if r < 0.25:
                return "COUNT(*)"
            if r < 0.4:
                return f"COUNT({self.expr(stack, self.pick([INT, STR, FLOAT]), d)})"
            if r < 0.5:
                return f"COUNT(DISTINCT {self.expr(stack, self.pick([INT, STR]), d)})"
            if r < 0.62:
                return f"SUM({'DISTINCT ' if self.chance(0.2) else ''}{self.expr(stack, INT, d)})"
            if r < 0.8:
                return f"{self.pick(['MIN', 'MAX'])}({self.expr(stack, INT, d)})"
            if r < 0.88:
                return f"COUNTIF({self.pred(stack, d)})"
            if r < 0.94:
                return f"(COUNT(*) {self.pick(['+', '-', '*'])} {self.literal(INT)})"
            return f"COALESCE(SUM({self.expr(stack, INT, d)}), 0)"
        if kind == FLOAT:
            if r < 0.5:
                return f"AVG({'DISTINCT ' if self.chance(0.15) else ''}{self.expr(stack, self.pick([INT, FLOAT]), d)})"
            if r < 0.8:
                return f"{self.pick(['SUM', 'MIN', 'MAX'])}({self.expr(stack, FLOAT, d)})"
            return f"SUM({self.expr(stack, INT, d)}) / NULLIF(COUNT(*), 0)"
        if kind == NUM:
            return f"{self.pick(['SUM', 'MIN', 'MAX'])}({self.expr(stack, NUM, d)})"
        if kind == STR:
            return f"{self.pick(['MIN', 'MAX'])}({self.expr(stack, STR, d)})"
        if kind == BOOL:
            if r < 0.4:
                return f"{self.pick(['LOGICAL_AND', 'LOGICAL_OR'])}({self.pred(stack, d)})"
            return f"{self.aggregate(stack, INT, d)} {self.pick(['>', '>=', '=', '<>'])} {self.pick(['0', '1', '2'])}"
        if kind == DATE:
            return f"{self.pick(['MIN', 'MAX'])}({self.expr(stack, DATE, d)})"
        return self.literal(kind)

    def comparable(self) -> str:
        return self.rng.choices([INT, STR, FLOAT, NUM, DATE, BOOL], weights=[10, 3, 2, 1, 1, 1])[0]

    def pred(self, stack: list[Scope], depth: int = 2, agg=False) -> str:
        r = self.rng.random()
        if depth > 0 and r < 0.12:
            return f"({self.pred(stack, depth - 1, agg)} AND {self.pred(stack, depth - 1, agg)})"
        if depth > 0 and r < 0.2:
            return f"({self.pred(stack, depth - 1, agg)} OR {self.pred(stack, depth - 1, agg)})"
        if depth > 0 and r < 0.25:
            return f"NOT ({self.pred(stack, depth - 1, agg)})"
        if depth > 0 and r < 0.29:
            return f"({self.pred(stack, depth - 1, agg)}) IS {self.pick(['TRUE', 'NOT TRUE', 'FALSE', 'NOT FALSE', 'NULL', 'NOT NULL'])}"
        kind = self.comparable()
        d = max(0, depth - 1)
        a = r
        if a < 0.5:
            op = self.pick(["=", "<>", "<", "<=", ">", ">="])
            return f"({self.expr(stack, kind, d, agg)}) {op} ({self.expr(stack, kind, d, agg)})" if kind == BOOL else f"{self.expr(stack, kind, d, agg)} {op} {self.expr(stack, kind, d, agg)}"
        if a < 0.58:
            return f"({self.expr(stack, kind, d, agg)}) IS {self.pick(['', 'NOT '])}NULL" if kind == BOOL else f"{self.expr(stack, kind, d, agg)} IS {self.pick(['', 'NOT '])}NULL"
        if a < 0.64:
            items = [self.literal(INT) for _ in range(self.rng.randint(1, 3))] + (["NULL"] if self.chance(0.25) else [])
            return f"{self.expr(stack, INT, d, agg)} {self.pick(['', 'NOT '])}IN ({', '.join(items)})"
        if a < 0.68:
            if kind == BOOL:
                kind = INT
            return f"{self.expr(stack, kind, d, agg)} IS {self.pick(['', 'NOT '])}DISTINCT FROM {self.expr(stack, kind, d, agg)}"
        if a < 0.71:
            lo = self.rng.randint(-1, 2)
            return f"{self.expr(stack, INT, d, agg)} {self.pick(['', 'NOT '])}BETWEEN {lo} AND {lo + self.rng.randint(0, 2)}"
        if a < 0.74:
            pattern = self.pick(["'a%'", "'%'", "'_'", "'%b'", "''"])
            return f"{self.expr(stack, STR, d, agg)} {self.pick(['', 'NOT '])}LIKE {pattern}"
        if a < 0.77:
            found = self.column(stack, BOOL)
            if found:
                return found[0]
        if depth > 0 and agg is False and a < 0.84:
            return f"{self.pick(['', 'NOT '])}EXISTS ({self.subquery(stack, depth - 1, cols=None)})"
        if depth > 0 and agg is False and a < 0.92:
            kind = self.pick([INT, INT, STR])
            return f"{self.expr(stack, kind, 0)} {self.pick(['', 'NOT '])}IN ({self.subquery(stack, depth - 1, cols=[kind])})"
        if depth > 0 and agg is False and a < 0.95:
            return f"{self.expr(stack, INT, 0)} {self.pick(['=', '<>', '<', '>', '<=', '>='])} {self.pick(['ANY', 'ALL'])} ({self.subquery(stack, depth - 1, cols=[INT])})"
        kind = INT
        return f"{self.expr(stack, kind, d, agg)} {self.pick(['=', '<', '>='])} {self.expr(stack, kind, d, agg)}"

    def scalar_subquery(self, stack: list[Scope], kind: str, depth: int) -> str:
        """A one-row subquery: an ungrouped aggregate, or ORDER BY a key LIMIT 1."""

        scope = Scope()
        from_sql = self.from_clause(stack + [scope], max(0, depth - 1), allow_joins=self.chance(0.25))
        inner = stack + [scope]
        where = f" WHERE {self.pred(inner, 1)}" if self.chance(0.75) else ""
        if self.chance(0.75):
            return f"(SELECT {self.aggregate(inner, kind, 1)} FROM {from_sql}{where})"
        keyed = [s for s in scope.sources if s.cols and s.cols[0][0] in ("id", "k")]
        if keyed and len(scope.sources) == 1:
            return f"(SELECT {self.expr(inner, kind, 1)} FROM {from_sql}{where} ORDER BY {keyed[0].alias}.{keyed[0].cols[0][0]} LIMIT 1)"
        return f"(SELECT {self.aggregate(inner, kind, 1)} FROM {from_sql}{where})"

    # -- sources ------------------------------------------------------------

    def alias_for(self, stack: list[Scope], table: str) -> str:
        used = {s.alias for s in stack[-1].sources}
        outer = [s.alias for scope in stack[:-1] for s in scope.sources]
        options = [table, table, "a", "b", "c", "t", "u", "p", "q"] + (outer if self.chance(0.4) else [])
        for _ in range(10):
            alias = self.pick(options)
            if alias not in used:
                return alias
        return self.name("s")

    def source(self, stack: list[Scope], depth: int) -> tuple[str, Source]:
        options = ["table"] * 10 + (["cte"] * 4 if self.ctes else []) + (["derived"] * 5 if depth > 0 else []) + ["unnest"]
        choice = self.pick(options)
        if choice == "cte":
            name = self.pick(self.ctes)
            alias = self.alias_for(stack, name)
            return (f"{name} AS {alias}" if alias != name else name), Source(alias, list(self.ctes[name]))
        if choice == "derived":
            sql, cols = self.query(stack[:-1], depth - 1, derived=True)
            alias = self.alias_for(stack, self.pick(["d", "q", "t", "u"]))
            return f"({sql}) AS {alias}", Source(alias, cols)
        if choice == "unnest":
            alias = self.alias_for(stack, "e")
            values = self.pick(["[1, 2, 2]", "[1, CAST(NULL AS INT64), 3]", "[0]", "CAST([] AS ARRAY<INT64>)"])
            return f"(SELECT e AS val FROM UNNEST({values}) AS e) AS {alias}", Source(alias, [("val", INT)])
        table = self.rng.choices(list(self.schema), weights=[5, 3, 2][: len(self.schema)])[0]
        alias = self.alias_for(stack, table)
        cols = [(c, t) for c, t in self.schema[table]]
        text = table if alias == table else f"{table} AS {alias}"
        return text, Source(alias, cols)

    def join_condition(self, stack: list[Scope], left: list[Source], right: Source) -> str:
        r = self.rng.random()
        lrefs = [(f"{s.alias}.{c}", t) for s in left for c, t in s.cols]
        rrefs = [(f"{right.alias}.{c}", t) for c, t in right.cols]
        pairs = [(a, b) for a, ta in lrefs for b, tb in rrefs if ta == tb]
        if pairs and r < 0.65:
            a, b = self.pick(pairs)
            cond = f"{a} = {b}"
            if self.chance(0.3):
                cond += f" AND {self.pred(stack, 0)}"  # DuckDB cannot run a subquery in an outer join's ON
            elif self.chance(0.1):
                cond += f" OR {self.pred(stack, 0)}"
            return cond
        if r < 0.75:
            return self.pick(["TRUE", "FALSE"])
        if pairs and r < 0.85:
            a, b = self.pick(pairs)
            return f"{a} {self.pick(['<', '<=', '<>', '>'])} {b}"
        return self.pred(stack, 0)

    def from_clause(self, stack: list[Scope], depth: int, allow_joins: bool = True) -> str:
        scope = stack[-1]
        text, source = self.source(stack, depth)
        scope.sources.append(source)
        joins = 0
        while allow_joins and joins < 3 and self.chance(0.45 if joins == 0 else 0.25):
            joins += 1
            kind = self.rng.choices(["JOIN", "LEFT JOIN", "RIGHT JOIN", "FULL JOIN", "CROSS JOIN", ","], weights=[5, 5, 2, 2, 1, 1])[0]
            rtext, right = self.source(stack, max(0, depth - 1))
            left = list(scope.sources)
            if kind in ("CROSS JOIN", ","):
                scope.sources.append(right)
                text += f" {kind} {rtext}" if kind != "," else f", {rtext}"
                continue
            common = sorted({c for c, _ in right.cols} & {c for s in left for c, _ in s.cols})
            if common and self.chance(0.15) and len(left) == 1 and all(sum(1 for c2, _ in s.cols if c2 == c) <= 1 for s in left + [right] for c in common):
                column = self.pick(common)
                lt = next(t for s in left for c, t in s.cols if c == column)
                rt = next(t for c, t in right.cols if c == column)
                if lt == rt:
                    scope.sources.append(right)
                    # USING merges the column; later references stay qualified, which both engines accept
                    text += f" {kind} {rtext} USING ({column})"
                    continue
            scope.sources.append(right)
            text += f" {kind} {rtext} ON {self.join_condition(stack, left, right)}"
        return text

    # -- queries ------------------------------------------------------------

    def subquery(self, stack: list[Scope], depth: int, cols: list[str] | None) -> str:
        """A subquery for EXISTS (``cols`` None) or IN/ANY (``cols`` the one output type); it may correlate."""

        scope = Scope()
        inner = stack + [scope]
        from_sql = self.from_clause(inner, depth, allow_joins=self.chance(0.3))
        conds = []
        outer_refs = [r for r in self.visible(inner) if r[2] > 0]
        if outer_refs and self.chance(0.7):
            ref, typ, _ = self.pick(outer_refs)
            mine = [r for r in self.visible(inner, typ, outer=False)]
            if mine:
                conds.append(f"{self.pick(mine)[0]} {self.pick(['=', '=', '<>', '<'])} {ref}")
        if self.chance(0.5):
            conds.append(self.pred(inner, 1))
        where = f" WHERE {' AND '.join(conds)}" if conds else ""
        if cols is None:
            if self.chance(0.15):
                return f"SELECT {self.aggregate(inner, INT, 1)} FROM {from_sql}{where}"  # always one row
            tail = " LIMIT 0" if self.chance(0.05) else ""
            item = self.pick(["1", "*", self.expr(inner, INT, 0)]) if scope.sources[0].cols else "1"
            return f"SELECT {item} FROM {from_sql}{where}{tail}"
        if self.chance(0.2):
            return f"SELECT {self.aggregate(inner, cols[0], 1)} FROM {from_sql}{where}"
        distinct = "DISTINCT " if self.chance(0.15) else ""
        return f"SELECT {distinct}{self.expr(inner, cols[0], 1)} FROM {from_sql}{where}"

    def items(self, stack: list[Scope], kinds: list[str], agg: bool, names: list[str]) -> list[str]:
        return [f"{self.expr(stack, kind, 2, agg)} AS {name}" for kind, name in zip(kinds, names)]

    def out_names(self, n: int) -> list[str]:
        pool = ["id", "x", "y", "k", "v", "s", "n"]
        names: list[str] = []
        for _ in range(n):
            if self.chance(0.4):
                name = self.pick(pool)
                if name not in names:
                    names.append(name)
                    continue
            names.append(self.name("c"))
        return names

    def select(self, stack: list[Scope], depth: int, kinds: list[str] | None = None, derived: bool = False) -> tuple[str, list[tuple[str, str]]]:
        scope = Scope()
        inner = stack + [scope]
        from_sql = self.from_clause(inner, depth)
        n = len(kinds) if kinds else self.rng.choice([1, 1, 2, 2, 3])
        if not kinds:
            available = [t for _, t, _ in self.visible(inner, outer=False)]
            kinds = [self.pick(available) if available and self.chance(0.7) else self.pick([INT, INT, INT, STR, FLOAT, NUM, BOOL, DATE]) for _ in range(n)]
        names = self.out_names(len(kinds))
        where = f" WHERE {self.pred(inner, 2)}" if self.chance(0.55) else ""
        mode = self.rng.random()
        group = having = qualify = ""
        distinct = ""
        order_limit = ""
        if mode < 0.2:
            # global aggregate: one row even over an empty input
            items = [f"{self.aggregate(inner, kind, 2) if self.chance(0.7) else self.expr(inner, kind, 2, [])} AS {name}" for kind, name in zip(kinds, names)]
            if self.chance(0.25):
                having = f" HAVING {self.pred(inner, 1, agg=[])}"
        elif mode < 0.5:
            keys = []
            for _ in range(self.rng.choice([1, 1, 2])):
                found = self.column(inner, None, outer=0.0)
                if found and found[0] not in [k[0] for k in keys]:
                    keys.append(found)
            if self.chance(0.15) or not keys:
                keys = [(f"({self.expr(inner, INT, 1)})", INT)] + keys[1:]
            key_sql = [k[0] for k in keys]
            items = []
            for i, (kind, name) in enumerate(zip(kinds, names)):
                matching = [k for k in keys if k[1] == kind]
                if matching and self.chance(0.45):
                    items.append(f"{self.pick(matching)[0]} AS {name}")
                elif self.chance(0.6):
                    items.append(f"{self.aggregate(inner, kind, 2)} AS {name}")
                else:
                    items.append(f"{self.expr(inner, kind, 2, keys)} AS {name}")
            g = self.rng.random()
            if g < 0.7:
                group = f" GROUP BY {', '.join(key_sql)}"
            elif g < 0.8:
                group = f" GROUP BY ROLLUP ({', '.join(key_sql)})"
            elif g < 0.88:
                group = f" GROUP BY CUBE ({', '.join(key_sql)})"
            else:
                sets = ["(" + ", ".join(key_sql) + ")", "()"] + [f"({k})" for k in key_sql[:1]]
                group = f" GROUP BY GROUPING SETS ({', '.join(self.rng.sample(sets, self.rng.randint(1, len(sets))))})"
            if "ROLLUP" in group or "CUBE" in group or "GROUPING SETS" in group:
                if self.chance(0.4) and kinds[-1] == INT:
                    items[-1] = f"GROUPING({key_sql[0]}) AS {names[-1]}"
            if self.chance(0.3):
                having = f" HAVING {self.pred(inner, 1, agg=keys)}"
        else:
            items = self.items(inner, kinds, False, names)
            if self.chance(0.15):
                # a window output
                part = self.column(inner, None, outer=0.0)
                orderc = self.column(inner, None, outer=0.0)
                partition = f"PARTITION BY {part[0]}" if part and self.chance(0.7) else ""
                order = f"ORDER BY {orderc[0]}" if orderc and self.chance(0.7) else ""
                func = self.pick(["ROW_NUMBER()", "RANK()", "DENSE_RANK()", "COUNT(*)", f"SUM({self.expr(inner, INT, 0)})", f"MIN({self.expr(inner, INT, 0)})", f"MAX({self.expr(inner, INT, 0)})"])
                if func in ("ROW_NUMBER()", "RANK()", "DENSE_RANK()") and not order:
                    order = f"ORDER BY {orderc[0]}" if orderc else ""
                if func in ("ROW_NUMBER()", "RANK()", "DENSE_RANK()") and not order:
                    func = "COUNT(*)"
                window = f"{func} OVER ({partition} {order})".replace("( ", "(").replace(" )", ")")
                if kinds[-1] == INT:
                    items[-1] = f"{window} AS {names[-1]}"
                elif self.chance(0.5):
                    qualify = f" QUALIFY {window} {self.pick(['= 1', '<= 1', '> 1'])}"
            if self.chance(0.2):
                distinct = "DISTINCT "
            elif self.chance(0.04):
                key = self.column(inner, None, outer=0.0)
                if key:
                    distinct = f"DISTINCT ON ({key[0]}) "
        if self.chance(0.12 if not derived else 0.2):
            keyed = [s for s in scope.sources if s.cols and s.cols[0][0] in ("id", "k")]
            if group or distinct or (having and not group):
                order_limit = f" ORDER BY 1 LIMIT {self.pick([0, 1, 2, 5])}"
            elif keyed and len(scope.sources) == 1:
                order_limit = f" ORDER BY {keyed[0].alias}.{keyed[0].cols[0][0]} LIMIT {self.pick([0, 1, 2])}"
            else:
                order_limit = f" ORDER BY {names[0]} LIMIT {self.pick([0, 1, 3])}"
        if distinct.startswith("DISTINCT ON"):
            key = distinct[len("DISTINCT ON ("):-2]
            order_limit = f" ORDER BY {key}, {names[0]}"
        sql = f"SELECT {distinct}{', '.join(items)} FROM {from_sql}{where}{group}{having}{qualify}{order_limit}"
        return sql, list(zip(names, kinds))

    def query(self, stack: list[Scope], depth: int, derived: bool = False, kinds: list[str] | None = None) -> tuple[str, list[tuple[str, str]]]:
        if depth > 0 and self.chance(0.22):
            left, cols = self.select(stack, depth - 1, kinds, derived=True)
            types = [c[1] for c in cols]
            if self.chance(0.15):
                # mixed numeric branches: INT64 against FLOAT64 or NUMERIC (an implicit conversion)
                types = [self.pick([FLOAT, NUM]) if t == INT and self.chance(0.5) else t for t in types]
            right, _ = self.select(stack, depth - 1, types, derived=True)
            op = self.rng.choices(["UNION ALL", "UNION DISTINCT", "INTERSECT DISTINCT", "EXCEPT DISTINCT", "INTERSECT ALL", "EXCEPT ALL"], weights=[5, 3, 2, 2, 1, 1])[0]
            if " LIMIT " in left or " ORDER BY " in left:
                left = f"({left})"
            if " LIMIT " in right or " ORDER BY " in right:
                right = f"({right})"
            sql = f"{left} {op} {right}"
            if self.chance(0.12):
                sql = f"({left} {op} {right}) {op.split()[0]} {'ALL' if op.endswith('ALL') else 'DISTINCT'} ({self.select(stack, depth - 1, [c[1] for c in cols], derived=True)[0]})"
            if self.chance(0.2):
                sql += f" ORDER BY {self.pick(['1', cols[0][0]])} LIMIT {self.pick([0, 1, 2])}" if self.chance(0.7) else f" LIMIT {self.pick([0, 1])}"
            return sql, cols
        return self.select(stack, depth, kinds, derived=derived)

    def statement(self) -> str:
        with_sql = ""
        if self.chance(0.15):
            ctes = []
            for _ in range(self.rng.choice([1, 1, 2])):
                name = self.pick(["c", "w", "u", "t2", "cte"]) if not self.ctes else self.name("w")
                if name in self.ctes:
                    continue
                if name in self.schema:
                    # shadows a table for the rest of the query; its body reads other tables only
                    saved = self.schema
                    self.schema = {k: v for k, v in saved.items() if k != name}
                    sql, cols = self.query([], 1)
                    self.schema = saved
                else:
                    sql, cols = self.query([], 1)
                ctes.append(f"{name} AS ({sql})")
                self.ctes[name] = cols
            with_sql = "WITH " + ", ".join(ctes) + " "
        sql, _ = self.query([], self.rng.choice([0, 1, 1, 2, 2, 3]))
        return with_sql + sql


def generate_cases(seed: int, count: int) -> list[dict]:
    rng = random.Random(seed)
    out = []
    for index in range(count):
        schema, constraints = make_schema(rng)
        gen = Gen(random.Random(rng.random()), schema, constraints)
        try:
            sql = gen.statement()
        except (IndexError, ValueError, StopIteration, RecursionError):
            continue
        out.append({"sql": sql, "dialect": "bigquery", "schema": schema, "constraints": constraints, "options": {}, "source": f"gen:{seed}:{index}"})
    return out


if __name__ == "__main__":
    import sys

    for case in generate_cases(int(sys.argv[1]) if len(sys.argv) > 1 else 1, int(sys.argv[2]) if len(sys.argv) > 2 else 10):
        print(case["sql"])
