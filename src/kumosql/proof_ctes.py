"""Independent check of CTE rewrites: scope-resolved binders, compared by expansion.

``remove_unused_ctes``, ``inline_single_use_ctes``, ``deduplicate_ctes`` and the prover's own CTE
normalization (renaming, reordering, merging, dropping) all rely on knowing which relation each
name in a FROM or JOIN means. A bug in how a rule finds references (a name that is only a table, a
reference inside another CTE, a nested WITH that shadows a name) would be shared by the prover's
normalizer, and both sides of a proof would agree on the wrong answer. This module re-derives the
meaning from the step's before and after statements alone and imports no rule, normalizer, prover or
comparator (``proof_steps`` holds only the checker's own record types and tree key).

The check replaces every CTE reference by its definition and removes every WITH clause. Both
statements are expanded with the same scope rules, and the expanded trees must be identical node for
node. A step that renames, reorders, merges, inlines or drops CTEs without changing what any name
means leaves the expansion unchanged. A step that captures a name, points a reference at a different
definition, or drops a CTE something still reads changes it.

The scope rules are the ones BigQuery documents: a name resolves case-insensitively, a CTE sees only
the CTEs defined before it in its own WITH clause (never itself) and those of enclosing scopes, a
nested WITH shadows the names it defines, and a one-part name in FROM means the CTE
even when it is also the alias of another FROM item (checked on BigQuery: `FROM a CROSS JOIN a AS b` and a
subquery reading `a AS b` under an outer `FROM a` both read the CTE). A step is refused (never accepted) when:

* a WITH is recursive, repeats a name, or a CTE carries a MATERIALIZED hint;
* a name that matches a visible CTE appears anywhere other than as a FROM or JOIN relation, or that
  relation carries anything beyond an alias or a PIVOT/UNPIVOT (a snapshot, a sample);
* a volatile call (``RAND()``, ``GENERATE_UUID()``, the current time) in a CTE is copied more than once by
  the expansion, that is, the CTE is read more than once, because merging, inlining or splitting its
  readers changes how many values are drawn;
* a CTE name disappears from the statement while a bare identifier spelled like it is still an
  argument of a function (some engines pass a table that way), because that read cannot be tracked;
* expansion would copy more than ``MAX_EXPANDED_NODES`` nodes.
"""

from __future__ import annotations

from dataclasses import dataclass
import re

from sqlglot import exp

from .proof_steps import RewriteStep, StepCheck, _key

CTE_FAMILY = "cte_binders"
CTE_ASSUMPTIONS = (
    "cte_scope_resolution",
    "reference_equals_inline_subquery",
    "volatile_bodies_refused",
    "unreferenced_ctes_have_no_effect",
    "expanded_statements_identical",
)
MAX_EXPANDED_NODES = 50_000


class _Rejected(ValueError):
    pass


_SAFE_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_VOLATILE = {
    "RAND", "RANDOM", "UUID", "GENERATEUUID", "RANDOMUUID", "SESSIONUSER", "CURRENTUSER",
    "CURRENTDATE", "CURRENTTIME", "CURRENTTIMESTAMP", "CURRENTDATETIME", "NOW", "SYSDATE",
}


def _norm_key(value):
    """The tree key with the quoting of plain names ignored (`a` and a name the same relation)."""

    if isinstance(value, exp.Identifier):
        text = value.this
        quoted = bool(value.args.get("quoted")) and not (isinstance(text, str) and _SAFE_NAME.fullmatch(text))
        return ("Identifier", text, quoted)
    if isinstance(value, exp.Expression):
        return (type(value).__name__, tuple(
            (name, _norm_key(child)) for name, child in sorted(value.args.items())
            if child is not None and child != [] and name != "comments"
        ))
    if isinstance(value, list):
        return tuple(_norm_key(child) for child in value)
    return value


def _first_difference(a, b, path: str = "statement") -> str:
    if type(a) is not type(b):
        return path
    if isinstance(a, exp.Expression):
        if isinstance(a, exp.Identifier) and _norm_key(a) == _norm_key(b):
            return ""
        for name in sorted(set(a.args) | set(b.args)):
            if name == "comments":
                continue
            found = _first_difference(a.args.get(name), b.args.get(name), f"{path}.{name}")
            if found:
                return found
        return ""
    if isinstance(a, list):
        if len(a) != len(b):
            return path
        for index, (x, y) in enumerate(zip(a, b)):
            found = _first_difference(x, y, f"{path}[{index}]")
            if found:
                return found
        return ""
    return "" if a == b else path


def _volatile_name(node: exp.Expression) -> str | None:
    names = {type(node).__name__.upper()}
    if isinstance(node, exp.Anonymous):
        names.add(str(node.name).upper().replace("_", ""))
    elif isinstance(node, exp.Func):
        names.add(node.sql_name().upper().replace("_", ""))
    found = names & _VOLATILE
    return sorted(found)[0] if found else None


def _tag_volatile(statement: exp.Expression) -> None:
    """Number every volatile call in the source, so its copies can be counted after expansion."""

    for index, node in enumerate(statement.walk()):
        if _volatile_name(node):
            node.meta["volatile_site"] = index


def _repeated_volatile(expanded: exp.Expression) -> str | None:
    """A volatile call that the expansion copied more than once: it would be evaluated once per copy."""

    seen: set[int] = set()
    for node in expanded.walk():
        site = node.meta.get("volatile_site")
        if site is None:
            continue
        if site in seen:
            return _volatile_name(node)
        seen.add(site)
    return None


def _with_arg(node: exp.Expression) -> tuple[str, exp.With] | None:
    for key, value in node.args.items():
        if isinstance(value, exp.With):
            return key, value
    return None


def _cte_name(cte: exp.CTE) -> str | None:
    alias = cte.args.get("alias")
    if alias is None or not isinstance(alias.this, exp.Identifier):
        return None
    return alias.this.name.lower()


def _is_candidate(table: exp.Table, env: dict) -> bool:
    return (
        not table.args.get("db")
        and not table.args.get("catalog")
        and isinstance(table.this, exp.Identifier)
        and table.name.lower() in env
    )


@dataclass
class _Expansion:
    references: int = 0
    nodes: int = 0
    defined: list[str] = None  # type: ignore[assignment]
    bodies: int = 0

    def __post_init__(self) -> None:
        self.defined = []


def _expand(node: exp.Expression, env: dict, state: _Expansion) -> None:
    """Replace, in place, every CTE reference under ``node`` by its definition and drop each WITH."""

    scope = _with_arg(node)
    if scope is not None:
        key, clause = scope
        if clause.args.get("recursive"):
            raise _Rejected("a recursive WITH clause is not expanded")
        env = dict(env)
        seen: set[str] = set()
        for cte in clause.expressions:
            name = _cte_name(cte)
            if name is None:
                raise _Rejected("a CTE has no plain name")
            if name in seen:
                raise _Rejected(f"the CTE name {name!r} is defined twice in one WITH clause")
            if cte.args.get("materialized") is not None:
                raise _Rejected("a CTE carries a MATERIALIZED hint")
            seen.add(name)
            state.defined.append(name)
            _expand(cte.this, env, state)
            alias = cte.args["alias"]
            env[name] = (cte.this, alias.args.get("columns"))
        node.set(key, None)
    for key, value in list(node.args.items()):
        if key == "comments":
            continue
        if isinstance(value, exp.Expression):
            _visit(node, key, value, None, env, state)
        elif isinstance(value, list):
            for index, child in enumerate(list(value)):
                if isinstance(child, exp.Expression):
                    _visit(node, key, child, index, env, state)


def _visit(parent, key, child, index, env, state: _Expansion) -> None:
    if isinstance(child, exp.Table) and _is_candidate(child, env):
        name = child.name.lower()
        if not (isinstance(parent, (exp.From, exp.Join)) and key == "this"):
            raise _Rejected(f"the CTE name {name!r} is read somewhere other than a FROM or JOIN relation")
        extra = {k for k, v in child.args.items() if v is not None and v != [] and k not in ("this", "alias", "pivots", "comments")}
        if extra:
            raise _Rejected(f"a reference to the CTE {name!r} carries {sorted(extra)[0]}")
        body, columns = env[name]
        copy = body.copy()
        state.nodes += sum(1 for _ in copy.walk())
        if state.nodes > MAX_EXPANDED_NODES:
            raise _Rejected(f"expanding the CTE references would copy more than {MAX_EXPANDED_NODES} nodes")
        alias = child.args.get("alias")
        label = alias.copy() if alias is not None else exp.TableAlias(this=exp.to_identifier(child.name))
        if columns and not label.args.get("columns"):
            label.set("columns", [column.copy() for column in columns])
        replacement = exp.Subquery(this=copy, alias=label)
        if child.args.get("pivots"):  # PIVOT and UNPIVOT apply to whatever relation the name stands for
            replacement.set("pivots", [pivot.copy() for pivot in child.args["pivots"]])
        state.references += 1
        if index is None:
            parent.set(key, replacement)
        else:
            children = list(parent.args[key])
            children[index] = replacement
            parent.set(key, children)
        return
    _expand(child, env, state)


def _vanished_names(before: _Expansion, after: _Expansion) -> set[str]:
    return {name for name in before.defined if before.defined.count(name) > after.defined.count(name)}


def _hidden_reads(statement: exp.Expression, names: set[str]) -> str | None:
    """A function argument spelled like a CTE name that no longer exists (a table passed by name)."""

    for column in statement.find_all(exp.Column):
        if column.table or column.name.lower() not in names:
            continue
        if isinstance(column.parent, (exp.Func, exp.Anonymous, exp.Unnest)):
            return column.name
    return None


def _check(before: exp.Expression, after: exp.Expression) -> tuple[bool, str, int]:
    old, new = before.copy(), after.copy()
    old_state, new_state = _Expansion(), _Expansion()
    _tag_volatile(old)
    _tag_volatile(new)
    _expand(old, {}, old_state)
    _expand(new, {}, new_state)
    for expanded in (old, new):
        repeated = _repeated_volatile(expanded)
        if repeated:
            raise _Rejected(
                f"a CTE that calls {repeated} is read more than once, and merging, inlining or reading it a "
                "different number of times changes its value"
            )
    vanished = _vanished_names(old_state, new_state)
    if vanished:
        hidden = _hidden_reads(before, vanished)
        if hidden:
            raise _Rejected(f"a bare identifier {hidden!r} spelled like a CTE that is gone is read as a function argument")
    if _norm_key(old) != _norm_key(new):
        where = _first_difference(old, new)
        return False, f"the statements differ once every CTE reference is replaced by its definition (at {where})", old_state.references
    return True, (
        f"identical once every CTE reference is replaced by its definition "
        f"({old_state.references} reference(s) before, {new_state.references} after)"
    ), old_state.references + new_state.references


def check_cte_transition(step: RewriteStep, before: exp.Expression, after: exp.Expression) -> StepCheck:
    """Check a CTE step from its parsed statements (the caller's own copies are never changed)."""

    if step.family != CTE_FAMILY:
        return StepCheck(step, False, f"no independent checker for the {step.family!r} family")
    if step.assumptions != CTE_ASSUMPTIONS:
        return StepCheck(step, False, "the step's assumptions are not the CTE assumptions")
    try:
        accepted, reason, cases = _check(before, after)
    except Exception as exc:  # noqa: BLE001 - an error in the checker is a rejection, never an acceptance
        return StepCheck(step, False, str(exc) or type(exc).__name__)
    return StepCheck(step, accepted, reason, cases)


__all__ = ["CTE_ASSUMPTIONS", "CTE_FAMILY", "MAX_EXPANDED_NODES", "check_cte_transition"]
