"""Is a query's result determined on one database, or does it rest on a tie broken by chance?

:mod:`kumosql.tie_determinism` answers that for every database that respects the declared keys. It is a
static pass, so it says ``unknown`` whenever *some* legal database could tie. A refutation is about one
database ``D``, and on ``D`` the question is simpler and sharper: do the rows that tie differ in anything
read afterwards, and does the answer change with the way the engine breaks the tie?

``profile`` runs a query on ``D`` under every tie-break it can name and records the distinct results:

* **Storage order.** DuckDB on one thread (and SQLite) break the ties of a window, a ``LIMIT``, an
  ``ARRAY_AGG`` and a bare column by the order the rows are stored in. Every order of each table of up to
  four rows is tried, then reversed, rotated and shuffled orders, at most ``max_orders`` databases.
* **Tied cut.** Each ``LIMIT``/``OFFSET`` (nested ones too) is run again with every output column added
  to its ``ORDER BY``, ascending and descending (what the proof re-check's tie probe does), so the cut
  falls on a different tied row.
* **Free picks.** ``ANY_VALUE``, ``FIRST``, ``LAST`` and ``ARBITRARY`` follow DuckDB's hash order, which
  storage order does not move. The query is run with each pick made to fail when its group holds more
  than one value (:func:`kumosql.counterexample.guard_arbitrary_picks`). A failure means the pick is free
  on ``D``: the results BigQuery could return are not all observed, so the profile is *incomplete*.

A profile is *determined* when it is complete and every variant returned the same result.

``refutation_verdict`` turns two profiles into the only conclusion a pair of queries allows on ``D``:

* both determined: they differ, or they do not;
* one determined and the other complete: they differ when the determined result is none of the other's
  observed results (it differs under every tried tie-break);
* anything else, when the results differ: ``unknown``. The difference may be which tied row each query
  kept, which is not a different answer.

The observed results are the tie-breaks that were tried, not all that exist: tables of up to four rows are
covered exhaustively, larger ones by a sample of orders. Preconditions the verdict does not check: the
engine must break ties by storage order (DuckDB with one thread; SQLite), which is what makes the storage
orders a probe of the ties BigQuery leaves open. Finding nothing never makes a pair equivalent; this module
only ever *withholds* a refutation.
"""

from __future__ import annotations

from dataclasses import dataclass
import itertools
import random
from typing import Any, Callable, Mapping, Sequence

import sqlglot
from sqlglot import exp

from .result_equivalence import (
    ExecutionError,
    QueryOutput,
    SyntheticDataset,
    SyntheticTable,
    compare_outputs,
)

_SET_OPERATIONS = (exp.SetOperation,) if hasattr(exp, "SetOperation") else (exp.Union,)


@dataclass(frozen=True)
class TieProfile:
    """The results one query gave on one database over every tie-break tried."""

    outputs: tuple[Any, ...]
    complete: bool = True
    reasons: tuple[str, ...] = ()
    sql: str = ""

    @property
    def determined(self) -> bool:
        """Every tie-break returned the same result and none was left unobserved."""

        return self.complete and len(self.outputs) == 1


@dataclass(frozen=True)
class TieVerdict:
    """``different`` (a refutation), ``same`` (the results agree) or ``unknown`` (a tie may explain it)."""

    status: str
    reason: str = ""


# ----- storage orders ----------------------------------------------------------------------------


def storage_orders(tables: Mapping[str, Sequence[tuple]], limit: int = 48, seed: int = 0):
    """The tables with their rows stored in other orders (the stored order first), at most ``limit`` of them.

    Each yield maps a table name to a tuple of rows. Tables of up to four rows take every order;
    combinations beyond ``limit`` are sampled. Larger tables keep their order, reversed, rotated both
    ways and a few shuffles.
    """

    rng = random.Random(seed)
    names = list(tables)
    options: list[list[tuple]] = []
    for name in names:
        rows = list(tables[name])
        if len(rows) <= 4:
            orders = [tuple(p) for p in itertools.permutations(rows)]
        else:
            orders = [tuple(rows), tuple(reversed(rows)), tuple(rows[1:] + rows[:1]), tuple(rows[-1:] + rows[:-1])]
            orders += [tuple(rng.sample(rows, len(rows))) for _ in range(4)]
        options.append(list(dict.fromkeys(orders)))
    total = 1
    for choices in options:
        total *= len(choices)
    if total <= limit:
        combinations: Any = itertools.product(*options)
    else:
        picked = {tuple(choices[0] for choices in options), tuple(choices[-1] for choices in options)}
        attempts = 0
        while len(picked) < limit and attempts < limit * 8:
            picked.add(tuple(rng.choice(choices) for choices in options))
            attempts += 1
        combinations = sorted(picked, key=lambda c: [options[i].index(r) for i, r in enumerate(c)])
    for combination in combinations:
        yield dict(zip(names, combination))


# ----- tied cuts ---------------------------------------------------------------------------------


def _width(node: exp.Expression) -> int | None:
    """How many columns ``node`` returns, or ``None`` when a ``*`` hides it."""

    while isinstance(node, (exp.Subquery, exp.Paren)):
        node = node.this
    if isinstance(node, _SET_OPERATIONS):
        return _width(node.this)
    if isinstance(node, exp.Select):
        if any(isinstance(e, exp.Star) or (isinstance(e, exp.Column) and isinstance(e.this, exp.Star)) for e in node.expressions):
            return None
        return len(node.expressions) or None
    return None


def _limited(node: exp.Expression) -> bool:
    return node.args.get("limit") is not None or node.args.get("offset") is not None or node.args.get("fetch") is not None


def cut_variants(sql: str, dialect: str = "duckdb") -> tuple[list[str], list[str]]:
    """``sql`` again with every output column added to the ``ORDER BY`` of each cut, ascending then descending.

    Returns ``(variants, unread)``: ``unread`` lists the cuts whose width a ``*`` hides, or that cannot be
    written again; those are only probed by storage order.
    """

    try:
        tree = sqlglot.parse_one(sql, read=dialect)
    except (sqlglot.errors.SqlglotError, ValueError, RecursionError):
        return [], ["unparsable"]
    kinds = (exp.Select, *_SET_OPERATIONS)
    if not any(_limited(node) for node in tree.find_all(*kinds)):
        return [], []
    variants: list[str] = []
    unread: list[str] = []
    for descending in (False, True):
        copy = tree.copy()
        for node in list(copy.find_all(*kinds)):
            if not _limited(node):
                continue
            width = _width(node)
            if width is None:
                unread.append("LIMIT over a star")
                continue
            keys = list(node.args["order"].expressions) if node.args.get("order") is not None else []
            # the descending reading is the exact reverse of the ascending one, NULLs included
            keys += [exp.Ordered(this=exp.Literal.number(i), desc=descending, nulls_first=descending) for i in range(1, width + 1)]
            node.set("order", exp.Order(expressions=keys))
        try:
            variants.append(copy.sql(dialect=dialect))
        except Exception:  # noqa: BLE001 - a construct sqlglot cannot write again
            unread.append("LIMIT not rewritable")
    return variants, sorted(set(unread))


# ----- profiles ----------------------------------------------------------------------------------


def probe(
    sql: str,
    tables: Mapping[str, Sequence[tuple]],
    run: Callable[[str, Mapping[str, Sequence[tuple]]], Any],
    same: Callable[[Any, Any], bool],
    *,
    engine: str = "duckdb",
    errors: tuple[type[BaseException], ...] = (ExecutionError,),
    max_orders: int = 48,
) -> TieProfile:
    """Run ``sql`` (text the engine reads) on ``tables`` under every tie-break tried.

    ``run(text, tables)`` returns a result, ``same`` says whether two results are the same bag and
    ``errors`` are what a failed run raises (a failure makes the profile incomplete).
    """

    from .counterexample import guard_arbitrary_picks

    outputs: list[Any] = []
    reasons: list[str] = []
    complete = True

    def keep(output: Any) -> None:
        if not any(same(seen, output) for seen in outputs):
            outputs.append(output)

    def attempt(text: str, data: Mapping[str, Sequence[tuple]], what: str) -> Any:
        nonlocal complete
        try:
            return run(text, data)
        except errors as exc:
            complete = False
            reasons.append(f"{what}: {str(exc)[:80]}")
            return None

    base = attempt(sql, tables, "stored order")
    if base is None:
        return TieProfile((), False, tuple(reasons), sql)
    keep(base)
    for other in storage_orders(tables, max_orders):
        output = attempt(sql, other, "storage order")
        if output is None:
            break
        keep(output)
    variants, unread = cut_variants(sql, engine)
    for what in unread:
        reasons.append(f"{what} is probed by storage order only")
    for variant in variants:
        output = attempt(variant, tables, "tied cut")
        if output is not None:
            keep(output)
    if engine == "duckdb":
        try:
            guarded = guard_arbitrary_picks(sql)
        except (sqlglot.errors.SqlglotError, ValueError, RecursionError):
            guarded = None
        if guarded is None:
            complete = False
            reasons.append("a pick (ANY_VALUE, FIRST, LAST) cannot be guarded")
        elif guarded != sql:
            try:
                run(guarded, tables)
            except errors:
                complete = False
                reasons.append("a pick (ANY_VALUE, FIRST, LAST) is free: its group holds several values")
    if len(outputs) > 1 and not reasons:
        reasons.append("the result changes with the tie-break")
    return TieProfile(tuple(outputs), complete, tuple(dict.fromkeys(reasons)), sql)


def profile(
    runner,
    sql: str,
    dataset: SyntheticDataset,
    compare: Mapping[str, Any],
    *,
    timeout: float | None = None,
    max_orders: int = 48,
) -> TieProfile:
    """:func:`probe` for a refuter's runner (:class:`~kumosql.result_equivalence.DatasetRunner` or SQLite's)."""

    if hasattr(runner, "run_prepared"):  # DuckDB
        engine, execute = "duckdb", runner.run_prepared
        try:
            text = runner.prepare(sql)
        except ExecutionError as exc:
            return TieProfile((), False, (f"not runnable: {str(exc)[:80]}",))
    else:  # kumosql.refute.SqliteRunner
        engine, execute, text = "sqlite", runner.run, sql

    def run(query: str, tables: Mapping[str, Sequence[tuple]]) -> QueryOutput:
        data = SyntheticDataset(dataset.seed, {n: SyntheticTable(dataset.tables[n].columns, tuple(rows)) for n, rows in tables.items()})
        return execute(query, data, timeout=timeout)

    def same(a: QueryOutput, b: QueryOutput) -> bool:
        return compare_outputs(a, b, **compare)[0]

    stored = {name: table.rows for name, table in dataset.tables.items()}
    return probe(text, stored, run, same, engine=engine, max_orders=max_orders)


def refutation_verdict(left: TieProfile, right: TieProfile, same: Callable[[Any, Any], bool]) -> TieVerdict:
    """What two profiles on one database allow: ``different``, ``same`` or ``unknown`` (see the module docstring)."""

    if not left.outputs or not right.outputs:
        return TieVerdict("unknown", "a query could not be probed: " + "; ".join(left.reasons + right.reasons))
    meeting = any(same(a, b) for a in left.outputs for b in right.outputs)
    if left.determined and right.determined:
        return TieVerdict("same" if meeting else "different")
    if meeting:
        return TieVerdict("unknown", "some tie-break makes the two results agree")
    for one, other, name in ((left, right, "left"), (right, left, "right")):
        if one.determined and other.complete:
            return TieVerdict("different", f"the {name} query is determined and the other differs under every tie-break tried")
    reasons = left.reasons + right.reasons
    if left.determined or right.determined:
        side = "right" if left.determined else "left"
        return TieVerdict("unknown", f"the {side} query may pick rows freely on this database: " + "; ".join(reasons))
    return TieVerdict("unknown", "both queries depend on a tie on this database: " + "; ".join(reasons))


def tie_verdict(
    runner,
    left: str,
    right: str,
    dataset: SyntheticDataset,
    compare: Mapping[str, Any],
    *,
    timeout: float | None = None,
    max_orders: int = 48,
) -> TieVerdict:
    """:func:`refutation_verdict` for two queries run on ``dataset`` by ``runner``."""

    return refutation_verdict(
        profile(runner, left, dataset, compare, timeout=timeout, max_orders=max_orders),
        profile(runner, right, dataset, compare, timeout=timeout, max_orders=max_orders),
        lambda a, b: compare_outputs(a, b, **compare)[0],
    )
