"""Analysis: a sqlglot BigQuery tree becomes typed, executable closures.

Names are resolved and every expression is typed once, before any row is read, the way BigQuery
analyses a query; execution then only runs closures. Anything this module does not recognise raises
:class:`Unsupported` rather than guessing, and every argument a sqlglot node carries must be one the
handler reads (``_only``), so a construct sqlglot represents with an extra flag can never be evaluated
as if the flag were absent.
"""

from __future__ import annotations

import itertools
import math
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Callable

from sqlglot import exp

from . import types as T
from . import values as V
from .errors import AnalysisError, EvalError, Unsupported
from .runtime import NULL, E, Env, const, is_constant

# ---------------------------------------------------------------------------------------------
# scopes
# ---------------------------------------------------------------------------------------------


@dataclass
class Source:
    """A range variable of a FROM clause: a table, subquery, UNNEST or CTE reference."""

    name: str | None
    cols: list  # [(name or None, slot, Type)]
    value_slot: int | None = None  # value tables (UNNEST, SELECT AS STRUCT/VALUE): the slot holding the value
    value_type: T.Type | None = None


@dataclass
class Ref:
    slot: int
    type: T.Type
    fields: tuple = ()  # field positions applied after reading the slot
    consumed: int = 1
    row_slots: tuple | None = None  # a whole table row read as a STRUCT


class Scope:
    parent: "Scope | None" = None

    def lookup(self, parts: list[str]) -> Ref | None:
        return None


class EmptyScope(Scope):
    def __init__(self, parent: Scope | None = None):
        self.parent = parent


class FromScope(Scope):
    def __init__(self, sources: list[Source], width: int, parent: Scope | None, merged=None, star=None):
        self.sources = sources
        self.width = width
        self.parent = parent
        self.merged = merged or {}  # JOIN USING: lowercase name -> (slot, Type)
        self._star = star

    def star(self) -> list[tuple[str | None, int, T.Type, tuple]]:
        """``SELECT *``: (name, slot, type, fields) in output order."""

        if self._star is not None:
            return self._star
        out = []
        for source in self.sources:
            out.extend(source_star(source))
        return out

    def lookup(self, parts: list[str]) -> Ref | None:
        first = parts[0].lower()
        hits = []
        if first in self.merged:
            slot, typ = self.merged[first]
            hits.append(Ref(slot, typ))
        else:
            for source in self.sources:
                for name, slot, typ in source.cols:
                    if name is not None and name.lower() == first:
                        hits.append(Ref(slot, typ))
                if source.value_slot is not None and source.value_type.kind == "STRUCT":
                    index = source.value_type.field_index(first)
                    if index is not None:
                        hits.append(Ref(source.value_slot, source.value_type, (index,)))
        aliases = [s for s in self.sources if s.name is not None and s.name.lower() == first]
        if hits and aliases:
            if len(parts) == 1 and len(aliases) == 1 and aliases[0].value_slot is not None:
                hits = []  # a value table's own alias
            else:
                raise Unsupported(f"{parts[0]} names both a column and a range variable")
        if len(hits) > 1:
            raise AnalysisError(f"Column name {parts[0]} is ambiguous")
        if hits:
            return hits[0]
        if len(aliases) > 1:
            raise AnalysisError(f"Range variable {parts[0]} is ambiguous")
        if not aliases:
            return None
        source = aliases[0]
        if source.value_slot is not None:
            return Ref(source.value_slot, source.value_type)
        if len(parts) == 1:
            fields = [(n, t) for n, _, t in source.cols]
            return Ref(-1, T.struct(fields), row_slots=tuple(s for _, s, _ in source.cols))
        second = parts[1].lower()
        found = [(slot, typ) for name, slot, typ in source.cols if name is not None and name.lower() == second]
        if len(found) > 1:
            raise AnalysisError(f"Column name {parts[1]} is ambiguous in {parts[0]}")
        if not found:
            raise AnalysisError(f"Name {parts[1]} not found inside {parts[0]}")
        return Ref(found[0][0], found[0][1], consumed=2)


def source_star(source: Source) -> list:
    if source.value_slot is not None:
        typ = source.value_type
        if typ.kind == "STRUCT":
            return [(name, source.value_slot, typ, (i,)) for i, (name, _) in enumerate(typ.fields)]  # typ: the slot's type
        return [(source.name, source.value_slot, typ, ())] + [(n, s, t, ()) for n, s, t in source.cols]
    return [(n, s, t, ()) for n, s, t in source.cols]


class GroupScope(Scope):
    """After GROUP BY: a name must match a grouping item (or a field of one)."""

    def __init__(self, base: FromScope, items: list, parent: Scope | None, compiler: "Compiler"):
        self.base = base
        self.items = items  # [(signature, slot, Type)]
        self.parent = parent
        self.compiler = compiler

    def lookup(self, parts: list[str]) -> Ref | None:
        ref = self.base.lookup(parts)
        if ref is None:
            return None
        if ref.row_slots is not None:
            raise Unsupported("a whole table row after GROUP BY")
        fields, typ = list(ref.fields), ref.type
        for name in parts[ref.consumed:]:
            if typ.kind != "STRUCT":
                break
            index = typ.field_index(name)
            if index is None:
                break
            fields.append(index)
            typ = typ.fields[index][1]
        consumed = ref.consumed + (len(fields) - len(ref.fields))
        target = ("col", id(self.base), ref.slot, tuple(fields))
        best = None
        for signature, slot, item_type in self.items:
            if not (isinstance(signature, tuple) and signature[:3] == target[:3]):
                continue
            prefix = signature[3]
            if tuple(fields[: len(prefix)]) == prefix and (best is None or len(prefix) > len(best[0])):
                best = (prefix, slot, item_type)
        if best is None:
            raise AnalysisError(f"{'.'.join(parts)} is neither grouped nor aggregated")
        prefix, slot, item_type = best
        return Ref(slot, item_type, tuple(fields[len(prefix):]), consumed)


class AliasScope(Scope):
    """ORDER BY / HAVING / QUALIFY names that are SELECT-list aliases (handled by AST substitution)."""


# ---------------------------------------------------------------------------------------------
# compile-time context
# ---------------------------------------------------------------------------------------------


@dataclass
class Cx:
    scope: Scope
    replace: dict = field(default_factory=dict)  # id(sqlglot node) -> E (aggregate and window results)
    group: Any = None  # GroupInfo while compiling after GROUP BY
    no_agg: str | None = "this clause"  # why aggregates are not allowed here (None: allowed via replace)
    in_agg: bool = False
    variables: dict = field(default_factory=dict)  # lexical GoogleSQL WITH expression variables, lowercased name -> E

    def with_scope(self, scope: Scope) -> "Cx":
        return Cx(scope, self.replace, self.group, self.no_agg, self.in_agg, self.variables)

    def with_variables(self, variables: dict) -> "Cx":
        return Cx(self.scope, self.replace, self.group, self.no_agg, self.in_agg, variables)


@dataclass
class GroupInfo:
    base: FromScope
    by_sig: dict  # signature -> E
    always: frozenset = frozenset()  # the signatures grouped by every grouping set
    by_node: dict = field(default_factory=dict)  # id(SELECT item node a GROUP BY ordinal or alias names) -> E

    def always_grouped(self, signature) -> bool:
        return signature in self.always


@dataclass
class Plan:
    """A compiled query: output columns and a function of the outer env returning its rows."""

    columns: list  # [(name or None, Type)]
    run: Callable[[Env], list]
    ordered: bool = False
    value_table: bool = False
    col_exprs: list | None = None  # the SELECT-list expressions (literal and NULL columns keep their literal-ness in set operations)

    @property
    def types(self) -> list[T.Type]:
        return [t for _, t in self.columns]


@dataclass
class CteEntry:
    id: int
    plan: Plan


_ids = itertools.count(1)


def _only(node: exp.Expression, *allowed: str) -> None:
    """Refuse a node carrying any argument the handler does not read."""

    for key, value in node.args.items():
        if key in allowed or value is None or value is False or value == [] or key in ("comments",):
            continue
        raise Unsupported(f"{type(node).__name__} with {key}")


def path_parts(node: exp.Expression) -> list[str] | None:
    """``a.b.c`` as names, for a Column or a Dot chain of identifiers; ``None`` for anything else."""

    if isinstance(node, exp.Column):
        if isinstance(node.this, exp.Star):
            return None
        parts = [p for p in (node.args.get("catalog"), node.args.get("db"), node.args.get("table")) if p is not None]
        names = []
        for p in parts + [node.this]:
            if not isinstance(p, exp.Identifier):
                return None
            names.append(p.name)
        return names
    if isinstance(node, exp.Dot):
        left = path_parts(node.this)
        if left is None or not isinstance(node.expression, exp.Identifier):
            return None
        return left + [node.expression.name]
    return None


def is_query(node: exp.Expression) -> bool:
    return isinstance(node, (exp.Select, exp.Union, exp.Intersect, exp.Except, exp.Subquery))


# ---------------------------------------------------------------------------------------------
# the compiler
# ---------------------------------------------------------------------------------------------


class Compiler:
    def __init__(self, database, tz, params: dict, mode: str, literals_decoded: bool, strict_certain: bool = False):
        self.strict_certain = strict_certain  # the text has STRICT CORRESPONDING and no BY NAME: a kindless by-name set operation is STRICT
        self.database = database
        self.tz = tz
        self.params = params
        self.mode = mode
        self.literals_decoded = literals_decoded
        self.ctes: dict = {}  # the CTEs visible to the clause being compiled (subqueries in expressions read it)

    # --- names -------------------------------------------------------------------------------

    def resolve(self, parts: list[str], scope: Scope) -> E | None:
        depth = 0
        current = scope
        while current is not None:
            ref = current.lookup(parts)
            if ref is not None:
                return self._ref_expr(ref, depth, parts)
            current = current.parent
            depth += 1
        return None

    def _ref_expr(self, ref: Ref, depth: int, parts: list[str]) -> E:
        if ref.row_slots is not None:
            slots = ref.row_slots
            getter = _row_getter(depth)
            fn = lambda env: (lambda row: tuple(row[s] for s in slots))(getter(env))  # noqa: E731
        else:
            fn = _slot_getter(depth, ref.slot)
        typ = ref.type
        if typ.foreign:
            raise Unsupported(f"column of GoogleSQL-only type {typ}")
        result = E(typ, fn)
        for index in ref.fields:
            result = _field(result, index)
        for name in parts[ref.consumed:]:
            result = self.field_access(result, name)
        return result

    def field_access(self, value: E, name: str) -> E:
        if value.type.kind != "STRUCT":
            if value.type.kind == "ARRAY":
                raise Unsupported("field access through an array")  # only UNNEST and FLATTEN take such a path (path_expr)
            raise AnalysisError(f"Cannot access field {name} on a value with type {value.type}")
        index = value.type.field_index(name)
        if index is None:
            raise AnalysisError(f"Field name {name} does not exist in {value.type}")
        return _field(value, index)

    # --- paths through arrays (UNNEST and FLATTEN arguments) ---------------------------------------------

    def path_expr(self, node: exp.Expression, cx: Cx) -> E:
        """Compile the argument of UNNEST or FLATTEN. A path ``root.f.g[OFFSET(1)].h`` may step through arrays of structs
        there: from the first array on, the remaining steps run on every element, and the results form one array
        (an array-valued result contributes its elements, nothing if NULL; a NULL element gives a NULL)."""

        steps: list = []
        while True:
            if isinstance(node, exp.Paren):
                node = node.this
            elif isinstance(node, exp.Dot) and isinstance(node.expression, exp.Identifier):
                steps.append(("field", node.expression.name))
                node = node.this
            elif isinstance(node, exp.Bracket):
                steps.append(("sub", node))
                node = node.this
            else:
                break
        steps.reverse()
        parts = path_parts(node)
        if parts is not None:
            root = None
            for k in range(1, len(parts) + 1):
                root = self.resolve(parts[:k], cx.scope)
                if root is not None:
                    steps = [("field", name) for name in parts[k:]] + steps
                    break
            if root is None:
                raise AnalysisError(f"Unrecognized name: {parts[0]}")
            root = self._checked(root)
        else:
            root = self.expr(node, cx)
        return self.path_steps(root, steps, cx) if steps else root

    def path_steps(self, root: E, steps: list, cx: Cx) -> E:
        t = root.type
        plan: list = []  # ("iter",) ("field", i) ("sub", index fn, base offset, safe)
        through_array = False
        for step in steps:
            if step[0] == "field":
                if t.kind == "ARRAY":
                    if t.elem.kind != "STRUCT":
                        raise AnalysisError(f"Cannot access field {step[1]} on a value with type {t}")
                    plan.append(("iter",))
                    through_array = True
                    t = t.elem
                if t.kind != "STRUCT":
                    raise AnalysisError(f"Cannot access field {step[1]} on a value with type {t}")
                index = t.field_index(step[1])
                if index is None:
                    raise AnalysisError(f"Field name {step[1]} does not exist in {t}")
                plan.append(("field", index))
                t = t.fields[index][1]
            else:
                node = step[1]
                _only(node, "this", "expressions", "offset", "safe", "returns_list_for_maps")
                if len(node.expressions) != 1:
                    raise Unsupported("subscript with several indexes")
                if t.kind != "ARRAY":
                    raise AnalysisError(f"Element access using [] is not supported on values of type {t}")
                position = self.expr(node.expressions[0], cx)
                if position.lit == "null":
                    position = self.coerce(position, T.INT64)
                if position.type != T.INT64:
                    raise AnalysisError(f"Array element access with array position of type {position.type} is not supported")
                plan.append(("sub", position.fn, node.args.get("offset") or 0, bool(node.args.get("safe"))))
                t = t.elem
        spread = through_array and t.kind == "ARRAY"
        if spread:
            plan.append(("spread",))
            result_type = t
        elif through_array:
            result_type = T.array(t)
        else:
            result_type = t
        root_fn = root.fn
        size = len(plan)

        def run(env):
            top = root_fn(env)
            if top is None and plan and plan[0][0] == "iter":
                return None
            out: list = []
            unordered = [False]

            def go(value, i):
                if i == size:
                    out.append(value)
                    return
                op = plan[i]
                kind = op[0]
                if kind == "iter":
                    if isinstance(value, V.UnorderedArray):
                        unordered[0] = True
                    for item in value or ():
                        go(item, i + 1)
                elif kind == "field":
                    go(None if value is None else value[op[1]], i + 1)
                elif kind == "sub":
                    position = op[1](env)
                    if value is None or position is None:
                        go(None, i + 1)
                        return
                    k = position - op[2]
                    if k < 0 or k >= len(value):
                        if not op[3]:
                            raise EvalError(f"Array index {position} is out of bounds")
                        go(None, i + 1)
                        return
                    if not V.ordered_kind(value):
                        env.ctx.nondet("element of an unordered array")
                    go(value[k], i + 1)
                else:  # spread
                    if isinstance(value, V.UnorderedArray):
                        unordered[0] = True
                    out.extend(value or ())

            go(top, 0)
            if not through_array:
                return out[0]
            return V.UnorderedArray(out) if unordered[0] else tuple(out)

        return E(result_type, run)

    # --- signatures for GROUP BY matching ---------------------------------------------------

    def signature(self, node: exp.Expression, scope: Scope) -> Any:
        parts = path_parts(node)
        if parts is not None:
            depth = 0
            current = scope
            while current is not None:
                try:
                    ref = current.lookup(parts)
                except (AnalysisError, Unsupported):
                    return ("unresolved", tuple(parts))
                if ref is not None:
                    fields, typ = list(ref.fields), ref.type
                    for name in parts[ref.consumed:]:
                        if typ.kind != "STRUCT":
                            return ("unresolved", tuple(parts))
                        index = typ.field_index(name)
                        if index is None:
                            return ("unresolved", tuple(parts))
                        fields.append(index)
                        typ = typ.fields[index][1]
                    if ref.row_slots is not None:
                        return ("row", depth, id(current), ref.row_slots)
                    if depth == 0 and isinstance(current, FromScope):
                        return ("col", id(current), ref.slot, tuple(fields))
                    return ("outer", depth, id(current), ref.slot, tuple(fields))
                current = current.parent
                depth += 1
            return ("unresolved", tuple(parts))
        if is_query(node) or isinstance(node, exp.Exists):
            return ("query", id(node))
        if isinstance(node, (exp.Rand,) + tuple(getattr(exp, n) for n in ("Uuid", "CurrentTimestamp") if hasattr(exp, n))):
            return ("volatile", id(node))
        if isinstance(node, exp.Paren):
            return self.signature(node.this, scope)
        items = []
        for key in sorted(node.args):
            value = node.args[key]
            if key in ("comments",) or value is None or value is False:
                continue
            if isinstance(value, exp.Expression):
                items.append((key, self.signature(value, scope)))
            elif isinstance(value, list):
                items.append((key, tuple(self.signature(v, scope) if isinstance(v, exp.Expression) else repr(v) for v in value)))
            else:
                if isinstance(node, exp.Identifier) and key == "quoted":
                    continue
                items.append((key, repr(value).lower() if isinstance(node, exp.Identifier) else repr(value)))
        if isinstance(node, exp.Anonymous):
            return ("call", str(node.this).upper(), tuple(items))
        return (type(node).__name__, tuple(items))

    # --- expressions -------------------------------------------------------------------------

    def expr(self, node: exp.Expression, cx: Cx) -> E:
        replaced = cx.replace.get(id(node))
        if replaced is not None:
            return replaced
        if cx.group is not None and not isinstance(node, (exp.Null, exp.Boolean, exp.Star, exp.Column, exp.Paren)):
            found = self._group_key_match(node, cx.group)
            if found is not None:
                return found
        handler = _HANDLERS.get(type(node))
        if handler is not None:
            return self._checked(handler(self, node, cx))
        from . import aggregates, functions

        if aggregates.is_aggregate(node) or isinstance(node, exp.Window):
            if cx.in_agg:
                raise AnalysisError("Aggregations of aggregations are not allowed")
            raise AnalysisError(f"Aggregate function not allowed in {cx.no_agg or 'this context'}")
        return self._checked(functions.compile_call(self, node, cx))

    def _group_key_match(self, node: exp.Expression, group: "GroupInfo") -> E | None:
        """The grouping key a post-GROUP BY expression stands for, or None.

        An expression whose every column is itself a grouping key is computed from the keys (NULL in the sets that do not
        group them): with ``GROUPING SETS (a, a + 1)``, ``a + 1`` is ``a + 1`` over the key ``a``, not the key ``a + 1``.
        An expression with a column that is not a key is that key when it is written like one (``GROUP BY a + b``). The
        SELECT item a GROUP BY ordinal or alias names (``GROUP BY 2``) is the key itself.
        """

        direct = group.by_node.get(id(node))
        if direct is not None:
            return direct
        signature = self.signature(node, group.base)
        found = group.by_sig.get(signature)
        if found is None:
            return None
        if node.find(exp.Column) is None and not group.always_grouped(signature):
            # a constant expression equal to a key some grouping sets leave out: GoogleSQL's rule for it is not established
            raise Unsupported("a constant expression equal to a GROUP BY expression some grouping sets leave out")
        if self._from_keys(node, group):
            return None
        return found

    def _from_keys(self, node: exp.Expression, group: "GroupInfo") -> bool:
        """Whether every column ``node`` reads (outside subqueries) is a grouping key, or sits in an operand that is one."""

        for child in node.iter_expressions():
            while isinstance(child, exp.Paren):
                child = child.this
            if is_query(child) or isinstance(child, (exp.Null, exp.Boolean, exp.Star)):
                continue
            sig = self.signature(child, group.base)
            if isinstance(child, exp.Literal):
                if sig in group.by_sig and not group.always_grouped(sig):
                    raise Unsupported("a constant that equals a GROUP BY constant some grouping sets leave out")
                continue
            if id(child) in group.by_node:
                continue
            if isinstance(child, exp.Column) or (isinstance(child, exp.Dot) and path_parts(child) is not None):
                if not (isinstance(sig, tuple) and sig[:1] == ("col",) and any(
                    isinstance(s, tuple) and s[:3] == sig[:3] and tuple(sig[3][: len(s[3])]) == tuple(s[3]) for s in group.by_sig
                )):
                    return False
                continue
            if sig in group.by_sig:
                continue
            if not self._from_keys(child, group):
                return False
        return True

    def _checked(self, value: E) -> E:
        if self.mode == "bigquery" and value.type.nested_array:
            raise AnalysisError("Arrays of arrays are not supported")
        return value

    def exprs(self, nodes, cx: Cx) -> list[E]:
        return [self.expr(n, cx) for n in nodes]

    # --- coercion ------------------------------------------------------------------------------

    def coerce(self, value: E, target: T.Type, what: str = "") -> E:
        if value.type == target:
            return value
        if value.lit == "null":
            return E(target, value.fn, "null", None)
        if value.lit == "literal" and value.type.kind == "ARRAY" and value.value == () and target.kind == "ARRAY":
            return const(target, ())  # the literal []
        if value.lit == "literal" and value.value is not None:
            source = value.type
            if source.kind == "FLOAT64" and target.kind in ("NUMERIC", "BIGNUMERIC"):
                try:
                    if value.exact is not None:
                        return const(target, V.decimal_of(target.kind)(value.exact))
                    return const(target, V.caster(source, target)(value.value, self.tz))
                except EvalError:
                    raise Unsupported(f"FLOAT64 literal that does not convert to {target}") from None
            if source.kind == "STRING" and target.kind in ("DATE", "DATETIME", "TIME", "TIMESTAMP", "BYTES") or (
                T.implicitly_coercible(source, target)
            ):
                if source.kind == "STRING" and target.kind == "BYTES":
                    raise AnalysisError("STRING literal does not coerce to BYTES in BigQuery")
                try:
                    converted = V.caster(source, target)(value.value, self.tz)
                except EvalError as error:
                    raise AnalysisError(f"Could not cast literal to {target}: {error}") from None
                return const(target, converted)
        if value.sub is not None and value.type.kind == "STRUCT" and T.coercible_with_info(value.type, target, value.info):
            convert = _struct_converter(value.type, target, value.info)
            fn = value.fn
            return E(target, lambda env: (lambda v: None if v is None else convert(v, env.ctx.tz))(fn(env)))
        if not T.implicitly_coercible(value.type, target):
            raise AnalysisError(f"{what or 'Value'} of type {value.type} does not coerce to {target}")
        convert = V.caster(value.type, target)
        fn = value.fn
        return E(target, lambda env: (lambda v: None if v is None else convert(v, env.ctx.tz))(fn(env)), value.lit if value.lit == "literal" and value.value is None else None)

    def unify(self, values: list[E], what: str = "") -> tuple[T.Type, list[E]]:
        target = T.supertype([(v.type, v.info) for v in values])
        return target, [self.coerce(v, target, what) for v in values]

    # --- queries -------------------------------------------------------------------------------

    def query(self, node: exp.Expression, scope: Scope, ctes: dict) -> Plan:
        saved = self.ctes
        try:
            return self._query(node, scope, ctes)
        finally:
            self.ctes = saved

    def _query(self, node: exp.Expression, scope: Scope, ctes: dict) -> Plan:
        self.ctes = ctes
        if isinstance(node, exp.Subquery):
            _only(node, "this", "order", "limit", "offset", "alias", "with_")
            if node.args.get("alias") is not None:
                raise Unsupported("aliased parenthesized query")
            ctes, entries = self._with(node, scope, ctes)
            inner = self.query(node.this, scope, ctes)
            self.ctes = ctes
            plan = self._order_limit(node, inner, scope, ctes)
            return self._wrap_with(entries, plan)
        if isinstance(node, exp.Select):
            ctes2, entries = self._with(node, scope, ctes)
            self.ctes = ctes2
            return self._wrap_with(entries, self.select(node, scope, ctes2))
        if isinstance(node, (exp.Union, exp.Intersect, exp.Except)):
            ctes2, entries = self._with(node, scope, ctes)
            plan = self.set_operation(node, scope, ctes2)
            self.ctes = ctes2
            plan = self._order_limit(node, plan, scope, ctes2)
            return self._wrap_with(entries, plan)
        raise Unsupported(f"query {type(node).__name__}")

    # WITH: CTE plans are compiled in order; at run time each is computed once per activation of the query.

    def _with(self, node: exp.Expression, scope: Scope, ctes: dict) -> tuple[dict, list]:
        with_ = node.args.get("with_") or node.args.get("with")
        if with_ is None:
            return ctes, []
        _only(with_, "expressions", "recursive")
        recursive = bool(with_.args.get("recursive"))
        ctes = dict(ctes)
        entries = []
        definitions = list(with_.expressions)
        if recursive:
            definitions = _dependency_order(definitions)
        for cte in definitions:
            _only(cte, "this", "alias")
            alias = cte.args.get("alias")
            if alias is None or alias.args.get("columns"):
                raise Unsupported("CTE column list")
            name = alias.name.lower()
            if name in [e[0] for e in entries]:
                raise AnalysisError(f"Duplicate alias {alias.name} for WITH subquery")
            entry_id = next(_ids)
            if recursive and _references(cte.this, name):
                plan = self._recursive_cte(cte.this, name, entry_id, scope, ctes)
            else:
                plan = self.query(cte.this, scope, ctes)
            ctes[name] = CteEntry(entry_id, plan)
            entries.append((name, entry_id, plan))
        return ctes, entries

    def _wrap_with(self, entries: list, plan: Plan) -> Plan:
        if not entries:
            return plan
        body = plan.run

        def run(env: Env) -> list:
            local = dict(env.ctes)
            inner = Env(env.row, env.outer, env.ctx, local)
            for _, entry_id, cte_plan in entries:
                local[entry_id] = cte_plan.run(inner)
            return body(inner)

        return Plan(plan.columns, run, plan.ordered, plan.value_table, plan.col_exprs)

    def _recursive_cte(self, node: exp.Expression, name: str, entry_id: int, scope: Scope, ctes: dict) -> Plan:
        if not isinstance(node, exp.Union) or node.args.get("by_name") or node.args.get("side") or node.args.get("kind"):
            raise Unsupported("recursive CTE that is not base UNION recursive")
        _only(node, "this", "expression", "distinct")
        base_node, step_node = node.this, node.expression
        if _references(base_node, name):
            raise AnalysisError("The base term of a recursive query must not reference it")
        base = self.query(base_node, scope, ctes)
        temp = dict(ctes)
        temp[name] = CteEntry(entry_id, base)
        step = self.query(step_node, scope, temp)
        if len(step.columns) != len(base.columns):
            raise AnalysisError("Recursive query branches have different numbers of columns")
        types = []
        for (_, a), (_, b) in zip(base.columns, step.columns):
            if a != b:
                if T.implicitly_coercible(b, a):
                    types.append(a)
                    continue
                raise Unsupported("recursive CTE whose terms have different types")
            types.append(a)
        converters = _converters(step.types, types)
        distinct = bool(node.args.get("distinct"))
        key_types = types

        def run(env: Env) -> list:
            rows = list(base.run(env))
            seen = set()
            if distinct:
                unique = []
                for row in rows:
                    key = V.row_key(key_types, row)
                    if key not in seen:
                        seen.add(key)
                        unique.append(row)
                rows = unique
            result = list(rows)
            working = rows
            iterations = 0
            local = dict(env.ctes)
            inner = Env(env.row, env.outer, env.ctx, local)
            first = True
            while working or first:  # the recursive term runs at least once, even over an empty non-recursive term
                first = False
                iterations += 1
                if iterations > env.ctx.max_recursion:
                    raise EvalError("Recursive query exceeded the maximum number of iterations")
                local[entry_id] = working
                produced = [_convert_row(converters, r, env.ctx.tz) for r in step.run(inner)]
                if distinct:
                    fresh = []
                    for row in produced:
                        key = V.row_key(key_types, row)
                        if key not in seen:
                            seen.add(key)
                            fresh.append(row)
                    produced = fresh
                result.extend(produced)
                working = produced
                if len(result) > 1_000_000:
                    raise Unsupported("recursive query too large")
            return result

        return Plan(list(zip([n for n, _ in base.columns], types)), run)

    # --- ORDER BY / LIMIT on a query (set operation or parenthesized query) ---------------------

    def _order_limit(self, node: exp.Expression, plan: Plan, scope: Scope, ctes: dict) -> Plan:
        order = node.args.get("order")
        limit = node.args.get("limit")
        offset = node.args.get("offset")
        if order is None and limit is None and offset is None:
            return plan
        keys = []
        if order is not None:
            _only(order, "expressions")
            out_scope = FromScope([Source(None, [(n, i, t) for i, (n, t) in enumerate(plan.columns)])], len(plan.columns), scope)
            cx = Cx(out_scope)
            for item in order.expressions:
                keys.append(self._order_key(item, cx, plan.columns, None))
        limit_fn = self._limit(limit, offset)
        inner = plan.run
        width = len(plan.columns)

        def run(env: Env) -> list:
            rows = inner(env)
            if keys:
                extended = [(row, tuple(k.fn(env.child(row)) for k, _, _ in keys)) for row in rows]
                extended = _sort(extended, [(k.type, desc, nulls_first) for k, desc, nulls_first in keys], env.ctx)
                rows = [r for r, _ in extended]
                if limit_fn is not None:
                    return limit_fn(rows, env, [k for _, k in extended])
                return rows
            if limit_fn is not None:
                return limit_fn(rows, env, None)
            return rows

        return Plan(plan.columns, run, bool(keys), plan.value_table, plan.col_exprs)

    def _order_key(self, item: exp.Expression, cx: Cx, columns: list, select_items) -> tuple:
        if not isinstance(item, exp.Ordered):
            item = exp.Ordered(this=item)
        _only(item, "this", "desc", "nulls_first")
        desc = bool(item.args.get("desc"))
        nulls_first = item.args.get("nulls_first")
        if nulls_first is None:
            nulls_first = not desc
        target = item.this
        if isinstance(target, exp.Literal) and not target.is_string:
            if not str(target.this).isdigit():
                raise Unsupported("ORDER BY a numeric literal that is not an integer")
            position = int(target.this)
            if not 1 <= position <= len(columns):
                raise AnalysisError(f"ORDER BY column number exceeds input table column count: {position}")
            key = E(columns[position - 1][1], _slot_getter(0, position - 1))
        else:
            key = self.expr(target, cx)
        if not T.comparable(key.type):
            raise AnalysisError(f"ORDER BY does not support expressions of type {key.type}")
        return key, desc, bool(nulls_first)

    def _limit(self, limit, offset):
        if limit is None and offset is None:
            return None
        count = None
        skip = 0
        if limit is not None:
            _only(limit, "expression", "offset")
            count = self._constant_int(limit.expression, "LIMIT")
            if limit.args.get("offset") is not None:
                skip = self._constant_int(limit.args["offset"], "OFFSET")
        if offset is not None:
            _only(offset, "expression")
            skip = self._constant_int(offset.expression, "OFFSET")

        def apply(rows: list, env: Env, sort_keys) -> list:
            n = len(rows)
            end = n if count is None else min(n, skip + count)
            if skip >= n or end <= skip:
                kept = []
            else:
                kept = rows[skip:end]
            if (skip > 0 or end < n) and n > 0:
                if not _cut_is_determined(rows, skip, end, sort_keys):
                    env.ctx.nondet("LIMIT over rows of undetermined order")
            return kept

        return apply

    def _constant_int(self, node: exp.Expression, what: str) -> int:
        value = self.expr(node, Cx(EmptyScope()))
        if value.lit is None and not isinstance(node, (exp.Cast,)):
            raise AnalysisError(f"{what} expects an integer literal or parameter")
        if value.type != T.INT64:
            raise AnalysisError(f"{what} expects an integer")
        result = value.fn(None) if value.lit else None
        if value.lit is None:
            raise Unsupported(f"{what} expression")
        if result is None:
            raise AnalysisError(f"{what} must not be NULL")
        if result < 0:
            raise AnalysisError(f"{what} expects a non-negative integer")
        return result

    # --- set operations --------------------------------------------------------------------------

    def set_operation(self, node: exp.Expression, scope: Scope, ctes: dict) -> Plan:
        _only(node, "this", "expression", "distinct", "by_name", "side", "kind", "on", "with_", "order", "limit", "offset")
        operation = type(node).__name__.upper()
        distinct = node.args.get("distinct")
        if distinct is None:
            raise AnalysisError("Set operation needs ALL or DISTINCT")
        # a chain `a UNION ALL b UNION ALL c` is one operation over three inputs: their column types unify together
        chain = [node]
        current = node.this
        while type(current) is type(node) and _same_set_mode(current, node):
            chain.insert(0, current)
            current = current.this
        operands = [current] + [link.expression for link in chain]
        plans = [self.query(operand, scope, ctes) for operand in operands]
        if any(p.value_table for p in plans):
            if not all(p.value_table for p in plans) or node.args.get("by_name"):
                raise Unsupported("set operation over value tables")
        if node.args.get("by_name"):
            outputs, positions = self._by_name_layout(node, plans, operation)
        else:
            if node.args.get("side") or node.args.get("kind") or node.args.get("on"):
                raise Unsupported("set operation mode")
            width = len(plans[0].columns)
            for p in plans[1:]:
                if len(p.columns) != width:
                    raise AnalysisError(f"Queries in {operation} have mismatched column count")
            outputs = [plans[0].columns[i][0] for i in range(width)]
            positions = [list(range(width)) for _ in plans]
        types = []
        for j in range(len(outputs)):
            members = [(p.columns[pos[j]][1], _column_info(p, pos[j])) for p, pos in zip(plans, positions) if pos[j] is not None]
            types.append(T.supertype(members))
        for t in types:
            if (distinct or operation != "UNION") and not T.groupable(t):
                raise AnalysisError(f"Column of type {t} cannot be used in {operation} DISTINCT")
        aligned = [self._align(p, pos, types) for p, pos in zip(plans, positions)]
        plan = self._set_plan(aligned, list(zip(outputs, types)), operation, bool(distinct))
        if plans[0].value_table:
            plan.value_table = True
        else:
            plan.col_exprs = [_merged_column(plans, positions, j) for j in range(len(outputs))]
        return plan

    def _by_name_layout(self, node, plans: list[Plan], operation: str):
        """The output column names of ``... BY NAME`` / ``CORRESPONDING`` and, per input, where each one sits (or ``None``)."""

        side = node.args.get("side")
        kind = node.args.get("kind")
        side = side.upper() if isinstance(side, str) else None
        kind = kind.upper() if isinstance(kind, str) else None
        if kind == "OUTER" and side is None:
            raise Unsupported("OUTER set operation without a side")
        if side is not None and kind not in (None, "OUTER"):
            raise Unsupported("set operation mode")
        mode = side or kind or "STRICT"
        by_list = None
        if node.args.get("on"):
            by_list = []
            for item in node.args["on"]:
                parts = path_parts(item)
                if parts is None or len(parts) != 1:
                    raise Unsupported("CORRESPONDING BY item that is not a column name")
                by_list.append(parts[0])
            if len({n.lower() for n in by_list}) != len(by_list):
                raise Unsupported("CORRESPONDING BY with a repeated column")
        lowered = []  # per input: name -> positions
        for p in plans:
            index: dict = {}
            for i, (name, _) in enumerate(p.columns):
                if name is not None:
                    index.setdefault(name.lower(), []).append(i)
            lowered.append(index)

        def unique(k: int, name: str):
            hits = lowered[k].get(name.lower())
            if hits is None:
                return None
            if len(hits) > 1:
                raise Unsupported(f"column {name} repeated in a set operation by name")
            return hits[0]

        def uncertain(why: str):
            if mode == "STRICT" and self.strict_certain:
                raise AnalysisError(f"STRICT set operation: {why}")
            raise Unsupported(f"set operation by name: {why}")

        if by_list is None and any(n is None for p in plans for n, _ in p.columns):
            raise Unsupported("set operation by name over unnamed columns")
        if mode == "STRICT":
            if by_list is None:
                names = [n for n, _ in plans[0].columns]
                for p in plans[1:]:
                    if {n.lower() for n, _ in p.columns} != {n.lower() for n in names}:
                        uncertain("the inputs have different column names")
            else:
                names = by_list
                for k, p in enumerate(plans):
                    if {n.lower() for n, _ in p.columns if n is not None} != {n.lower() for n in names} or len(p.columns) != len(names):
                        uncertain("the inputs' columns differ from the BY list")
        elif by_list is not None:
            names = by_list
            for k in range(len(plans)):
                missing = [n for n in names if n.lower() not in lowered[k]]
                if mode == "INNER" and missing:
                    raise Unsupported("CORRESPONDING BY column missing from an input")
                if mode == "LEFT" and k == 0 and missing:
                    raise Unsupported("CORRESPONDING BY column missing from the left input")
            if mode == "FULL" and any(all(n.lower() not in lowered[k] for k in range(len(plans))) for n in names):
                raise Unsupported("CORRESPONDING BY column missing from every input")
        elif mode == "INNER":
            names = [n for n, _ in plans[0].columns if all(n.lower() in lowered[k] for k in range(1, len(plans)))]
        elif mode == "LEFT":
            names = [n for n, _ in plans[0].columns]
        else:  # FULL
            names = []
            seen: set = set()
            for p in plans:
                for n, _ in p.columns:
                    if n.lower() not in seen:
                        seen.add(n.lower())
                        names.append(n)
        if not names:
            raise Unsupported("set operation by name with no output columns")
        positions = [[unique(k, n) for n in names] for k in range(len(plans))]
        return names, positions

    def _align(self, plan: Plan, positions: list, types: list[T.Type]):
        """A function giving the plan's rows in the output's column order and types."""

        converters = []
        for pos, target in zip(positions, types):
            if pos is None:
                converters.append(None)
                continue
            source = plan.columns[pos][1]
            item = plan.col_exprs[pos] if plan.col_exprs is not None else None
            if source == target or (item is not None and item.lit == "null"):
                converters.append(None)
            elif item is not None and item.lit == "literal" and item.value is not None:
                folded = self.coerce(item, target).value
                converters.append(lambda v, tz, folded=folded: folded)
            elif item is not None and item.sub is not None and source.kind == "STRUCT":
                if not T.coercible_with_info(source, target, item.info):
                    raise AnalysisError(f"Value of type {source} does not coerce to {target}")
                converters.append(_struct_converter(source, target, item.info))
            else:
                if not T.implicitly_coercible(source, target):
                    raise AnalysisError(f"Value of type {source} does not coerce to {target}")
                converters.append(V.caster(source, target))
        run = plan.run
        identity = all(p == i for i, p in enumerate(positions)) and len(positions) == len(plan.columns)

        def rows(env: Env) -> list:
            tz = env.ctx.tz
            out = []
            for row in run(env):
                if not identity:
                    row = tuple(None if p is None else row[p] for p in positions)
                out.append(_convert_row(converters, row, tz))
            return out

        return rows

    def _set_plan(self, aligned: list, columns: list, operation: str, distinct: bool) -> Plan:
        types = [t for _, t in columns]

        def combine(a: list, b: list) -> list:
            if operation == "UNION":
                rows = a + b
                return _dedupe(rows, types) if distinct else rows
            counts_b: dict = {}
            for row in b:
                key = V.row_key(types, row)
                counts_b[key] = counts_b.get(key, 0) + 1
            out = []
            if distinct:
                seen = set()
                for row in a:
                    key = V.row_key(types, row)
                    if key in seen:
                        continue
                    present = counts_b.get(key, 0) > 0
                    if (operation == "INTERSECT") == present:
                        seen.add(key)
                        out.append(row)
                return out
            used: dict = {}
            for row in a:
                key = V.row_key(types, row)
                k = used.get(key, 0)
                used[key] = k + 1
                if operation == "INTERSECT":
                    if k < counts_b.get(key, 0):
                        out.append(row)
                else:
                    if k >= counts_b.get(key, 0):
                        out.append(row)
            return out

        def run(env: Env) -> list:
            rows = aligned[0](env)
            for part in aligned[1:]:
                rows = combine(rows, part(env))
            return rows

        return Plan(columns, run)

    # --- SELECT ------------------------------------------------------------------------------------

    def select(self, node: exp.Select, scope: Scope, ctes: dict) -> Plan:
        _only(node, "expressions", "from_", "from", "joins", "where", "group", "having", "qualify", "order", "limit",
              "offset", "distinct", "kind", "windows", "with_", "laterals")
        if node.args.get("laterals"):
            raise Unsupported("LATERAL VIEW")
        from_node = node.args.get("from_") or node.args.get("from")
        if from_node is not None:
            _only(from_node, "this")
            from_scope, from_run = self.from_clause(from_node.this, node.args.get("joins") or [], scope, ctes)
        else:
            if node.args.get("joins"):
                raise AnalysisError("JOIN without FROM")
            from_scope = FromScope([], 0, scope)
            from_run = lambda env: [()]  # noqa: E731
        distinct_node = node.args.get("distinct")
        if distinct_node is not None:
            _only(distinct_node, "on")
            if distinct_node.args.get("on") is not None:
                raise Unsupported("DISTINCT ON")
        kind = node.args.get("kind")
        if kind is not None and str(kind).upper() not in ("STRUCT", "VALUE"):
            raise Unsupported(f"SELECT AS {kind}")
        windows = {}
        for definition in node.args.get("windows") or []:
            windows[definition.name.lower()] = definition
        # SELECT-list items: expand stars; keep (name, node) pairs
        items = self._select_items(node.expressions, from_scope)
        alias_nodes = {}
        for name, item_node, _ in items:
            if name is not None and item_node is not None:
                alias_nodes.setdefault(name.lower(), []).append(item_node)
        where = node.args.get("where")
        where_e = None
        if where is not None:
            where_e = self._bool(self.expr(where.this, Cx(from_scope, no_agg="WHERE")), "WHERE")
        having = node.args.get("having")
        qualify = node.args.get("qualify")
        order = node.args.get("order")
        having_node = self._substitute_aliases(having.this, alias_nodes, from_scope) if having is not None else None
        qualify_node = self._substitute_aliases(qualify.this, alias_nodes, from_scope) if qualify is not None else None
        order_items = []
        if order is not None:
            _only(order, "expressions")
            for item in order.expressions:
                order_items.append(item)
        # find this level's aggregates and window functions
        from . import aggregates, windows as W

        aggs: list = []
        wins: list = []
        roots = [n for _, n, _ in items if n is not None]
        roots += [n for n in (having_node, qualify_node) if n is not None]
        order_roots = []
        for item in order_items:
            target = item.this if isinstance(item, exp.Ordered) else item
            substituted = self._substitute_order(target, alias_nodes, from_scope, items)
            order_roots.append(substituted)
        roots += [r for r in order_roots if isinstance(r, exp.Expression)]
        for root in roots:
            _collect(root, aggs, wins, True)
        group = node.args.get("group")
        aggregating = group is not None or bool(aggs) or having_node is not None
        replace: dict = {}
        group_info = None
        final_scope: Scope = from_scope
        group_stage = None
        if aggregating:
            group_stage, final_scope, group_info = self._group_stage(node, group, items, aggs, from_scope, scope, replace)
        base_width = from_scope.width if not aggregating else group_stage.width
        cx_final = Cx(final_scope, replace, group_info, no_agg=None)
        having_e = None
        if having_node is not None:
            having_e = self._bool(self._alias_guard(lambda: self.expr(having_node, cx_final), alias_nodes, having_node), "HAVING")
        # windows: computed on the rows after HAVING, appended to each row
        window_stage = None
        if wins:
            specs = []
            for i, window_node in enumerate(wins):
                specs.append(W.compile_window(self, window_node, cx_final, windows))
                replace[id(window_node)] = E(specs[-1].type, _slot_getter(0, base_width + i))
            window_stage = specs
        cx_post = Cx(final_scope, replace, group_info, no_agg=None)
        qualify_e = None
        if qualify_node is not None:
            if not wins:
                raise AnalysisError("QUALIFY needs a window function")
            qualify_e = self._bool(self._alias_guard(lambda: self.expr(qualify_node, cx_post), alias_nodes, qualify_node), "QUALIFY")
        # SELECT list
        out_exprs = []
        out_columns = []
        for name, item_node, star_ref in items:
            if star_ref is not None:
                slot, typ, fields = star_ref
                if aggregating:
                    value = self._star_after_group(slot, typ, fields, from_scope, final_scope)
                else:
                    value = E(typ, _slot_getter(0, slot))
                    for index in fields:
                        value = _field(value, index)
                if value.type.foreign:
                    raise Unsupported(f"column of GoogleSQL-only type {value.type}")
            else:
                value = self.expr(item_node, cx_post)
            out_exprs.append(value)
            out_columns.append((name, value.type))
        value_table = False
        item_columns = out_columns  # ORDER BY an output name or position means a SELECT-list item, also under AS STRUCT
        as_struct = kind is not None and str(kind).upper() == "STRUCT"
        if kind is not None and str(kind).upper() == "STRUCT":
            struct_type = T.struct([(n, t) for n, t in out_columns])
            parts = list(out_exprs)
            out_exprs = [E(struct_type, lambda env, parts=parts: tuple(p.fn(env) for p in parts))]
            out_columns = [(None, struct_type)]
            value_table = True
        elif kind is not None and str(kind).upper() == "VALUE":
            if len(out_exprs) != 1:
                raise AnalysisError("SELECT AS VALUE needs exactly one column")
            out_columns = [(None, out_exprs[0].type)]
            value_table = True
        is_distinct = distinct_node is not None
        if is_distinct:
            for _, t in out_columns:
                if not T.groupable(t):
                    raise AnalysisError(f"Column of type {t} cannot be used in SELECT DISTINCT")
        # ORDER BY keys
        order_keys = []
        if order_items:
            out_scope = FromScope([Source(None, [(n, i, t) for i, (n, t) in enumerate(out_columns)])], len(out_columns), scope)
            for item, substituted in zip(order_items, order_roots):
                ordered = item if isinstance(item, exp.Ordered) else exp.Ordered(this=item)
                if isinstance(substituted, int):  # an output column position
                    desc = bool(ordered.args.get("desc"))
                    nulls_first = ordered.args.get("nulls_first")
                    nulls_first = (not desc) if nulls_first is None else bool(nulls_first)
                    _only(ordered, "this", "desc", "nulls_first")
                    position = substituted
                    if as_struct:
                        key = E(item_columns[position][1], (lambda p: lambda env: env.row[0][p])(position))
                    else:
                        key = E(out_columns[position][1], _out_getter(position))
                    if not T.comparable(key.type):
                        raise AnalysisError(f"ORDER BY does not support expressions of type {key.type}")
                    order_keys.append((key, desc, nulls_first, True))
                    continue
                if is_distinct:
                    raise Unsupported("ORDER BY an expression not in the SELECT DISTINCT list")
                replacement = exp.Ordered(this=substituted, desc=ordered.args.get("desc"), nulls_first=ordered.args.get("nulls_first"))
                key, desc, nulls_first = self._alias_guard(
                    lambda: self._order_key(replacement, cx_post, out_columns, None), alias_nodes, substituted
                )
                order_keys.append((key, desc, nulls_first, False))
        limit_fn = self._limit(node.args.get("limit"), node.args.get("offset"))
        n_out = len(out_columns)
        is_value = kind is not None and str(kind).upper() == "VALUE"
        out_types = [t for _, t in out_columns]
        sort_spec = [(k.type, desc, nf) for k, desc, nf, _ in order_keys]

        def run(env: Env) -> list:
            ctx = env.ctx
            rows = from_run(env)
            if where_e is not None:
                fn = where_e.fn
                rows = [r for r in rows if fn(Env(r, env, ctx, env.ctes)) is True]
            if group_stage is not None:
                rows = group_stage.run(rows, env)
            if having_e is not None:
                fn = having_e.fn
                rows = [r for r in rows if fn(Env(r, env, ctx, env.ctes)) is True]
            if window_stage is not None:
                rows = W.apply_windows(window_stage, rows, env)
            if qualify_e is not None:
                fn = qualify_e.fn
                rows = [r for r in rows if fn(Env(r, env, ctx, env.ctes)) is True]
            out = []
            for r in rows:
                row_env = Env(r, env, ctx, env.ctes)
                values = tuple(e.fn(row_env) for e in out_exprs)
                if order_keys:
                    keys = tuple(
                        (k.fn(Env(values, env, ctx, env.ctes)) if on_output else k.fn(row_env)) for k, _, _, on_output in order_keys
                    )
                    out.append((values, keys))
                else:
                    out.append((values, ()))
            if is_distinct:
                seen = set()
                unique = []
                for values, keys in out:
                    key = V.row_key(out_types, values)
                    if key not in seen:
                        seen.add(key)
                        unique.append((values, keys))
                out = unique
            if order_keys:
                out = _sort(out, sort_spec, ctx)
            if limit_fn is not None:
                rows_out = [v for v, _ in out]
                return limit_fn(rows_out, env, [k for _, k in out] if order_keys else None)
            return [v for v, _ in out]

        plan = Plan(out_columns, run, bool(order_keys), value_table, None if value_table else out_exprs)
        return plan

    @staticmethod
    def _alias_guard(compile_clause, alias_nodes: dict, node: exp.Expression):
        """Run ``compile_clause``. A SELECT alias used inside a subquery of HAVING, QUALIFY or ORDER BY is not substituted
        there (``_substitute_aliases`` stays out of subqueries); a name the subquery then cannot find is declined, not
        reported as unknown."""

        try:
            return compile_clause()
        except AnalysisError as error:
            message = str(error)
            if message.startswith("Unrecognized name: "):
                name = message[len("Unrecognized name: "):].strip().lower()
                if name in alias_nodes and any(
                    isinstance(c, exp.Column) and not c.args.get("table") and c.name.lower() == name
                    for q in node.find_all(exp.Subquery, exp.Select) for c in q.find_all(exp.Column)
                ):
                    raise Unsupported("a SELECT alias used inside a subquery of HAVING, QUALIFY or ORDER BY") from None
            raise

    def _bool(self, value: E, clause: str) -> E:
        if value.lit == "null":
            return E(T.BOOL, value.fn)
        if value.type != T.BOOL:
            raise AnalysisError(f"{clause} clause should return type BOOL, but returns {value.type}")
        return value

    def _select_items(self, expressions, from_scope: FromScope):
        """[(output name, node or None, (slot, type, fields) for a star column or None)]."""

        items = []
        for item in expressions:
            if isinstance(item, exp.Star):
                _only(item, "except_", "except", "replace", "rename")
                if item.args.get("rename"):
                    raise Unsupported("SELECT * RENAME")
                columns = from_scope.star()
                if not columns:
                    raise AnalysisError("SELECT * would expand to zero columns")
                items.extend(self._star_items(columns, item, from_scope))
                continue
            if isinstance(item, exp.Column) and isinstance(item.this, exp.Star):
                star = item.this
                _only(star, "except_", "except", "replace", "rename")
                parts = path_parts(exp.Column(this=item.args["table"], table=item.args.get("db"), db=item.args.get("catalog")))
                columns = self._dot_star(parts, from_scope)
                items.extend(self._star_items(columns, star, from_scope))
                continue
            if isinstance(item, exp.Dot) and isinstance(item.expression, exp.Star):
                raise Unsupported("expression.* on a computed value")
            name = None
            node = item
            if isinstance(item, exp.Alias):
                _only(item, "this", "alias")
                name = item.alias
                node = item.this
            else:
                parts = path_parts(item)
                if parts is not None:
                    name = parts[-1]
                elif isinstance(item, exp.Dot) and isinstance(item.expression, exp.Identifier):
                    name = item.expression.name  # (expr).field is named field
            items.append((name, node, None))
        return items

    def _dot_star(self, parts: list[str], from_scope: FromScope) -> list:
        lowered = parts[0].lower()
        if len(parts) == 1:
            sources = [s for s in from_scope.sources if s.name is not None and s.name.lower() == lowered]
            columns_hit = from_scope.lookup(parts) if not sources else None
            if sources:
                if len(sources) > 1:
                    raise AnalysisError(f"Ambiguous {parts[0]}.*")
                return source_star(sources[0])
            if columns_hit is not None:
                return self._struct_star(columns_hit)
        ref = from_scope.lookup(parts)
        if ref is None:
            raise Unsupported(f"{'.'.join(parts)}.* outside this query")
        fields, typ = list(ref.fields), ref.type
        for index in ref.fields:
            typ = typ.fields[index][1]
        for name in parts[ref.consumed:]:
            index = typ.field_index(name) if typ.kind == "STRUCT" else None
            if index is None:
                raise AnalysisError(f"Field {name} not found")
            fields.append(index)
            typ = typ.fields[index][1]
        return self._struct_star(Ref(ref.slot, ref.type, tuple(fields)))

    def _struct_star(self, ref: Ref) -> list:
        """The fields of the struct a Ref reads (``ref.type`` is the slot's type, ``ref.fields`` the path into it)."""

        if ref.row_slots is not None:
            raise AnalysisError("Dot-star is only supported for STRUCT values")
        typ = ref.type
        for index in ref.fields:
            typ = typ.fields[index][1]
        if typ.kind != "STRUCT":
            raise AnalysisError("Dot-star is only supported for STRUCT values")
        return [(name, ref.slot, ref.type, tuple(ref.fields) + (i,)) for i, (name, _) in enumerate(typ.fields)]

    def _star_items(self, columns, star: exp.Expression, from_scope: FromScope):
        except_ = star.args.get("except_") or star.args.get("except") or []
        replace = star.args.get("replace") or []
        excluded = set()
        for column in except_:
            parts = path_parts(column)
            if parts is None or len(parts) != 1:
                raise Unsupported("SELECT * EXCEPT of a path")
            excluded.add(parts[0].lower())
        names = {(n or "").lower() for n, _, _, _ in columns}
        for name in excluded:
            if name not in names:
                raise AnalysisError(f"Column {name} in SELECT * EXCEPT list does not exist")
        replacements = {}
        for alias in replace:
            if not isinstance(alias, exp.Alias):
                raise Unsupported("SELECT * REPLACE item")
            replacements[alias.alias.lower()] = alias.this
            if alias.alias.lower() not in names:
                raise AnalysisError(f"Column {alias.alias} in SELECT * REPLACE list does not exist")
        out = []
        for name, slot, typ, fields in columns:
            lowered = (name or "").lower()
            if lowered in excluded:
                continue
            if lowered in replacements:
                out.append((name, replacements[lowered], None))
            else:
                out.append((name, None, (slot, typ, fields)))
        if not out:
            raise AnalysisError("SELECT * EXCEPT removed every column")
        return out

    def _star_after_group(self, slot, typ, fields, from_scope: FromScope, final_scope: GroupScope) -> E:
        signature = ("col", id(from_scope), slot, tuple(fields))
        best = None
        for item_sig, item_slot, item_type in final_scope.items:
            if isinstance(item_sig, tuple) and item_sig[:3] == signature[:3] and tuple(fields[: len(item_sig[3])]) == item_sig[3]:
                if best is None or len(item_sig[3]) > len(best[0]):
                    best = (item_sig[3], item_slot, item_type)
        if best is None:
            raise AnalysisError("SELECT * expands to a column that is neither grouped nor aggregated")
        value = E(best[2], _slot_getter(0, best[1]))
        for index in fields[len(best[0]):]:
            value = _field(value, index)
        return value

    # Aliases in HAVING, QUALIFY and ORDER BY: a single name that is a SELECT alias (and not a different FROM column)
    # stands for the aliased expression.

    def _substitute_aliases(self, node: exp.Expression, alias_nodes: dict, from_scope: FromScope) -> exp.Expression:
        if not alias_nodes:
            return node
        node = node.copy()

        def swap(n):
            if isinstance(n, exp.Column) and not n.args.get("table") and isinstance(n.this, exp.Identifier):
                targets = alias_nodes.get(n.name.lower())
                if not targets:
                    return n
                try:
                    column = from_scope.lookup([n.name])
                except (AnalysisError, Unsupported):
                    column = None
                if column is not None:
                    if len(targets) == 1 and path_parts(targets[0]) == [n.name]:
                        return n
                    if len(targets) == 1 and self.signature(targets[0], from_scope) == self.signature(n, from_scope):
                        return n
                    raise Unsupported(f"{n.name} is both a SELECT alias and a column")
                if len(targets) > 1:
                    raise AnalysisError(f"Alias {n.name} is ambiguous")
                return targets[0].copy()
            return n

        return _transform_level(node, swap)

    def _substitute_order(self, target: exp.Expression, alias_nodes: dict, from_scope: FromScope, items):
        """An ORDER BY item: an output position (int) or the expression to sort by."""

        if isinstance(target, exp.Literal) and not target.is_string:
            if not str(target.this).isdigit():
                raise Unsupported("ORDER BY a numeric literal that is not an integer")
            position = int(target.this)
            if not 1 <= position <= len(items):
                raise AnalysisError(f"ORDER BY column number exceeds input table column count: {position}")
            return position - 1
        if isinstance(target, exp.Column) and not target.args.get("table") and isinstance(target.this, exp.Identifier):
            lowered = target.name.lower()
            positions = [i for i, (name, _, _) in enumerate(items) if name is not None and name.lower() == lowered]
            if positions:
                try:
                    column = from_scope.lookup([target.name])
                except (AnalysisError, Unsupported):
                    column = None
                if len(positions) > 1:
                    # several SELECT items share the name: fine only if they all read the same column
                    raise Unsupported(f"ORDER BY {target.name} names several SELECT items")
                # a SELECT-list name takes precedence over a FROM column of the same name in ORDER BY
                return positions[0]
        return self._substitute_aliases(target, alias_nodes, from_scope)

    # --- GROUP BY ----------------------------------------------------------------------------------

    def _group_stage(self, node, group, items, aggs, from_scope: FromScope, scope: Scope, replace: dict):
        from . import aggregates

        grouping_sets, group_nodes, named = self._grouping_sets(group, items, from_scope)
        cx_from = Cx(from_scope, no_agg="GROUP BY")
        item_exprs = []
        signatures = []
        for g in group_nodes:
            if isinstance(g, tuple):  # a SELECT * column by position
                _, slot, typ, fields = g
                value = E(typ, _slot_getter(0, slot))
                for index in fields:
                    value = _field(value, index)
                item_exprs.append(value)
                signatures.append(("col", id(from_scope), slot, fields))
                continue
            value = self.expr(g, cx_from)
            if not T.groupable(value.type):
                raise AnalysisError(f"Grouping by expressions of type {value.type} is not allowed")
            item_exprs.append(value)
            signatures.append(self.signature(g, from_scope))
        n_items = len(item_exprs)
        group_items = []
        by_sig = {}
        for i, (signature, value) in enumerate(zip(signatures, item_exprs)):
            getter = E(value.type, _slot_getter(0, i))
            group_items.append((signature, i, value.type))
            by_sig.setdefault(signature, getter)
        mask_slot = n_items
        always = frozenset(signatures[i] for i in range(n_items) if all(i in s for s in grouping_sets))
        by_node = {node_id: E(item_exprs[slot].type, _slot_getter(0, slot)) for node_id, slot in named.items()}
        group_info = GroupInfo(from_scope, by_sig, always, by_node)
        group_scope = GroupScope(from_scope, group_items, scope, self)
        # aggregates: arguments read the FROM row
        specs = []
        for i, agg_node in enumerate(aggs):
            spec = aggregates.compile_aggregate(self, agg_node, Cx(from_scope, no_agg=None, in_agg=True))
            specs.append(spec)
            replace[id(agg_node)] = E(spec.type, _raising(_slot_getter(0, n_items + 1 + i)))
        replace["__grouping__"] = (signatures, mask_slot)
        stage = GroupStage(item_exprs, grouping_sets, specs, n_items)
        return stage, group_scope, group_info

    def _grouping_sets(self, group, items, from_scope: FromScope):
        """(list of sets of item positions, item nodes)."""

        if group is None:
            return [()], [], {}
        _only(group, "expressions", "all", "grouping_sets", "rollup", "cube", "totals")
        if group.args.get("totals"):
            raise Unsupported("WITH TOTALS")
        nodes: list = []
        signatures: list = []
        named: dict = {}  # id(SELECT item node named by an ordinal or alias) -> the position of its key

        def position(n: exp.Expression) -> int:
            written = n
            n = self._group_item(n, items, from_scope)
            signature = ("col", id(from_scope), n[1], n[3]) if isinstance(n, tuple) else self.signature(n, from_scope)
            for i, s in enumerate(signatures):
                if s == signature and s[0] != "unresolved":
                    if n is not written and not isinstance(n, tuple):
                        named[id(n)] = i
                    return i
            nodes.append(n)
            signatures.append(signature)
            if n is not written and not isinstance(n, tuple):
                named[id(n)] = len(nodes) - 1
            return len(nodes) - 1

        if group.args.get("all"):
            for name, item_node, star in items:
                if star is not None:
                    raise Unsupported("GROUP BY ALL with SELECT *")
                if not _contains_aggregate(item_node) and not _contains_window(item_node):
                    named[id(item_node)] = position(item_node)
            return [tuple(range(len(nodes)))], nodes, named
        def rollup_sets(g) -> list:
            parts = [self._set_items(e, position) for e in g.expressions]
            return [tuple(itertools.chain.from_iterable(parts[:k])) for k in range(len(parts), -1, -1)]

        def cube_sets(g) -> list:
            parts = [self._set_items(e, position) for e in g.expressions]
            if len(parts) > 12:
                raise Unsupported("CUBE with too many items")
            sets = []
            for mask in range(2 ** len(parts) - 1, -1, -1):
                chosen = [parts[i] for i in range(len(parts)) if mask & (1 << (len(parts) - 1 - i))]
                sets.append(tuple(itertools.chain.from_iterable(chosen)))
            return sets

        factors: list[list[tuple]] = []
        for g in group.expressions:
            if isinstance(g, exp.Rollup):
                factors.append(rollup_sets(g))
            elif isinstance(g, exp.Cube):
                factors.append(cube_sets(g))
            elif isinstance(g, exp.GroupingSets):
                sets = []
                for e in g.expressions:  # a ROLLUP or CUBE among the sets contributes its own sets
                    if isinstance(e, exp.Rollup):
                        sets.extend(rollup_sets(e))
                    elif isinstance(e, exp.Cube):
                        sets.extend(cube_sets(e))
                    else:
                        sets.append(self._set_items(e, position))
                factors.append(sets)
            else:
                factors.append([(position(g),)])
        for key in ("grouping_sets", "rollup", "cube"):
            if group.args.get(key):
                raise Unsupported(f"GROUP BY {key} in this sqlglot version")
        result = [()]
        for sets in factors:
            result = [a + b for a in result for b in sets]
        cleaned = []
        for s in result:
            unique = []
            for p in s:
                if p not in unique:
                    unique.append(p)
            cleaned.append(tuple(unique))
        return cleaned, nodes, named

    def _set_items(self, node: exp.Expression, position) -> tuple:
        if isinstance(node, exp.Tuple):
            return tuple(position(e) for e in node.expressions)
        if isinstance(node, exp.Paren) and isinstance(node.this, exp.Tuple):
            return tuple(position(e) for e in node.this.expressions)
        if isinstance(node, exp.Paren):
            return (position(node.this),)
        return (position(node),)

    def _group_item(self, node: exp.Expression, items, from_scope: FromScope) -> exp.Expression:
        if isinstance(node, exp.Literal) and not node.is_string:
            if not str(node.this).isdigit():
                raise Unsupported("GROUP BY a numeric literal that is not an integer")
            position = int(node.this)
            if not 1 <= position <= len(items):
                raise AnalysisError(f"GROUP BY position {position} is out of range")
            name, item_node, star = items[position - 1]
            if item_node is None:
                slot, typ, fields = star
                return ("star", slot, typ, tuple(fields))
            if _contains_aggregate(item_node):
                raise AnalysisError("GROUP BY position refers to an aggregate")
            return item_node
        if isinstance(node, exp.Column) and not node.args.get("table") and isinstance(node.this, exp.Identifier):
            lowered = node.name.lower()
            try:
                column = from_scope.lookup([node.name])
            except (AnalysisError, Unsupported):
                column = None
            if column is None:
                matches = [n for name, n, _ in items if name is not None and name.lower() == lowered and n is not None]
                if len(matches) == 1:
                    if _contains_aggregate(matches[0]):
                        raise AnalysisError("GROUP BY refers to an aggregate")
                    return matches[0]
                if len(matches) > 1:
                    raise AnalysisError(f"GROUP BY {node.name} is ambiguous")
        return node

    # --- FROM --------------------------------------------------------------------------------------
    #
    # Every FROM item compiles to (sources, width, run, star, correlated). ``run(env, left_row)`` returns the
    # item's rows; ``left_row`` is the row of the items to its left (``()`` for the first), which only a
    # correlated UNNEST reads. Names in such an UNNEST resolve against the left items first, then the
    # enclosing queries, so depth 0 is the left row and depth 1 the enclosing query's row.

    def from_clause(self, first: exp.Expression, joins: list, scope: Scope, ctes: dict):
        empty = FromScope([], 0, scope)
        sources, width, item_run, star, _ = self.from_item(first, scope, ctes, empty)
        current = FromScope(sources, width, scope, star=star)
        run = lambda env: item_run(env, ())  # noqa: E731
        for join in joins:
            current, run = self.join(current, run, join, scope, ctes)
        return current, run

    def from_item(self, node: exp.Expression, scope: Scope, ctes: dict, left: FromScope):
        pivots = node.args.get("pivots")
        if pivots:
            if len(pivots) != 1:
                raise Unsupported("several PIVOT / UNPIVOT operators on one table")
            bare = node.copy()
            bare.set("pivots", None)
            base = self.from_item(bare, scope, ctes, left)
            if base[4]:
                raise Unsupported("PIVOT / UNPIVOT of a correlated item")
            return self._pivot_item(pivots[0], base, scope)
        if isinstance(node, exp.Table):
            _only(node, "this", "db", "catalog", "alias", "joins")
            alias = node.args.get("alias")
            alias_name = None
            if alias is not None:
                _only(alias, "this")
                alias_name = alias.name
            if node.args.get("joins"):
                if alias_name:
                    raise Unsupported("aliased parenthesized join")
                return self._paren_join(node, scope, ctes)
            pieces = [p for p in (node.args.get("catalog"), node.args.get("db"), node.this) if p is not None]
            if not all(isinstance(p, exp.Identifier) for p in pieces):
                raise Unsupported("table expression")
            parts = [p.name for p in pieces]
            if len(parts) >= 2 and self._names_value(parts[0], left):
                return self._unnest_path(parts, alias_name, scope, left)
            if len(parts) == 1 and parts[0].lower() in ctes:
                entry = ctes[parts[0].lower()]
                entry_id = entry.id
                run = lambda env, row: env.ctes[entry_id]  # noqa: E731
                if entry.plan.value_table:
                    return [Source(alias_name or parts[0], [], 0, entry.plan.columns[0][1])], 1, run, None, False
                cols = [(n, i, t) for i, (n, t) in enumerate(entry.plan.columns)]
                return [Source(alias_name or parts[0], cols)], len(cols), run, None, False
            table = self.database.table(".".join(parts))
            if table is None:
                raise AnalysisError(f"Table not found: {'.'.join(parts)}")
            cols = [(n, i, t) for i, (n, t) in enumerate(table.columns)]
            rows = table.rows
            run = lambda env, row: rows  # noqa: E731
            return [Source(alias_name or parts[-1], cols)], len(cols), run, None, False
        if isinstance(node, exp.Subquery):
            _only(node, "this", "alias", "order", "limit", "offset", "with_")
            alias = node.args.get("alias")
            alias_name = None
            if alias is not None:
                _only(alias, "this")
                alias_name = alias.name
            tail = any(node.args.get(k) for k in ("order", "limit", "offset", "with_"))
            if isinstance(node.this, exp.Table) and node.this.args.get("joins") and not tail:
                if alias_name:
                    raise Unsupported("aliased parenthesized join")
                return self._paren_join(node.this, scope, ctes)
            inner = node.this
            if tail:
                inner = exp.Subquery(this=node.this, order=node.args.get("order"), limit=node.args.get("limit"),
                                     offset=node.args.get("offset"), with_=node.args.get("with_"))
            plan = self.query(inner, scope, ctes)
            plan_run = plan.run
            run = lambda env, row: plan_run(env)  # noqa: E731
            if plan.value_table:
                return [Source(alias_name, [], 0, plan.columns[0][1])], 1, run, None, False
            cols = [(n, i, t) for i, (n, t) in enumerate(plan.columns)]
            return [Source(alias_name, cols)], len(cols), run, None, False
        if isinstance(node, exp.Unnest):
            return self._unnest(node, scope, left)
        raise Unsupported(f"FROM item {type(node).__name__}")

    # --- PIVOT and UNPIVOT ------------------------------------------------------------------------------

    def _pivot_base(self, base, scope: Scope):
        sources, width, run, star, _ = base
        columns = star if star is not None else [c for src in sources for c in source_star(src)]
        for name, slot, typ, fields in columns:
            if name is None or fields:
                raise Unsupported("PIVOT / UNPIVOT over unnamed or struct-field columns")
        if any(src.value_slot is not None for src in sources):
            raise Unsupported("PIVOT / UNPIVOT of a value table")
        return FromScope(sources, width, scope, star=star), columns, run

    def _pivot_item(self, pivot: exp.Expression, base, scope: Scope):
        _only(pivot, "expressions", "fields", "unpivot", "include_nulls", "default_on_null", "value_columns_first", "alias",
              "columns", "identify_pivot_strings", "prefixed_pivot_columns", "pivot_column_naming", "with_")
        if pivot.args.get("default_on_null"):
            raise Unsupported("PIVOT DEFAULT ON NULL")
        from_scope, columns, base_item_run = self._pivot_base(base, scope)
        base_run = lambda env: base_item_run(env, ())  # noqa: E731
        alias = pivot.args.get("alias")
        alias_name = None
        if alias is not None:
            _only(alias, "this")
            alias_name = alias.name
        if pivot.args.get("unpivot"):
            out_cols, run = self._unpivot(pivot, from_scope, columns, base_run)
        else:
            out_cols, run = self._pivot(pivot, from_scope, columns, base_run)
        cols = [(n, i, t) for i, (n, t) in enumerate(out_cols)]
        wrapped = lambda env, row: run(env)  # noqa: E731
        return [Source(alias_name, cols)], len(cols), wrapped, None, False

    def _unpivot(self, pivot: exp.Expression, from_scope: FromScope, columns: list, base_run):
        fields = pivot.args.get("fields") or []
        if len(fields) != 1 or not isinstance(fields[0], exp.In) or len(pivot.expressions) != 1:
            raise Unsupported("UNPIVOT form")
        include_nulls = bool(pivot.args.get("include_nulls"))
        values_node = pivot.expressions[0]
        value_names = [v.name for v in (values_node.expressions if isinstance(values_node, exp.Tuple) else [values_node])]
        if not all(isinstance(v, exp.Identifier) for v in (values_node.expressions if isinstance(values_node, exp.Tuple) else [values_node])):
            raise Unsupported("UNPIVOT value column")
        in_node = fields[0]
        if not isinstance(in_node.this, exp.Identifier):
            raise Unsupported("UNPIVOT name column")
        name_column = in_node.this.name
        items = []  # (label, [slots], [types])
        used: set = set()
        for item in in_node.expressions:
            label = None
            if isinstance(item, exp.PivotAlias):
                if not (isinstance(item.args.get("alias"), exp.Literal) and item.args["alias"].is_string):
                    raise Unsupported("UNPIVOT label that is not a string literal")
                label = item.args["alias"].this
                item = item.this
            refs = item.expressions if isinstance(item, exp.Tuple) else [item]
            slots, types, names = [], [], []
            for ref_node in refs:
                parts = path_parts(ref_node)
                if parts is None:
                    raise Unsupported("UNPIVOT column that is not a name")
                ref = from_scope.lookup(parts)
                if ref is None or ref.fields or ref.row_slots is not None:
                    raise AnalysisError(f"Column {'.'.join(parts)} in UNPIVOT is not a column of the table")
                if ref.type.foreign:
                    raise Unsupported(f"column of GoogleSQL-only type {ref.type}")
                slots.append(ref.slot)
                types.append(ref.type)
                names.append(next(n for n, s, _, _ in columns if s == ref.slot))
            if len(slots) != len(value_names):
                raise AnalysisError("UNPIVOT value column count differs from the IN list items")
            used.update(slots)
            items.append((label if label is not None else "_".join(names), slots, types))
        if not items:
            raise AnalysisError("UNPIVOT needs at least one column")
        keep = [(n, s, t) for n, s, t, _ in columns if s not in used]
        value_types = [T.supertype([(it[2][j], None) for it in items]) for j in range(len(value_names))]
        for t in value_types:
            if t.foreign:
                raise Unsupported(f"column of GoogleSQL-only type {t}")
        out = [(n, t) for n, _, t in keep] + list(zip(value_names, value_types)) + [(name_column, T.STRING)]
        lowered = [n.lower() for n, _ in out]
        if len(set(lowered)) != len(lowered):
            raise Unsupported("UNPIVOT output with repeated column names")
        convert = [[None if it[2][j] == value_types[j] else V.caster(it[2][j], value_types[j]) for j in range(len(value_names))]
                   for it in items]
        keep_slots = [s for _, s, _ in keep]

        def run(env: Env) -> list:
            tz = env.ctx.tz
            rows = []
            for row in base_run(env):
                head = tuple(row[s] for s in keep_slots)
                for (label, slots, _), conv in zip(items, convert):
                    values = tuple(row[s] if c is None or row[s] is None else c(row[s], tz) for s, c in zip(slots, conv))
                    if not include_nulls and all(v is None for v in values):
                        continue
                    rows.append(head + values + (label,))
            return rows

        return out, run

    def _pivot(self, pivot: exp.Expression, from_scope: FromScope, columns: list, base_run):
        from . import aggregates

        fields = pivot.args.get("fields") or []
        if len(fields) != 1 or not isinstance(fields[0], exp.In):
            raise Unsupported("PIVOT form")
        in_node = fields[0]
        for_node = in_node.this
        while isinstance(for_node, exp.Paren):
            for_node = for_node.this
        if isinstance(for_node, exp.Tuple):
            raise Unsupported("PIVOT FOR several expressions")
        aggregates_nodes = []  # (aggregate node, alias or None)
        for item in pivot.expressions:
            alias = None
            if isinstance(item, exp.Alias):
                alias = item.alias
                item = item.this
            if not (aggregates.is_aggregate(item) or isinstance(item, (exp.IgnoreNulls, exp.RespectNulls))):
                raise AnalysisError("PIVOT expressions must be aggregate function calls")
            aggregates_nodes.append((item, alias))
        if not aggregates_nodes:
            raise AnalysisError("PIVOT needs an aggregate function")
        if len(aggregates_nodes) > 1 and any(a is None for _, a in aggregates_nodes):
            raise Unsupported("several PIVOT aggregates without aliases")

        def volatile(node) -> bool:
            return any(isinstance(n, (exp.Rand, exp.Uuid)) or type(n).__name__ in ("Rand", "Uuid", "GenerateUuid")
                       for n in node.find_all(exp.Expression))

        if any(volatile(n) for n, _ in aggregates_nodes) or volatile(for_node):
            raise Unsupported("PIVOT with a volatile expression")
        # the columns the aggregates and the FOR expression read are not group columns
        referenced: set = set()
        for node in [n for n, _ in aggregates_nodes] + [for_node]:
            for column in _columns_outside_queries(node):
                parts = path_parts(column)
                if parts is None:
                    raise Unsupported("PIVOT expression with a computed name")
                ref = from_scope.lookup(parts)
                if ref is None:
                    raise AnalysisError(f"Unrecognized name: {parts[0]}")
                if ref.row_slots is not None:
                    raise Unsupported("PIVOT over a whole row")
                referenced.add(ref.slot)
        keep = [(n, s, t) for n, s, t, _ in columns if s not in referenced]
        for _, _, t in keep:
            if t.foreign or not T.groupable(t):
                raise Unsupported(f"PIVOT group column of type {t}")
        cx = Cx(from_scope, no_agg=None, in_agg=True)
        for_e = self.expr(for_node, Cx(from_scope, no_agg="PIVOT"))
        values = []  # (name, E)
        for item in in_node.expressions:
            alias = None
            if isinstance(item, exp.PivotAlias):
                alias = item.args["alias"]
                item = item.this
                if isinstance(alias, exp.Literal) and alias.is_string:
                    alias = alias.this
                elif isinstance(alias, exp.Identifier):
                    alias = alias.name
                else:
                    raise Unsupported("PIVOT value alias")
            value = self._pivot_value(item)
            if alias is None:
                alias = _pivot_value_name(item, value)
                if not (alias[0].isalpha() or alias[0] == "_"):
                    alias = "\0" + alias  # a generated name that starts with a digit gets "_" when alone
            values.append((alias, value))
        values = [(a, v) for a, v in values]
        if not values:
            raise AnalysisError("PIVOT needs at least one value")
        target, coerced = self.unify([for_e] + [v for _, v in values], "PIVOT value")
        if not T.groupable(target):
            raise AnalysisError(f"PIVOT FOR expression of type {target}")
        for_fn = coerced[0].fn
        keys = [coerced[i + 1].fn(None) for i in range(len(values))]
        wanted = [V.group_key(target, k) for k in keys]
        specs = [aggregates.compile_aggregate(self, n, cx) for n, _ in aggregates_nodes]
        out = [(n, t) for n, _, t in keep]
        for vname, _ in values:
            for (_, agg_alias), spec in zip(aggregates_nodes, specs):
                plain = vname.lstrip("\0")
                out.append((f"{agg_alias}_{plain}" if agg_alias else ("_" + plain if vname.startswith("\0") else vname), spec.type))
        lowered = [n.lower() for n, _ in out]
        if len(set(lowered)) != len(lowered):
            raise Unsupported("PIVOT output with repeated column names")
        keep_slots = [s for _, s, _ in keep]
        keep_types = [t for _, _, t in keep]

        def run(env: Env) -> list:
            ctx = env.ctx
            groups: dict = {}
            for row in base_run(env):
                key = tuple(V.group_key(t, row[s]) for s, t in zip(keep_slots, keep_types))
                bucket = groups.get(key)
                if bucket is None:
                    bucket = groups[key] = (tuple(row[s] for s in keep_slots), [[] for _ in values])
                matched = V.group_key(target, for_fn(Env(row, env, ctx, env.ctes)))
                for i, w in enumerate(wanted):
                    if matched == w:
                        bucket[1][i].append(row)
            if not groups and not keep_slots:
                groups[()] = ((), [[] for _ in values])
            rows = []
            for head, members in groups.values():
                tail = []
                for member in members:
                    for spec in specs:
                        tail.append(spec.compute(member, env))
                rows.append(head + tuple(tail))
            return rows

        return out, run

    def _pivot_value(self, node: exp.Expression) -> E:
        """A PIVOT IN value: a literal (or a negated one); anything else, such as a named constant, is not evaluated."""

        inner = node
        while isinstance(inner, exp.Paren):
            inner = inner.this
        if isinstance(inner, (exp.Literal, exp.Null, exp.Boolean, exp.Neg, exp.Cast)) or (
            isinstance(inner, exp.Expression) and not list(inner.find_all(exp.Column))
        ):
            return self.expr(inner, Cx(EmptyScope(), no_agg="PIVOT"))
        raise Unsupported("PIVOT IN value that is not a constant expression")

    def _names_value(self, name: str, left: FromScope) -> bool:
        """Whether ``name`` is a range variable or column visible here (so ``name.x`` in FROM is an array path)."""

        try:
            return self.resolve([name], left) is not None
        except (AnalysisError, Unsupported):
            return True

    def _paren_join(self, table: exp.Table, scope: Scope, ctes: dict):
        first = table.copy()
        joins = first.args.get("joins") or []
        first.set("joins", None)
        inner_scope, inner_run = self.from_clause(first, joins, scope, ctes)
        run = lambda env, row: inner_run(env)  # noqa: E731
        return inner_scope.sources, inner_scope.width, run, inner_scope.star(), False

    def _array_expr(self, compile_in, scope: Scope, left: FromScope) -> tuple[E, bool]:
        """Compile an UNNEST argument: uncorrelated if it compiles without the left items, else against them."""

        if left.sources:
            try:
                return compile_in(EmptyScope(scope)), False
            except (AnalysisError, Unsupported):
                return compile_in(left), True
        return compile_in(EmptyScope(scope)), False

    def _unnest(self, node: exp.Unnest, scope: Scope, left: FromScope):
        _only(node, "expressions", "alias", "offset", "explode_array")
        if len(node.expressions) != 1:
            raise Unsupported("UNNEST of several arrays")
        argument = node.expressions[0]
        value, correlated = self._array_expr(lambda s: self.path_expr(argument, Cx(s, no_agg="UNNEST")), scope, left)
        alias = node.args.get("alias")
        alias_name = None
        if alias is not None:
            _only(alias, "this", "columns")
            names = alias.args.get("columns") or []
            if alias.args.get("this") is not None and names:
                raise Unsupported("UNNEST with table and column aliases")
            if len(names) > 1:
                raise Unsupported("UNNEST with several column aliases")
            alias_name = names[0].name if names else alias.name
        offset = node.args.get("offset")
        offset_name = None
        if offset is not None and offset is not False:
            offset_name = offset.name if isinstance(offset, exp.Expression) else "offset"
        return self._unnest_value(value, alias_name, offset_name, correlated)

    def _unnest_path(self, parts: list[str], alias_name: str | None, scope: Scope, left: FromScope):
        def compile_in(s):
            value = self.resolve(parts[:1], s)
            if value is None:
                raise AnalysisError(f"Unrecognized name: {parts[0]}")
            return self.path_steps(value, [("field", n) for n in parts[1:]], Cx(s, no_agg="UNNEST"))

        value, correlated = self._array_expr(compile_in, scope, left)
        return self._unnest_value(value, alias_name or parts[-1], None, correlated)

    def _unnest_value(self, value: E, alias_name, offset_name, correlated: bool):
        if value.lit == "null":
            raise AnalysisError("UNNEST of an untyped NULL")
        if value.type.kind != "ARRAY":
            raise AnalysisError(f"Values referenced in UNNEST must be arrays. UNNEST contains expression of type {value.type}")
        elem = value.type.elem
        if elem.foreign:
            raise Unsupported(f"array of GoogleSQL-only type {elem}")
        fn = value.fn
        cols = []
        if offset_name is not None:
            cols.append((offset_name, 1, T.INT64))

        def run(env: Env, row) -> list:
            array_value = fn(Env(row, env, env.ctx, env.ctes))
            if array_value is None:
                return []
            if offset_name is not None:
                if not V.ordered_kind(array_value):
                    env.ctx.nondet("UNNEST WITH OFFSET of an unordered array")
                return [(v, i) for i, v in enumerate(array_value)]
            return [(v,) for v in array_value]

        source = Source(alias_name, cols, 0, elem)
        return [source], 1 + (1 if offset_name is not None else 0), run, None, correlated

    def join(self, left_scope: FromScope, left_run, join: exp.Join, scope: Scope, ctes: dict):
        _only(join, "this", "kind", "side", "on", "using", "method")
        if join.args.get("method"):
            raise Unsupported(f"{join.args['method']} JOIN")
        side = (join.args.get("side") or "").upper()
        kind = (join.args.get("kind") or "").upper()
        if kind not in ("", "INNER", "OUTER", "CROSS"):
            raise Unsupported(f"{kind} JOIN")
        on = join.args.get("on")
        using = join.args.get("using") or []
        right_sources, right_width, right_run, right_star, correlated = self.from_item(join.this, scope, ctes, left_scope)
        offset = left_scope.width
        shifted = [_shift(s, offset) for s in right_sources]
        sources = left_scope.sources + shifted
        width = left_scope.width + right_width
        left_star = left_scope.star()
        right_star_cols = [(n, s + offset, t, f) for n, s, t, f in (right_star or [c for src in right_sources for c in source_star(src)])]
        merged = dict(left_scope.merged)
        star = left_star + right_star_cols
        if correlated and side in ("RIGHT", "FULL"):
            raise AnalysisError(f"{side} JOIN with a correlated array")
        array_item = isinstance(join.this, exp.Unnest) or correlated  # an array scan joins without a condition
        if kind == "CROSS" and (on is not None or using):
            raise AnalysisError("CROSS JOIN with a condition")
        if side in ("LEFT", "RIGHT", "FULL") and on is None and not using and not array_item:
            raise AnalysisError("An outer join needs a join condition")
        if kind == "INNER" and on is None and not using and not array_item:
            raise AnalysisError("INNER JOIN needs a join condition")
        extra = []
        if using:
            rscope = FromScope(right_sources, right_width, None)
            used_slots = set()
            star = []
            for i, identifier in enumerate(using):
                name = identifier.name
                lref = left_scope.lookup([name])
                rref = rscope.lookup([name])
                if lref is None or rref is None or lref.fields or rref.fields or lref.row_slots or rref.row_slots:
                    raise AnalysisError(f"Column {name} in USING clause not found on both sides of the join")
                common = T.supertype([(lref.type, None), (rref.type, None)])
                if not T.equatable(common):
                    raise AnalysisError(f"Column {name} in USING has a type that cannot be compared")
                slot = width + i
                merged[name.lower()] = (slot, common)
                star.append((name, slot, common, ()))
                used_slots.update((lref.slot, rref.slot + offset))
                extra.append((lref.slot, lref.type, rref.slot + offset, rref.type, common))
            star += [c for c in left_star if not (c[1] in used_slots and not c[3])]
            star += [c for c in right_star_cols if not (c[1] in used_slots and not c[3])]
        new_scope = FromScope(sources, width + len(extra), scope, merged=merged, star=star)
        condition = None
        if on is not None:
            condition = self._bool(self.expr(on, Cx(new_scope, no_agg="JOIN")), "ON")
        converters = [(V.caster(lt, common), V.caster(rt, common), common) for _, lt, _, rt, common in extra]
        using_slots = [(ls, rs) for ls, _, rs, _, _ in extra]
        cond_fn = condition.fn if condition is not None else None
        right_nulls = (None,) * right_width
        left_nulls = (None,) * left_scope.width
        n_extra = len(extra)

        def merged_values(row, env, prefer_right: bool) -> tuple:
            tz = env.ctx.tz
            values = []
            for (ls, rs), (lc, rc, _) in zip(using_slots, converters):
                lv, rv = row[ls], row[rs]
                if lv is not None and not prefer_right:
                    values.append(lc(lv, tz))
                elif rv is not None:
                    values.append(rc(rv, tz))
                elif lv is not None:
                    values.append(lc(lv, tz))
                else:
                    values.append(None)
            return tuple(values)

        def matches(row, env) -> bool:
            if n_extra:
                tz = env.ctx.tz
                for (ls, rs), (lc, rc, common) in zip(using_slots, converters):
                    lv, rv = row[ls], row[rs]
                    if lv is None or rv is None or V.sql_equal(common, lc(lv, tz), rc(rv, tz)) is not True:
                        return False
                row = row + merged_values(row, env, side == "RIGHT")
            if cond_fn is not None:
                return cond_fn(Env(row, env, env.ctx, env.ctes)) is True
            return True

        def emit(row, env, prefer_right=False):
            if n_extra:
                return row + merged_values(row, env, prefer_right)
            return row

        def run(env: Env) -> list:
            lrows = left_run(env)
            out = []
            if correlated:
                for lrow in lrows:
                    hit = False
                    for rrow in right_run(env, lrow):
                        row = lrow + rrow
                        if matches(row, env):
                            hit = True
                            out.append(emit(row, env))
                    if not hit and side == "LEFT":
                        out.append(emit(lrow + right_nulls, env))
                return out
            rrows = right_run(env, ())
            matched_right = [False] * len(rrows)
            for lrow in lrows:
                hit = False
                for j, rrow in enumerate(rrows):
                    row = lrow + rrow
                    if matches(row, env):
                        hit = True
                        matched_right[j] = True
                        out.append(emit(row, env, side == "RIGHT"))
                if not hit and side in ("LEFT", "FULL"):
                    out.append(emit(lrow + right_nulls, env))
            if side in ("RIGHT", "FULL"):
                for j, rrow in enumerate(rrows):
                    if not matched_right[j]:
                        out.append(emit(left_nulls + rrow, env, True))
            return out

        return new_scope, run


# ---------------------------------------------------------------------------------------------
# GROUP BY execution
# ---------------------------------------------------------------------------------------------


class GroupStage:
    def __init__(self, items: list[E], sets: list[tuple], aggs: list, n_items: int):
        self.items = items
        self.sets = sets
        self.aggs = aggs
        self.n_items = n_items
        self.width = n_items + 1 + len(aggs)
        self.types = [e.type for e in items]

    def run(self, rows: list, env: Env) -> list:
        ctx = env.ctx
        item_fns = [e.fn for e in self.items]
        keyed = []
        for row in rows:
            row_env = Env(row, env, ctx, env.ctes)
            keyed.append((row, tuple(fn(row_env) for fn in item_fns)))
        out = []
        for active in self.sets:
            groups: dict = {}
            order = []
            for row, values in keyed:
                key = tuple(V.group_key(self.types[i], values[i]) for i in active)
                bucket = groups.get(key)
                if bucket is None:
                    bucket = groups[key] = (values, [])
                    order.append(key)
                bucket[1].append(row)
            if not groups and (not active):
                groups[()] = ((None,) * self.n_items, [])
                order.append(())
            for key in order:
                values, members = groups[key]
                group_values = tuple(values[i] if i in active else None for i in range(self.n_items))
                agg_values = tuple(_deferred(spec.compute, members, env) for spec in self.aggs)
                out.append(group_values + (frozenset(active),) + agg_values)
        return out


# ---------------------------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------------------------


class _Failed:
    """An aggregate that raised: GoogleSQL evaluates only the branches a conditional takes, so the error surfaces only if read."""

    __slots__ = ("error",)

    def __init__(self, error: EvalError):
        self.error = error


def _deferred(compute, members, env):
    try:
        return compute(members, env)
    except EvalError as error:
        return _Failed(error)


def _raising(getter):
    def read(env):
        value = getter(env)
        if isinstance(value, _Failed):
            raise value.error
        return value

    return read


def _slot_getter(depth: int, slot: int):
    if depth == 0:
        return lambda env: env.row[slot]
    if depth == 1:
        return lambda env: env.outer.row[slot]
    if depth == 2:
        return lambda env: env.outer.outer.row[slot]

    def fn(env):
        for _ in range(depth):
            env = env.outer
        return env.row[slot]

    return fn


def _row_getter(depth: int):
    def fn(env):
        for _ in range(depth):
            env = env.outer
        return env.row

    return fn


def _out_getter(position: int):
    return lambda env: env.row[position]


def _field(value: E, index: int) -> E:
    fn = value.fn
    typ = value.type.fields[index][1]
    return E(typ, lambda env: (lambda v: None if v is None else v[index])(fn(env)))


def _shift(source: Source, offset: int) -> Source:
    return Source(
        source.name,
        [(n, s + offset, t) for n, s, t in source.cols],
        None if source.value_slot is None else source.value_slot + offset,
        source.value_type,
    )


def _columns_outside_queries(node: exp.Expression) -> list:
    """The Column nodes of an expression that are not inside a subquery."""

    out: list = []

    def walk(n):
        if is_query(n):
            return
        if isinstance(n, exp.Column):
            out.append(n)
            return
        for child in n.iter_expressions():
            walk(child)

    walk(node)
    return out


def _pivot_value_name(node: exp.Expression, value: E) -> str:
    """The column name a PIVOT value gets without an alias: ``_100`` for 100, ``NULL``, ``true``, or a string that is a name."""

    import re

    if value.lit == "null":
        return "NULL"
    if value.type == T.INT64 and value.value is not None and value.value >= 0:
        return str(value.value)
    if value.type == T.BOOL and value.value is not None:
        return "true" if value.value else "false"
    if value.type == T.STRING and value.value is not None and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value.value):
        return value.value
    raise Unsupported("PIVOT value whose column name is generated from a value this evaluator does not name")


def _same_set_mode(a: exp.Expression, b: exp.Expression) -> bool:
    """Whether two set-operation nodes are the same operation with the same mode (so a chain of them is one n-ary operation)."""

    keys = ("distinct", "by_name", "side", "kind")
    if any(bool(a.args.get(k)) != bool(b.args.get(k)) or (isinstance(a.args.get(k), str) and a.args.get(k) != b.args.get(k)) for k in keys):
        return False
    on_a = [x.sql("bigquery").lower() for x in a.args.get("on") or []]
    on_b = [x.sql("bigquery").lower() for x in b.args.get("on") or []]
    return on_a == on_b


def _column_info(plan: Plan, i: int):
    if plan.col_exprs is None or plan.col_exprs[i] is None:
        return None
    return plan.col_exprs[i].info


def _merged_column(plans: list, positions: list, j: int):
    """The expression standing for output column ``j`` when every input's column there is the NULL literal, else ``None``."""

    items = [p.col_exprs[pos[j]] if p.col_exprs is not None and pos[j] is not None else None for p, pos in zip(plans, positions)]
    if all(i is not None and i.lit == "null" for i in items):
        return items[0]
    return None


def _struct_converter(source: T.Type, target: T.Type, info):
    """Convert a STRUCT value whose NULL-literal fields may have any type (they only ever hold NULL)."""

    parts = []
    for (_, s), (_, t), i in zip(source.fields, target.fields, info[1]):
        if i == "null" or s == t:
            parts.append(None)
        elif isinstance(i, tuple) and s.kind == "STRUCT":
            parts.append(_struct_converter(s, t, i))
        else:
            parts.append(V.caster(s, t))
    return lambda v, tz: tuple(x if p is None or x is None else p(x, tz) for p, x in zip(parts, v))


def _converters(source_types: list[T.Type], target_types: list[T.Type]):
    out = []
    for s, t in zip(source_types, target_types):
        if s == t:
            out.append(None)
        else:
            out.append(V.caster(s, t))
    return out


def _convert_row(converters, row: tuple, tz) -> tuple:
    if not any(converters):
        return tuple(row)
    return tuple(v if c is None or v is None else c(v, tz) for c, v in zip(converters, row))


def _dedupe(rows: list, types: list[T.Type]) -> list:
    seen = set()
    out = []
    for row in rows:
        key = V.row_key(types, row)
        if key not in seen:
            seen.add(key)
            out.append(row)
    return out


class _Desc:
    __slots__ = ("key",)

    def __init__(self, key):
        self.key = key

    def __lt__(self, other):
        return other.key < self.key

    def __eq__(self, other):
        return self.key == other.key


def _sort(items: list, spec: list, ctx) -> list:
    """Sort (values, keys) pairs by keys per spec [(type, desc, nulls_first)] (stable)."""

    def key(item):
        parts = []
        for value, (typ, desc, nulls_first) in zip(item[1], spec):
            if value is None:
                parts.append(_NullAware(0 if nulls_first else 2, None))
            else:
                k = V.sort_key(typ, value)
                parts.append(_NullAware(1, _Desc(k) if desc else k))
        return parts

    return sorted(items, key=key)


class _NullAware:
    __slots__ = ("rank", "value")

    def __init__(self, rank, value):
        self.rank, self.value = rank, value

    def __lt__(self, other):
        if self.rank != other.rank:
            return self.rank < other.rank
        if self.value is None:
            return False
        return self.value < other.value

    def __eq__(self, other):
        return self.rank == other.rank and (self.value is None or self.value == other.value)


def _cut_is_determined(rows: list, start: int, end: int, sort_keys) -> bool:
    """Whether LIMIT/OFFSET keeps the same multiset of rows whatever order ties come in."""

    if sort_keys is None:
        return len({repr(r) for r in rows}) <= 1
    for boundary in (start, end):
        if boundary <= 0 or boundary >= len(rows):
            continue
        key = sort_keys[boundary]
        if sort_keys[boundary - 1] != key:
            continue
        tied = [repr(rows[i]) for i in range(len(rows)) if sort_keys[i] == key]
        if len(set(tied)) > 1:
            return False
    return True


def _dependency_order(definitions: list) -> list:
    """The CTEs of a WITH RECURSIVE with each one after those it reads (a CTE may read one defined later); a cycle between
    different CTEs is refused."""

    names = {}
    for cte in definitions:
        alias = cte.args.get("alias")
        if alias is None:
            return definitions
        names.setdefault(alias.name.lower(), cte)
    reads = {n: {m for m in names if m != n and _references(cte.this, m)} for n, cte in names.items()}
    ordered: list = []
    done: set = set()
    visiting: set = set()

    def visit(name: str) -> None:
        if name in done:
            return
        if name in visiting:
            raise Unsupported("mutually recursive CTEs")
        visiting.add(name)
        for dep in sorted(reads[name], key=lambda m: list(names).index(m)):
            visit(dep)
        visiting.discard(name)
        done.add(name)
        ordered.append(name)

    for name in names:
        visit(name)
    seen = {id(names[n]) for n in ordered}
    return [names[n] for n in ordered] + [c for c in definitions if id(c) not in seen]


def _references(node: exp.Expression, name: str) -> bool:
    for table in node.find_all(exp.Table):
        if not table.args.get("db") and table.name.lower() == name:
            return True
    return False


def _collect(node: exp.Expression, aggs: list, wins: list, root: bool) -> None:
    from . import aggregates

    if not root and is_query(node):
        return
    if isinstance(node, exp.Exists) or (isinstance(node, exp.Array) and any(is_query(e) for e in node.expressions)):
        return
    if isinstance(node, exp.Window):
        wins.append(node)
        function = node.this
        while isinstance(function, (exp.IgnoreNulls, exp.RespectNulls)):
            function = function.this
        for child in function.iter_expressions():
            _collect(child, aggs, wins, False)
        for child in node.args.get("partition_by") or []:
            _collect(child, aggs, wins, False)
        if node.args.get("order") is not None:
            _collect(node.args["order"], aggs, wins, False)
        return
    if aggregates.is_aggregate(node):
        aggs.append(node)
        return
    for child in node.iter_expressions():
        _collect(child, aggs, wins, False)


def _contains_window(node: exp.Expression) -> bool:
    aggs: list = []
    wins: list = []
    _collect(node, aggs, wins, True)
    return bool(wins)


def _contains_aggregate(node: exp.Expression) -> bool:
    aggs: list = []
    wins: list = []
    _collect(node, aggs, wins, True)
    return bool(aggs)


def _transform_level(node: exp.Expression, fn) -> exp.Expression:
    """Apply ``fn`` to every node of this query level (not inside subqueries), bottom-up replacement."""

    if is_query(node):
        return node
    replaced = fn(node)
    if replaced is not node:
        return replaced
    for key, value in list(node.args.items()):
        if isinstance(value, exp.Expression):
            new = _transform_level(value, fn)
            if new is not value:
                node.set(key, new)
        elif isinstance(value, list):
            changed = False
            new_list = []
            for item in value:
                if isinstance(item, exp.Expression):
                    new = _transform_level(item, fn)
                    changed |= new is not item
                    new_list.append(new)
                else:
                    new_list.append(item)
            if changed:
                node.set(key, new_list)
    return node


_HANDLERS: dict = {}


def handles(*classes):
    def register(fn):
        for cls in classes:
            if cls is not None:
                _HANDLERS[cls] = fn
        return fn

    return register
