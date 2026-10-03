"""Independent checks of rewrite steps, so a proof cannot repeat the mistake it is meant to catch.

The structural prover normalizes both sides of a proof with code that rules also use (the subquery
lifter, the predicate folding in ``cleanup`` and ``equivalence``). A bug shared by a rule and that
normalization would make both sides agree, and the proof would approve the rule's wrong output. A
family of steps is safeguarded here by a checker that imports none of that code: it re-derives every
claim from the step's before and after statements alone.

The first family is predicate cleanup (``remove_trivial_predicates``, and the prover's own
``_normalize_predicates``). A step is accepted only when:

* everything outside WHERE, HAVING, QUALIFY and JOIN ... ON is unchanged node for node (projections,
  tables, joins, grouping, ordering, statement targets), so the predicate identity holds for every
  row, group and join match;
* a JOIN ON is never dropped, HAVING without GROUP BY is never dropped (it makes the query an
  aggregate), and QUALIFY is dropped only where a window function is in its query (without one,
  GoogleSQL rejects the original);
* every predicate that is not a constant (a column, a comparison, a function call) keeps each of its
  occurrences, so nothing that could raise an error or read a volatile value is discarded; each
  occurrence is its own atom, so two calls of ``RAND()`` are never treated as one value;
* only literal comparisons with one clear meaning become constants: two INT64 literals, a numeric
  literal compared with the same text, or two identical string literals without escapes, compared
  with ``=`` or ``!=``; anything else stays an opaque atom;
* the old and new predicates give the same value, TRUE, FALSE or NULL, for every combination of
  TRUE, FALSE and NULL for their atoms, evaluated by SQLite (an implementation of SQL's three-valued
  logic that shares no code with KumoSQL), at most ``MAX_PREDICATE_ATOMS`` atoms per clause.

A change nested inside an atom (``x IN (SELECT y FROM u WHERE TRUE)``) is checked the same way, one
level down. Any failure, including an error in the checker, rejects the step: the caller reports
the change as unproven, with the reason and, for a disagreement, the atom values that show it.
Statements are parsed by sqlglot, which stays inside the trusted boundary.
"""

from __future__ import annotations

from collections import Counter
from contextlib import closing
from dataclasses import dataclass
import itertools
import re
import sqlite3

import sqlglot
from sqlglot import exp

PREDICATE_FAMILY = "predicate_cleanup"
PREDICATE_ASSUMPTIONS = (
    "sql_three_valued_logic",
    "exact_literal_domain",
    "opaque_predicate_occurrences_preserved",
    "clause_context_preserved",
    "statement_frame_preserved",
)
MAX_PREDICATE_ATOMS = 8


@dataclass(frozen=True)
class RewriteStep:
    """One changed statement, as the acceptance layer saw it: replayable with ``check_predicate_step``."""

    rule: str
    family: str
    statement_index: int
    before_sql: str
    after_sql: str
    assumptions: tuple[str, ...] = PREDICATE_ASSUMPTIONS
    section_index: int = -1

    def to_json(self) -> dict[str, object]:
        return {
            "rule": self.rule,
            "family": self.family,
            "statement_index": self.statement_index,
            "section_index": self.section_index,
            "before_sql": self.before_sql,
            "after_sql": self.after_sql,
            "assumptions": list(self.assumptions),
        }


@dataclass(frozen=True)
class StepCheck:
    """The independent checker's verdict on one step."""

    step: RewriteStep
    accepted: bool
    reason: str
    cases_checked: int = 0
    #: On a disagreement: each atom's SQL and the value (True, False or None for NULL) that shows it.
    counterexample: tuple[tuple[str, bool | None], ...] = ()

    def to_json(self) -> dict[str, object]:
        return {
            "step": self.step.to_json(),
            "accepted": self.accepted,
            "reason": self.reason,
            "cases_checked": self.cases_checked,
            "counterexample": [[label, value] for label, value in self.counterexample],
        }


class _Rejected(ValueError):
    pass


def _key(value):
    """A node's identity: its type and every argument, without comments or source positions."""

    if isinstance(value, exp.Expression):
        return (type(value).__name__, tuple(
            (name, _key(child)) for name, child in sorted(value.args.items())
            if child is not None and child != [] and name != "comments"
        ))
    if isinstance(value, list):
        return tuple(_key(child) for child in value)
    return value


def same_tree(a: exp.Expression, b: exp.Expression) -> bool:
    """Whether two trees are identical node for node (comments aside); exact, unlike sqlglot's ``==``."""

    return _key(a) == _key(b)


def _unparen(node):
    while isinstance(node, exp.Paren):
        node = node.this
    return node


_COMPARATORS = {exp.EQ: "=", exp.NEQ: "!=", exp.GT: ">", exp.GTE: ">=", exp.LT: "<", exp.LTE: "<="}
_INTEGER = re.compile(r"0|[1-9][0-9]*")


def _literal_pair(node):
    """The two values of a literal comparison whose meaning is certain, or None.

    Two INT64 literals compare exactly. Any other numeric literal is only compared with the same
    text (BigQuery may coerce INT64 to FLOAT64 and lose precision, and reads ``1e309`` as infinity,
    which equals itself). Strings only compare equal or unequal as identical text without escapes.
    """

    if type(node) not in _COMPARATORS:
        return None
    a, b = _unparen(node.this), _unparen(node.expression)
    if not isinstance(a, exp.Literal) or not isinstance(b, exp.Literal):
        return None
    if a.is_string or b.is_string:
        if a.is_string and b.is_string and a.this == b.this and "\\" not in a.this and type(node) in (exp.EQ, exp.NEQ):
            return a.this, b.this
        return None
    texts = a.this, b.this
    if all(_INTEGER.fullmatch(text) for text in texts):
        values = tuple(int(text) for text in texts)
        return values if max(values) <= 2**63 - 1 else None
    if texts[0] == texts[1]:
        try:
            value = float(texts[0])
        except ValueError:
            return None
        return value, value
    return None


_BOOLEAN_NODES = (exp.And, exp.Or, exp.Not, exp.Paren, exp.Boolean, exp.Null)


def _atoms(node, found: list) -> None:
    """The opaque atoms of a predicate, in order: everything that is not a connective or a constant."""

    node = _unparen(node)
    if isinstance(node, (exp.Boolean, exp.Null)) or _literal_pair(node) is not None:
        return
    if isinstance(node, (exp.And, exp.Or)):
        _atoms(node.this, found)
        _atoms(node.expression, found)
        return
    if isinstance(node, exp.Not):
        _atoms(node.this, found)
        return
    found.append(node)


class _Model:
    """Compiles a predicate to SQLite, naming each atom by the variable the caller assigned it."""

    def __init__(self, connection: sqlite3.Connection, names: dict[int, str]):
        self.connection = connection
        self.names = names

    def compile(self, node) -> str:
        node = _unparen(node)
        if isinstance(node, exp.Boolean):
            return "1" if node.this else "0"
        if isinstance(node, exp.Null):
            return "NULL"
        if isinstance(node, (exp.And, exp.Or)):
            op = "AND" if isinstance(node, exp.And) else "OR"
            return f"({self.compile(node.this)} {op} {self.compile(node.expression)})"
        if isinstance(node, exp.Not):
            return f"(NOT {self.compile(node.this)})"
        pair = _literal_pair(node)
        if pair is not None:
            # Values are bound, never spliced into the SQL; SQLite does the comparison.
            truth = self.connection.execute(f"SELECT ? {_COMPARATORS[type(node)]} ?", pair).fetchone()[0]
            return "1" if truth else "0"
        return self.names[id(node)]


def _assign_atoms(before_atoms: list, after_atoms: list, nested: list, path: str) -> tuple[dict[int, str], list[str]]:
    """Give each atom a variable, so that an atom and its counterpart share one.

    When the atoms are the same multiset (``x AND y`` becomes ``y AND x``), the k-th occurrence of
    an atom on one side is the k-th occurrence of the same atom on the other. Otherwise the atoms must
    pair up in order, each pair identical or itself an accepted change (checked one level down).
    Two occurrences of the same expression are always separate variables: a volatile call can differ.
    """

    names: dict[int, str] = {}
    labels: list[str] = []
    old_keys = [_key(atom) for atom in before_atoms]
    new_keys = [_key(atom) for atom in after_atoms]
    if Counter(old_keys) == Counter(new_keys):
        variables: dict[tuple, str] = {}
        for atoms, keys in ((before_atoms, old_keys), (after_atoms, new_keys)):
            seen: Counter = Counter()
            for atom, key in zip(atoms, keys):
                slot = (key, seen[key])
                seen[key] += 1
                if slot not in variables:
                    variables[slot] = f"a{len(variables)}"
                    labels.append(atom.sql(dialect="bigquery", comments=False) + (f" [occurrence {slot[1] + 1}]" if slot[1] else ""))
                names[id(atom)] = variables[slot]
        return names, labels
    if len(before_atoms) != len(after_atoms):
        raise _Rejected("a non-constant predicate was added or removed (an expression or its errors could be discarded)")
    for index, (old, new, old_key, new_key) in enumerate(zip(before_atoms, after_atoms, old_keys, new_keys)):
        if old_key != new_key:
            try:
                _compare_frame(old, new, nested, f"{path}<atom {index + 1}>")
            except _Rejected:
                raise _Rejected("a non-constant predicate changed (an expression or its errors could be discarded)") from None
        variable = f"a{index}"
        names[id(old)] = names[id(new)] = variable
        labels.append(old.sql(dialect="bigquery", comments=False))
    return names, labels


def _predicate_check(before, after, nested: list, path: str):
    """``(agree, cases checked, counterexample)`` for two predicates over the same atoms."""

    before_atoms: list = []
    after_atoms: list = []
    _atoms(before, before_atoms)
    _atoms(after, after_atoms)
    names, labels = _assign_atoms(before_atoms, after_atoms, nested, path)
    if len(labels) > MAX_PREDICATE_ATOMS:
        raise _Rejected(f"more than {MAX_PREDICATE_ATOMS} non-constant predicates in one clause; the exhaustive check is not run")
    variables = [f"a{i}" for i in range(len(labels))]
    assignments = list(itertools.product((None, False, True), repeat=len(variables)))
    with closing(sqlite3.connect(":memory:")) as connection:
        model = _Model(connection, names)
        left, right = model.compile(before), model.compile(after)
        columns = ", ".join(f"{name} BOOLEAN" for name in variables) or "unused BOOLEAN"
        connection.execute(f"CREATE TABLE cases ({columns})")
        rows = assignments if variables else [(None,)]
        connection.executemany(f"INSERT INTO cases VALUES ({', '.join('?' for _ in rows[0])})", rows)
        outputs = connection.execute(f"SELECT {left}, {right} FROM cases").fetchall()
        if len(outputs) != len(rows):
            raise _Rejected("the exhaustive check did not evaluate every case")
        for values, (a, b) in zip(rows, outputs):
            if a != b:
                return False, len(outputs), tuple(zip(labels, values)) if variables else ()
    return True, len(outputs), ()


def _clause_predicate(slot, clause):
    if clause is None:
        return exp.true()
    if slot == "on":
        return clause
    expected = {"where": exp.Where, "having": exp.Having, "qualify": exp.Qualify}[slot]
    if not isinstance(clause, expected):
        raise _Rejected(f"unexpected {slot} clause shape")
    return clause.this


def _has_window_in_scope(select: exp.Select) -> bool:
    return any(window.find_ancestor(exp.Select) is select for window in select.find_all(exp.Window))


def _compare_frame(old, new, predicates: list, path: str = "statement") -> None:
    """Require ``old`` and ``new`` to differ only inside predicate clauses; collect those pairs."""

    if _key(old) == _key(new):
        return
    if not isinstance(old, exp.Expression) or type(old) is not type(new):
        raise _Rejected(f"the statement changed outside a predicate, at {path}")
    slots = {"where", "having", "qualify"} if isinstance(old, exp.Select) else {"on"} if isinstance(old, exp.Join) else set()
    for name in sorted(set(old.args) | set(new.args)):
        if name == "comments":
            continue
        a, b = old.args.get(name), new.args.get(name)
        if _key(a) == _key(b):
            continue
        location = f"{path}.{name}"
        if name in slots:
            if a is None:
                raise _Rejected(f"a predicate clause was added at {location}")
            if b is None:
                if name == "on":
                    raise _Rejected("a JOIN ON condition was dropped")
                if name == "having" and not old.args.get("group"):
                    raise _Rejected("HAVING without GROUP BY was dropped (it makes the query an aggregate)")
                if name == "qualify" and not _has_window_in_scope(old):
                    raise _Rejected("QUALIFY was dropped from a query without a window function")
            predicates.append((location, _clause_predicate(name, a), _clause_predicate(name, b)))
        elif isinstance(a, list) and isinstance(b, list) and len(a) == len(b):
            for index, (x, y) in enumerate(zip(a, b)):
                _compare_frame(x, y, predicates, f"{location}[{index}]")
        elif isinstance(a, exp.Expression) and isinstance(b, exp.Expression):
            _compare_frame(a, b, predicates, location)
        else:
            raise _Rejected(f"the statement changed outside a predicate, at {location}")


def _check(before: exp.Expression, after: exp.Expression) -> tuple[bool, str, int, tuple]:
    predicates: list = []
    _compare_frame(before, after, predicates)
    cases = checked_clauses = 0
    while predicates:
        path, old, new = predicates.pop(0)
        agree, count, witness = _predicate_check(old, new, predicates, path)
        cases += count
        checked_clauses += 1
        if not agree:
            return False, f"the predicates disagree at {path}", cases, witness
    return True, f"only predicates changed; {checked_clauses} clause(s) agree on every TRUE/FALSE/NULL case", cases, ()


def check_predicate_transition(step: RewriteStep, before: exp.Expression, after: exp.Expression) -> StepCheck:
    """Check a predicate-cleanup step from its parsed statements (the caller's own copies)."""

    if step.family != PREDICATE_FAMILY:
        return StepCheck(step, False, f"no independent checker for the {step.family!r} family")
    if step.assumptions != PREDICATE_ASSUMPTIONS:
        return StepCheck(step, False, "the step's assumptions are not the predicate-cleanup assumptions")
    try:
        accepted, reason, cases, witness = _check(before, after)
    except Exception as exc:  # noqa: BLE001 - an error in the checker is a rejection, never an acceptance
        return StepCheck(step, False, str(exc) or type(exc).__name__)
    return StepCheck(step, accepted, reason, cases, witness)


def _parse_one(sql: str) -> exp.Expression:
    nodes = [node for node in sqlglot.parse(sql, read="bigquery", error_level=sqlglot.ErrorLevel.RAISE) if node is not None]
    if len(nodes) != 1:
        raise _Rejected("a step must hold exactly one statement on each side")
    return nodes[0]


def check_predicate_step(step: RewriteStep) -> StepCheck:
    """Replay a recorded step from its SQL text."""

    try:
        before, after = _parse_one(step.before_sql), _parse_one(step.after_sql)
    except Exception as exc:  # noqa: BLE001
        return StepCheck(step, False, f"the step could not be parsed: {exc}")
    return check_predicate_transition(step, before, after)


__all__ = [
    "MAX_PREDICATE_ATOMS",
    "PREDICATE_ASSUMPTIONS",
    "PREDICATE_FAMILY",
    "RewriteStep",
    "StepCheck",
    "check_predicate_step",
    "check_predicate_transition",
    "same_tree",
]
