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
"""

from __future__ import annotations

from pathlib import Path
import sys

TOOLS = Path(__file__).resolve().parent.parent
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import sqlglot  # noqa: E402

import singh_bedathur_bench as sbb  # noqa: E402

from recheck.engine import Case, Column, Table  # noqa: E402

_KIND = {"BIGINT": "int", "VARCHAR": "text", "DATE": "date"}


def _column(name: str, sql_type: str) -> Column:
    if sql_type.startswith("DECIMAL"):
        return Column(name, "decimal", sql_type=sql_type)
    return Column(name, _KIND[sql_type], sql_type=sql_type)


def engine_tables(pair: sbb.Pair, kinds: dict, widen: bool = False) -> dict[str, Table]:
    """The harness's tables (``new_database``): every column nullable, no keys; ``widen`` makes BIGINT columns DECIMAL(18,3)."""

    out = {}
    for table, columns in pair.tables.items():
        types = [kinds.get((table, c), "BIGINT") for c in columns]
        if widen:
            types = ["DECIMAL(18,3)" if t == "BIGINT" else t for t in types]
        out[table] = Table(table, [_column(c, t) for c, t in zip(columns, types)])
    return out


class Singh:
    """``items()`` lists every pair (picklable); ``case(item)`` proves it as the harness does and returns a Case, or None."""

    def __init__(self, widen: bool = False):
        self.widen = widen
        self.name = "singh-fractions" if widen else "singh"

    def items(self) -> list[dict]:
        return [
            {"pair": p.key, "index": p.index, "left": p.left, "right": p.right, "tables": p.tables, "label": p.gold, "held_out": p.held_out}
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
        return Case(
            self.name, pair.key, left_sql, right_sql, engine_tables(pair, kinds, self.widen),
            setup=sbb.DUCKDB_SETTINGS, held_out=pair.held_out, source=(pair.left, pair.right), dialect="mysql",
            meta={"label": pair.gold, "reason": (reason or "")[:200], "index": pair.index},
        )


ADAPTERS = {a.name: a for a in [Singh(), Singh(widen=True)]}
