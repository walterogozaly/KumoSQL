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
    def __init__(self, schemas: list[dict]):
        self.schemas = schemas
        self.n = 0

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
        if op in AGGS or op in ("ANY_VALUE", "COUNT", "$SUM0", "RANK", "FIRST_VALUE", "LAST_VALUE"):
            raise Skip("window function in scalar position")
        raise Skip(f"unsupported operator {op}")

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
                    raise Skip("multi-column COUNT(DISTINCT)")
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
        src, n = self.rel(b["source"], outer)
        if not b["target"]:
            raise Skip("projection with no columns")
        a = self.alias()
        ctx = outer + self.cols(a, n)
        exprs = [self.expr(t, ctx) for t in b["target"]]
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

    def rel_correlate(self, b, outer):
        left, ln = self.rel(b["left"], outer)
        la = self.alias()
        lctx = outer + self.cols(la, ln)
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
        src, n = self.rel(b["source"], outer)
        a = self.alias()
        ctx = outer + self.cols(a, n)
        keys = [self.expr(k, ctx) for k in b["keys"]]
        aggs = [self.agg(f, ctx) for f in b["function"]]
        if not keys and not aggs:
            raise Skip("aggregate with no keys and no functions")
        sel = self.select_list(keys + aggs)
        gb = f" GROUP BY {', '.join(keys)}" if keys else ""
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
    (re.compile(r"\) FILTER \$"), "aggregate FILTER clause (not represented in QED's IR)"),
    (re.compile(r"WITHIN DISTINCT"), "aggregate WITHIN DISTINCT (not represented in QED's IR)"),
    (re.compile(r"WITHIN GROUP"), "aggregate WITHIN GROUP (not represented in QED's IR)"),
    (re.compile(r"\bOVER\b"), "window function"),
]


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
    for q in doc["queries"]:
        sql, _ = Converter(schemas).rel(q, [])
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
