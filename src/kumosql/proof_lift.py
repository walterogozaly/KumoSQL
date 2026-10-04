"""Independent check of the subquery lifter: every lifted CTE must expand back into the subquery it replaced.

``lift_subqueries`` moves each FROM or JOIN subquery into a CTE with a generated name. The prover lifts both
sides of a proof with the same lifter, so a lifter bug (a name a table already has, a name captured by a nested
WITH, a subquery that read an outer relation, a subquery that was dropped or changed) is approved by both
sides. This module re-derives the claim from the step's before and after text alone. It imports no rule,
normalizer, lifter or prover. It uses ``proof_steps``'s record types and ``proof_ctes``'s expansion, which
are independent of the lifter themselves.

A step is accepted only when all of these hold:

* **Fresh names.** The lifted CTEs are the CTEs of the after statement whose name does not occur as an
  identifier anywhere in the before statement (a table, alias, column or CTE spelled the same, in any case),
  and each is defined once. A generated name that a physical table already has is therefore not a lifted CTE,
  and the extra CTE it makes fails the next check.
* **Exact restoration.** Replacing each reference to a lifted CTE by its body as a derived table
  (``(body) AS alias``), and dropping those CTEs, reproduces the before statement node for node. Every original
  CTE and every other node must be unchanged, and each lifted CTE is read exactly once, by a FROM or JOIN
  relation that carries nothing but an alias, a PIVOT/UNPIVOT, a sample or a lateral (what the subquery carried).
* **Scope-correct meaning.** The same restoration is also done by ``proof_ctes``'s scope rules, replacing
  every CTE reference, original or lifted, by its definition. The two statements must expand to the same tree.
  A body that read a name defined by a nested WITH, a body placed after the CTE that reads it, and a reference
  that no longer reaches its definition all make the trees differ. An unaliased subquery becomes a relation
  named like its CTE, which is not a change because that name occurs nowhere in the before statement.
* **Closed bodies.** A lifted body is checked where it stood: no qualified column in it may name a relation of
  an enclosing query that the body does not define itself, because a CTE cannot see the query that reads it.
  Unqualified columns cannot be resolved without a schema and are not checked.
* **Volatile bodies.** A lifted body holding a volatile call (``RAND()``, ``GENERATE_UUID()``, the current
  time) is refused when its reference sits inside an expression subquery, because a derived table there is
  evaluated per outer row while a CTE need not be.

Both statements are printed and read once by sqlglot before they are compared (its printer rewrites ``INT`` as
``INT64``, ``DISTINCT ON`` as a window function and so on), so only what the lifter changed remains; two
statements that agree after that print are accepted as a step that lifted nothing.

Refused, never guessed: a step in which a recursive or MATERIALIZED WITH, a duplicate CTE name, a column list on
a lifted CTE or a CTE name read outside FROM and JOIN appears, anything ``proof_ctes`` cannot follow, and any
error in the checker. Both statements are read again from the step's SQL text, never from the caller's
in-memory trees.
"""

from __future__ import annotations

from collections import Counter

import sqlglot
from sqlglot import exp

from .proof_ctes import _Expansion, _Rejected, _cte_name, _expand, _first_difference, _norm_key, _volatile_name
from .proof_steps import RewriteStep, StepCheck

LIFT_FAMILY = "subquery_lift"
LIFT_ASSUMPTIONS = (
    "lifted_names_fresh",
    "lifted_ctes_restore_exactly",
    "cte_scope_resolution",
    "lifted_bodies_closed",
    "volatile_bodies_not_repeated",
)
MAX_ITERATIONS = 10_000

_CARRIED = ("pivots", "sample", "laterals")
_STRUCTURAL = (exp.From, exp.Join, exp.CTE, exp.With, exp.SetOperation, exp.Subquery, exp.Create, exp.Insert)


def _parse(sql: str) -> exp.Expression:
    nodes = [node for node in sqlglot.parse(sql, read="bigquery", error_level=sqlglot.ErrorLevel.RAISE) if node is not None]
    if len(nodes) != 1:
        raise _Rejected("a step must hold exactly one statement on each side")
    return nodes[0]


def _reparse(sql: str) -> exp.Expression:
    """The statement as sqlglot reads the text, after one print and read of the tree.

    The lifter's output is text that sqlglot printed, so a statement must be printed the same way before
    the two are compared: sqlglot's printer rewrites some constructs as it prints (``INT`` as ``INT64``,
    ``DISTINCT ON`` as a window function) and reads the printed form as a different tree. Both sides get the
    same print, so only a change the lifter made remains. This trusts sqlglot's printer the way every proof
    trusts its parser.
    """

    return _parse(_parse(sql).sql(dialect="bigquery", comments=False))


def _identifier_names(statement: exp.Expression) -> set[str]:
    names = set()
    for node in statement.find_all(exp.Identifier):
        if isinstance(node.this, str):
            names.add(node.this.lower())
    return names


def _is_reference(table: exp.Expression, names) -> bool:
    return (
        isinstance(table, exp.Table)
        and not table.args.get("db")
        and not table.args.get("catalog")
        and isinstance(table.this, exp.Identifier)
        and table.name.lower() in names
    )


def _fresh_ctes(before: exp.Expression, after: exp.Expression) -> dict[str, exp.CTE]:
    """The lifted CTEs: those of ``after`` whose name occurs nowhere in ``before``."""

    known = _identifier_names(before)
    fresh: dict[str, exp.CTE] = {}
    for cte in after.find_all(exp.CTE):
        name = _cte_name(cte)
        if name is None:
            raise _Rejected("a CTE has no plain name")
        if name in known:
            continue
        if name in fresh:
            raise _Rejected(f"the lifted CTE name {name!r} is defined twice")
        clause = cte.parent
        if isinstance(clause, exp.With) and clause.args.get("recursive"):
            raise _Rejected("a lifted CTE sits in a recursive WITH clause")
        if cte.args["alias"].args.get("columns"):
            raise _Rejected(f"the lifted CTE {name!r} has a column list")
        fresh[name] = cte
    return fresh


def _drop_with_entry(cte: exp.CTE) -> None:
    clause = cte.parent
    remaining = [other for other in clause.expressions if other is not cte]
    clause.set("expressions", remaining)
    if not remaining:
        owner = clause.parent
        for key, value in list(owner.args.items()):
            if value is clause:
                owner.set(key, None)


def _ancestors(node: exp.Expression):
    node = node.parent
    while node is not None:
        yield node
        node = node.parent


def _in_expression_subquery(node: exp.Expression) -> bool:
    """Whether ``node`` is inside a subquery used as an expression (scalar, EXISTS, IN, ARRAY)."""

    for ancestor in _ancestors(node):
        parent = ancestor.parent
        if isinstance(ancestor, (exp.Select, exp.SetOperation, exp.Subquery)) and parent is not None:
            if isinstance(ancestor, exp.Subquery) and isinstance(parent, (exp.From, exp.Join)):
                continue
            if not isinstance(parent, _STRUCTURAL):
                return True
    return False


def _restore_exactly(before: exp.Expression, after: exp.Expression, fresh: dict[str, exp.CTE]) -> int:
    """Check ``after`` with each lifted CTE written back as a derived table equals ``before``; the number lifted."""

    references = Counter(
        table.name.lower() for table in after.find_all(exp.Table) if _is_reference(table, fresh)
    )
    for name in fresh:
        if references[name] != 1:
            raise _Rejected(f"the lifted CTE {name!r} is read {references[name]} times; a lifted subquery is read exactly once")
    unlifted = after.copy()
    bodies: dict[str, exp.Expression] = {}
    for cte in list(unlifted.find_all(exp.CTE)):
        name = _cte_name(cte)
        if name in fresh:
            bodies[name] = cte.this
            _drop_with_entry(cte)
    for _ in range(MAX_ITERATIONS):
        sites = [table for table in unlifted.find_all(exp.Table) if _is_reference(table, bodies)]
        if not sites:
            break
        for site in sites:
            name = site.name.lower()
            if not (isinstance(site.parent, (exp.From, exp.Join)) and site.arg_key == "this"):
                raise _Rejected(f"the lifted CTE {name!r} is read somewhere other than a FROM or JOIN relation")
            extra = {
                key for key, value in site.args.items()
                if value is not None and value != [] and key not in ("this", "alias", "comments", *_CARRIED)
            }
            if extra:
                raise _Rejected(f"the reference to the lifted CTE {name!r} carries {sorted(extra)[0]}")
            alias = site.args.get("alias")
            replacement = exp.Subquery(this=bodies[name].copy(), alias=alias.copy() if alias is not None else None)
            for key in _CARRIED:
                if site.args.get(key):
                    replacement.set(key, [item.copy() for item in site.args[key]])
            site.replace(replacement)
    else:
        raise _Rejected("the lifted CTEs refer to each other in a cycle")
    if _norm_key(unlifted) != _norm_key(before):
        raise _Rejected(
            "the statement is not the before statement once each lifted CTE is written back as a derived table "
            f"(at {_first_difference(before, unlifted)})"
        )
    return len(fresh)


def _volatile_in(body: exp.Expression) -> str | None:
    for node in body.walk():
        found = _volatile_name(node)
        if found:
            return found
    return None


def _source_names(select: exp.Expression, both: bool) -> set[str]:
    """Names a column can use to qualify a relation of ``select`` (the alias hides the table's own name)."""

    items = []
    for key in ("from_", "from"):
        if select.args.get(key) is not None:
            items.append(select.args[key].this)
    items.extend(join.this for join in select.args.get("joins") or [])
    items.extend(select.args.get("laterals") or [])
    names = set()
    for item in items:
        if not isinstance(item, exp.Expression):
            continue
        names.add((item.alias_or_name or "").lower())
        if both and isinstance(item, exp.Table) and item.name:
            names.add(item.name.lower())
    names.discard("")
    return names


def _read_outer_relation(body: exp.Expression, site: exp.Subquery) -> str | None:
    """A qualified column in ``body`` naming a relation of an enclosing query that ``body`` does not define."""

    outer: set[str] = set()
    for ancestor in _ancestors(site):
        if isinstance(ancestor, exp.Select):
            outer |= _source_names(ancestor, both=True)
    if not outer:
        return None
    for column in body.find_all(exp.Column):
        parts = {part.lower() for part in (column.text("catalog"), column.text("db"), column.table) if part}
        if not parts & outer:
            continue
        inner: set[str] = set()
        node = column.parent
        while node is not None:
            if isinstance(node, exp.Select):
                inner |= _source_names(node, both=False)
            if node is body:
                break
            node = node.parent
        if not parts & inner:
            return column.sql(dialect="bigquery")
    return None


def _check_expansion(before: exp.Expression, after: exp.Expression, fresh: dict[str, exp.CTE]) -> None:
    """Scope-correct restoration: replace every CTE reference by its definition on both sides and compare."""

    old, new = before.copy(), after.copy()
    fresh_new = _fresh_ctes(old, new)
    for cte in fresh_new.values():
        cte.this.meta["lifted_body"] = _cte_name(cte)
    _expand(old, {}, _Expansion())
    _expand(new, {}, _Expansion())
    # An unaliased derived table becomes a relation named like its CTE; the name occurs nowhere in ``before``.
    for subquery in new.find_all(exp.Subquery):
        alias = subquery.args.get("alias")
        if alias is not None and isinstance(alias.this, exp.Identifier) and alias.name.lower() in fresh and not alias.args.get("columns"):
            subquery.set("alias", None)
    if _norm_key(old) != _norm_key(new):
        raise _Rejected(
            "the statements differ once every CTE reference is replaced by its definition "
            f"(at {_first_difference(old, new)}); a lifted body may have changed meaning where it now sits"
        )
    for subquery in new.find_all(exp.Subquery):
        name = subquery.this.meta.get("lifted_body") if isinstance(subquery.this, exp.Expression) else None
        if name is None:
            continue
        read = _read_outer_relation(subquery.this, subquery)
        if read:
            raise _Rejected(f"the lifted body of {name!r} reads {read}, a relation of the query around it that a CTE cannot see")
        volatile = _volatile_in(subquery.this)
        if volatile and _in_expression_subquery(subquery):
            raise _Rejected(
                f"the lifted body of {name!r} calls {volatile} inside an expression subquery, "
                "where a derived table is evaluated per outer row and a CTE need not be"
            )


def _check(step: RewriteStep) -> tuple[bool, str, int]:
    before, after = _reparse(step.before_sql), _reparse(step.after_sql)
    if _norm_key(before) == _norm_key(after):
        return True, "nothing was lifted: the statements are identical once sqlglot has printed both", 0
    fresh = _fresh_ctes(before, after)
    if not fresh:
        return False, (
            "no CTE with a new name was added, so no subquery was lifted "
            "(a CTE named like a table, alias or column the statement already has is not a lifted CTE)"
        ), 0
    lifted = _restore_exactly(before, after, fresh)
    _check_expansion(before, after, fresh)
    return True, f"{lifted} lifted CTE(s) written back as the subqueries they replaced give the before statement", lifted


def check_lift_transition(step: RewriteStep, before: exp.Expression, after: exp.Expression) -> StepCheck:
    """Check a lift step from the step's SQL text (the caller's trees are not trusted and not changed)."""

    if step.family != LIFT_FAMILY:
        return StepCheck(step, False, f"no independent checker for the {step.family!r} family")
    if step.assumptions != LIFT_ASSUMPTIONS:
        return StepCheck(step, False, "the step's assumptions are not the lift assumptions")
    try:
        accepted, reason, cases = _check(step)
    except Exception as exc:  # noqa: BLE001 - an error in the checker is a rejection, never an acceptance
        return StepCheck(step, False, str(exc) or type(exc).__name__)
    return StepCheck(step, accepted, reason, cases)


__all__ = ["LIFT_ASSUMPTIONS", "LIFT_FAMILY", "check_lift_transition"]
