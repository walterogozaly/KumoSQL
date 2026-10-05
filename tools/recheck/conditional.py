"""Conditional proofs of ``tools/conditional_bench.py``: pairs proved equal only under named conditions.

The eval (``prove_equivalent_algebraic(..., conditional=True)``) proves a pair its plain prover cannot
under a minimal set of NOT NULL columns, unique keys and single-column foreign keys read off the queries,
and then checks the proof on random databases repaired to meet those conditions. This adapter proves each
pair the eval's way and hands the *conditions* to the search as declared constraints: a NOT NULL condition
is ``Column.not_null``, a unique condition a table key, a foreign key a foreign key of the table. Every
database the search draws, shrinks or compares therefore meets every condition (the engine builds such
databases directly; it does not repair random ones), and a difference on one is a false proof. Pairs the
prover proves outright are not conditional proofs (the plain evals re-check them) and read as
``not-proven`` here.

* ``conditional-equivalence-singh``: the 2,800 Singh & Bedathur pairs, proved by the eval's ``_singh_prove``
  (the pair, then its canonical rewrite) and run on the DuckDB SQL of the text that was proved, with the
  harness's inferred column types and its MySQL-on-DuckDB settings.
* ``conditional-equivalence-verieql``: VeriEQL's LeetCode cases, proved as ``decide_verieql`` proves them
  (the spec's own keys, NOT NULL columns and foreign keys, exact arithmetic, MySQL), with the conditions
  added to the spec (``Searcher`` and the ``verieql`` adapter's tables and ``legal`` read it). The eval scores
  every eighth case, and this adapter lists the same ones. A proof that rests on a composite foreign key is
  skipped: the executed check cannot impose one (the eval counts these as not re-checkable too).

The condition search has a wall clock (``conditional_seconds``; the eval uses 600 s). The default here is
the same; ``KUMOSQL_RECHECK_CONDITIONAL_SECONDS`` shortens it on a loaded machine. A shorter clock can only
lose proofs (a pair then reads ``not-proven``), never add one.
"""

from __future__ import annotations

import os
from pathlib import Path
import sys

TOOLS = Path(__file__).resolve().parent.parent
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import sqlglot  # noqa: E402

import conditional_bench as cb  # noqa: E402
import singh_bedathur_bench as sbb  # noqa: E402
import verieql_bench as vb  # noqa: E402
from kumosql import counterexample as cx  # noqa: E402
from kumosql.smt_equivalence import SmtStatus, TableConstraints  # noqa: E402

from recheck import singh as singh_adapter  # noqa: E402
from recheck import verieql as verieql_adapter  # noqa: E402
from recheck.engine import Case, Table  # noqa: E402

SECONDS = float(os.environ.get("KUMOSQL_RECHECK_CONDITIONAL_SECONDS", cb.SEARCH_SECONDS))


def apply_conditions(tables: dict[str, Table], conditions) -> dict[str, Table]:
    """``tables`` with each condition declared: NOT NULL on the column, a key, or a foreign key (names in any case)."""

    by_name = {name.lower(): table for name, table in tables.items()}

    def column(table: Table, name: str) -> str:
        return table.columns[table.index(name)].name

    for c in conditions:
        table = by_name[c.table.lower()]
        if c.kind == "not_null":
            table.columns[table.index(c.columns[0])].not_null = True
        elif c.kind == "unique":
            table.keys.append(tuple(column(table, n) for n in c.columns))
        elif c.kind == "foreign_key":
            parent = by_name[c.parent.lower()]
            table.foreign_keys.append((tuple(column(table, n) for n in c.columns), parent.name, tuple(column(parent, n) for n in c.parent_columns)))
        else:
            raise ValueError(f"unknown condition kind {c.kind!r}")
    return tables


class _Base:
    name = ""

    def items(self) -> list[dict]:
        raise NotImplementedError

    def case(self, item: dict) -> Case | None:
        raise NotImplementedError


class ConditionalSingh(_Base):
    name = "conditional-equivalence-singh"

    def items(self) -> list[dict]:
        return [
            {"pair": p.key, "index": p.index, "left": p.left, "right": p.right, "tables": p.tables, "label": p.gold, "held_out": p.held_out}
            for p in sbb.load_pairs()
        ]

    def case(self, item: dict) -> Case | None:
        pair = sbb.Pair(item["index"], item["left"], item["right"], item["tables"], item["label"])
        try:
            trees = [sqlglot.parse_one(pair.left, read="mysql"), sqlglot.parse_one(pair.right, read="mysql")]
            cb.SEARCH_SECONDS = SECONDS  # the eval's options (``_prove_options`` reads this) with this run's wall clock
            result, texts = cb._singh_prove(pair)
        except Exception:  # a crash is a failure to prove, never a proof
            return None
        if result.status is not SmtStatus.PROVEN_CONDITIONALLY:
            return None
        conditions = list(result.conditions)
        # as ``_singh_search`` runs it: the kinds come from the pair's own trees, the SQL from the text that was proved
        left_sql, right_sql = sbb.translate(texts[0]), sbb.translate(texts[1])
        kinds = sbb.column_kinds(trees, pair.tables)
        tables = apply_conditions(singh_adapter.engine_tables(pair, kinds), conditions)
        return Case(
            self.name, pair.key, left_sql, right_sql, tables, setup=sbb.DUCKDB_SETTINGS, held_out=pair.held_out, source=texts,
            dialect="mysql",
            meta={"label": pair.gold, "index": pair.index, "reason": (result.reason or "")[:200], "conditions": [c.text for c in conditions]},
        )


class ConditionalVeriEQL(_Base):
    name = "conditional-equivalence-verieql"

    def items(self) -> list[dict]:
        # the eval scores every eighth case (``conditional_bench.py verieql --every 8``)
        return [{"pair": f"leetcode:{case['index']}", "case": case} for case in vb.load_cases("leetcode")[::8]]

    def case(self, item: dict) -> Case | None:
        from kumosql.algebraic_equivalence import prove_equivalent_algebraic

        case = item["case"]
        try:  # ``decide_verieql``, up to the proof
            spec = vb.build_spec(case)
            left, right, predicates = vb.repaired_pair(case, spec)
        except Exception:
            return None
        if predicates:
            return None
        lower = {t.name.lower(): t for t in spec.tables.values()}
        schema = {n: [c.name.lower() for c in t.columns] for n, t in lower.items()}
        types = {n: {c.name.lower(): c.type for c in t.columns} for n, t in lower.items()}
        constraints = {
            n: TableConstraints(
                not_null=frozenset(c.name.lower() for c in t.columns if c.not_null),
                keys=tuple(tuple(k.lower() for k in key) for key in ([t.primary_key] if t.primary_key else []) + list(t.unique)),
                foreign_keys=tuple(
                    ((column.lower(),), parent.lower(), (parent_column.lower(),))
                    for child, column, parent, parent_column in spec.foreign_keys
                    if child.lower() == n
                ),
            )
            for n, t in lower.items()
        }
        options = dict(schema=schema, types=types, compare_names=False, dialect="mysql", exact_arithmetic=True, timeout_ms=3000, conditional_seconds=SECONDS)
        try:
            result = prove_equivalent_algebraic(left, right, constraints=constraints, conditional=True, **options)
        except Exception:
            return None
        if result.status is not SmtStatus.PROVEN_CONDITIONALLY:
            return None
        conditions = list(result.conditions)
        if any(c.kind == "foreign_key" and len(c.columns) != 1 for c in conditions):
            return None  # the executed check cannot impose a composite foreign key
        stricter = vb.build_spec(case)  # the spec again, with the conditions as extra constraints
        names = {t.name.lower(): t for t in stricter.tables.values()}

        def actual(table, name):
            return next(col.name for col in table.columns if col.name.lower() == name.lower())

        for c in conditions:
            table = names[c.table]
            if c.kind == "not_null":
                table.column(actual(table, c.columns[0])).not_null = True
            elif c.kind == "unique":
                table.unique.append(tuple(actual(table, n) for n in c.columns))
            else:
                parent = names[c.parent]
                stricter.foreign_keys.append((table.name, actual(table, c.columns[0]), parent.name, actual(parent, c.parent_columns[0])))
        try:
            searcher = cx.Searcher(stricter, left, right)
        except Exception:
            return None
        if not searcher.runs():
            return None
        tables = verieql_adapter.engine_tables(stricter)
        return Case(
            self.name, item["pair"], searcher.left_sql, searcher.right_sql, tables, legal=verieql_adapter._legal(stricter, tables),
            source=(left, right), dialect="mysql",
            meta={"suite": "leetcode", "index": case["index"], "reason": (result.reason or "")[:200], "conditions": [c.text for c in conditions]},
        )


ADAPTERS = {a.name: a for a in [ConditionalSingh(), ConditionalVeriEQL()]}
