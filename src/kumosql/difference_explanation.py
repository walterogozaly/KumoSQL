"""Explain a query difference as a verified predicate: "equivalent except when P" (issue #512).

When two queries are not equivalent, :func:`explain_difference` looks for a short SQL predicate P over the
tables the queries read such that the queries return the same rows on every database in which no row
satisfies P ("equivalent except when ``status IS NULL``"). A reviewer can then accept the change, or add a
NOT NULL assertion, without reading a counterexample database. In the verdict model this is the conditional
verdict (:mod:`kumosql.conditional_equivalence`) under one more condition kind, ``no_rows``; :func:`conditions_of`
turns an explanation into those conditions.

How a predicate is found (row-local select-project-join pairs only; everything else is unknown):

1. Both queries are compiled by the SMT prover's compiler into one block each, over the same tables, each table
   read once, with no subquery, DISTINCT, aggregate or set operation. On a database with one row per table the
   outputs differ exactly when ``D = XOR(cond_a, cond_b) OR (cond_a AND cond_b AND outputs differ)`` holds.
2. Candidate atoms come from the queries themselves (every comparison or predicate they write, ``col IS NULL`` and
   ``col IS NOT NULL`` for every column they read, and the equality behind each inequality such as ``x > 5``).
   Each atom mentions one table. A search over conjunctions and disjunctions of at most three atoms finds one that
   Z3 shows equal to D (``exact``), else the tightest one that D implies (not ``exact``: it covers the difference
   and more). A predicate across tables is a disjunction of single-table atoms, because the proof below filters
   each table on its own.
3. Before a predicate is returned it must pass both checks:

   * witness: a one-row-per-table database that satisfies P and on which the outputs differ is replayed on
     DuckDB, and the difference counts only if DuckDB with its optimizer off agrees, the BigQuery reading of the
     queries is faithful (:mod:`kumosql.bigquery_on_duckdb`) and no arbitrary pick decides it
     (:func:`kumosql.counterexample.guard_arbitrary_picks`);
   * proof: every table t is replaced by ``(SELECT cols FROM t WHERE (P_t) IS NOT TRUE)`` under its own alias in
     both queries, and the rewritten pair is proved equivalent by both
     :func:`kumosql.algebraic_equivalence.prove_equivalent_algebraic` and
     :func:`kumosql.smt_equivalence.prove_equivalent_smt`.

Unknown beats wrong: any failure, unsupported construct or exhausted time budget returns ``None``.
The function is opt-in; nothing else calls it.
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

        for node in tree.find_all(*_PREDICATES):
            if node.find(exp.Subquery, exp.Select, exp.Window, exp.AggFunc):
                continue
            occ = owner_of(node)
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

    def check(self, *formulas):
        """``(result, model or None)`` of the formulas under the declared facts."""

        if time.monotonic() > self.deadline or self.rounds >= MAX_ROUNDS:
            raise _Out()
        self.rounds += 1
        smt.z3  # noqa: B018 - z3 is present (checked by the caller)
        solver = bounded_solver(self.prover.timeout_ms)
        solver.add(*self.prover._typing(self.occs))
        solver.add(*self.prover._constraint_facts(self.occs))
        solver.add(*self.prover._group_member_facts(self.occs))
        solver.add(*self.facts)
        solver.add(*formulas)
        result = solver.check()
        if result != smt.z3.sat:
            return result, None
        return result, self.prover._counterexample(solver.assertions(), solver.model(), self.values)

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

    def search(self, candidates: list[_Candidate], rejected: set) -> _Candidate | None:
        """The first candidate P (fewest atoms first) with ``D = P AND S`` shown by Z3, or ``None`` when none is left.

        D is where the outputs differ and S where some query returns a row, so D implies S. A P that also holds
        on every row (nothing to explain) is passed over.
        """

        z3 = smt.z3
        alive = [c for c in candidates if c not in rejected]
        while True:
            alive = [c for c in alive if all((self.value(c, row) and s) == d for row, d, s in self.samples)]
            if not alive:
                return None
            candidate = alive[0]
            p = self.term(candidate)
            for formula in (z3.And(self.region, z3.Not(p)), z3.And(p, self.seen, z3.Not(self.region))):
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
        if table.args.get("alias") is not None:
            flat.set("alias", table.args["alias"].copy())
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


def _prove_rewritten(left_sql: str, right_sql: str, filters, options: dict) -> bool:
    from .algebraic_equivalence import prove_equivalent_algebraic

    dialect = options.get("dialect", "bigquery")
    pair = [_rewrite(sql, dialect, filters) for sql in (left_sql, right_sql)]
    if None in pair:
        return False
    for prove in (prove_equivalent_algebraic, smt.prove_equivalent_smt):
        if prove(*pair, **options).status is not smt.SmtStatus.PROVEN_EQUIVALENT:
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

    z3 = smt.z3
    result, model = problem.check(problem.region, problem.term(candidate))
    if result != z3.sat:
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
    problem: _Problem, candidate: _Candidate, atoms: list[_Atom], occs: list, left: str, right: str, options: dict, columns, qualify: bool
) -> DifferenceExplanation | None:
    dialect = options.get("dialect", "bigquery")
    witness = _witness(problem, candidate, occs, options.get("schema"), problem.prover.constraints)
    if witness is None:
        return None
    sql, shown = _join(candidate, atoms, dialect, qualify)
    tables = _tables_of(candidate, atoms)
    explanation = DifferenceExplanation(sql, shown, tables, True, {k: [dict(r) for r in v] for k, v in witness.items()})
    spelled = {t.lower(): t for t in witness}
    held = conditions_of(explanation, dialect=dialect, _candidate=(candidate, atoms), _spelled=spelled)
    if not any(broken_by(c, witness) for c in held):
        return None
    if not replay(left, right, witness, dialect=dialect, types=options.get("types")):
        return None
    if not _prove_rewritten(left, right, _filters(candidate, atoms, dialect, columns), options):
        return None
    return explanation


@serialized
def explain_difference(left_sql: str, right_sql: str, **prover_options) -> DifferenceExplanation | None:
    """Return a verified difference predicate for two non-equivalent queries, or None when none is verified."""

    seconds = prover_options.pop("explain_seconds", DEFAULT_SECONDS)
    options = {k: v for k, v in prover_options.items() if k in _OPTIONS}
    smt._check_options(options)
    if smt.z3 is None:
        return None
    try:
        return _explain(left_sql, right_sql, options, time.monotonic() + seconds)
    except (smt.Unsupported, UnmodeledConstruct, sqlglot.errors.SqlglotError, RecursionError, _Out, smt.z3.Z3Exception, KeyError):
        return None


def _explain(left_sql: str, right_sql: str, options: dict, deadline: float) -> DifferenceExplanation | None:
    from .algebraic_equivalence import prove_equivalent_algebraic

    dialect = options.get("dialect", "bigquery")
    schema, types, constraints = options.get("schema"), options.get("types"), options.get("constraints")
    exact_arithmetic = options.get("exact_arithmetic", False)
    timeout_ms = options.get("timeout_ms", 5000)
    if dialect == "bigquery" and (invalid_type_name(left_sql) or invalid_type_name(right_sql)):
        return None
    left, right = (string_number_literals.normalize(sql, dialect, types) for sql in (left_sql, right_sql))
    if dialect == "bigquery":
        left, right = canonical_literals(left), canonical_literals(right)
    left, right, problem = positional_sql_pair(left, right, dialect)
    if problem or string_number_compare.problem(left, dialect, types, plain_ok=True) or string_number_compare.problem(right, dialect, types, plain_ok=True):
        return None
    trees = [_single_select(sql, dialect) for sql in (left, right)]
    if None in trees or any(t.args.get("distinct") or t.args.get("group") or t.args.get("having") for t in trees):
        return None

    compiler = smt._Compiler(schema, exact_arithmetic, dialect, types)
    compiled = [compiler.compile(sql) for sql in (left, right)]
    if compiler.uses_uf or compiler.limit_opaque or compiler.window_opaque or compiler.big_literals or compiler.timestamp_literals:
        return None
    blocks = []
    for union in compiled:
        if union.distinct or len(union.branches) != 1:
            return None
        block = union.branches[0]
        if not isinstance(block, smt._Spj) or block.distinct or block.subs or any(o.opaque or o.table == smt._UNNEST_TABLE for o in block.occs):
            return None
        blocks.append(block)
    a, b = blocks
    if len(a.outputs) != len(b.outputs) or (options.get("compare_names", True) and list(compiled[0].names) != list(compiled[1].names)):
        return None
    tables = Counter(o.table.lower() for o in a.occs)
    if tables != Counter(o.table.lower() for o in b.occs) or max(tables.values()) > 1:
        return None  # another table multiset, or a table read twice: not row-local

    order = compiler.order_facts()
    for block in blocks:
        block.facts = block.facts + order + compiler.typed_facts(smt._block_occs(block))
    prover = smt._Prover(timeout_ms, constraints)
    mapping = next(prover._bijections(a.occs, b.occs))
    pairs = prover._pairs(mapping)
    facts = a.facts + [smt._subst(f, pairs) for f in b.facts]
    z3 = smt.z3
    cond_a, cond_b = a.cond.t, smt._subst(b.cond.t, pairs)
    outs_b = [smt._subst_val(v, pairs) for v in b.outputs]
    region = z3.Or(z3.Xor(cond_a, cond_b), z3.And(cond_a, cond_b, z3.Not(smt._rows_eq(a.outputs, outs_b))))

    # equivalent pairs have no region, and the provers decide those first
    if prove_equivalent_algebraic(left_sql, right_sql, **options).status is smt.SmtStatus.PROVEN_EQUIVALENT:
        return None

    atoms = _written_atoms([t for t in trees], schema, dialect)
    by_table = {o.table.lower(): o for o in a.occs}
    usable = []
    for atom in atoms:
        occ = by_table.get(atom.table)
        if occ is None:
            continue
        table_sql = next((t.copy() for t in trees[0].find_all(exp.Table) if ".".join(p.name for p in t.parts).lower() == atom.table), None)
        if table_sql is None:
            table_sql = next(t.copy() for t in trees[1].find_all(exp.Table) if ".".join(p.name for p in t.parts).lower() == atom.table)
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
    if not usable:
        return None

    problem_ = _Problem(prover, a.occs, facts, region, z3.Or(cond_a, cond_b), usable, deadline)
    result, model = problem_.check(region)
    if result != z3.sat:
        return None  # unsat: the pair agrees on every one-row database; unknown: nothing to say
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
    for _ in range(MAX_VERIFICATIONS):
        candidate = problem_.search(candidates, rejected)
        if candidate is None:
            return None
        tables = _tables_of(candidate, usable)
        qualify = len(tables) > 1
        if qualify and len({_short(t) for t in tables}) < len(tables):
            rejected.add(candidate)  # two tables of one short name: the columns could not be told apart
            continue
        found = _verify(problem_, candidate, usable, a.occs, left_sql, right_sql, options, columns, qualify)
        if found is not None:
            return found
        rejected.add(candidate)
    return None


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
