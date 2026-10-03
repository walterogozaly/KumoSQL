"""Bounded model finding with row multiplicities: databases of a few distinct rows, each repeated.

``bounded_equivalence`` searches the databases of at most a handful of rows per table, so a
difference that needs a group of 1,001 rows (``HAVING COUNT(*) > 1000``) is out of its reach. Here
each of a few symbolic rows also carries a symbolic copy count: the table holds that many identical
copies of the row. Both queries are compiled to symbolic bags over those rows, where

* a joined row's copies are the product of its parts' copies, and a filter or a semi-join keeps them;
* COUNT, SUM and AVG weigh every member of a group by its copies (MIN, MAX and the DISTINCT forms
  do not need to);
* DISTINCT, GROUP BY and UNION keep one copy, INTERSECT ALL and EXCEPT ALL take the smaller count
  and the difference of the counts;
* LIMIT keeps the part of a run of copies that falls inside the window,

and z3 looks for a database on which some output row comes out a different number of times. The
model is expanded into its copies and handed to the judge (:class:`kumosql.refutation_replay.Judge`),
which runs both queries on DuckDB: only a database the judge confirms is returned, so a gap in this
encoding costs a refutation, never a wrong one.

Tables with a declared key hold each row once (copies of a row would repeat its key). Rows repeat in
one table at a time first, where every count stays linear, then in all tables together. Window
functions and uninterpreted functions are not modeled.
"""

from __future__ import annotations

import time

from sqlglot import exp

from . import bounded_equivalence as be
from .bounded_equivalence import Compiler, Rel, Row, Scope, SymbolicDatabase, Unsupported, z3
from .set_operations import positional_sql_pair

MAX_COPIES = 50_000  # copies of one row: the expanded database is loaded into DuckDB
ATTEMPTS = 3  # models tried per encoding before moving on


def _one():
    return z3.IntVal(1)


def _copies(row: Row):
    copies = getattr(row, "copies", None)
    return _one() if copies is None else copies


def _row(present, vals, copies, keys=None) -> Row:
    row = Row(present, vals, keys)
    row.copies = copies
    return row


def _times(a, b):
    if z3.is_int_value(a) and a.as_long() == 1:
        return b
    if z3.is_int_value(b) and b.as_long() == 1:
        return a
    return a * b


def _smaller(a, b):
    return z3.If(a < b, a, b)


def _larger(a, b):
    return z3.If(a > b, a, b)


def _total(terms):
    return z3.Sum(*terms) if terms else z3.IntVal(0)


class CountingDatabase(SymbolicDatabase):
    """A :class:`SymbolicDatabase` whose rows in ``repeat`` tables come in ``copies`` copies (1 to ``cap``)."""

    def __init__(self, schema, rows, *, repeat=frozenset(), cap: int = 1, restrict=()):
        super().__init__(schema, rows, restrict=restrict)
        for name, slots in self.tables.items():
            repeatable = name in repeat and not schema.tables[name].keys
            for index, slot in enumerate(slots):
                if repeatable:
                    copies = z3.Int(f"{name}#{index}#copies")
                    self.constraints += [copies >= 1, copies <= cap]
                    slot.copies = copies
                else:
                    slot.copies = _one()


class CountingCompiler(Compiler):
    """The bounded compiler, with every row carrying its number of copies (see the module docstring)."""

    # -- rows that keep or multiply their copies ------------------------------------------------

    def _scope(self, rel: Rel, row: Row, outer) -> Scope:
        scope = super()._scope(rel, row, outer)
        scope.copies = _copies(row)  # read by the aggregates of a group built from these scopes
        return scope

    def table_factor(self, node, outer: Scope | None) -> Rel:
        if isinstance(node, exp.Table) and not (not node.db and not node.catalog and node.name.lower() in self._ctes[-1]):
            if isinstance(node.this, exp.Func):
                raise Unsupported("table function")
            alias = node.alias.lower() if node.alias else node.name.lower()
            table, slots = self.db.table_for(node)
            cols = [(alias, c.name.lower()) for c in table.columns]
            kinds = [be.kind_of(c.type) or "unsupported" for c in table.columns]
            return Rel(cols, kinds, [_row(r.present, list(r.vals), _copies(r)) for r in slots])
        return super().table_factor(node, outer)

    def _renamed(self, inner: Rel, alias, columns, node) -> Rel:
        if node.args.get("pivots") or node.args.get("sample"):
            raise Unsupported("pivot or sample")
        names = columns or [n for _, n in inner.cols]
        return Rel([(alias, n) for n in names], list(inner.kinds), [_row(r.present, r.vals, _copies(r)) for r in inner.rows])

    def _lateral(self, left: Rel, join: exp.Join, outer):
        raise Unsupported("LATERAL")

    def _e_Window(self, node, scope):
        raise Unsupported("window function")

    def _e_Anonymous(self, node, scope):
        raise Unsupported(f"function {node.this}")

    def select(self, node: exp.Select, outer: Scope | None) -> Rel:
        if self._windowed(node):
            raise Unsupported("window function")
        if node.args.get("qualify") or node.args.get("windows"):
            raise Unsupported("window clause")
        distinct = node.args.get("distinct")
        if isinstance(distinct, exp.Distinct) and distinct.args.get("on"):
            raise Unsupported("DISTINCT ON")
        source = self.from_clause(node, outer)
        where = node.args.get("where")
        rows = source.rows
        if where is not None:
            rows = [
                _row(z3.And(row.present, be.truth(self.expr(where.this, self._scope(source, row, outer)))), row.vals, _copies(row))
                for row in rows
            ]
        source = Rel(source.cols, source.kinds, rows, source.hidden, source.first)
        items = list(node.expressions)
        if node.args.get("group") is not None or self._aggregated(node):
            out = self.grouped(node, source, items, outer)
        else:
            out = self.projected(node, source, items, outer)
        if distinct is not None:
            out = self.dedupe(out)
        return self.limited(node, out)

    def projected(self, node, source: Rel, items, outer: Scope | None) -> Rel:
        names = self._output_names(items, source)
        order = node.args.get("order")
        out_rows = []
        for row in source.rows:
            scope = self._scope(source, row, outer)
            scope.aliases = {
                item.alias.lower(): (lambda e=item.this, s=scope: self.expr(e, s)) for item in items if isinstance(item, exp.Alias)
            }
            cells = self._expand(items, source, row, scope)
            keys = self._sort_keys(order, items, cells, scope, outer) if order is not None else None
            out_rows.append(_row(row.present, cells, _copies(row), keys))
        return self._finish(names, out_rows)

    def _finish(self, names, rows: list[Row]) -> Rel:
        rel = super()._finish(names, rows)
        return Rel(rel.cols, rel.kinds, [_row(f.present, f.vals, _copies(r), f.keys) for f, r in zip(rel.rows, rows)])

    def join(self, left: Rel, join: exp.Join, outer: Scope | None) -> Rel:
        if isinstance(join.this, exp.Lateral):
            raise Unsupported("LATERAL")
        if join.args.get("using") or join.args.get("method") or (join.kind or "").upper() == "NATURAL":
            raise Unsupported("USING or NATURAL join")
        right = self.table_factor(join.this, outer)
        side = (join.side or "").upper()
        kind = (join.kind or "").upper()
        if kind in ("ANTI", "SEMI"):
            return self._semi_join(left, right, join, kind, outer)
        on = join.args.get("on")
        cols = left.cols + right.cols
        kinds = left.kinds + right.kinds
        if len(left.rows) * len(right.rows) > be.MAX_ROWS:
            raise Unsupported("join too large for the bound")
        conditions = []
        for a in left.rows:
            line = []
            for b in right.rows:
                if on is None:
                    line.append(be._true())
                    continue
                scope = Scope([(q, n, v) for (q, n), v in zip(cols, a.vals + b.vals)], outer,
                              hidden=[id(v) for i, v in enumerate(a.vals + b.vals) if i in left.hidden or i - len(left.cols) in right.hidden])
                line.append(be.truth(self.expr(on, scope)))
            conditions.append(line)
        rows = []
        for i, a in enumerate(left.rows):
            for j, b in enumerate(right.rows):
                rows.append(_row(z3.And(a.present, b.present, conditions[i][j]), a.vals + b.vals, _times(_copies(a), _copies(b))))
        if side in ("LEFT", "FULL"):
            blanks = [be.null_value(k) for k in right.kinds]
            for i, a in enumerate(left.rows):
                matched = z3.Or(*[z3.And(b.present, conditions[i][j]) for j, b in enumerate(right.rows)])
                rows.append(_row(z3.And(a.present, z3.Not(matched)), a.vals + blanks, _copies(a)))
        if side in ("RIGHT", "FULL"):
            blanks = [be.null_value(k) for k in left.kinds]
            for j, b in enumerate(right.rows):
                matched = z3.Or(*[z3.And(a.present, conditions[i][j]) for i, a in enumerate(left.rows)])
                rows.append(_row(z3.And(b.present, z3.Not(matched)), blanks + b.vals, _copies(b)))
        hidden = frozenset(set(left.hidden) | {len(left.cols) + i for i in right.hidden})
        first = tuple(list(left.first) + [len(left.cols) + i for i in right.first])
        return Rel(cols, kinds, rows, hidden, first)

    def _semi_join(self, left: Rel, right: Rel, join: exp.Join, kind: str, outer) -> Rel:
        rel = super()._semi_join(left, right, join, kind, outer)
        return Rel(rel.cols, rel.kinds, [_row(r.present, r.vals, _copies(a)) for r, a in zip(rel.rows, left.rows)], rel.hidden, rel.first)

    # -- set operations ----------------------------------------------------------------------------

    def set_operation(self, node, outer: Scope | None) -> Rel:
        modifiers = [k for k, v in node.args.items() if v and k not in ("this", "expression", "distinct", "with_", "with", "order")]
        if modifiers:
            raise Unsupported(f"set operation with {', '.join(sorted(modifiers))}")
        left = self.query(node.this, outer)
        right = self.query(node.expression, outer)
        if len(left.cols) != len(right.cols):
            raise Unsupported("set operation column counts differ")
        kinds = []
        for a, b in zip(left.kinds, right.kinds):
            kinds.append(be.unify(be.null_value(a) if a != "null" else be.null_value(), be.null_value(b) if b != "null" else be.null_value())[0].kind)
        left_rows = [_row(r.present, [be.to_kind(v, k) for v, k in zip(r.vals, kinds)], _copies(r)) for r in left.rows]
        right_rows = [_row(r.present, [be.to_kind(v, k) for v, k in zip(r.vals, kinds)], _copies(r)) for r in right.rows]
        distinct = bool(node.args.get("distinct", True))
        cols = [(None, n) for _, n in left.cols]
        if isinstance(node, exp.Union):
            rel = Rel(cols, kinds, left_rows + right_rows)
            return self.dedupe(rel) if distinct else rel

        def equal(row, other):
            return z3.And(other.present, *[be.same(a, b) for a, b in zip(row.vals, other.vals)])

        out = []
        if distinct:
            for row in self.dedupe(Rel(cols, kinds, left_rows)).rows:
                found = z3.Or(*[equal(row, o) for o in right_rows]) if right_rows else be._false()
                keep = found if isinstance(node, exp.Intersect) else z3.Not(found)
                out.append(_row(z3.And(row.present, keep), row.vals, _one()))
            return Rel(cols, kinds, out)
        for i, row in enumerate(left_rows):
            counts = [z3.If(equal(row, o), _copies(o), 0) for o in left_rows]
            earlier = _total(counts[:i])
            on_left = _total(counts)
            on_right = _total([z3.If(equal(row, o), _copies(o), 0) for o in right_rows])
            target = _smaller(on_left, on_right) if isinstance(node, exp.Intersect) else _larger(on_left - on_right, z3.IntVal(0))
            mine = _larger(_smaller(_copies(row), target - earlier), z3.IntVal(0))
            out.append(_row(z3.And(row.present, mine > 0), row.vals, mine))
        return Rel(cols, kinds, out)

    # -- aggregates --------------------------------------------------------------------------------

    def _aggregate_over(self, node, group) -> be.V:
        weights = [getattr(s, "copies", None) for s in group.scopes]
        argument = node.this
        if (
            all(w is None or (z3.is_int_value(w) and w.as_long() == 1) for w in weights)
            or isinstance(argument, exp.Distinct)
            or not isinstance(node, (exp.Count, exp.Sum, exp.Avg))
        ):
            return super()._aggregate_over(node, group)  # MIN, MAX and DISTINCT ignore copies
        weights = [_one() if w is None else w for w in weights]
        if isinstance(node, exp.Count) and isinstance(argument, exp.Star):
            return be.V("int", _total([z3.If(m, w, 0) for m, w in zip(group.members, weights)]), be._false())
        key = id(argument)
        if key not in group.cache:
            group.cache[key] = [self.expr(argument, s) for s in group.scopes]
        values = group.cache[key]
        present = [z3.And(m, z3.Not(v.null)) for m, v in zip(group.members, values)]
        counted = _total([z3.If(p, w, 0) for p, w in zip(present, weights)])
        if isinstance(node, exp.Count):
            return be.V("int", counted, be._false())
        values = [be.to_kind(v, "int") if v.kind == "bool" else v for v in values]
        kinds = {v.kind for v in values} - {"null"}
        if not kinds:
            return be.null_value()
        if not kinds <= {"int", "real"}:
            raise Unsupported("SUM or AVG of a non-number")
        kind = "real" if "real" in kinds else "int"
        values = [be.to_kind(v, kind) for v in values]
        zero = z3.IntVal(0) if kind == "int" else z3.RealVal(0)
        total = z3.Sum(*[z3.If(p, _times(w if kind == "int" else z3.ToReal(w), v.val), zero) for p, w, v in zip(present, weights, values)]) if present else zero
        none = z3.Not(z3.Or(*present)) if present else be._true()
        if isinstance(node, exp.Sum):
            return be.V(kind, total, none)
        real_total = z3.ToReal(total) if kind == "int" else total
        return be.V("real", real_total / z3.If(counted == 0, z3.RealVal(1), z3.ToReal(counted)), none)

    # -- ORDER BY / LIMIT --------------------------------------------------------------------------

    def limited(self, node, rel: Rel) -> Rel:
        limit = node.args.get("limit")
        offset = node.args.get("offset")
        if limit is None and offset is None:
            return rel
        count = None
        if limit is not None:
            target = limit.expression
            if not (isinstance(target, exp.Literal) and not target.is_string):
                raise Unsupported("LIMIT must be a literal")
            count = int(target.this)
        skip = 0
        if offset is not None:
            target = offset.expression if hasattr(offset, "expression") else offset
            if not (isinstance(target, exp.Literal) and not target.is_string):
                raise Unsupported("OFFSET must be a literal")
            skip = int(target.this)
        if node.args.get("order") is None:
            raise Unsupported("LIMIT without ORDER BY")
        rows = rel.rows
        before = [[None] * len(rows) for _ in rows]  # before[j][i]: row j sorts strictly before row i
        for i, a in enumerate(rows):
            for j, b in enumerate(rows):
                if i != j:
                    before[j][i] = self._precedes(b, a)
        for i in range(len(rows)):  # ties go by position (the judge drops a database where a tie decides)
            for j in range(i + 1, len(rows)):
                before[i][j] = z3.Or(before[i][j], z3.Not(z3.Or(before[j][i], before[i][j])))
        out = []
        for i, a in enumerate(rows):
            rank = _total([z3.If(z3.And(b.present, before[j][i]), _copies(b), 0) for j, b in enumerate(rows) if j != i])
            end = rank + _copies(a)
            if count is not None:
                end = _smaller(end, z3.IntVal(skip + count))
            kept = _larger(end - _larger(rank, z3.IntVal(skip)), z3.IntVal(0))
            out.append(_row(z3.And(a.present, kept > 0), a.vals, kept, a.keys))
        return Rel(rel.cols, rel.kinds, out)


def bag_difference(left: Rel, right: Rel):
    """A formula that holds when some row comes out of ``left`` and ``right`` a different number of times."""

    if len(left.cols) != len(right.cols):
        raise Unsupported("different column counts")
    kinds = []
    for a, b in zip(left.kinds, right.kinds):
        if a == b or b == "null":
            kinds.append(a)
        elif a == "null":
            kinds.append(b)
        elif {a, b} <= {"int", "real", "bool"}:
            kinds.append("real" if "real" in (a, b) else "int")
        else:
            raise Unsupported(f"column types differ ({a} vs {b})")
    L = [_row(r.present, [be.to_kind(v, k) for v, k in zip(r.vals, kinds)], _copies(r)) for r in left.rows]
    R = [_row(r.present, [be.to_kind(v, k) for v, k in zip(r.vals, kinds)], _copies(r)) for r in right.rows]
    cache: dict = {}

    def eq(a: Row, b: Row):
        key = (id(a), id(b))
        if key not in cache:
            cache[key] = cache[(id(b), id(a))] = z3.And(*[be.same(x, y) for x, y in zip(a.vals, b.vals)]) if a.vals else be._true()
        return cache[key]

    def count(rows, probe):
        return _total([z3.If(z3.And(r.present, eq(r, probe)), _copies(r), 0) for r in rows])

    clauses = [z3.And(row.present, count(L, row) != count(R, row)) for row in L + R]
    return z3.Or(*clauses) if clauses else be._false()


def database_from_model(database: CountingDatabase, model) -> dict[str, list[tuple]]:
    """Each present row of the model, repeated as many times as its copies say."""

    out: dict[str, list[tuple]] = {}
    for name, slots in database.tables.items():
        table = database.schema.tables[name]
        rows = []
        for slot in slots:
            if not z3.is_true(model.eval(slot.present, model_completion=True)):
                continue
            row = tuple(
                be._model_value(model, v, be.kind_of(c.type) or "int") if v.kind != "unsupported" else None
                for v, c in zip(slot.vals, table.columns)
            )
            rows.extend([row] * model.eval(slot.copies, model_completion=True).as_long())
        out[name] = rows
    return out


def _cap(left: str, right: str, dialect: str) -> int:
    """Enough copies to cross every integer constant the queries compare against (twice over)."""

    import sqlglot

    largest = 8
    for sql in (left, right):
        try:
            tree = sqlglot.parse_one(sql, read=dialect)
        except Exception:  # noqa: BLE001
            continue
        for literal in tree.find_all(exp.Literal):
            if not literal.is_string:
                try:
                    largest = max(largest, abs(int(float(literal.this))))
                except (TypeError, ValueError, OverflowError):
                    pass
    return min(MAX_COPIES, 2 * largest + 2)


def _block(database: CountingDatabase, model):
    """A clause no later model with the same present rows, values and copies satisfies."""

    same = []
    for slots in database.tables.values():
        for slot in slots:
            present = model.eval(slot.present, model_completion=True)
            same.append(slot.present == present)
            if not z3.is_true(present):
                continue
            same.append(slot.copies == model.eval(slot.copies, model_completion=True))
            for v in slot.vals:
                if v.kind == "unsupported":
                    continue
                same.append(v.null == model.eval(v.null, model_completion=True))
                same.append(v.val == model.eval(v.val, model_completion=True))
    return z3.Not(z3.And(*same))


def find_counterexample(left: str, right: str, typed, constraints, *, dialect: str, judge, deadline: float, bounds=(1, 2, 3)):
    """A database (``{table: [row tuple]}``) on which the judge confirms the queries differ, or ``None``.

    ``typed`` maps each table to its ``{column: SQL type}``; ``constraints`` holds the
    ``TableConstraints`` of the prover. The search stops at ``deadline`` (``time.monotonic()``).
    """

    if z3 is None:
        return None
    left, right, problem = positional_sql_pair(left, right, dialect)
    if problem:
        return None
    schema = be.schema_from_prover({t: list(c) for t, c in typed.items()}, constraints, typed)
    repeatable = [name for name, table in schema.tables.items() if not table.keys]
    if not repeatable:
        return None
    plans = [frozenset({name}) for name in repeatable]
    if len(repeatable) > 1:
        plans.append(frozenset(repeatable))
    cap = _cap(left, right, dialect)
    for bound in bounds:
        for repeat in plans:
            remaining = deadline - time.monotonic()
            if remaining <= 0.05:
                return None
            try:
                found = _attempt(left, right, schema, bound, repeat, cap, dialect, judge, deadline)
            except Unsupported:
                return None  # the encoding cannot read one of the queries: no bound or plan will help
            except Exception:  # noqa: BLE001 - an encoding failure is no evidence either way
                found = None
            if found is not None:
                return found
    return None


def _attempt(left, right, schema, bound, repeat, cap, dialect, judge, deadline):
    from .refutation_replay import Verdict

    restrictions: list[str] = []
    database = CountingDatabase(schema, bound, repeat=repeat, cap=cap, restrict=restrictions)
    compiler = CountingCompiler(database, dialect)
    left_rel = compiler.compile(left)
    right_rel = compiler.compile(right)
    solver = z3.Solver()
    solver.add(*database.constraints, *compiler.side_conditions)
    solver.add(bag_difference(left_rel, right_rel))
    for _ in range(ATTEMPTS):
        remaining = deadline - time.monotonic()
        if remaining <= 0.05:
            return None
        solver.set("timeout", max(1, int(remaining * 1000)))
        if solver.check() != z3.sat:
            return None
        model = solver.model()
        data = database_from_model(database, model)
        if judge.verdict(data) is Verdict.DIFFERS:
            return data
        solver.add(_block(database, model))
    return None


__all__ = ["CountingCompiler", "CountingDatabase", "bag_difference", "find_counterexample"]
