"""Which declared guarantees a rewrite's equivalence proof actually needs.

A proof that uses declared NOT NULL columns, keys and foreign keys is only as good as
those declarations (BigQuery does not enforce them). ``needed_guarantees`` finds a minimal
set of declared facts the proof relies on: it proves the pair with every fact on the
queried tables, then drops facts one at a time and keeps a fact out when the proof still
goes through without it. What is left is minimal: remove any one of them and the proof
fails, or the prover abstains. The result names the facts in words (``orders.customer_id
is NOT NULL``, ``(id) is unique in customers``, ``orders(customer_id) references
customers(id)``) so a recommendation can say what has to hold in the data.

The prover is passed in, so any prover with the ``prove_equivalent_*`` signature works;
the default is the algebraic one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Mapping

import sqlglot
from sqlglot import exp

from .ast_utils import table_parts
from .smt_equivalence import SmtEquivalenceResult, SmtStatus, TableConstraints


@dataclass(frozen=True)
class Guarantee:
    """One declared fact: a NOT NULL column, a unique key or a foreign key."""

    kind: str  # "not_null", "unique" or "foreign_key"
    table: str
    columns: tuple[str, ...]
    parent: str = ""
    parent_columns: tuple[str, ...] = ()

    @property
    def label(self) -> str:
        if self.kind == "not_null":
            return f"{self.table}.{self.columns[0]} is NOT NULL"
        if self.kind == "unique":
            return f"({', '.join(self.columns)}) is unique in {self.table}"
        return f"{self.table}({', '.join(self.columns)}) references {self.parent}({', '.join(self.parent_columns)})"


@dataclass(frozen=True)
class GuaranteeReport:
    """Outcome of ``needed_guarantees``."""

    status: str  # "proven", "not_proven" or "refuted"
    needed: tuple[Guarantee, ...] = ()  # a minimal sufficient set (empty when none is needed)
    offered: tuple[Guarantee, ...] = ()  # every declared fact on the queried tables
    proofs_run: int = 0
    result: SmtEquivalenceResult | None = field(default=None, compare=False)

    @property
    def labels(self) -> tuple[str, ...]:
        return tuple(g.label for g in self.needed)


def guarantees_of(constraints: Mapping[str, TableConstraints], tables: set[str] | None = None) -> list[Guarantee]:
    """Every declared fact, in a fixed order (NOT NULL, then keys, then foreign keys)."""

    found: list[Guarantee] = []
    for table in sorted(constraints):
        if tables is not None and table.lower() not in tables:
            continue
        facts = constraints[table]
        found += [Guarantee("not_null", table, (column,)) for column in sorted(facts.not_null)]
        found += [Guarantee("unique", table, tuple(key)) for key in facts.keys]
        found += [
            Guarantee("foreign_key", table, tuple(fk[0]), fk[1], tuple(fk[2]))
            for fk in getattr(facts, "foreign_keys", ())
        ]
    return found


def constraints_with(guarantees: list[Guarantee]) -> dict[str, TableConstraints]:
    """The constraints object holding exactly these facts."""

    by_table: dict[str, dict] = {}
    for g in guarantees:
        entry = by_table.setdefault(g.table, {"not_null": set(), "keys": [], "foreign_keys": []})
        if g.kind == "not_null":
            entry["not_null"].add(g.columns[0])
        elif g.kind == "unique":
            entry["keys"].append(g.columns)
        else:
            entry["foreign_keys"].append((g.columns, g.parent, g.parent_columns))
    out = {}
    for table, entry in by_table.items():
        extra = {"foreign_keys": tuple(entry["foreign_keys"])} if "foreign_keys" in TableConstraints.__dataclass_fields__ else {}
        out[table] = TableConstraints(not_null=frozenset(entry["not_null"]), keys=tuple(entry["keys"]), **extra)
    return out


def tables_of(*queries: str, dialect: str = "bigquery") -> set[str]:
    """Lower-case names of the tables the queries read, as written and as dotted suffixes."""

    names: set[str] = set()
    for sql in queries:
        for table in sqlglot.parse_one(sql, read=dialect).find_all(exp.Table):
            parts = table_parts(table)
            names.update(".".join(parts[i:]) for i in range(len(parts)))
    return names


def needed_guarantees(
    left_sql: str,
    right_sql: str,
    *,
    schema: Mapping[str, list[str]] | None,
    constraints: Mapping[str, TableConstraints],
    prove: Callable[..., SmtEquivalenceResult] | None = None,
    dialect: str = "bigquery",
    **options,
) -> GuaranteeReport:
    """A minimal set of ``constraints`` that the proof of ``left_sql`` = ``right_sql`` needs."""

    if prove is None:
        from .algebraic_equivalence import prove_equivalent_algebraic as prove
    queried = tables_of(left_sql, right_sql, dialect=dialect)
    # a foreign key's parent is read by the rewrite only through the join it removes, so its facts count too
    parents = {g.parent.lower() for g in guarantees_of(constraints, queried) if g.kind == "foreign_key"}
    offered = guarantees_of(constraints, queried | parents)
    runs = 0

    def attempt(facts: list[Guarantee]) -> SmtEquivalenceResult:
        nonlocal runs
        runs += 1
        return prove(left_sql, right_sql, schema=dict(schema or {}), constraints=constraints_with(facts) or None, dialect=dialect, **options)

    full = attempt(offered)
    if not full.proven:
        status = "refuted" if full.status is SmtStatus.NOT_EQUIVALENT else "not_proven"
        return GuaranteeReport(status, offered=tuple(offered), proofs_run=runs, result=full)
    keep = list(offered)
    for fact in list(offered):
        trial = [g for g in keep if g is not fact]
        if attempt(trial).proven:
            keep = trial
    return GuaranteeReport("proven", needed=tuple(keep), offered=tuple(offered), proofs_run=runs, result=full)


def without(constraints: Mapping[str, TableConstraints], fact: Guarantee) -> dict[str, TableConstraints]:
    """``constraints`` with one fact removed."""

    return constraints_with([g for g in guarantees_of(constraints) if g != fact])
