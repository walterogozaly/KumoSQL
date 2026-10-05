"""Convert the QED prover's CockroachDB test corpus into SQL pairs.

Source: https://github.com/qed-solver/prover (tests/cockroach/{memo,norm,xform}/*.json, MIT).
Each JSON file holds ``schemas``, ``queries`` (two relational-algebra trees in QED's JSON IR,
one per optimizer plan) and ``help`` (the CockroachDB plan dumps the trees were read from).
This script turns every file into a pair of MySQL-flavoured SQL strings plus schema DDL, or
skips it with an explicit reason. It never guesses semantics: anything it does not model
exactly is a skip.

    python tools/qed_cockroach_to_sql.py --src /path/to/prover/tests/cockroach \
        [--out tests/fixtures/qed_cockroach]

The relational part is the Calcite converter's (``tools/qed_to_sql.py``): scans select their
columns aliased ``c0..cN``, every other operator is a derived table whose columns are
``c0..cN``, and inside a subquery or the right side of an apply join QED's ordinals count the
enclosing relation's columns first. What differs for CockroachDB:

  * the JSON schemas carry no names, so scan ``i`` reads table ``t<i>`` with columns
    ``t<i>_c0..t<i>_cN`` (unique across tables, as the benchmark's data generator keys on column names); a scan index is one table (the plan dumps agree: one schema per distinct
    table, however many plans or index scans read it);
  * operator spellings: ``EQ NE LT GT LE GE`` and ``<`` ``<=`` are the plain comparisons,
    ``IS`` and ``<=>`` are ``IS NOT DISTINCT FROM`` (the ``<=>`` joins are how the IR spells
    an index join back to the primary index, null-safe on every key column), ``IS NOT`` is
    ``IS DISTINCT FROM``, ``PLUS MINUS MULT`` and ``UNARY MINUS`` are arithmetic, an
    ``AND``/``OR`` of one operand is that operand, of none TRUE/FALSE (a filter's list of
    conjuncts), and ``CASE`` carries its input first and its ELSE last;
  * ``union`` is UNION ALL (a plain UNION is a ``distinct`` over it), ``intersect`` and
    ``except`` are the set forms (the plan dumps say ``intersect``/``except``, never ``-all``);
  * columns of a type with no exact SQL encoding (OID, JSONB, geometry, arrays, UUID, ..) stay
    in the table as opaque BIGINT placeholders and may travel through filters, sorts and joins,
    but a pair that reads one in an expression, groups, dedups or unions it, or returns it, is
    skipped;
  * ``FUNCTION``, ``UDF``, ``PLACEHOLDER``, ``CONST AGG`` and the like are skipped: the IR does
    not even keep a function's name.
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

sys.path.insert(0, str(Path(__file__).resolve().parent))
from qed_to_sql import Skip, qident, sql_string  # noqa: E402

# ----------------------------------------------------------------------- types
FAMILY = {
    "INT": "num", "FLOAT": "num", "DECIMAL": "num",
    "STRING": "str", "VARCHAR": "str",
    "BOOL": "bool", "BOOLEAN": "bool",
    "DATE": "time", "TIMESTAMP": "time",
}
DDL_TYPES = {
    "INT": "BIGINT", "FLOAT": "DOUBLE", "DECIMAL": "DECIMAL(19, 2)", "STRING": "VARCHAR(255)",
    "VARCHAR": "VARCHAR(255)", "BOOL": "BOOLEAN", "DATE": "DATE", "TIMESTAMP": "TIMESTAMP",
}
CAST_TYPES = {"INT": "BIGINT", "FLOAT": "DOUBLE", "DECIMAL": "DECIMAL(19, 2)", "STRING": "VARCHAR",
              "BOOL": "BOOLEAN", "DATE": "DATE", "TIMESTAMP": "TIMESTAMP"}
CMP = {"EQ": "=", "NE": "<>", "LT": "<", "GT": ">", "LE": "<=", "GE": ">=", "<": "<", "<=": "<="}
ARITH = {"PLUS": "+", "MINUS": "-", "MULT": "*"}
LIKES = {"LIKE": "LIKE", "NOT LIKE": "NOT LIKE"}
INT_RE = re.compile(r"^-?\d+$")
NUM_RE = re.compile(r"^-?\d+(\.\d+)?$")
STR_RE = re.compile(r"^'((?:[^']|'')*)'$", re.S)
DATE_RE = re.compile(r"^'(\d{4}-\d\d-\d\d)'$")
TS_RE = re.compile(r"^'(\d{4}-\d\d-\d\d) (\d\d:\d\d:\d\d)'$")

HELP_SKIPS = [
    (re.compile(r"check constraint expressions"), "plan relies on a CHECK constraint that QED's JSON schema drops"),
    (re.compile(r"computed column expressions"), "plan relies on a computed column definition that QED's JSON schema drops"),
]


def family(typ: str | None) -> str:
    f = FAMILY.get(typ or "")
    if f is None:
        raise Skip(f"operand of unsupported type {typ}")
    return f


class Dead(str):
    """Context entry of a column whose type has no exact encoding (an opaque BIGINT placeholder in the
    DDL). It may be carried through filters, joins, projections and the result, but an expression that
    reads it skips the pair; ``typ`` is the CockroachDB type it stands for."""

    typ: str

    def __new__(cls, name: str, typ: str):
        obj = super().__new__(cls, name)
        obj.typ = typ
        return obj


# Types the pair may group, dedup, union or return as opaque values: for these, equality of two values
# is identity of the stored value, so a column of arbitrary BIGINT stand-ins has the same equalities.
OPAQUE_EQ = {"JSONB", "OID", "UUID", "ENCODEDKEY", "BYTES", "INT[]", "STRING[]", "FLOAT[]", "REGPROC",
             "REGCLASS", "REGNAMESPACE", "INET", "TIME", "TIMETZ", "TIMESTAMPTZ", "INT2", "INT4", "FLOAT4",
             "CHAR", '"CHAR"', "NAME", "BIT", "VARBIT", "INTERVAL"}


class Converter:
    def __init__(self, schemas: list[dict]):
        self.schemas = schemas
        self.n = 0

    def alias(self) -> str:
        self.n += 1
        return f"t{self.n}"

    # ------------------------------------------------------------------ scalars
    def literal(self, op: str, typ: str) -> str:
        if op == "PLACEHOLDER":
            raise Skip("PLACEHOLDER (a query parameter has no value)")
        if op in ("TRUE", "true", "FALSE", "false"):
            return op.upper()
        if op == "NULL":
            return "NULL"
        if typ == "INT" and INT_RE.fullmatch(op):
            if abs(int(op)) >= 2**63:
                raise Skip("integer literal outside BIGINT")
            return f"({op})" if op.startswith("-") else op
        if typ in ("FLOAT", "DECIMAL") and NUM_RE.fullmatch(op):
            return f"({op})" if op.startswith("-") else op
        if typ in ("STRING", "VARCHAR"):
            m = STR_RE.match(op)
            if m is None:
                raise Skip("string literal with a prefix or escape")
            if "\\" in op:
                raise Skip("string literal with a backslash (escape rules differ between engines)")
            return sql_string(m.group(1).replace("''", "'"))
        if typ == "DATE":
            m = DATE_RE.match(op)
            if m:
                dt.date.fromisoformat(m.group(1))
                return f"DATE '{m.group(1)}'"
        if typ == "TIMESTAMP":
            m = TS_RE.match(op)
            if m:
                dt.datetime.fromisoformat(f"{m.group(1)} {m.group(2)}")
                return f"TIMESTAMP '{m.group(1)} {m.group(2)}'"
        raise Skip(f"literal of type {typ}")

    def expr(self, e: dict, ctx: list[str]) -> str:
        if "column" in e:
            i = e["column"]
            if not 0 <= i < len(ctx):
                raise Skip(f"column ordinal {i} out of range ({len(ctx)})")
            if isinstance(ctx[i], Dead):
                raise Skip(f"reads a column of type {ctx[i].typ} (no exact encoding)")
            return ctx[i]
        op = e["operator"]
        typ = e.get("type")
        ops = e.get("operand", [])
        if "query" in e:
            return self.subquery_expr(e, ctx)
        if op in ("AND", "OR"):
            a = [self.expr(o, ctx) for o in ops]
            if not a:
                return "TRUE" if op == "AND" else "FALSE"
            return a[0] if len(a) == 1 else "(" + f" {op} ".join(a) + ")"
        if not ops:
            return self.literal(op, typ)
        if op in ("FUNCTION", "U D F", "PLACEHOLDER", "SCALAR LIST"):
            raise Skip(f"unsupported operator {op} (function name or arguments not kept by QED's IR)")
        a = [self.expr(o, ctx) for o in ops]
        if op in CMP and len(a) == 2:
            if family(ops[0].get("type")) != family(ops[1].get("type")):
                raise Skip("implicit cross-type comparison")
            return f"({a[0]} {CMP[op]} {a[1]})"
        if op in ("IS", "IS NOT", "<=>") and len(a) == 2:
            if family(ops[0].get("type")) != family(ops[1].get("type")):
                raise Skip("implicit cross-type comparison")
            if op == "IS NOT":
                return f"(NOT ({a[0]} <=> {a[1]}))"
            return f"({a[0]} <=> {a[1]})"
        if op == "NOT" and len(a) == 1:
            return f"(NOT {a[0]})"
        if op in ARITH and len(a) == 2:
            if any(family(o.get("type")) != "num" for o in ops) or family(typ) != "num":
                raise Skip(f"{op} on a non-numeric type")
            return f"({a[0]} {ARITH[op]} {a[1]})"
        if op == "UNARY MINUS" and len(a) == 1:
            if family(ops[0].get("type")) != "num":
                raise Skip("UNARY MINUS on a non-numeric type")
            return f"(-{a[0]})"
        if op in LIKES and len(a) == 2:
            if family(ops[0].get("type")) != "str" or family(ops[1].get("type")) != "str":
                raise Skip("LIKE on a non-string type")
            return f"({a[0]} {LIKES[op]} {a[1]})"
        if op == "COALESCE" and len(ops) == 1 and ops[0].get("operator") == "SCALAR LIST":
            items = [self.expr(o, ctx) for o in ops[0]["operand"]]
            if len(items) < 2:
                raise Skip("COALESCE of fewer than two values")
            return f"COALESCE({', '.join(items)})"
        if op == "CASE" and len(a) >= 4 and len(a) % 2 == 0:
            # operands: the CASE input, then WHEN/THEN pairs, then ELSE (NULL when absent)
            head = a[0]
            pairs = "".join(f" WHEN {a[i]} THEN {a[i + 1]}" for i in range(1, len(a) - 1, 2))
            return f"(CASE {head}{pairs} ELSE {a[-1]} END)"
        if op == "CAST" and len(a) == 1:
            src, dst = ops[0].get("type"), typ
            if src == dst and dst in CAST_TYPES:
                return a[0]
            if src == "INT" and dst in ("FLOAT", "DECIMAL"):
                return f"CAST({a[0]} AS {CAST_TYPES[dst]})"
            raise Skip(f"CAST from {src} to {dst}")
        raise Skip(f"unsupported operator {op}")

    def subquery_expr(self, e: dict, ctx: list[str]) -> str:
        op = e["operator"]
        ops = e.get("operand", [])
        sub, n, dead = self.rel(e["query"], ctx)
        if op == "EXISTS" and not ops:
            return f"EXISTS ({sub})"
        if op == "IN" and len(ops) == 1:
            if n != 1 or dead:
                raise Skip("IN over a subquery that is not one supported column")
            return f"({self.expr(ops[0], ctx)} IN ({sub}))"
        raise Skip(f"unsupported subquery operator {op}")

    def agg(self, f: dict, ctx: list[str]) -> str:
        if not isinstance(f, dict):
            raise Skip(f"aggregate QED's IR cannot represent: {f}")
        op = f["operator"]
        ops = f.get("operand", [])
        dis = "DISTINCT " if f.get("distinct") else ""
        if op == "COUNT ROWS" and not ops and not dis:
            return "COUNT(*)"
        if len(ops) != 1:
            raise Skip(f"unsupported aggregate {op}")
        x = self.expr(ops[0], ctx)
        fam, typ = family(ops[0].get("type")), ops[0].get("type")
        if op == "COUNT":
            return f"COUNT({dis}{x})"
        if op in ("SUM", "SUM INT") and fam == "num":
            return f"SUM({dis}{x})"
        if op in ("MIN", "MAX") and fam in ("num", "str", "time"):
            return f"{op}({dis}{x})"
        if op == "AVG" and typ in ("FLOAT", "DECIMAL"):
            return f"AVG({dis}{x})"
        raise Skip(f"unsupported aggregate {op} over {typ}")

    # ---------------------------------------------------------------- relations
    @staticmethod
    def cols(alias: str, n: int, dead={}) -> list[str]:
        return [Dead(f"{alias}.c{i}", dead[i]) if i in dead else f"{alias}.c{i}" for i in range(n)]

    @staticmethod
    def select_list(exprs: list[str]) -> str:
        if not exprs:
            raise Skip("zero-column relation")
        return ", ".join(f"{e} AS c{i}" for i, e in enumerate(exprs))

    def live(self, alias: str, n: int, dead) -> list[str]:
        """Pass-through select items: every column, opaque ones included, unread."""
        return [f"{alias}.c{i}" for i in range(n)]

    @staticmethod
    def need_equality(dead: dict, what: str) -> None:
        for t in dead.values():
            if t not in OPAQUE_EQ:
                raise Skip(f"{what} over a column of type {t} (no exact encoding)")

    def rel(self, node: dict, outer: list[str]) -> tuple[str, int, dict]:
        if len(node) != 1:
            raise Skip(f"unexpected relation node keys {sorted(node)}")
        kind, body = next(iter(node.items()))
        fn = getattr(self, "rel_" + kind, None)
        if fn is None:
            raise Skip(f"unsupported relational operator {kind}")
        return fn(body, outer)

    def rel_scan(self, i: int, outer):
        s = self.schemas[i]
        n = len(s["types"])
        dead = {j: t for j, t in enumerate(s["types"]) if t not in DDL_TYPES}
        sel = ", ".join(f"t{i}_c{j} AS c{j}" for j in range(n))
        return f"SELECT {sel} FROM t{i}", n, dead

    def rel_project(self, b, outer):
        src, n, dead = self.rel(b["source"], outer)
        if not b["target"]:
            raise Skip("projection with no columns")
        a = self.alias()
        ctx = outer + self.cols(a, n, dead)
        exprs, out_dead = [], {}
        for j, t in enumerate(b["target"]):
            c = t.get("column", -1) - len(outer)  # ordinals count the enclosing relation's columns first
            if "column" in t and 0 <= c < n and c in dead:
                exprs.append(ctx[len(outer) + c])
                out_dead[j] = dead[c]
            else:
                exprs.append(self.expr(t, ctx))
        if n == 0:
            from_ = "(SELECT 1 AS z)"  # the one empty row of `VALUES ()`
        else:
            from_ = f"({src})"
        return f"SELECT {self.select_list(exprs)} FROM {from_} AS {a}", len(exprs), out_dead

    def rel_filter(self, b, outer):
        src, n, dead = self.rel(b["source"], outer)
        a = self.alias()
        ctx = outer + self.cols(a, n, dead)
        cond = self.expr(b["condition"], ctx)
        sel = self.select_list(self.live(a, n, dead))
        return f"SELECT {sel} FROM ({src}) AS {a} WHERE {cond}", n, dead

    def rel_distinct(self, b, outer):
        src, n, dead = self.rel(b, outer)
        self.need_equality(dead, "DISTINCT")
        a = self.alias()
        return f"SELECT DISTINCT {self.select_list(self.live(a, n, dead))} FROM ({src}) AS {a}", n, dead

    def rel_join(self, b, outer):
        left, ln, ld = self.rel(b["left"], outer)
        right, rn, rd = self.rel(b["right"], outer)
        la, ra = self.alias(), self.alias()
        ctx = outer + self.cols(la, ln, ld) + self.cols(ra, rn, rd)
        cond = self.expr(b["condition"], ctx)
        kind = b["kind"]
        if kind in ("SEMI", "ANTI"):
            neg = "NOT " if kind == "ANTI" else ""
            sel = self.select_list(self.live(la, ln, ld))
            return (f"SELECT {sel} FROM ({left}) AS {la} WHERE {neg}EXISTS "
                    f"(SELECT 1 FROM ({right}) AS {ra} WHERE {cond})"), ln, ld
        if kind not in ("INNER", "LEFT", "RIGHT", "FULL"):
            raise Skip(f"join kind {kind}")
        sel = self.select_list(self.live(la, ln, ld) + self.live(ra, rn, rd))
        dead = {**ld, **{ln + j: t for j, t in rd.items()}}
        return f"SELECT {sel} FROM ({left}) AS {la} {kind} JOIN ({right}) AS {ra} ON {cond}", ln + rn, dead

    def rel_correlate(self, b, outer):
        left, ln, ld = self.rel(b["left"], outer)
        la = self.alias()
        right, rn, rd = self.rel(b["right"], outer + self.cols(la, ln, ld))
        ra = self.alias()
        kind = b["kind"]
        if kind != "INNER":
            raise Skip(f"correlate kind {kind}")
        sel = self.select_list(self.live(la, ln, ld) + self.live(ra, rn, rd))
        dead = {**ld, **{ln + j: t for j, t in rd.items()}}
        return f"SELECT {sel} FROM ({left}) AS {la} CROSS JOIN LATERAL ({right}) AS {ra}", ln + rn, dead

    def rel_group(self, b, outer):
        src, n, dead = self.rel(b["source"], outer)
        a = self.alias()
        ctx = outer + self.cols(a, n, dead)
        keys, out_dead = [], {}
        for j, k in enumerate(b["keys"]):
            c = k.get("column", -1) - len(outer)
            if "column" in k and 0 <= c < n and c in dead:
                self.need_equality({0: dead[c]}, "GROUP BY")
                keys.append(ctx[len(outer) + c])
                out_dead[j] = dead[c]
            else:
                keys.append(self.expr(k, ctx))
        aggs = [self.agg(f, ctx) for f in b["function"]]
        if not keys and not aggs:
            raise Skip("aggregate with no keys and no functions")
        sel = self.select_list(keys + aggs)
        gb = f" GROUP BY {', '.join(keys)}" if keys else ""
        from_ = "(SELECT 1 AS z)" if n == 0 else f"({src})"
        return f"SELECT {sel} FROM {from_} AS {a}{gb}", len(keys) + len(aggs), out_dead

    def rel_sort(self, b, outer):
        src, n, dead = self.rel(b["source"], outer)
        a = self.alias()
        sql = f"SELECT {self.select_list(self.live(a, n, dead))} FROM ({src}) AS {a}"
        if b["collation"]:
            if outer:
                raise Skip("ORDER BY inside a correlated subquery (ordinal base not verified)")
            parts = []
            for col, _typ, d in b["collation"]:
                if not 0 <= col < n:
                    raise Skip("collation ordinal out of range")
                if col in dead:
                    raise Skip(f"ORDER BY a column of type {dead[col]} (no exact encoding)")
                if d not in ("ASCENDING", "DESCENDING"):
                    raise Skip(f"sort direction {d}")
                parts.append(f"{a}.c{col}{' DESC' if d == 'DESCENDING' else ''}")
            sql += " ORDER BY " + ", ".join(parts)
        ctx = outer + self.cols(a, n, dead)
        if (b.get("limit") is not None or b.get("offset") is not None) and not b["collation"]:
            raise Skip("LIMIT/OFFSET without ORDER BY (nondeterministic)")
        if b.get("limit") is not None:
            sql += f" LIMIT {self.expr(b['limit'], ctx)}"
        if b.get("offset") is not None:
            sql += f" OFFSET {self.expr(b['offset'], ctx)}"
        return sql, n, dead

    def _setop(self, items, outer, keyword):
        if len(items) < 2:
            raise Skip(f"{keyword} with fewer than two inputs")
        parts, n0, all_dead = [], None, {}
        for it in items:
            s, n, dead = self.rel(it, outer)
            self.need_equality(dead, keyword)
            all_dead.update(dead)
            if n0 is None:
                n0 = n
            elif n != n0:
                raise Skip(f"{keyword} inputs with different widths")
            parts.append(f"({s})")
        a = self.alias()
        body = f" {keyword} ".join(parts)
        return f"SELECT {self.select_list(self.live(a, n0, all_dead))} FROM ({body}) AS {a}", n0, all_dead

    def rel_union(self, b, outer):
        return self._setop(b, outer, "UNION ALL")

    def rel_intersect(self, b, outer):
        return self._setop(b, outer, "INTERSECT")

    def rel_except(self, b, outer):
        return self._setop(b, outer, "EXCEPT")

    def rel_values(self, b, outer):
        types = b["schema"]
        n = len(types)
        for t in types:
            if t not in DDL_TYPES:
                raise Skip(f"VALUES column type {t}")
        rows = b["content"]
        if n == 0:
            if len(rows) != 1:
                raise Skip("zero-column VALUES with other than one row")
            return "SELECT 1 AS z", 0, {}
        if not rows:
            exprs = [f"CAST(NULL AS {CAST_TYPES[t]})" for t in types]
            return f"SELECT {self.select_list(exprs)} WHERE 1 = 0", n, {}
        sels = []
        for r in rows:
            if len(r) != n:
                raise Skip("VALUES row width mismatch")
            sels.append(f"SELECT {self.select_list([self.expr(c, outer) for c in r])}")
        if len(sels) == 1:
            return sels[0], n, {}
        a = self.alias()
        return (f"SELECT {self.select_list(self.cols(a, n))} FROM ({' UNION ALL '.join(sels)}) AS {a}",
                n, {})


# ----------------------------------------------------------------------- DDL
def schema_ddl(i: int, s: dict) -> str:
    cols = []
    for j, (t, nullable) in enumerate(zip(s["types"], s["nullable"])):
        # a type with no exact encoding is an opaque placeholder no converted pair may read
        cols.append(f"  t{i}_c{j} {DDL_TYPES.get(t, 'BIGINT')}{'' if nullable else ' NOT NULL'}")
    pk_used = False
    for key in s["key"]:
        if not key:
            raise Skip("table key is the empty set (at most one row), not expressible in the DDL")
        if any(s["types"][j] not in DDL_TYPES and s["types"][j] not in OPAQUE_EQ for j in key):
            raise Skip("table key over a column with no exact encoding")
        names = ", ".join(f"t{i}_c{j}" for j in key)
        if not pk_used and all(not s["nullable"][j] for j in key):
            cols.append(f"  PRIMARY KEY ({names})")
            pk_used = True
        else:
            cols.append(f"  UNIQUE ({names})")
    return f"CREATE TABLE t{i} (\n" + ",\n".join(cols) + "\n);"


def schemas_struct(schemas: list[dict]) -> list[dict]:
    return [
        {
            "table": f"t{i}",
            "columns": [{"name": f"t{i}_c{j}", "type": t, "nullable": n}
                        for j, (t, n) in enumerate(zip(s["types"], s["nullable"]))],
            "keys": [[f"t{i}_c{j}" for j in k] for k in s["key"]],
        }
        for i, s in enumerate(schemas)
    ]


def used_scans(node, acc: set[int]) -> set[int]:
    if isinstance(node, dict):
        if isinstance(node.get("scan"), int):
            acc.add(node["scan"])
        for v in node.values():
            used_scans(v, acc)
    elif isinstance(node, list):
        for v in node:
            used_scans(v, acc)
    return acc


def convert_case(doc: dict) -> dict:
    schemas = doc["schemas"]
    help_text = "".join(doc.get("help", []))
    for pat, why in HELP_SKIPS:
        if pat.search(help_text):
            raise Skip(why)
    if len(doc["queries"]) != 2:
        raise Skip("not exactly two queries")
    # only tables a query reads are declared (the others are never referenced)
    used = sorted(used_scans(doc["queries"], set()))
    ddl = "\n".join(schema_ddl(i, schemas[i]) for i in used)
    out = []
    for q in doc["queries"]:
        sql, _, dead = Converter(schemas).rel(q, [])
        Converter.need_equality(dead, "returning (a bag comparison)")
        out.append(sql)
    structs = schemas_struct(schemas)
    return {"sql_a": out[0], "sql_b": out[1], "ddl": ddl, "schemas": [structs[i] for i in used]}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--src", required=True, type=Path, help="QED prover tests/cockroach directory")
    ap.add_argument("--out", type=Path,
                    default=Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "qed_cockroach")
    args = ap.parse_args()

    files = sorted(args.src.glob("*/*.json"), key=lambda p: (p.parent.name, int(p.stem)))
    cases, skipped = [], []
    ddl_ids: dict[str, str] = {}
    for f in files:
        doc = json.loads(f.read_text())
        name = f"{f.parent.name}/{f.stem}"
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
    with (args.out / "qed_cockroach_pairs.jsonl").open("w") as fh:
        for c in cases:
            fh.write(json.dumps(c, ensure_ascii=False, separators=(",", ":")) + "\n")
    with (args.out / "qed_cockroach_skipped.jsonl").open("w") as fh:
        for s in skipped:
            fh.write(json.dumps(s, ensure_ascii=False) + "\n")
    (args.out / "qed_cockroach_schemas.json").write_text(
        json.dumps({sid: ddl for ddl, sid in ddl_ids.items()}, indent=1, ensure_ascii=False) + "\n")

    reasons = Counter(re.sub(r"\(\d+\)|\d+", "N", s["reason"]) if s["reason"].startswith("column ordinal") else s["reason"]
                      for s in skipped)
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
