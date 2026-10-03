"""Proven pairs of the Singh & Bedathur LeetCode eval (``tools/singh_bedathur_bench.py``).

Each pair is proved exactly as the harness proves it (``prove``: the algebraic prover with column names
only, then the constraint-free canonical rewrites), and a proven pair becomes a Case with the DuckDB SQL
the harness runs (``translate``), the column types the harness infers from the queries
(``column_kinds``) and its MySQL-on-DuckDB settings (NULLs sort first, strings compare without case).
The pairs carry no keys, NOT NULL facts or foreign keys, so every database with those columns counts.

``singh-fractions`` re-checks the same proofs with every whole-number column widened to
``DECIMAL(18,3)``. The files give no column types, so a proof claims every typing; the harness only lets
summed, averaged or rounded columns hold fractions. A difference found only there needs the column's
real LeetCode type before it counts (see the findings page).

``singh-leetcode-types`` re-checks them with each column's type from VeriEQL's copy of the same LeetCode
problem (every pair is in VeriEQL's LeetCode set with the same text): a column the queries never compare
with a string literal is a whole number for the harness, but may be a string in LeetCode, where MySQL's
case-insensitive comparison applies. Keys, NOT NULL and ENUM value lists stay off (this eval has none).

DuckDB cannot show where MySQL itself departs from the translation (strings compared with numbers, inexact
decimal division, accent-insensitive collation); ``recheck/singh_mysql.py`` runs the same Cases' original
text on a MySQL 8 server.
"""

from __future__ import annotations

from pathlib import Path
import re
import sys

TOOLS = Path(__file__).resolve().parent.parent
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import sqlglot  # noqa: E402

import singh_bedathur_bench as sbb  # noqa: E402

from recheck.engine import Case, Column, Table  # noqa: E402

_KIND = {"BIGINT": "int", "VARCHAR": "text", "DATE": "date", "TIME": "time", "BOOLEAN": "bool"}
# VeriEQL's column types as the harness's DuckDB types (an ENUM is a string; its value list is a constraint)
_LEETCODE = {"INT": "BIGINT", "VARCHAR": "VARCHAR", "DATE": "DATE", "NUMERIC": "DECIMAL(18,3)", "TIME": "TIME", "BOOL": "BOOLEAN"}


def _column(name: str, sql_type: str) -> Column:
    if sql_type.startswith("DECIMAL"):
        return Column(name, "decimal", sql_type=sql_type)
    return Column(name, _KIND[sql_type], sql_type=sql_type)


def engine_tables(pair: sbb.Pair, kinds: dict, widen: bool = False, leetcode: dict | None = None) -> dict[str, Table]:
    """The harness's tables (``new_database``): every column nullable, no keys; ``widen`` makes BIGINT columns
    DECIMAL(18,3); ``leetcode`` (table -> column -> VeriEQL type) overrides the inferred types."""

    out = {}
    for table, columns in pair.tables.items():
        types = [kinds.get((table, c), "BIGINT") for c in columns]
        if widen:
            types = ["DECIMAL(18,3)" if t == "BIGINT" else t for t in types]
        if leetcode is not None:
            given = leetcode.get(table, {})
            types = [_leetcode_type(given.get(c), t) for c, t in zip(columns, types)]
        out[table] = Table(table, [_column(c, t) for c, t in zip(columns, types)])
    return out


def _leetcode_type(given: str | None, inferred: str) -> str:
    if given is None:
        return inferred
    return "VARCHAR" if given.startswith("ENUM") else _LEETCODE.get(given, inferred)


def _text(sql: str) -> str:
    return re.sub(r"\s+", " ", sql.strip().rstrip(";").strip()).upper()


def leetcode_types() -> dict[tuple[str, str], dict]:
    """(left, right) text -> table -> column -> type, from VeriEQL's LeetCode set (downloaded once)."""

    import verieql_bench

    found: dict[tuple[str, str], dict] = {}
    for case in verieql_bench.load_cases("leetcode"):
        schema = {t.lower(): {c.lower(): kind for c, kind in columns.items()} for t, columns in case["schema"].items()}
        left, right = (_text(q) for q in case["pair"])
        found.setdefault((left, right), schema)
        found.setdefault((right, left), schema)
    return found


class Singh:
    """``items()`` lists every pair (picklable); ``case(item)`` proves it as the harness does and returns a Case, or None."""

    def __init__(self, widen: bool = False, leetcode: bool = False):
        self.widen = widen
        self.leetcode = leetcode
        self.name = "singh-fractions" if widen else "singh-leetcode-types" if leetcode else "singh"

    def items(self) -> list[dict]:
        types = leetcode_types() if self.leetcode else {}
        return [
            {"pair": p.key, "index": p.index, "left": p.left, "right": p.right, "tables": p.tables, "label": p.gold, "held_out": p.held_out,
             "leetcode": types.get((_text(p.left), _text(p.right)))}
            for p in sbb.load_pairs()
        ]

    def case(self, item: dict) -> Case | None:
        pair = sbb.Pair(item["index"], item["left"], item["right"], item["tables"], item["label"])
        try:  # ``decide``: a pair the prover's parser rejects is unsupported, never proven
            trees = [sqlglot.parse_one(pair.left, read="mysql"), sqlglot.parse_one(pair.right, read="mysql")]
        except sqlglot.errors.SqlglotError:
            return None
        try:
            proved, _, reason = sbb.prove(pair)
        except Exception:  # a crash is a failure to prove, never a proof
            return None
        if not proved:
            return None
        # a translation error propagates: the harness counts such a proof unchecked, so it must not read as "not-proven"
        left_sql, right_sql = sbb.translate(pair.left), sbb.translate(pair.right)
        kinds = sbb.column_kinds(trees, pair.tables)
        if self.leetcode and item.get("leetcode") is None:
            raise LookupError("no VeriEQL schema for this pair")
        return Case(
            self.name, pair.key, left_sql, right_sql, engine_tables(pair, kinds, self.widen, item.get("leetcode") if self.leetcode else None),
            setup=sbb.DUCKDB_SETTINGS, held_out=pair.held_out, source=(pair.left, pair.right), dialect="mysql",
            meta={"label": pair.gold, "reason": (reason or "")[:200], "index": pair.index},
        )


ADAPTERS = {a.name: a for a in [Singh(), Singh(widen=True), Singh(leetcode=True)]}
