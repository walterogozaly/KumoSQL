"""Convert the QED prover's Calcite test corpus into SQL pairs.

Source: https://github.com/qed-solver/prover (tests/calcite/*.json, MIT).
Each JSON file holds ``schemas``, ``queries`` (two relational-algebra trees in
QED's JSON IR) and ``help`` (Calcite plan dumps). This script turns every file
into a pair of MySQL-flavoured SQL strings plus schema DDL, or skips it with an
explicit reason. It never guesses semantics: anything it does not model exactly
is a skip.

    python tools/qed_to_sql.py --src /path/to/prover/tests/calcite \
        [--out tests/fixtures/qed]

Conventions of the generated SQL
  * scans select the schema's own field names and alias them ``c0..cN``;
    every other operator is a derived table whose columns are ``c0..cN``,
    so QED's ordinal column references map to ``alias.c<i>``;
  * inside a subquery (EXISTS / IN / scalar / ANY, and the right side of a
    correlate) QED ordinals count the *enclosing* relation's columns first and
    the subquery's own input columns after them; the converter keeps that
    context as a list of qualified names and emits correlated references;
  * QED's IR drops the OVER clause of a window function (a window is a bare
    ``COUNT`` / ``SUM`` / ``RANK`` operator in a project), but the ``help`` plan
    dump keeps it, so the partition and order of a window are read from there
    and the function and its arguments from the IR; anything else about a
    window (frames, several windows, a nullable ORDER BY key) is a skip;
  * QED's IR also drops the grouping sets of an aggregate (``groups=[[{0, 1}, {0}, {}]]``
    in the help dump); they are read from there and written as
    ``GROUP BY GROUPING SETS``, so a pair is never scored as its plain
    GROUP BY reading;
  * a projection with no columns is a bag of empty rows, equal exactly when
    the row counts are; where only that count can matter (the root of a query,
    the right side of a SEMI / ANTI correlate) it becomes ``SELECT 1 AS c0``;
  * ``ROW(a, b, ..)`` in the root projection is its fields as separate columns
    (the same injective rewrite on both sides of a pair);
  * ``union`` is UNION ALL; a ``distinct`` node is SELECT DISTINCT; ``intersect``
    and ``except`` are the set (DISTINCT) forms (the help dumps say all=false).
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

try:  # only used to decide which identifiers need quoting
    from sqlglot.tokens import Tokenizer as _Tok

    _RESERVED = {k.upper() for k in _Tok.KEYWORDS}
except Exception:  # pragma: no cover
    _RESERVED = set()


class Skip(Exception):
    """Raised when a construct cannot be converted faithfully."""


INT_TYPES = {"INTEGER", "BIGINT", "SMALLINT", "TINYINT"}
DDL_TYPES = {
    "INTEGER": "INTEGER", "BIGINT": "BIGINT", "SMALLINT": "SMALLINT",
    "TINYINT": "TINYINT", "VARCHAR": "VARCHAR(255)", "CHAR": "CHAR(255)",
    "BOOLEAN": "BOOLEAN", "TIMESTAMP": "TIMESTAMP", "DATE": "DATE",
    "DECIMAL": "DECIMAL(19, 2)", "DOUBLE": "DOUBLE", "FLOAT": "FLOAT",
    "REAL": "REAL",
}
CAST_TYPES = {
    "INTEGER": "INTEGER", "BIGINT": "BIGINT", "SMALLINT": "SMALLINT",
    "TINYINT": "TINYINT", "VARCHAR": "VARCHAR", "CHAR": "CHAR",
    "BOOLEAN": "BOOLEAN", "TIMESTAMP": "TIMESTAMP", "DATE": "DATE",
    "DECIMAL": "DECIMAL", "DOUBLE": "DOUBLE", "FLOAT": "FLOAT", "REAL": "REAL",
}
CMP = {"=": "=", "<>": "<>", ">": ">", "<": "<", ">=": ">=", "<=": "<="}
FAMILY = {
    **{t: "num" for t in ("INTEGER", "BIGINT", "SMALLINT", "TINYINT", "DECIMAL", "DOUBLE", "FLOAT", "REAL")},
    "VARCHAR": "str", "CHAR": "str", "BOOLEAN": "bool", "DATE": "time", "TIMESTAMP": "time",
}
ARITH = {"+": "+", "-": "-", "*": "*"}
FUNCS = {"UPPER": "UPPER", "LOWER": "LOWER", "MOD": "MOD", "POWER": "POWER"}
WINDOW_FUNCS = {"COUNT", "SUM", "MIN", "MAX", "AVG", "RANK"}
AGGS = {
    "SUM": "SUM", "MIN": "MIN", "MAX": "MAX", "AVG": "AVG",
    "STDDEV_POP": "STDDEV_POP", "STDDEV_SAMP": "STDDEV_SAMP",
    "VAR_POP": "VAR_POP", "VAR_SAMP": "VAR_SAMP", "BIT_AND": "BIT_AND",
    "BIT_OR": "BIT_OR", "BOOL_AND": "BOOL_AND", "BOOL_OR": "BOOL_OR",
}
NUM_RE = re.compile(r"^-?\d+(\.\d+)?$")
STR_RE = re.compile(r"^_ISO-8859-1'(.*)'$", re.S)
CAL_RE = re.compile(r"^java\.util\.GregorianCalendar\[time=(-?\d+),")


def qident(name: str) -> str:
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) and name.upper() not in _RESERVED:
        return name
    return "`" + name.replace("`", "``") + "`"


def sql_string(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def table_name(schema: dict) -> str:
    return schema["name"].split(".")[-1]


def split_top(s: str, sep: str) -> list[str]:
    """Split on ``sep`` outside single-quoted strings."""
    out, cur, q, i = [], "", False, 0
    while i < len(s):
        ch = s[i]
        if ch == "'":
            q = not q
        if not q and s.startswith(sep, i):
            out.append(cur)
            cur = ""
            i += len(sep)
            continue
        cur += ch
        i += 1
    out.append(cur)
    return out


class Converter:
    def __init__(self, schemas: list[dict], windows: list[dict] | None = None):
        self.schemas = schemas
        self.n = 0
        self.windows = windows or []  # OVER specs read from the help dump, in plan order
        self.windows_used = 0
        self.dummy_ok: set[int] = set()  # zero-column projections whose only use is their row count
        self.root_project: int | None = None
        self.cur_source: dict | None = None
        self.cur_outer: list[str] = []
        self.group_sets: list[tuple[list[int], list[list[int]]] | None] = []  # per LogicalAggregate of the dump, plan order
        self.group_idx = 0

    def alias(self) -> str:
        self.n += 1
        return f"t{self.n}"

    # ------------------------------------------------------------------ scalars
    def literal(self, op: str, typ: str) -> str | None:
        if op in ("true", "false"):
            return op.upper()
        if op == "NULL":
            return "NULL"
        if NUM_RE.fullmatch(op):
            return f"({op})" if op.startswith("-") else op
        m = STR_RE.match(op)
        if m:
            return sql_string(m.group(1).replace("''", "'"))
        m = CAL_RE.match(op)
        if m:
            d = dt.datetime(1970, 1, 1) + dt.timedelta(milliseconds=int(m.group(1)))
            if typ == "DATE":
                return f"DATE '{d:%Y-%m-%d}'"
            if typ == "TIMESTAMP":
                return f"TIMESTAMP '{d:%Y-%m-%d %H:%M:%S}'"
            raise Skip(f"calendar literal of type {typ}")
        return None

    def sarg_value(self, tok: str, typ: str) -> str:
        tok = tok.strip()
        m = STR_RE.match(tok)
        if m:
            return sql_string(m.group(1).replace("''", "'"))
        if NUM_RE.fullmatch(tok):
            return f"({tok})" if tok.startswith("-") else tok
        if re.fullmatch(r"\d{4}-\d\d-\d\d", tok) and typ == "DATE":
            return f"DATE '{tok}'"
        if re.fullmatch(r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d", tok) and typ == "TIMESTAMP":
            return f"TIMESTAMP '{tok}'"
        raise Skip(f"Sarg value {tok!r}")

    def search(self, x: str, sarg: str, typ: str) -> str:
        if not (sarg.startswith("Sarg[") and sarg.endswith("]")):
            raise Skip("SEARCH operand is not a Sarg")
        body = sarg[5:-1]
        if "NULL AS" in body:
            raise Skip("Sarg with null handling")
        disj = []
        # items are separated by ", " at top level, but ranges contain ".."
        for item in split_top(body, ", "):
            item = item.strip()
            if item[:1] in "[(" and ".." in item and item[-1] in "])":
                lo, hi = split_top(item[1:-1], "..")
                parts = []
                if lo != "-\u221e":
                    parts.append(f"{x} {'>=' if item[0] == '[' else '>'} {self.sarg_value(lo, typ)}")
                if hi != "+\u221e":
                    parts.append(f"{x} {'<=' if item[-1] == ']' else '<'} {self.sarg_value(hi, typ)}")
                if not parts:
                    parts.append(f"{x} IS NOT NULL")
                disj.append("(" + " AND ".join(parts) + ")")
            else:
                disj.append(f"{x} = {self.sarg_value(item, typ)}")
        if not disj:
            return f"({x} IS NULL AND {x} IS NOT NULL)"
        return "(" + " OR ".join(disj) + ")"

    def expr(self, e: dict, ctx: list[str]) -> str:
        if "column" in e:
            i = e["column"]
            if e.get("type") == "ANY":
                raise Skip("column of type ANY (struct/map)")
            if not 0 <= i < len(ctx):
                raise Skip(f"column ordinal {i} out of range ({len(ctx)})")
            return ctx[i]
        op = e["operator"]
        typ = e.get("type")
        ops = e.get("operand", [])
        if "query" in e:
            return self.subquery_expr(e, ctx)
        if not ops:
            lit = self.literal(op, typ)
            if lit is not None:
                return lit
            if op == "CURRENT_TIMESTAMP":
                return "CURRENT_TIMESTAMP"  # constant within a statement, so equal on both sides of a pair
        if op == "SEARCH":
            if len(ops) != 2 or "column" in ops[1]:
                raise Skip("SEARCH shape")
            return self.search(self.expr(ops[0], ctx), ops[1]["operator"], ops[0].get("type"))
        a = [self.expr(o, ctx) for o in ops] if op not in ("EXTRACT", "TRIM") else None
        if (op in CMP or op in ("IS NOT DISTINCT FROM", "IS DISTINCT FROM")) and len(a) == 2:
            fams = {FAMILY.get(o.get("type"), o.get("type")) for o in ops}
            if len(fams) != 1:
                raise Skip("implicit cross-type comparison")
        if op in CMP and len(a) == 2:
            return f"({a[0]} {CMP[op]} {a[1]})"
        if op in ("AND", "OR") and len(a) >= 2:
            return "(" + f" {op} ".join(a) + ")"
        if op == "NOT" and len(a) == 1:
            return f"(NOT {a[0]})"
        if op in ("IS NULL", "IS NOT NULL", "IS TRUE", "IS NOT TRUE", "IS FALSE", "IS NOT FALSE") and len(a) == 1:
            return f"({a[0]} {op})"
        if op == "IS NOT DISTINCT FROM" and len(a) == 2:
            return f"({a[0]} <=> {a[1]})"
        if op == "IS DISTINCT FROM" and len(a) == 2:
            return f"(NOT ({a[0]} <=> {a[1]}))"
        if op in ARITH and len(a) == 2:
            if typ in INT_TYPES:  # Calcite promotes to the result type; DuckDB would not
                a = [x if o.get("type") == typ or o.get("type") not in INT_TYPES else f"CAST({x} AS {typ})"
                     for x, o in zip(a, ops)]
            return f"({a[0]} {ARITH[op]} {a[1]})"
        if op == "-" and len(a) == 1:
            return f"(-{a[0]})"
        if op == "/" and len(a) == 2:
            if typ in INT_TYPES:
                return f"({a[0]} DIV {a[1]})"
            return f"({a[0]} / {a[1]})"
        if op == "||" and len(a) == 2:
            return f"CONCAT({a[0]}, {a[1]})"
        if op == "LIKE" and len(a) == 2:
            return f"({a[0]} LIKE {a[1]})"
        if op in FUNCS:
            return f"{FUNCS[op]}({', '.join(a)})"
        if op == "CAST" and len(a) == 1:
            if typ not in CAST_TYPES:
                raise Skip(f"CAST to {typ}")
            return f"CAST({a[0]} AS {CAST_TYPES[typ]})"
        if op == "CASE" and len(a) >= 3 and len(a) % 2 == 1:
            whens = "".join(f" WHEN {a[i]} THEN {a[i + 1]}" for i in range(0, len(a) - 1, 2))
            return f"(CASE{whens} ELSE {a[-1]} END)"
        if op == "EXTRACT" and len(ops) == 2 and ops[0].get("type") == "SYMBOL":
            unit = ops[0]["operator"]
            if unit not in ("YEAR", "MONTH", "DAY", "HOUR", "MINUTE", "SECOND", "QUARTER"):
                raise Skip(f"EXTRACT unit {unit}")
            return f"EXTRACT({unit} FROM {self.expr(ops[1], ctx)})"
        if op == "TRIM" and len(ops) == 3 and ops[0].get("type") == "SYMBOL":
            side = ops[0]["operator"]
            if side not in ("BOTH", "LEADING", "TRAILING"):
                raise Skip(f"TRIM side {side}")
            return f"TRIM({side} {self.expr(ops[1], ctx)} FROM {self.expr(ops[2], ctx)})"
        if op in WINDOW_FUNCS:
            return self.window(op, a, ctx)
        if op in AGGS or op in ("ANY_VALUE", "COUNT", "$SUM0", "RANK", "FIRST_VALUE", "LAST_VALUE"):
            raise Skip("window function in scalar position")
        raise Skip(f"unsupported operator {op}")

    def window(self, op: str, args: list[str], ctx: list[str]) -> str:
        if self.windows_used >= len(self.windows):
            raise Skip("window function without a readable OVER clause in the plan dump")
        spec = self.windows[self.windows_used]
        self.windows_used += 1
        if spec["func"] != op:
            raise Skip(f"window function {op} does not match the plan dump ({spec['func']})")
        if self.cur_outer:
            raise Skip("window function inside a correlated subquery")
        if (op == "RANK") != bool(spec["order"]) or (op == "RANK" and args):
            raise Skip("RANK without ORDER BY or with arguments")
        parts = []
        if spec["partition"]:
            parts.append("PARTITION BY " + ", ".join(self.ordinal(i, ctx) for i in spec["partition"]))
        if spec["order"]:
            for i, _desc in spec["order"]:
                if not self.not_null(self.cur_source, i):
                    raise Skip("window ORDER BY key that may be NULL (Calcite and SQL engines disagree on NULL order)")
            parts.append("ORDER BY " + ", ".join(self.ordinal(i, ctx) + (" DESC" if d else "") for i, d in spec["order"]))
        if op == "COUNT":
            call = f"COUNT({args[0] if args else '*'})"
        elif op == "RANK":
            call = "RANK()"
        else:
            if len(args) != 1:
                raise Skip(f"window {op} arity")
            call = f"{op}({args[0]})"
        return f"{call} OVER ({' '.join(parts)})"

    @staticmethod
    def ordinal(i: int, ctx: list[str]) -> str:
        if not 0 <= i < len(ctx):
            raise Skip("window ordinal out of range")
        return ctx[i]

    def not_null(self, node: dict | None, i: int) -> bool:
        """The column ``i`` of ``node`` is declared NOT NULL (followed through scans, filters and column projections)."""

        if node is None or len(node) != 1:
            return False
        kind, body = next(iter(node.items()))
        if kind == "scan":
            nullable = self.schemas[body]["nullable"]
            return 0 <= i < len(nullable) and not nullable[i]
        if kind in ("filter", "sort"):
            return self.not_null(body["source"], i)
        if kind == "project" and 0 <= i < len(body["target"]) and "column" in body["target"][i]:
            return self.not_null(body["source"], body["target"][i]["column"])
        return False

    def subquery_expr(self, e: dict, ctx: list[str]) -> str:
        op = e["operator"]
        ops = e.get("operand", [])
        sub, _ = self.rel(e["query"], ctx)
        if op == "EXISTS" and not ops:
            return f"EXISTS ({sub})"
        if op == "$SCALAR_QUERY" and not ops:
            return f"({sub})"
        a = [self.expr(o, ctx) for o in ops]
        if op == "IN" and a:
            lhs = a[0] if len(a) == 1 else "(" + ", ".join(a) + ")"
            return f"({lhs} IN ({sub}))"
        m = re.fullmatch(r"(=|<>|<|<=|>|>=) (SOME|ALL)", op)
        if m and len(a) == 1:
            q = "ANY" if m.group(2) == "SOME" else "ALL"
            return f"({a[0]} {m.group(1)} {q} ({sub}))"
        raise Skip(f"unsupported subquery operator {op}")

    def agg(self, f: dict, ctx: list[str]) -> str:
        op = f["operator"]
        ops = f.get("operand", [])
        if f.get("ignoreNulls"):
            raise Skip("aggregate with IGNORE NULLS")
        dis = "DISTINCT " if f.get("distinct") else ""
        a = [self.expr(o, ctx) for o in ops]
        if op == "COUNT":
            if not a:
                if dis:
                    raise Skip("COUNT(DISTINCT) without operand")
                return "COUNT(*)"
            if len(a) != 1:
                if dis:
                    # MySQL's COUNT(DISTINCT a, b): distinct tuples with no NULL member (sqlglot spells it for DuckDB)
                    return f"COUNT(DISTINCT {', '.join(a)})"
                # COUNT(a, b) counts rows where every argument is non-null
                nn = " AND ".join(f"{x} IS NOT NULL" for x in a)
                return f"COUNT(CASE WHEN {nn} THEN 1 END)"
            return f"COUNT({dis}{a[0]})"
        if op == "$SUM0" and len(a) == 1:
            return f"COALESCE(SUM({dis}{a[0]}), 0)"
        if op == "AVG" and len(a) == 1 and f.get("type") in INT_TYPES:
            # Calcite types AVG over integers as an integer: SUM / COUNT with integer division
            return f"(SUM({dis}{a[0]}) DIV COUNT({dis}{a[0]}))"
        if op.startswith(("STDDEV", "VAR_")) and f.get("type") in INT_TYPES:
            raise Skip("integer-typed STDDEV/VAR aggregate (Calcite integer arithmetic not modelled)")
        if op in AGGS and len(a) == 1:
            return f"{AGGS[op]}({dis}{a[0]})"
        if op == "ANY_VALUE":
            raise Skip("nondeterministic aggregate ANY_VALUE")
        raise Skip(f"unsupported aggregate {op}")

    # ---------------------------------------------------------------- relations
    @staticmethod
    def cols(alias: str, n: int) -> list[str]:
        return [f"{alias}.c{i}" for i in range(n)]

    @staticmethod
    def select_list(exprs: list[str]) -> str:
        return ", ".join(f"{e} AS c{i}" for i, e in enumerate(exprs))

    def rel(self, node: dict, outer: list[str]) -> tuple[str, int]:
        """Return (SQL, ncols); the SQL's output columns are named c0..cN-1."""
        if len(node) != 1:
            raise Skip(f"unexpected relation node keys {sorted(node)}")
        kind, body = next(iter(node.items()))
        fn = getattr(self, "rel_" + kind, None)
        if fn is None:
            raise Skip(f"unsupported relational operator {kind}")
        return fn(body, outer)

    def rel_scan(self, i: int, outer):
        s = self.schemas[i]
        if "ANY" in s["types"]:
            raise Skip("table with ANY-typed (struct/map) column")
        sel = self.select_list([qident(f) for f in s["fields"]])
        return f"SELECT {sel} FROM {qident(table_name(s))}", len(s["fields"])

    def rel_project(self, b, outer):
        node = b["source"]
        src, n = self.rel(node, outer)
        if not b["target"]:
            if id(b) not in self.dummy_ok:
                raise Skip("projection with no columns whose row count can matter beyond the query's own result")
            a = self.alias()
            return f"SELECT 1 AS c0 FROM ({src}) AS {a}", 1
        a = self.alias()
        ctx = outer + self.cols(a, n)
        saved = self.cur_source, self.cur_outer
        self.cur_source, self.cur_outer = node, outer
        try:
            targets = b["target"]
            if id(b) == self.root_project:
                targets = [f for t in targets for f in (t["operand"] if t.get("operator") == "ROW" and "query" not in t else [t])]
            exprs = [self.expr(t, ctx) for t in targets]
        finally:
            self.cur_source, self.cur_outer = saved
        return f"SELECT {self.select_list(exprs)} FROM ({src}) AS {a}", len(exprs)

    def rel_filter(self, b, outer):
        src, n = self.rel(b["source"], outer)
        a = self.alias()
        ctx = outer + self.cols(a, n)
        cond = self.expr(b["condition"], ctx)
        sel = self.select_list(self.cols(a, n))
        return f"SELECT {sel} FROM ({src}) AS {a} WHERE {cond}", n

    def rel_distinct(self, b, outer):
        src, n = self.rel(b, outer)
        a = self.alias()
        return f"SELECT DISTINCT {self.select_list(self.cols(a, n))} FROM ({src}) AS {a}", n

    def rel_join(self, b, outer):
        if b["kind"] in ("SEMI", "ANTI"):
            self.allow_empty(b["right"])
        left, ln = self.rel(b["left"], outer)
        right, rn = self.rel(b["right"], outer)
        la, ra = self.alias(), self.alias()
        ctx = outer + self.cols(la, ln) + self.cols(ra, rn)
        cond = self.expr(b["condition"], ctx)
        kind = b["kind"]
        if kind in ("SEMI", "ANTI"):
            neg = "NOT " if kind == "ANTI" else ""
            sel = self.select_list(self.cols(la, ln))
            return (f"SELECT {sel} FROM ({left}) AS {la} WHERE {neg}EXISTS "
                    f"(SELECT 1 FROM ({right}) AS {ra} WHERE {cond})"), ln
        jk = {"INNER": "INNER", "LEFT": "LEFT", "RIGHT": "RIGHT", "FULL": "FULL"}.get(kind)
        if jk is None:
            raise Skip(f"join kind {kind}")
        sel = self.select_list(self.cols(la, ln) + self.cols(ra, rn))
        return f"SELECT {sel} FROM ({left}) AS {la} {jk} JOIN ({right}) AS {ra} ON {cond}", ln + rn

    def allow_empty(self, node: dict) -> None:
        """Mark ``node`` (if a project) as one whose columns nobody reads: only whether it has rows."""

        if len(node) == 1 and "project" in node:
            self.dummy_ok.add(id(node["project"]))

    def rel_correlate(self, b, outer):
        left, ln = self.rel(b["left"], outer)
        la = self.alias()
        lctx = outer + self.cols(la, ln)
        if b["kind"] in ("SEMI", "ANTI"):
            self.allow_empty(b["right"])
        right, rn = self.rel(b["right"], lctx)
        ra = self.alias()
        kind = b["kind"]
        if kind in ("SEMI", "ANTI"):
            neg = "NOT " if kind == "ANTI" else ""
            sel = self.select_list(self.cols(la, ln))
            return f"SELECT {sel} FROM ({left}) AS {la} WHERE {neg}EXISTS ({right})", ln
        sel = self.select_list(self.cols(la, ln) + self.cols(ra, rn))
        if kind == "INNER":
            return f"SELECT {sel} FROM ({left}) AS {la} CROSS JOIN LATERAL ({right}) AS {ra}", ln + rn
        if kind == "LEFT":
            return f"SELECT {sel} FROM ({left}) AS {la} LEFT JOIN LATERAL ({right}) AS {ra} ON TRUE", ln + rn
        raise Skip(f"correlate kind {kind}")

    def rel_group(self, b, outer):
        idx = self.group_idx  # the help dump prints aggregates outermost first
        self.group_idx += 1
        src, n = self.rel(b["source"], outer)
        a = self.alias()
        ctx = outer + self.cols(a, n)
        keys = [self.expr(k, ctx) for k in b["keys"]]
        aggs = [self.agg(f, ctx) for f in b["function"]]
        if not keys and not aggs:
            raise Skip("aggregate with no keys and no functions")
        sel = self.select_list(keys + aggs)
        gb = f" GROUP BY {', '.join(keys)}" if keys else ""
        sets = self.group_sets[idx] if idx < len(self.group_sets) else None
        if sets is not None:
            ordinals, groups = sets
            if [k.get("column") for k in b["keys"]] != ordinals:
                raise Skip("grouping sets that do not line up with the aggregate's keys")
            rendered = ", ".join("(" + ", ".join(keys[ordinals.index(o)] for o in g) + ")" for g in groups)
            gb = f" GROUP BY GROUPING SETS ({rendered})"
        return f"SELECT {sel} FROM ({src}) AS {a}{gb}", len(keys) + len(aggs)

    def rel_sort(self, b, outer):
        src, n = self.rel(b["source"], outer)
        a = self.alias()
        sql = f"SELECT {self.select_list(self.cols(a, n))} FROM ({src}) AS {a}"
        if b["collation"]:
            parts = []
            for col, _typ, d in b["collation"]:
                if not 0 <= col < n:
                    raise Skip("collation ordinal out of range")
                if d not in ("ASCENDING", "DESCENDING"):
                    raise Skip(f"sort direction {d}")
                parts.append(f"{a}.c{col}{' DESC' if d == 'DESCENDING' else ''}")
            sql += " ORDER BY " + ", ".join(parts)
        ctx = outer + self.cols(a, n)
        if (b.get("limit") is not None or b.get("offset") is not None) and not b["collation"]:
            raise Skip("LIMIT/OFFSET without ORDER BY (nondeterministic)")
        if b.get("limit") is not None:
            sql += f" LIMIT {self.expr(b['limit'], ctx)}"
        if b.get("offset") is not None:
            sql += f" OFFSET {self.expr(b['offset'], ctx)}"
        return sql, n

    def _setop(self, items, outer, keyword):
        if len(items) < 2:
            raise Skip(f"{keyword} with fewer than two inputs")
        parts, n0 = [], None
        for it in items:
            s, n = self.rel(it, outer)
            if n0 is None:
                n0 = n
            elif n != n0:
                raise Skip(f"{keyword} inputs with different widths")
            parts.append(f"({s})")
        a = self.alias()
        body = f" {keyword} ".join(parts)
        return f"SELECT {self.select_list(self.cols(a, n0))} FROM ({body}) AS {a}", n0

    def rel_union(self, b, outer):
        return self._setop(b, outer, "UNION ALL")

    def rel_intersect(self, b, outer):
        return self._setop(b, outer, "INTERSECT")

    def rel_except(self, b, outer):
        return self._setop(b, outer, "EXCEPT")

    def rel_values(self, b, outer):
        types = b["schema"]
        n = len(types)
        if n == 0:
            raise Skip("VALUES with no columns")
        for t in types:
            if t not in CAST_TYPES:
                raise Skip(f"VALUES column type {t}")
        rows = b["content"]
        if not rows:
            exprs = [f"CAST(NULL AS {CAST_TYPES[t]})" for t in types]
            return f"SELECT {self.select_list(exprs)} WHERE 1 = 0", n
        sels = []
        for r in rows:
            if len(r) != n:
                raise Skip("VALUES row width mismatch")
            sels.append(f"SELECT {self.select_list([self.expr(c, []) for c in r])}")
        if len(sels) == 1:
            return sels[0], n
        a = self.alias()
        return f"SELECT {self.select_list(self.cols(a, n))} FROM ({' UNION ALL '.join(sels)}) AS {a}", n


# ----------------------------------------------------------------------- DDL
def schema_ddl(s: dict) -> str:
    cols = []
    for f, t, nullable in zip(s["fields"], s["types"], s["nullable"]):
        if t not in DDL_TYPES:
            raise Skip(f"column type {t}")
        cols.append(f"  {qident(f)} {DDL_TYPES[t]}{'' if nullable else ' NOT NULL'}")
    pk_used = False
    for key in s["key"]:
        names = ", ".join(qident(s["fields"][i]) for i in key)
        if not pk_used and all(not s["nullable"][i] for i in key):
            cols.append(f"  PRIMARY KEY ({names})")
            pk_used = True
        else:
            cols.append(f"  UNIQUE ({names})")
    return f"CREATE TABLE {qident(table_name(s))} (\n" + ",\n".join(cols) + "\n);"


def schemas_struct(schemas: list[dict]) -> list[dict]:
    return [
        {
            "table": table_name(s),
            "columns": [
                {"name": f, "type": t, "nullable": n}
                for f, t, n in zip(s["fields"], s["types"], s["nullable"])
            ],
            "keys": [[s["fields"][i] for i in k] for k in s["key"]],
        }
        for s in schemas
    ]


HELP_SKIPS = [
    (re.compile(r"(?s)groups=\[\[.*\) FILTER \$|\) FILTER \$.*groups=\[\["), "aggregate FILTER clause and grouping sets (neither is represented in QED's IR; QED proves the plain GROUP BY reading, not Calcite's query)"),
    (re.compile(r"\) FILTER \$"), "aggregate FILTER clause (not represented in QED's IR)"),
    (re.compile(r"WITHIN DISTINCT"), "aggregate WITHIN DISTINCT (not represented in QED's IR)"),
    (re.compile(r"WITHIN GROUP"), "aggregate WITHIN GROUP (not represented in QED's IR)"),
]

_AGG_LINE = re.compile(r"LogicalAggregate\(group=\[\{([\d, ]*)\}\](?:, groups=\[\[(.*?)\]\])?[,)]")


def parse_group_sets(help_text: str) -> list[tuple[list[int], list[list[int]]] | None]:
    """For each LogicalAggregate of a plan dump, outermost first: ``(group ordinals, grouping sets)`` or ``None``."""

    out = []
    for m in _AGG_LINE.finditer(help_text):
        if m.group(2) is None:
            out.append(None)
            continue
        ordinals = [int(x) for x in m.group(1).split(",") if x.strip()]
        sets = [[int(x) for x in g.split(",") if x.strip()] for g in re.findall(r"\{([\d, ]*)\}", m.group(2))]
        out.append((ordinals, sets))
    return out


_SPEC = re.compile(r"(?:PARTITION BY (?P<part>\$\d+(?:, \$\d+)*))?(?: ?ORDER BY (?P<order>\$\d+(?: DESC)?(?:, \$\d+(?: DESC)?)*))?")


def parse_windows(help_text: str) -> list[dict]:
    """The ``f(args) OVER (PARTITION BY .. ORDER BY ..)`` calls of one plan dump, in the order they print.

    The function name and arguments are read back from QED's IR; only the partition and order ordinals
    are taken from here. Frames (``ROWS``/``RANGE``) and null ordering are not read: they make the call a skip.
    """

    out, pos = [], 0
    while (i := help_text.find(" OVER (", pos)) >= 0:
        close = help_text.find(")", i + 7)
        spec_text = help_text[i + 7:close]
        # the function name sits before the argument list that closes just before " OVER"
        depth, j = 0, i - 1
        while j >= 0:
            if help_text[j] == ")":
                depth += 1
            elif help_text[j] == "(":
                depth -= 1
                if depth == 0:
                    break
            j -= 1
        name = re.search(r"([A-Z_$0-9]+)$", help_text[:j])
        m = _SPEC.fullmatch(spec_text)
        if m is None or name is None:
            raise Skip("window function with a frame or an OVER clause this converter does not read")
        out.append({
            "func": name.group(1),
            "partition": [int(x[1:]) for x in (m.group("part") or "").split(", ") if x],
            "order": [(int(x.split()[0][1:]), x.endswith("DESC")) for x in (m.group("order") or "").split(", ") if x],
        })
        pos = close
    return out


def convert_case(doc: dict) -> dict:
    schemas = doc["schemas"]
    help_text = "".join(doc.get("help", []))
    for pat, why in HELP_SKIPS:
        if pat.search(help_text):
            raise Skip(why)
    if any(table_name(s).upper().startswith("EMPTY_") for s in schemas):
        raise Skip("table is empty only by test-harness convention (not expressible in the schema)")
    # Calcite flattens struct columns into names such as "F1"."A0"; drop the quotes.
    for s in schemas:
        s["fields"] = [f.replace('"', "") for f in s["fields"]]
    names = [table_name(s) for s in schemas]
    if len(set(names)) != len(names):
        raise Skip("schemas share a last name component")
    if len(doc["queries"]) != 2:
        raise Skip("not exactly two queries")
    ddl = "\n".join(schema_ddl(s) for s in schemas)
    out = []
    plans = doc.get("help", [])
    for i, q in enumerate(doc["queries"]):
        windows = parse_windows(plans[i]) if len(plans) == 2 else []
        conv = Converter(schemas, windows)
        conv.group_sets = parse_group_sets(plans[i]) if len(plans) == 2 else []
        body = next(iter(q.values())) if len(q) == 1 else None
        if "project" in q:
            conv.root_project = id(body)
            conv.dummy_ok.add(id(body))
        sql, _ = conv.rel(q, [])
        if any(g is not None for g in conv.group_sets) and conv.group_idx != len(conv.group_sets):
            raise Skip("grouping sets whose aggregates do not line up with the plan dump")
        if conv.windows_used != len(windows):
            raise Skip("window functions in the plan dump that the IR does not show as windows")
        out.append(sql)
    return {"sql_a": out[0], "sql_b": out[1], "ddl": ddl, "schemas": schemas_struct(schemas)}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--src", required=True, type=Path, help="QED prover tests/calcite directory")
    ap.add_argument("--out", type=Path, default=Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "qed")
    args = ap.parse_args()

    files = sorted(args.src.glob("*.json"))
    cases, skipped = [], []
    ddl_ids: dict[str, str] = {}
    for f in files:
        doc = json.loads(f.read_text())
        name = f.stem
        try:
            c = convert_case(doc)
        except Skip as e:
            skipped.append({"name": name, "reason": str(e)})
            continue
        except RecursionError:
            skipped.append({"name": name, "reason": "recursion limit"})
            continue
        sid = ddl_ids.setdefault(c["ddl"], f"s{len(ddl_ids) + 1}")
        cases.append({"name": name, "schema_id": sid, **c})

    args.out.mkdir(parents=True, exist_ok=True)
    with (args.out / "qed_calcite_pairs.jsonl").open("w") as fh:
        for c in cases:
            fh.write(json.dumps(c, ensure_ascii=False, separators=(",", ":")) + "\n")
    with (args.out / "qed_calcite_skipped.jsonl").open("w") as fh:
        for s in skipped:
            fh.write(json.dumps(s, ensure_ascii=False) + "\n")
    (args.out / "qed_calcite_schemas.json").write_text(
        json.dumps({sid: ddl for ddl, sid in ddl_ids.items()}, indent=1, ensure_ascii=False) + "\n")

    reasons = Counter(s["reason"] for s in skipped)
    commit = "unknown"
    try:
        commit = subprocess.run(["git", "-C", str(args.src), "rev-parse", "HEAD"], capture_output=True,
                                text=True, check=True).stdout.strip()
    except Exception:
        pass
    summary = {"source_commit": commit, "files": len(files), "converted": len(cases),
               "skipped": len(skipped), "skip_reasons": dict(reasons.most_common())}
    (args.out / "summary.json").write_text(json.dumps(summary, indent=1, ensure_ascii=False) + "\n")
    print(json.dumps(summary, indent=1, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
