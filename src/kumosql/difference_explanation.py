"""Explain a query difference as a verified predicate: "equivalent except when P" (issue #512).

When two queries are not equivalent, :func:`explain_difference` looks for a short SQL predicate P over the
tables the queries read such that the queries return the same rows on every database in which no row
satisfies P ("equivalent except when ``status IS NULL``"). A reviewer can then accept the change, or add a
NOT NULL assertion, without reading a counterexample database. In the verdict model this is the conditional
verdict (:mod:`kumosql.conditional_equivalence`) under one more condition kind, ``no_rows``; :func:`conditions_of`
turns an explanation into those conditions.

How a predicate is found (select-project-join pairs, and grouped pairs on one row; everything else is unknown):

1. Both queries are compiled by the SMT prover's compiler into one block each, over the same tables, each table
   read once, with no subquery, DISTINCT or set operation. On a database with one row per table the outputs differ
   exactly when ``D = XOR(cond_a, cond_b) OR (cond_a AND cond_b AND outputs differ)`` holds. In a grouped block
   the one row is its own group (``COUNT`` is 1 or 0, ``SUM``, ``MIN`` and ``MAX`` are the value).
2. Candidate atoms come from the queries themselves (every comparison or predicate they write, ``col IS NULL`` and
   ``col IS NOT NULL`` for every column they read, and the equality behind each inequality such as ``x > 5``).
   Each atom mentions one table. A search over conjunctions and disjunctions of at most three atoms, shortest
   first, finds a P that Z3 shows equal to D (``exact``); failing that, one with ``D = P AND S``, where S holds
   when at least one query returns the row (D implies S): the queries differ exactly on the rows they return that
   satisfy P, which is not ``exact``. A predicate across tables is a disjunction of single-table atoms, because
   the proof below filters each table on its own, so a difference that only a comparison across tables
   (``a.x > b.y``) describes has no predicate. A P that Z3 shows only implied by D is never returned, and
   neither is one that every row satisfies. A pair of grouped blocks is never ``exact``: the groups of a table of
   many rows can interact, so only the proof speaks for them.
3. Before a predicate is returned it must pass both checks:

   * witness: a one-row-per-table database that satisfies P and on which the outputs differ is replayed on
     DuckDB, and the difference counts only if DuckDB with its optimizer off agrees, the BigQuery reading of the
     queries is faithful (:mod:`kumosql.bigquery_on_duckdb`) and no arbitrary pick decides it
     (:func:`kumosql.counterexample.guard_arbitrary_picks`);
   * proof: every table t is replaced by ``(SELECT cols FROM t WHERE (P_t) IS NOT TRUE)`` under its own alias in
     both queries, and the rewritten pair is proved equivalent by both
     :func:`kumosql.smt_equivalence.prove_equivalent_smt` and
     :func:`kumosql.algebraic_equivalence.prove_equivalent_algebraic`.

Unknown beats wrong: any failure, unsupported construct or exhausted time budget returns ``None``
(:func:`explain_or_why` also says why). The budget (``explain_seconds``, 30 by default) is checked between solver
calls and caps the time limit of each prover call, so a very large query pair can overrun it. The function is
opt-in; nothing else calls it.
"""

from __future__ import annotations

import itertools
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Mapping

import sqlglot
from sqlglot import exp

from . import smt_equivalence as smt
from .ast_utils import UnmodeledConstruct
from .conditional_equivalence import Condition, _Reader, broken_by, no_rows
from .set_operations import positional_sql_pair
from .solver_lock import bounded_solver, serialized
from .string_literals import canonical_literals
from .type_names import invalid_type_name
from . import smt_values, string_number_compare, string_number_literals

DEFAULT_SECONDS = 30.0
MAX_ATOMS = 3  # atoms in a predicate
MAX_LIBRARY = 36  # atoms searched
MAX_ROUNDS = 400  # solver round trips of one search
MAX_VERIFICATIONS = 4  # candidates taken through both checks

_OPTIONS = ("schema", "types", "constraints", "exact_arithmetic", "timeout_ms", "dialect", "compare_names")
_PREDICATES = (exp.EQ, exp.NEQ, exp.LT, exp.LTE, exp.GT, exp.GTE, exp.Is, exp.In, exp.Between, exp.Like, exp.ILike, exp.NullSafeEQ, exp.NullSafeNEQ)
_ROW_AGGREGATES = (exp.Min, exp.Max, exp.Sum, exp.Avg)
_INEQUALITIES = (exp.NEQ, exp.LT, exp.LTE, exp.GT, exp.GTE)


@dataclass(frozen=True)
class DifferenceExplanation:
    """A predicate P, verified both ways, such that the pair is equivalent except on rows where P holds."""

    sql: str  # the predicate over one table's columns, for example "status IS NULL"
    atoms: tuple[str, ...]  # the atoms the predicate is built from, as SQL text
    tables: tuple[str, ...]  # lower-case tables the predicate reads
    exact: bool  # True when the outputs differ exactly where P holds, False when P only covers the difference
    witness: dict = field(default_factory=dict)  # a database (table -> rows) with one row satisfying P where the outputs differ

    def to_json(self) -> dict:
        return {"sql": self.sql, "atoms": list(self.atoms), "tables": list(self.tables), "exact": self.exact}


# ---- the atoms --------------------------------------------------------------------------------


@dataclass
class _Atom:
    table: str  # lower-case table key
    node: exp.Expression  # the predicate over unqualified columns
    base: tuple  # an atom and its complement share a base
    written: bool  # spelled in one of the queries
    negated: bool = False
    term: object = None  # Z3: the predicate is TRUE, over the first query's table occurrence
    columns: frozenset = frozenset()

    def local(self, dialect: str) -> str:
        text = _sql(self.node, dialect)
        return f"({text}) IS NOT TRUE" if self.negated else text

    def shown(self, dialect: str, qualifier: str | None) -> str:
        node = self.node.copy()
        if qualifier:
            for column in node.find_all(exp.Column):
                column.set("table", exp.to_identifier(qualifier))
        text = _sql(node, dialect)
        if self.negated:
            return f"({text}) IS NOT TRUE"
        return f"({text})" if isinstance(self.node, (exp.Between, exp.And, exp.Or)) else text


def _sql(node: exp.Expression, dialect: str) -> str:
    """``node`` as SQL, with ``x IS NOT NULL`` spelled that way (sqlglot writes ``NOT x IS NULL``)."""

    if isinstance(node, exp.Not) and isinstance(node.this, exp.Is) and isinstance(node.this.expression, exp.Null):
        return f"{node.this.this.sql(dialect=dialect)} IS NOT NULL"
    return node.sql(dialect=dialect)


def _row_level(node: exp.Expression) -> exp.Expression | None:
    """``node`` as a test on one row: ``MAX(x) > 5`` reads ``x > 5`` (MIN, MAX, SUM and AVG of one row are the row's value),
    ``None`` for a subquery, a window or another aggregate."""

    if node.find(exp.Subquery, exp.Select, exp.Window):
        return None
    if not node.find(exp.AggFunc):
        return node
    node = node.copy()
    for call in list(node.find_all(exp.AggFunc)):
        if not isinstance(call, _ROW_AGGREGATES) or call.this is None or isinstance(call.this, exp.Distinct) or call.args.get("expressions"):
            return None
        call.replace(call.this.copy())
    return node


def _unqualified(node: exp.Expression) -> exp.Expression:
    node = node.copy()
    for column in node.find_all(exp.Column):
        for part in ("table", "db", "catalog"):
            column.set(part, None)
    return node


def _single_select(sql: str, dialect: str) -> exp.Select | None:
    tree = sqlglot.parse_one(sql, read=dialect)
    return tree if isinstance(tree, exp.Select) and not tree.args.get("with_") and not tree.args.get("with") else None


def _written_atoms(trees: list[exp.Select], schema, dialect: str) -> list[_Atom]:
    """The candidate atoms of the queries, one table each, most natural first."""

    found: dict[tuple, _Atom] = {}

    def add(atom: _Atom) -> None:
        found.setdefault((atom.table, atom.node.sql(dialect=dialect), atom.negated), atom)

    derived: list[_Atom] = []
    nulls: list[_Atom] = []
    for tree in trees:
        reader = _Reader(tree, schema)
        tables, total = reader.scope(tree)
        if total != len(tables):
            return []

        def owner_of(node: exp.Expression):
            owners = {reader.owner(c).ident if reader.owner(c) else None for c in node.find_all(exp.Column)}
            if len(owners) != 1 or None in owners:
                return None
            return reader.owner(next(node.find_all(exp.Column)))

        for found_node in tree.find_all(*_PREDICATES):
            node = _row_level(found_node)
            if node is None:
                continue
            occ = owner_of(found_node)
            if occ is None:
                continue
            columns = frozenset(c.name.lower() for c in node.find_all(exp.Column))
            local = _unqualified(node)
            if isinstance(node, exp.Is) and not isinstance(node.expression, exp.Null):
                continue
            kind = "null" if isinstance(node, exp.Is) else None
            base = ("null", occ.key, local.this.name.lower()) if kind and isinstance(local.this, exp.Column) else (occ.key, local.sql(dialect=dialect))
            add(_Atom(occ.key, local, base, True, columns=columns))
            if isinstance(node, _INEQUALITIES) and not isinstance(node, exp.NEQ):
                equal = exp.EQ(this=local.this.copy(), expression=local.expression.copy())
                derived.append(_Atom(occ.key, equal, (occ.key, equal.sql(dialect=dialect)), False, columns=columns))
        for column in tree.find_all(exp.Column):
            occ = reader.owner(column)
            if occ is None:
                continue
            bare = exp.column(column.this.copy())
            for text in ("IS NULL", "IS NOT NULL"):
                node = exp.Is(this=bare.copy(), expression=exp.Null()) if text == "IS NULL" else exp.Not(this=exp.Is(this=bare.copy(), expression=exp.Null()))
                nulls.append(_Atom(occ.key, node, ("null", occ.key, column.name.lower()), False, columns=frozenset({column.name.lower()})))
    for atom in derived + nulls:
        add(atom)
    atoms = list(found.values())
    # a written atom's complement, spelled IS NOT TRUE; a pair of null tests is already a complement
    for atom in list(atoms):
        if atom.written and atom.base[0] != "null":
            add(_Atom(atom.table, atom.node, atom.base, False, negated=True, columns=atom.columns))
    atoms = list(found.values())
    atoms.sort(key=lambda a: (a.negated, not a.written))
    return atoms[:MAX_LIBRARY]


# ---- candidates and the search ----------------------------------------------------------------


@dataclass(frozen=True)
class _Candidate:
    atoms: tuple[int, ...]
    op: str  # "and" or "or"


def _candidates(atoms: list[_Atom]) -> list[_Candidate]:
    found: list[tuple[tuple, _Candidate]] = []
    for size in range(1, MAX_ATOMS + 1):
        for combo in itertools.combinations(range(len(atoms)), size):
            chosen = [atoms[i] for i in combo]
            if len({a.base for a in chosen}) < size:
                continue  # an atom with its own complement
            single = len({a.table for a in chosen}) == 1
            ops = ("and", "or") if size > 1 and single else ("and",) if size == 1 else ("or",)
            for op in ops:
                rank = (size, sum(a.negated for a in chosen), sum(not a.written for a in chosen), op == "or", combo)
                found.append((rank, _Candidate(combo, op)))
    found.sort(key=lambda item: item[0])
    return [candidate for _, candidate in found]


class _Out(Exception):
    """The time budget or the round limit ran out."""


class _Problem:
    """The solver side of one pair: the region D, the atoms, and models of the formulas asked about."""

    def __init__(self, prover, occs, facts, region, seen, atoms: list[_Atom], deadline: float):
        self.prover, self.occs, self.facts, self.region, self.seen, self.atoms, self.deadline = prover, occs, facts, region, seen, atoms, deadline
        self.values = [v for occ in occs for v in occ.cols.values()]
        self.samples: list[tuple[list[bool], bool, bool]] = []
        self.rounds = 0

    def _solver(self, formulas):
        solver = bounded_solver(self.prover.timeout_ms)
        solver.add(*self.prover._typing(self.occs))
        solver.add(*self.prover._constraint_facts(self.occs))
        solver.add(*self.prover._group_member_facts(self.occs))
        solver.add(*self.facts)
        solver.add(*formulas)
        return solver

    def _tick(self) -> None:
        if time.monotonic() > self.deadline or self.rounds >= MAX_ROUNDS:
            raise _Out()
        self.rounds += 1

    def check(self, *formulas):
        """``(result, model or None)`` of the formulas under the declared facts."""

        self._tick()
        solver = self._solver(formulas)
        result = solver.check()
        if result != smt.z3.sat:
            return result, None
        return result, self.prover._counterexample(solver.assertions(), solver.model(), self.values)

    def readable_model(self, *formulas):
        """A model of the formulas in which each column holds a (non-NULL) whole number wherever nothing forbids it, or ``None``."""

        z3 = smt.z3
        V = smt._value_sort()
        self._tick()
        solver = self._solver(formulas)
        if solver.check() != z3.sat:
            return None
        for v in self.values:
            whole = z3.And(V.is_Num(v.val), z3.IsInt(V.num(v.val)))
            for wish in (z3.And(z3.Not(v.null), whole), z3.Implies(z3.Not(v.null), whole)):
                solver.push()
                solver.add(wish)
                if solver.check() == z3.sat:
                    break
                solver.pop()
        return solver.model() if solver.check() == z3.sat else None

    def term(self, candidate: _Candidate):
        terms = [self.atoms[i].term for i in candidate.atoms]
        return terms[0] if len(terms) == 1 else (smt.z3.And(*terms) if candidate.op == "and" else smt.z3.Or(*terms))

    def add_sample(self, model) -> None:
        z3 = smt.z3
        row = [z3.is_true(model.eval(a.term, model_completion=True)) for a in self.atoms]
        self.samples.append((row, z3.is_true(model.eval(self.region, model_completion=True)), z3.is_true(model.eval(self.seen, model_completion=True))))

    @staticmethod
    def value(candidate: _Candidate, row: list[bool]) -> bool:
        values = [row[i] for i in candidate.atoms]
        return all(values) if candidate.op == "and" else any(values)

    def search(self, candidates: list[_Candidate], rejected: set, strict: bool) -> _Candidate | None:
        """The first candidate P (fewest atoms first) that Z3 shows equal to D (``strict``), or with ``D = P AND S`` (not
        ``strict``), else ``None``. S holds where some query returns the row, so D implies S: a P that is not strict
        covers the difference and more. A P that holds on every row (nothing to explain) is passed over.
        """

        z3 = smt.z3
        alive = [c for c in candidates if c not in rejected]
        while True:
            alive = [c for c in alive if all(((self.value(c, row) and (strict or s)) == d) for row, d, s in self.samples)]
            if not alive:
                return None
            candidate = alive[0]
            p = self.term(candidate)
            gap = z3.And(p, z3.Not(self.region)) if strict else z3.And(p, self.seen, z3.Not(self.region))
            for formula in (z3.And(self.region, z3.Not(p)), gap):
                result, model = self.check(formula)
                if result == z3.sat:
                    self.add_sample(model)
                    break
                if result != z3.unsat:
                    rejected.add(candidate)
                    alive.remove(candidate)
                    break
            else:
                if self.check(z3.Not(p))[0] == z3.sat:
                    return candidate
                rejected.add(candidate)  # true of every row: nothing to explain
                alive.remove(candidate)


# ---- the two checks -----------------------------------------------------------------------------


_DUCK_TYPES = {smt_values.INT64: "BIGINT", smt_values.NUMERIC: "DECIMAL(38,9)", smt_values.FLOAT64: "DOUBLE", smt_values.STRING: "VARCHAR", smt_values.BOOL: "BOOLEAN"}


def _kind_of(values: list) -> str | None:
    kinds = {type(v) for v in values if v is not None}
    if not kinds:
        return ""
    if kinds <= {int, float} and bool not in kinds:
        return "DOUBLE" if float in kinds else "BIGINT"
    return {bool: "BOOLEAN", str: "VARCHAR"}.get(next(iter(kinds))) if len(kinds) == 1 else None


def _bag(rows) -> Counter:
    return Counter(tuple(round(v, 6) if isinstance(v, float) else v for v in row) for row in rows)


def _flat_name(table: str) -> str:
    return table.replace(".", "__")


def _flat(sql: str, dialect: str) -> str:
    """``sql`` with each base table written as one name without dots, so a table ``p.d.t`` is the DuckDB table ``p__d__t``."""

    tree = sqlglot.parse_one(sql, read=dialect)
    for table in list(tree.find_all(exp.Table)):
        name = _flat_name(".".join(p.name for p in table.parts))
        flat = exp.Table(this=exp.to_identifier(name, quoted=True))
        # the name the query reads the table by (its alias, else the table's own last part) stays valid
        flat.set("alias", exp.TableAlias(this=exp.to_identifier(table.alias_or_name)))
        table.replace(flat)
    return tree.sql(dialect=dialect)


def replay(left_sql: str, right_sql: str, tables: Mapping[str, list[dict]], *, dialect: str = "bigquery", types=None) -> bool:
    """Whether the queries return different bags on ``tables`` when run on DuckDB, a difference that holds up.

    It holds up when DuckDB with its optimizer off returns the same rows as with it on
    (:func:`kumosql.duckdb_load.run_unoptimized`), the BigQuery reading of the SQL is faithful
    (:mod:`kumosql.bigquery_on_duckdb`: no construct without a faithful reading, and no failure BigQuery shares), the
    result rows are ones BigQuery could return, and no aggregate pick decides it
    (:func:`kumosql.counterexample.guard_arbitrary_picks`). Anything else is ``False``.
    """

    try:
        import duckdb

        from . import bigquery_on_duckdb
        from .counterexample import guard_arbitrary_picks, to_duckdb
        from .duckdb_load import insert_rows, run_unoptimized, small_database
    except ImportError:
        return False
    if dialect == "bigquery" and (invalid_type_name(left_sql) or invalid_type_name(right_sql)):
        return False
    declared = {k.lower(): {c.lower(): t for c, t in v.items()} for k, v in (types or {}).items()}
    columns: dict[str, list[str]] = {}
    guesses: dict[tuple[str, str], list[str]] = {}
    for table, rows in tables.items():
        names = sorted({n for r in rows for n in r})
        columns[table] = names
        for name in names:
            known = _DUCK_TYPES.get(smt_values.type_class(declared.get(table.lower(), {}).get(name.lower())))
            seen = _kind_of([r.get(name) for r in rows])
            if seen is None:
                return False
            if known == "BIGINT" and seen == "DOUBLE":
                known = "DOUBLE"
            guesses[table, name] = [known or seen] if (known or seen) else ["BIGINT", "VARCHAR"]
    try:
        flat = [_flat(sql, dialect) for sql in (left_sql, right_sql)]
        translated = [to_duckdb(sql, dialect) for sql in flat]
        guarded = [guard_arbitrary_picks(sql) for sql in translated]
    except (sqlglot.errors.SqlglotError, ValueError):
        return False
    if any(g is None for g in guarded):
        return False
    for attempt in range(2):
        db = small_database()
        try:
            if dialect == "bigquery":
                bigquery_on_duckdb.configure(db)
            for table, rows in tables.items():
                kinds = [guesses[table, n][min(attempt, len(guesses[table, n]) - 1)] for n in columns[table]]
                quoted = '"' + _flat_name(table) + '"'
                db.execute(f"CREATE TABLE {quoted} ({', '.join(chr(34) + n + chr(34) + ' ' + k for n, k in zip(columns[table], kinds))})")
                insert_rows(db, quoted, [[r.get(n) for n in columns[table]] for r in rows])
            read = bigquery_on_duckdb.bigquery_rows if dialect == "bigquery" else (lambda rows: rows)
            plain = [_bag(read(db.execute(sql).fetchall())) for sql in translated]
            unoptimized = [_bag(read(rows)) for rows in run_unoptimized(db, *translated)]
            if plain != unoptimized or plain[0] == plain[1]:
                return False
            return all(_bag(read(db.execute(g).fetchall())) == bag for g, bag in zip(guarded, plain))
        except (duckdb.Error, bigquery_on_duckdb.UnfaithfulOutput):
            continue
        finally:
            db.close()
    return False


def _rewrite(sql: str, dialect: str, filters: Mapping[str, tuple[str, list[str]]]) -> str | None:
    """``sql`` with each table in ``filters`` (lower-case key -> predicate, columns) read through
    ``(SELECT columns FROM t WHERE (predicate) IS NOT TRUE)`` under the alias the query gave it."""

    tree = sqlglot.parse_one(sql, read=dialect)
    for table in list(tree.find_all(exp.Table)):
        key = ".".join(p.name for p in table.parts).lower()
        if key not in filters:
            continue
        predicate, columns = filters[key]
        alias = table.alias_or_name
        bare = table.copy()
        bare.set("alias", None)
        inner = exp.select(*[exp.column(exp.to_identifier(c, quoted=False)) for c in columns]).from_(bare).where(
            sqlglot.parse_one(f"({predicate}) IS NOT TRUE", read=dialect)
        )
        table.replace(exp.Subquery(this=inner, alias=exp.TableAlias(this=exp.to_identifier(alias))))
    return tree.sql(dialect=dialect)


def _within(options: dict, deadline: float) -> dict:
    """``options`` with the solver time limit cut to what is left of the budget (the limit applies to each solver check)."""

    left = int((deadline - time.monotonic()) * 1000)
    if left <= 0:
        raise _Out()
    return {**options, "timeout_ms": max(50, min(options.get("timeout_ms", 5000), left // 2))}


def _prove_rewritten(left_sql: str, right_sql: str, filters, options: dict, deadline: float) -> bool:
    from .algebraic_equivalence import prove_equivalent_algebraic

    dialect = options.get("dialect", "bigquery")
    pair = [_rewrite(sql, dialect, filters) for sql in (left_sql, right_sql)]
    if None in pair:
        return False
    for prove in (smt.prove_equivalent_smt, prove_equivalent_algebraic):  # the quicker one first
        if prove(*pair, **_within(options, deadline)).status is not smt.SmtStatus.PROVEN_EQUIVALENT:
            return False
    return True


# ---- the entry points ---------------------------------------------------------------------------


def _short(table: str) -> str:
    return table.rsplit(".", 1)[-1]


def _join(candidate: _Candidate, atoms: list[_Atom], dialect: str, qualify: bool) -> tuple[str, tuple[str, ...]]:
    shown = tuple(atoms[i].shown(dialect, _short(atoms[i].table) if qualify else None) for i in candidate.atoms)
    return (f" {candidate.op.upper()} ".join(shown), shown)


def _tables_of(candidate: _Candidate, atoms: list[_Atom]) -> tuple[str, ...]:
    return tuple(sorted({atoms[i].table for i in candidate.atoms}))


def _filters(candidate: _Candidate, atoms: list[_Atom], dialect: str, columns: Mapping[str, list[str]]):
    """Per table, the predicate its rows must not satisfy and the columns the rewritten table keeps."""

    joined: dict[str, list[str]] = {}
    for i in candidate.atoms:
        joined.setdefault(atoms[i].table, []).append(atoms[i].local(dialect))
    wrapped = {t: ([parts[0]] if len(parts) == 1 else [f"({p})" for p in parts]) for t, parts in joined.items()}
    return {t: (f" {candidate.op.upper()} ".join(parts), columns[t]) for t, parts in wrapped.items()}


def _witness(problem: _Problem, candidate: _Candidate, occs: list, schema, constraints) -> dict | None:
    """One row per table that satisfies the predicate and on which the queries differ, or ``None``."""

    model = problem.readable_model(problem.region, problem.term(candidate))
    if model is None:
        return None
    tables: dict[str, list[dict]] = {}
    for occ in occs:
        row = {name: smt._export(smt._cell(model, v)) for name, v in occ.cols.items()}
        listed = {k.lower(): v for k, v in (schema or {}).items()}.get(occ.table.lower())
        required = (constraints or {}).get(occ.table.lower()) if constraints else None
        for name in listed or ():
            if name.lower() not in row:
                row[name.lower()] = 0 if required is not None and name.lower() in required.not_null else None
        tables[occ.table] = [row]
    legal = problem.prover.legal_database(tables)
    return legal or None


def _verify(
    problem: _Problem, candidate: _Candidate, atoms: list[_Atom], occs: list, left: str, right: str, options: dict, columns, qualify: bool, exact: bool
) -> tuple[DifferenceExplanation | None, str]:
    """``(explanation, "")`` when the candidate passes both checks, else ``(None, the check it failed)``."""

    dialect = options.get("dialect", "bigquery")
    witness = _witness(problem, candidate, occs, options.get("schema"), problem.prover.constraints)
    if witness is None:
        return None, "witness"
    sql, shown = _join(candidate, atoms, dialect, qualify)
    tables = _tables_of(candidate, atoms)
    explanation = DifferenceExplanation(sql, shown, tables, exact, {k: [dict(r) for r in v] for k, v in witness.items()})
    spelled = {t.lower(): t for t in witness}
    held = conditions_of(explanation, dialect=dialect, _candidate=(candidate, atoms), _spelled=spelled)
    if not any(broken_by(c, witness) for c in held):
        return None, "witness"
    if not replay(left, right, witness, dialect=dialect, types=options.get("types")):
        return None, "witness"
    if not _prove_rewritten(left, right, _filters(candidate, atoms, dialect, columns), options, problem.deadline):
        return None, "proof"
    return explanation, ""


class _Unknown(Exception):
    """Nothing can be said, and why (the reason is for tools that count the unknowns)."""


@serialized
def explain_difference(left_sql: str, right_sql: str, **prover_options) -> DifferenceExplanation | None:
    """Return a verified difference predicate for two non-equivalent queries, or None when none is verified."""

    try:
        return explain_or_why(left_sql, right_sql, **prover_options)[0]
    except (smt.Unsupported, UnmodeledConstruct, sqlglot.errors.SqlglotError, RecursionError, smt.z3.Z3Exception):
        return None


@serialized
def explain_or_why(left_sql: str, right_sql: str, **prover_options) -> tuple[DifferenceExplanation | None, str]:
    """``(explanation, "")``, or ``(None, why there is none)``; the options are those of :func:`explain_difference`."""

    seconds = prover_options.pop("explain_seconds", DEFAULT_SECONDS)
    options = {k: v for k, v in prover_options.items() if k in _OPTIONS}
    smt._check_options(options)
    if smt.z3 is None:
        return None, "z3-solver is not installed"
    try:
        return _explain(left_sql, right_sql, options, time.monotonic() + seconds), ""
    except _Unknown as unknown:
        return None, str(unknown)
    except _Out:
        return None, "out of time"
    except (smt.Unsupported, UnmodeledConstruct) as error:
        return None, f"unsupported: {error}"


def _one_row(block):
    """``(row passes, outputs)`` of a block on a database where each table holds one row.

    A select-project-join block is its own condition and outputs. In a grouped block the one row is its own group, so
    each aggregate is that row's value (COUNT is 1 or 0, SUM, MIN, MAX and LOGICAL_AND/OR are the argument) and the
    group passes when the filter and HAVING hold; a global aggregate always returns a row, of the empty-group values
    when the filter fails.
    """

    z3 = smt.z3
    V = smt._value_sort()
    if isinstance(block, smt._Spj):
        return block.cond.t, list(block.outputs)

    def value(call, empty: bool):
        if empty:
            return smt._aggregate_of_nothing(call)
        if call.func == "COUNT":
            return smt._Val(z3.BoolVal(False), V.Num(1) if call.arg is None else z3.If(call.arg.null, V.Num(0), V.Num(1)))
        if call.func == "COUNTIF":
            return smt._Val(z3.BoolVal(False), z3.If(z3.And(z3.Not(call.arg.null), V.bool(call.arg.val)), V.Num(1), V.Num(0)))
        if call.func in ("SUM", "MIN", "MAX", "LOGICAL_AND", "LOGICAL_OR"):
            return call.arg
        raise _Unknown(f"the aggregate {call.func} is not read on one row")

    def read(empty: bool):
        pairs = []
        for call in block.aggs:
            got = value(call, empty)
            if not z3.is_false(call.var.null):
                pairs.append((call.var.null, got.null))
            pairs.append((call.var.val, got.val))
        having = smt._subst(block.having.t, pairs) if block.having is not None else z3.BoolVal(True)
        return having, [smt._subst_val(v, pairs) for v in block.outputs]

    having, outputs = read(False)
    if not block.is_global:
        return z3.And(block.cond.t, having), outputs
    none_having, none_outputs = read(True)
    held = block.cond.t
    return (
        z3.If(held, having, none_having),
        [smt._Val(z3.If(held, v.null, w.null), z3.If(held, v.val, w.val)) for v, w in zip(outputs, none_outputs)],
    )


def _explain(left_sql: str, right_sql: str, options: dict, deadline: float) -> DifferenceExplanation:
    dialect = options.get("dialect", "bigquery")
    schema, types, constraints = options.get("schema"), options.get("types"), options.get("constraints")
    exact_arithmetic = options.get("exact_arithmetic", False)
    timeout_ms = options.get("timeout_ms", 5000)
    if dialect == "bigquery" and (invalid_type_name(left_sql) or invalid_type_name(right_sql)):
        raise _Unknown("BigQuery rejects a type name")
    left, right = (string_number_literals.normalize(sql, dialect, types) for sql in (left_sql, right_sql))
    if dialect == "bigquery":
        left, right = canonical_literals(left), canonical_literals(right)
    left, right, problem = positional_sql_pair(left, right, dialect)
    if problem or string_number_compare.problem(left, dialect, types, plain_ok=True) or string_number_compare.problem(right, dialect, types, plain_ok=True):
        raise _Unknown("strings compared with numbers, or a BY NAME set operation")
    trees = [_single_select(sql, dialect) for sql in (left, right)]
    if None in trees:
        raise _Unknown("not a single select")

    compiler = smt._Compiler(schema, exact_arithmetic, dialect, types)
    compiled = [compiler.compile(sql) for sql in (left, right)]
    if compiler.uses_uf or compiler.limit_opaque or compiler.window_opaque or compiler.big_literals or compiler.timestamp_literals:
        raise _Unknown("a construct whose values are free in the model")
    blocks = []
    for union in compiled:
        if union.distinct or len(union.branches) != 1:
            raise _Unknown("a set operation")
        block = union.branches[0]
        if block.distinct or block.subs or any(o.opaque or o.table == smt._UNNEST_TABLE for o in block.occs):
            raise _Unknown("DISTINCT, a subquery or a derived table")
        blocks.append(block)
    a, b = blocks
    if len(a.outputs) != len(b.outputs) or (options.get("compare_names", True) and list(compiled[0].names) != list(compiled[1].names)):
        raise _Unknown("the output columns differ")
    tables = Counter(o.table.lower() for o in a.occs)
    if tables != Counter(o.table.lower() for o in b.occs):
        raise _Unknown("the queries read different tables")
    if max(tables.values(), default=1) > 1:
        raise _Unknown("a table is read twice")
    rows_only = isinstance(a, smt._Spj) and isinstance(b, smt._Spj)  # a grouped pair is read on one row per table only

    order = compiler.order_facts()
    for block in blocks:
        block.facts = block.facts + order + compiler.typed_facts(smt._block_occs(block))
    prover = smt._Prover(timeout_ms, constraints)
    mapping = next(prover._bijections(a.occs, b.occs))
    pairs = prover._pairs(mapping)
    facts = a.facts + [smt._subst(f, pairs) for f in b.facts]
    z3 = smt.z3
    cond_a, outs_a = _one_row(a)
    cond_b, outs_b = _one_row(b)
    cond_b, outs_b = smt._subst(cond_b, pairs), [smt._subst_val(v, pairs) for v in outs_b]
    region = z3.Or(z3.Xor(cond_a, cond_b), z3.And(cond_a, cond_b, z3.Not(smt._rows_eq(outs_a, outs_b))))

    usable, facts = _usable_atoms(compiler, trees, a, schema, dialect, facts)
    if not usable:
        raise _Unknown("no atom to build a predicate from")
    problem_ = _Problem(prover, a.occs, facts, region, z3.Or(cond_a, cond_b), usable, deadline)
    result, model = problem_.check(region)
    if result != z3.sat:
        raise _Unknown("the queries agree on every one-row database" if result == z3.unsat else "the solver gave no answer")
    problem_.add_sample(model)
    result, model = problem_.check(z3.Not(region))
    if result == z3.sat:
        problem_.add_sample(model)

    columns = {}
    for occ in a.occs:
        listed = {k.lower(): v for k, v in (schema or {}).items()}.get(occ.table.lower())
        columns[occ.table.lower()] = [c for c in (listed or sorted(occ.cols))]
    candidates = _candidates(usable)
    rejected: set = set()
    why = "no predicate of at most three atoms describes the difference"
    verified = 0
    for strict in (True, False):
        while verified < MAX_VERIFICATIONS:
            candidate = problem_.search(candidates, rejected, strict)
            if candidate is None:
                break
            tables = _tables_of(candidate, usable)
            qualify = len(tables) > 1
            if qualify and len({_short(t) for t in tables}) < len(tables):
                rejected.add(candidate)  # two tables of one short name: the columns could not be told apart
                continue
            verified += 1
            found, failed = _verify(problem_, candidate, usable, a.occs, left_sql, right_sql, options, columns, qualify, strict and rows_only)
            if found is not None:
                return found
            why = f"a predicate failed its {failed}"
            rejected.add(candidate)
    raise _Unknown(why)


def _usable_atoms(compiler, trees, a, schema, dialect: str, facts: list):
    """The atoms that compile, each with its Z3 term over the first query's table occurrences, and the facts they add."""

    z3 = smt.z3
    by_table = {o.table.lower(): o for o in a.occs}
    usable = []
    for atom in _written_atoms(list(trees), schema, dialect):
        occ = by_table.get(atom.table)
        table_sql = next((t.copy() for tree in trees for t in tree.find_all(exp.Table) if ".".join(p.name for p in t.parts).lower() == atom.table), None)
        if occ is None or table_sql is None:
            continue
        table_sql.set("alias", None)
        probe = f"SELECT 1 AS probe FROM {table_sql.sql(dialect=dialect)} WHERE {_sql(atom.node, dialect)}"
        try:
            union = compiler.compile(probe)
        except (smt.Unsupported, UnmodeledConstruct, sqlglot.errors.SqlglotError):
            continue
        if len(union.branches) != 1 or not isinstance(union.branches[0], smt._Spj):
            continue
        block = union.branches[0]
        if block.subs or len(block.occs) != 1 or block.occs[0].opaque:
            continue
        move = smt._occ_pairs(block.occs[0], occ)
        atom.term = smt._subst(block.cond.t, move)
        if atom.negated:
            atom.term = z3.Not(atom.term)
        facts = facts + [smt._subst(f, move) for f in block.facts]
        usable.append(atom)
    return usable, facts


def conditions_of(explanation: DifferenceExplanation, *, dialect: str = "bigquery", _candidate=None, _spelled=None) -> list[Condition]:
    """The ``no_rows`` conditions an explanation stands for: one per table, "no row of t has P_t".

    A predicate over one table is that table's condition; a predicate over several is a disjunction of atoms
    that each name their table, and each table gets the disjunction of its own atoms. Table names are spelled as
    the explanation's witness spells them (the queries' own spelling).
    """

    spelled = _spelled or {k.lower(): k for k in explanation.witness}
    if _candidate is not None:
        candidate, atoms = _candidate
        per_table: dict[str, tuple[str, list[str]]] = {}
        parts = _filters(candidate, atoms, dialect, {atoms[i].table: [] for i in candidate.atoms})
        for table, (predicate, _) in parts.items():
            per_table[table] = (predicate, sorted({c for i in candidate.atoms if atoms[i].table == table for c in atoms[i].columns}))
    elif len(explanation.tables) == 1:
        tree = sqlglot.parse_one(f"SELECT 1 FROM t WHERE {explanation.sql}", read=dialect)
        per_table = {explanation.tables[0]: (explanation.sql, sorted({c.name.lower() for c in tree.find_all(exp.Column)}))}
    else:
        grouped: dict[str, list[str]] = {}
        shorts = {_short(t): t for t in explanation.tables}
        for atom in explanation.atoms:
            node = sqlglot.parse_one(atom, read=dialect)
            owners = {c.table for c in node.find_all(exp.Column)}
            if len(owners) != 1 or next(iter(owners)) not in shorts:
                return []
            for column in node.find_all(exp.Column):
                column.set("table", None)
            grouped.setdefault(shorts[next(iter(owners))], []).append(node.sql(dialect=dialect))
        per_table = {
            t: (" OR ".join(parts), sorted({c.name.lower() for p in parts for c in sqlglot.parse_one(p, read=dialect).find_all(exp.Column)}))
            for t, parts in grouped.items()
        }
    return [no_rows(spelled.get(table, table).split("."), predicate, columns, dialect) for table, (predicate, columns) in sorted(per_table.items())]
