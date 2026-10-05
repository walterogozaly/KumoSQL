"""SQL (a sqlglot tree) to the multiplicity algebra of :mod:`kumosql.uexpr.ir`.

A query becomes ``Query(out, body)``: ``body`` is a numeric term whose free variables
are the output variables ``out`` (one scalar variable per column), and its value is how
many times the tuple ``out`` occurs in the result.

* A base table ``R AS a`` is a tuple variable ``x`` with factor ``R(x)``.
* ``FROM a, b WHERE p`` is ``Σ x, y. A(x)·B(y)·[p]``; the select list adds ``[out ≡ e]``.
* A derived table or CTE is its query with fresh output variables, summed over.
* ``LEFT JOIN`` is the inner join plus the unmatched left rows padded with NULLs:
  ``Σ x. A(x)·[¬∃(Σ y. B(y)·[on])]·[out ≡ (x, NULL..)]``; ``RIGHT`` and ``FULL`` likewise.
* ``DISTINCT`` and ``UNION`` squash: ``[∃ body]``. ``INTERSECT`` and ``EXCEPT`` multiply
  squashes; their ``ALL`` forms use ``min`` and monus.
* ``GROUP BY k`` sums over group variables ``g``: ``Σ g. [∃(Σ x. In(x)·[k(x) ≡ g])]·[out ≡ f(g, aggs(g))]``
  where each aggregate is a function of the bag ``Σ x. In(x)·[k(x) ≡ g]``.
* Predicates are translated to the pair (is TRUE, is FALSE), so NOT, IN, NOT IN and
  quantified comparisons follow three-valued logic exactly.

Anything outside this fragment raises :class:`Unsupported`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import datetime
from fractions import Fraction
import re

import sqlglot
from sqlglot import exp

from .ir import (
    NULL,
    ONE,
    ZERO,
    Agg,
    Arith,
    BoolV,
    Cmp,
    Col,
    Exists,
    FConst,
    FALSE,
    Fn,
    IsNull,
    Ite,
    Lit,
    NConst,
    NInd,
    NMin,
    NMonus,
    NRel,
    NSum,
    Ref,
    Same,
    Scalar,
    SVar,
    TRUE,
    Truth,
    TVar,
    conj,
    disj,
    fresh_id,
    freshen_free,
    ind,
    nadd,
    neg,
    nmul,
    nsum,
    replace,
    subst,
    value_kind,
    family,
)


class Unsupported(Exception):
    """The query is outside the fragment the procedure decides."""


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------

_INT_TYPES = {"INT", "INTEGER", "BIGINT", "SMALLINT", "TINYINT", "MEDIUMINT", "INT64", "INT32", "INT16", "INT8", "SIGNED", "UNSIGNED", "BYTEINT", "UBIGINT", "UINT"}
_NUM_TYPES = {"DECIMAL", "NUMERIC", "FLOAT", "DOUBLE", "REAL", "FLOAT64", "FLOAT32", "BIGNUMERIC", "BIGDECIMAL", "DEC", "NUMBER", "MONEY"}
_STR_TYPES = {"VARCHAR", "CHAR", "TEXT", "STRING", "NVARCHAR", "NCHAR", "CHARACTER", "MEDIUMTEXT", "LONGTEXT", "TINYTEXT"}
_BOOL_TYPES = {"BOOL", "BOOLEAN"}
_DATE_TYPES = {"DATE"}
_TIME_TYPES = {"TIMESTAMP", "DATETIME"}


def kind_of_type(declared: str | None) -> str | None:
    if not declared:
        return None
    base = re.split(r"[\s(]", str(declared).strip().upper(), maxsplit=1)[0]
    if base in _INT_TYPES:
        return "int"
    if base in _NUM_TYPES:
        return "num"
    if base in _STR_TYPES:
        return "str"
    if base in _BOOL_TYPES:
        return "bool"
    if base in _DATE_TYPES:
        return "date"
    if base in _TIME_TYPES:
        return "time"
    return None


@dataclass
class TableInfo:
    key: str
    columns: list | None  # None: not declared (columns are discovered from the queries)
    kinds: dict
    not_null: frozenset
    keys: tuple  # tuples of columns; each is unique and NOT NULL
    foreign: tuple  # (columns, parent key, parent columns)
    discovered: list = field(default_factory=list)

    def all_columns(self) -> list:
        return list(self.columns) if self.columns is not None else sorted(self.discovered)

    @property
    def declared(self) -> bool:
        return self.columns is not None


class Catalog:
    """Tables, columns, kinds and declared constraints, shared by both queries of a proof."""

    def __init__(self, schema=None, types=None, constraints=None, dialect: str = "bigquery"):
        self.dialect = dialect
        self.schema = {k.lower(): [c.lower() for c in cols] for k, cols in (schema or {}).items()}
        self.types = {k.lower(): {c.lower(): t for c, t in cols.items()} for k, cols in (types or {}).items()}
        self.constraints = {k.lower(): v for k, v in (constraints or {}).items()}
        self.tables: dict[str, TableInfo] = {}
        self.used_constraints = False

    def key(self, table: exp.Table) -> str:
        parts = [p.name for p in table.parts]
        key = ".".join(parts)
        return key if self.dialect == "bigquery" else key.lower()

    def info(self, key: str) -> TableInfo:
        found = self.tables.get(key)
        if found is not None:
            return found
        low = key.lower()
        columns = self.schema.get(low)
        kinds = {c: kind_of_type(t) for c, t in self.types.get(low, {}).items()}
        cons = self.constraints.get(low)
        not_null = frozenset(c.lower() for c in getattr(cons, "not_null", ()) or ())
        keys = []
        for k in getattr(cons, "keys", ()) or ():
            cols = tuple(c.lower() for c in k)
            if cols and (columns is None or all(c in columns for c in cols)):
                keys.append(cols)
                not_null = not_null | frozenset(cols)
        foreign = []
        for fk in getattr(cons, "foreign_keys", ()) or ():
            try:
                cols, parent, pcols = fk
            except (TypeError, ValueError):
                continue
            cols = (cols,) if isinstance(cols, str) else tuple(cols)
            pcols = (pcols,) if isinstance(pcols, str) else tuple(pcols)
            foreign.append((tuple(c.lower() for c in cols), parent if self.dialect == "bigquery" else parent.lower(), tuple(c.lower() for c in pcols)))
        info = TableInfo(key, list(columns) if columns is not None else None, kinds, not_null, tuple(keys), tuple(foreign))
        self.tables[key] = info
        return info

    def column_kind(self, key: str, column: str) -> str | None:
        return self.info(key).kinds.get(column)

    def discover(self, key: str, column: str) -> None:
        info = self.info(key)
        if info.columns is None and column not in info.discovered:
            info.discovered.append(column)


# ---------------------------------------------------------------------------
# Scopes
# ---------------------------------------------------------------------------


class Source:
    """The columns an alias in FROM provides, in order."""

    def __init__(self, columns: list, dynamic: TVar | None = None, catalog: Catalog | None = None):
        self.columns = columns  # [(name, Value)]
        self.dynamic = dynamic  # a table without declared columns: any name is a column
        self.catalog = catalog

    def lookup(self, name: str):
        for n, v in self.columns:
            if n == name:
                return v
        if self.dynamic is not None:
            self.catalog.discover(self.dynamic.table, name)
            v = Col(self.dynamic, name, self.catalog.column_kind(self.dynamic.table, name))
            self.columns.append((name, v))
            return v
        return None

    def has(self, name: str) -> bool | None:
        if any(n == name for n, _ in self.columns):
            return True
        return None if self.dynamic is not None else False

    def star(self) -> list:
        if self.dynamic is not None:
            raise Unsupported("SELECT * from a table without declared columns")
        return list(self.columns)


@dataclass
class GroupCtx:
    keys: list  # key values over the first copy of the rows
    gvars: list  # one scalar variable per key
    base_vars: frozenset  # variables of the first copy (a value using them must be a key)
    make_agg: object  # callable(exp node) -> Value
    base_scope: "Scope"


class Scope:
    def __init__(self, sources: dict, outer: "Scope | None" = None, using: dict | None = None, ctes: dict | None = None):
        self.sources = sources  # alias -> Source, in FROM order
        self.outer = outer
        self.ctes = ctes if ctes is not None else (outer.ctes if outer is not None else {})
        self.using = using or {}  # unqualified merged USING column -> Value
        self.aliases: dict = {}  # select-list alias -> exp (GROUP BY / HAVING / ORDER BY)
        self.group: GroupCtx | None = None


@dataclass
class Rows:
    vars: tuple
    body: object
    sources: dict  # alias -> Source
    using: dict = field(default_factory=dict)


@dataclass
class Query:
    out: tuple  # output scalar variables
    body: object  # Num with ``out`` free
    names: list
    single: tuple | None = None  # values, when the query returns exactly one row (global aggregate)


def _whole_number(node) -> int | None:
    if isinstance(node, exp.Literal) and not node.is_string and node.this.isdigit():
        return int(node.this)
    return None


def _limit_zero(node) -> bool:
    limit = node.args.get("limit")
    value = limit.expression if isinstance(limit, exp.Limit) else None
    return isinstance(value, exp.Literal) and not value.is_string and value.this == "0"


def _from(select):
    return select.args.get("from") or select.args.get("from_")


def _with(node):
    return node.args.get("with") or node.args.get("with_")


_AGG_TYPES = {
    exp.Count: "COUNT",
    exp.Sum: "SUM",
    exp.Avg: "AVG",
    exp.Min: "MIN",
    exp.Max: "MAX",
    exp.CountIf: "COUNTIF",
    exp.LogicalAnd: "LOGICAL_AND",
    exp.LogicalOr: "LOGICAL_OR",
}

_NONDETERMINISTIC = {
    "RAND", "RANDOM", "UUID", "GENERATE_UUID", "NOW", "CURRENT_DATE", "CURRENT_TIME", "CURRENT_TIMESTAMP",
    "CURRENT_DATETIME", "SYSDATE", "ANY_VALUE", "ARRAY_AGG", "STRING_AGG", "GROUP_CONCAT", "APPROX_COUNT_DISTINCT",
    "APPROX_QUANTILES", "APPROX_TOP_COUNT", "APPROX_TOP_SUM", "SESSION_USER", "CURRENT_USER", "USER", "LAST_INSERT_ID",
    "ROW_NUMBER", "RANK", "DENSE_RANK", "NTILE", "LAG", "LEAD", "FIRST_VALUE", "LAST_VALUE", "NTH_VALUE", "PERCENT_RANK", "CUME_DIST",
}
_NONDETERMINISTIC_TYPES = {
    "AnyValue", "ApproxDistinct", "ApproxQuantile", "ArrayAgg", "CurrentDate", "CurrentDatetime", "CurrentTime",
    "CurrentTimestamp", "CurrentUser", "GroupConcat", "MaxBy", "MinBy", "ArgMax", "ArgMin", "Rand", "TableSample",
    "Uuid", "Window", "WindowSpec", "StringAgg", "Randn",
}

# Functions that return NULL whenever an argument is NULL (in every dialect we read).
_STRICT = {
    "UPPER", "LOWER", "TRIM", "LTRIM", "RTRIM", "LENGTH", "CHAR_LENGTH", "CHARACTER_LENGTH", "SUBSTRING", "SUBSTR", "ABS",
    "ROUND", "FLOOR", "CEIL", "CEILING", "MOD", "INTDIV", "POWER", "POW", "SQRT", "EXP", "LN", "LOG", "LOG10", "SIGN",
    "LIKE", "ILIKE", "REPLACE", "LEFT", "RIGHT", "REVERSE", "LPAD", "RPAD", "STRPOS", "INSTR", "POSITION", "EXTRACT",
    "YEAR", "MONTH", "DAY", "HOUR", "MINUTE", "SECOND", "QUARTER", "WEEK", "DATE", "TIMESTAMP", "UNIX_TIMESTAMP",
    "DATE_ADD", "DATE_SUB", "DATEDIFF", "DATE_DIFF", "DATE_TRUNC", "TIMESTAMP_TRUNC", "CAST", "BITWISEAND", "BITWISEOR",
    "BITWISEXOR", "BITWISENOT", "SHIFTLEFT", "SHIFTRIGHT", "TRUNC", "TRUNCATE", "ASCII", "CHR", "INITCAP", "DIV",
    "REGEXP_CONTAINS", "REGEXPLIKE", "STARTS_WITH", "ENDS_WITH", "STARTSWITH", "ENDSWITH", "SAFE_DIVIDE",
}

_CMP = {exp.EQ: "=", exp.NEQ: "<>", exp.GT: ">", exp.GTE: ">=", exp.LT: "<", exp.LTE: "<="}
_NEGATE = {"=": "<>", "<>": "=", "<": ">=", ">=": "<", ">": "<=", "<=": ">"}
_FLIP = {"=": "=", "<>": "<>", "<": ">", ">": "<", "<=": ">=", ">=": "<="}

_EPOCH = datetime.date(1970, 1, 1)


def _day_number(text: str) -> int | None:
    m = re.fullmatch(r"\s*(\d{4})-(\d{1,2})-(\d{1,2})\s*", text)
    if not m:
        return None
    try:
        return (datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3))) - _EPOCH).days
    except ValueError:
        return None


def _date_of(day: int) -> datetime.date:
    return _EPOCH + datetime.timedelta(days=day)


def _add_months(day: int, months: int) -> int | None:
    d = _date_of(day)
    total = d.year * 12 + (d.month - 1) + months
    year, month = divmod(total, 12)
    try:
        return (datetime.date(year, month + 1, d.day) - _EPOCH).days
    except ValueError:
        return None  # the day does not exist in that month: engines differ (clamp or error)


def _number(text: str) -> Fraction:
    return Fraction(text)


class Translator:
    def __init__(self, catalog: Catalog, *, exact: bool = False, group_by_constants: bool = False):
        self.catalog = catalog
        self.dialect = catalog.dialect
        self.exact = exact
        self.group_by_constants = group_by_constants
        self.limit_sources = False  # a LIMIT query was read as an opaque relation named by its text

    # ---- entry ----------------------------------------------------------

    def translate(self, tree: exp.Expression) -> Query:
        for sub in tree.walk():
            name = type(sub).__name__
            if name in _NONDETERMINISTIC_TYPES:
                raise Unsupported(f"{name} is not modeled")
            if isinstance(sub, exp.Anonymous) and (sub.name or "").upper() in _NONDETERMINISTIC:
                raise Unsupported(f"{sub.name} is not modeled")
            if isinstance(sub, (exp.Pivot, exp.Unnest, exp.Lateral)):
                raise Unsupported(f"{name} is not modeled")
        return self.query(tree, None, {})

    # ---- queries --------------------------------------------------------

    def query(self, node, outer: Scope | None, ctes: dict) -> Query:
        # A parenthesized query's ORDER BY alone does not change the bag.
        while isinstance(node, exp.Subquery) and not any(node.args.get(k) for k in ("limit", "offset")):
            node = node.this
        if isinstance(node, exp.Subquery):
            raise Unsupported("a parenthesized query with ORDER BY or LIMIT")
        if node.args.get("limit") is not None or node.args.get("offset") is not None:
            if _limit_zero(node) and not node.args.get("offset"):
                bare = node.copy()
                for key in ("limit", "offset", "order"):
                    bare.set(key, None)
                q = self.query(bare, outer, ctes)
                return Query(q.out, ZERO, q.names)
            return self.limited(node, outer, ctes)
        if node.args.get("fetch") is not None:
            raise Unsupported("FETCH")
        with_clause = _with(node)
        if with_clause is not None:
            if with_clause.args.get("recursive"):
                raise Unsupported("recursive CTE")
            ctes = dict(ctes)
            for cte in with_clause.expressions:
                if cte.args.get("materialized") is False:
                    pass
                ctes[cte.alias_or_name.lower()] = (cte, dict(ctes), outer)
        if isinstance(node, exp.Select):
            return self.select(node, outer, ctes)
        if isinstance(node, (exp.Union, exp.Intersect, exp.Except)):
            return self.set_operation(node, outer, ctes)
        if isinstance(node, exp.Values):
            return self.values(node, outer, ctes)
        raise Unsupported(f"{type(node).__name__} query")

    def limited(self, node, outer, ctes) -> Query:
        """A query inside the query that ends in LIMIT n [OFFSET m] (n and m integer literals, n >= 1).

        Its select list may be all constants: every row is then the same tuple, so the limit keeps
        ``min(n, count - m)`` copies whatever rows it cuts. Otherwise the limited result is an opaque
        relation named by the query's text (``limit_sources``): the same text, over the same database,
        returns the same rows each time.
        """

        limit, offset = node.args.get("limit"), node.args.get("offset")
        if node.args.get("fetch") is not None or not isinstance(limit, exp.Limit) or limit.args.get("offset") is not None:
            raise Unsupported("LIMIT or OFFSET inside the query")
        count = _whole_number(limit.expression)
        skip = 0 if offset is None else _whole_number(offset.expression) if isinstance(offset, exp.Offset) else None
        if count is None or count < 1 or skip is None or not isinstance(node, exp.Select):
            raise Unsupported("LIMIT or OFFSET inside the query")
        bare = node.copy()
        for key in ("limit", "offset", "order"):
            bare.set(key, None)
        constants = all(isinstance(e.this if isinstance(e, exp.Alias) else e, (exp.Literal, exp.Null, exp.Boolean)) for e in node.expressions)
        if constants:
            q = self.query(bare, outer, ctes)
            if count == 1 and not skip:
                return Query(q.out, ind(Exists(q.body)), q.names)
            rest = NMonus(q.body, NConst(Fraction(skip))) if skip else q.body
            return Query(q.out, NMin(rest, NConst(Fraction(count))), q.names)
        for table in node.find_all(exp.Table):
            if not table.db and not table.catalog and table.name.lower() in ctes:
                raise Unsupported("a LIMIT query reading a CTE")
        q = self.query(bare, None, {})  # no outer scope: a correlated query is not one relation
        columns = [f"c{i}" for i in range(len(q.out))]
        key = "limit!" + node.sql(dialect=self.dialect)
        # LIMIT 1 keeps at most one row, once: any two of its rows are the same row (the empty key).
        keys = ((),) if count == 1 and not skip else ()
        self.catalog.tables[key] = TableInfo(key, columns, {c: v.kind for c, v in zip(columns, q.out)}, frozenset(), keys, ())
        self.limit_sources = True
        x = TVar(fresh_id(), key)
        out = tuple(SVar(fresh_id(), v.kind) for v in q.out)
        body = nsum((x,), nmul(NRel(x), ind(conj(*[Same(Ref(o), Col(x, c, v.kind)) for o, c, v in zip(out, columns, q.out)]))))
        return Query(out, body, q.names)

    def set_operation(self, node, outer, ctes) -> Query:
        for key in ("by_name", "side", "kind", "on"):
            if node.args.get(key):
                raise Unsupported("set operation variant")
        left = self.query(node.this, outer, ctes)
        right = self.query(node.expression, outer, ctes)
        if len(left.out) != len(right.out):
            raise Unsupported("set operation branches have different widths")
        out = []
        for a, b in zip(left.out, right.out):
            ka, kb = a.kind, b.kind
            if ka is not None and kb is not None and family(ka) != family(kb):
                raise Unsupported("set operation columns of different types")
            out.append(SVar(fresh_id(), ka if ka == kb else (ka or kb if family(ka) == family(kb) or None in (ka, kb) else None)))
        lb = subst(left.body, {v: Ref(o) for v, o in zip(left.out, out)})
        rb = subst(right.body, {v: Ref(o) for v, o in zip(right.out, out)})
        distinct = node.args.get("distinct")
        if isinstance(node, exp.Union):
            body = nadd(lb, rb)
            if distinct:
                body = ind(Exists(body))
        elif isinstance(node, exp.Intersect):
            body = nmul(ind(Exists(lb)), ind(Exists(rb))) if distinct or distinct is None else NMin(lb, rb)
        else:
            body = nmul(ind(Exists(lb)), ind(neg(Exists(rb)))) if distinct or distinct is None else NMonus(lb, rb)
        return Query(tuple(out), body, left.names)

    def values(self, node: exp.Values, outer, ctes, names: list | None = None) -> Query:
        rows = []
        for row in node.expressions:
            items = row.expressions if isinstance(row, exp.Tuple) else [row]
            rows.append([self.value(e, Scope({}, outer, ctes=ctes)) for e in items])
        if not rows:
            raise Unsupported("empty VALUES")
        width = len(rows[0])
        if any(len(r) != width for r in rows):
            raise Unsupported("VALUES rows of different widths")
        kinds = []
        for j in range(width):
            ks = {value_kind(r[j]) for r in rows if not (isinstance(r[j], Lit) and r[j].value is None)}
            fam = {family(k) for k in ks}
            if len(fam) > 1:
                raise Unsupported("VALUES column of mixed types")
            kinds.append("num" if ks == {"int", "num"} else (next(iter(ks)) if len(ks) == 1 else None))
        out = tuple(SVar(fresh_id(), k) for k in kinds)
        body = nadd(*[ind(conj(*[Same(Ref(o), v) for o, v in zip(out, r)])) for r in rows])
        alias = node.args.get("alias")
        cols = [c.name.lower() for c in (alias.columns if alias is not None else [])]
        if names is None:
            names = cols if len(cols) == width else [f"expr${i}" for i in range(width)]
        return Query(out, body, names)

    # ---- FROM -----------------------------------------------------------

    def table_rows(self, table: exp.Table, outer, ctes) -> Rows:
        if table.args.get("joins") or table.args.get("pivots") or table.args.get("laterals"):
            raise Unsupported("table modifiers")
        if not isinstance(table.this, exp.Identifier):
            raise Unsupported("table function")
        alias_node = table.args.get("alias")
        if alias_node is not None and alias_node.columns:
            raise Unsupported("column list on a table alias")
        alias = table.alias_or_name.lower()
        name = table.name.lower()
        if not table.db and not table.catalog and name in ctes:
            cte, cte_ctes, cte_outer = ctes[name]
            q = self.query(cte.this, cte_outer, cte_ctes)
            names = [c.name.lower() for c in (cte.args["alias"].columns if cte.args.get("alias") is not None else [])]
            if names:
                if len(names) != len(q.names):
                    raise Unsupported("CTE column list")
                q.names = names
            return self._derived_rows(alias, q)
        key = self.catalog.key(table)
        info = self.catalog.info(key)
        x = TVar(fresh_id(), key)
        if info.declared:
            src = Source([(c, Col(x, c, info.kinds.get(c))) for c in info.columns])
        else:
            src = Source([], dynamic=x, catalog=self.catalog)
        return Rows((x,), NRel(x), {alias: src})

    def _derived_rows(self, alias: str, q: Query) -> Rows:
        names = [n.lower() if n else "" for n in q.names]
        cols = [(n, Ref(v)) for n, v in zip(names, q.out)]
        return Rows(tuple(q.out), q.body, {alias: Source(cols)})

    def item_rows(self, item, outer, ctes) -> Rows:
        if isinstance(item, exp.Table):
            return self.table_rows(item, outer, ctes)
        if isinstance(item, exp.Subquery):
            alias_node = item.args.get("alias")
            alias = item.alias_or_name.lower() or f"$anon{fresh_id()}"
            q = self.query(item.this, outer, ctes)
            if alias_node is not None and alias_node.columns:
                names = [c.name.lower() for c in alias_node.columns]
                if len(names) != len(q.out):
                    raise Unsupported("derived table column list")
                q.names = names
            return self._derived_rows(alias, q)
        if isinstance(item, exp.Values):
            alias = item.alias_or_name.lower() or f"$anon{fresh_id()}"
            q = self.values(item, outer, ctes)
            return self._derived_rows(alias, q)
        raise Unsupported(f"FROM item {type(item).__name__}")

    def from_rows(self, select: exp.Select, outer, ctes) -> Rows:
        clause = _from(select)
        if clause is None:
            if select.args.get("joins"):
                raise Unsupported("joins without FROM")
            return Rows((), ONE, {})
        rows = self.item_rows(clause.this, outer, ctes)
        for join in select.args.get("joins") or []:
            rows = self.join(rows, join, outer, ctes)
        return rows

    def join(self, left: Rows, join: exp.Join, outer, ctes) -> Rows:
        if join.args.get("method") or join.args.get("global") or join.args.get("match_condition"):
            raise Unsupported("NATURAL, ASOF or other join methods")
        kind = (join.args.get("kind") or "").upper()
        side = (join.args.get("side") or "").upper()
        if kind in ("SEMI", "ANTI", "STRAIGHT_JOIN") or kind not in ("", "INNER", "CROSS", "OUTER"):
            raise Unsupported(f"{kind} join")
        right = self.item_rows(join.this, outer, ctes)
        overlap = set(left.sources) & set(right.sources)
        if overlap:
            raise Unsupported(f"duplicate alias {sorted(overlap)[0]}")
        sources = {**left.sources, **right.sources}
        using = dict(left.using)
        on_t = TRUE
        using_cols = join.args.get("using") or []
        scope = Scope(sources, outer, using=dict(left.using), ctes=ctes)
        if using_cols:
            if right.using:
                raise Unsupported("USING over a join that has USING")
            conds = []
            for ident in using_cols:
                name = ident.name.lower()
                lv = self._unqualified(name, Scope(left.sources, None, left.using))
                rv = self._unqualified(name, Scope(right.sources, None))
                conds.append(Cmp("=", lv, rv))
                if side == "RIGHT":
                    using[name] = rv
                elif side == "FULL":
                    using[name] = Ite(IsNull(lv), rv, lv)
                else:
                    using[name] = lv
            on_t = conj(*conds)
        on = join.args.get("on")
        if on is not None:
            on_t = conj(on_t, self.pred(on, scope)[0])
        if not side:
            if kind == "CROSS" and on is not None:
                raise Unsupported("CROSS JOIN with ON")
            return Rows(left.vars + right.vars, nmul(left.body, right.body, ind(on_t)), sources, using)
        # Outer joins: fresh output variables for every column, matched rows plus NULL-padded rows.
        lcols = self._columns_of(left)
        rcols = self._columns_of(right)
        ovars = [SVar(fresh_id(), value_kind(v)) for _, _, v in lcols + rcols]
        lvals = [v for _, _, v in lcols]
        rvals = [v for _, _, v in rcols]
        pad_l = [Lit(None, value_kind(v)) for v in lvals]
        pad_r = [Lit(None, value_kind(v)) for v in rvals]

        def bind(values):
            return ind(conj(*[Same(Ref(o), v) for o, v in zip(ovars, values)]))

        parts = [nsum(left.vars + right.vars, nmul(left.body, right.body, ind(on_t), bind(lvals + rvals)))]
        if side in ("LEFT", "FULL"):
            (lbody, lv2), m1 = freshen_free((left.body, tuple(lvals)), left.vars)
            (rbody, on2), m2 = freshen_free((right.body, on_t), right.vars)
            on2 = subst(on2, m1)
            unmatched = ind(neg(Exists(nsum(tuple(m2[v] for v in right.vars), nmul(rbody, ind(on2))))))
            parts.append(nsum(tuple(m1[v] for v in left.vars), nmul(lbody, unmatched, bind(list(lv2) + pad_r))))
        if side in ("RIGHT", "FULL"):
            (rbody, rv2), m2 = freshen_free((right.body, tuple(rvals)), right.vars)
            (lbody, on2), m1 = freshen_free((left.body, on_t), left.vars)
            on2 = subst(on2, m2)
            unmatched = ind(neg(Exists(nsum(tuple(m1[v] for v in left.vars), nmul(lbody, ind(on2))))))
            parts.append(nsum(tuple(m2[v] for v in right.vars), nmul(rbody, unmatched, bind(pad_l + list(rv2)))))
        # Rebuild the sources over the output variables.
        new_sources = {}
        index = 0
        for alias, src in list(left.sources.items()) + list(right.sources.items()):
            cols = []
            for name, _ in src.columns:
                cols.append((name, Ref(ovars[index])))
                index += 1
            new_sources[alias] = Source(cols)
        mapping = {v: Ref(o) for v, o in zip(lvals + rvals, ovars)}
        new_using = {name: replace(v, mapping) for name, v in using.items()}
        return Rows(tuple(ovars), nadd(*parts), new_sources, new_using)

    def _columns_of(self, rows: Rows) -> list:
        out = []
        for alias, src in rows.sources.items():
            if src.dynamic is not None:
                raise Unsupported("outer join over a table without declared columns")
            for name, v in src.columns:
                out.append((alias, name, v))
        return out

    # ---- SELECT ---------------------------------------------------------

    def select(self, node: exp.Select, outer, ctes) -> Query:
        for key in ("qualify", "laterals", "pivots", "connect", "match", "prewhere", "windows", "into", "sample", "settings", "format"):
            if node.args.get(key):
                raise Unsupported(f"{key.upper()} clause")
        distinct_node = node.args.get("distinct")
        if distinct_node is not None and distinct_node.args.get("on"):
            raise Unsupported("DISTINCT ON")
        if node.args.get("kind"):
            raise Unsupported("SELECT AS STRUCT/VALUE")
        if any(isinstance(e, exp.Window) for item in node.expressions for e in item.walk()):
            raise Unsupported("window function")
        group = node.args.get("group")
        having = node.args.get("having")
        grouped = group is not None or having is not None or any(_has_aggregate(e) for e in node.expressions)
        if grouped:
            q = self.grouped(node, outer, ctes)
        else:
            rows = self.from_rows(node, outer, ctes)
            scope = Scope(rows.sources, outer, rows.using, ctes)
            where = node.args.get("where")
            cond = self.pred(where.this, scope)[0] if where is not None else TRUE
            items = self.select_items(node, scope)
            out = tuple(SVar(fresh_id(), value_kind(v)) for _, v in items)
            body = nsum(rows.vars, nmul(rows.body, ind(cond), ind(conj(*[Same(Ref(o), v) for o, (_, v) in zip(out, items)]))))
            single = tuple(v for _, v in items) if _from(node) is None and cond == TRUE else None
            q = Query(out, body, [n for n, _ in items], single)
        if distinct_node is not None:
            q = Query(q.out, ind(Exists(q.body)), q.names)
        return q

    def select_items(self, node: exp.Select, scope: Scope) -> list:
        items = []
        for item in node.expressions:
            if isinstance(item, exp.Star):
                if scope.using:
                    raise Unsupported("SELECT * over USING")
                for alias, src in self._sources(scope).items():
                    items.extend(src.star())
                continue
            if isinstance(item, exp.Column) and isinstance(item.this, exp.Star):
                alias = item.table.lower()
                src = scope.sources.get(alias)
                if src is None:
                    raise Unsupported(f"unknown alias {alias}")
                items.extend(src.star())
                continue
            if isinstance(item, exp.Alias) and isinstance(item.this, exp.Star):
                raise Unsupported("aliased star")
            expr = item.this if isinstance(item, exp.Alias) else item
            name = item.alias_or_name.lower() if item.alias_or_name else ""
            items.append((name, self.value(expr, scope)))
        if not items:
            raise Unsupported("empty select list")
        return items

    @staticmethod
    def _sources(scope: Scope) -> dict:
        return scope.sources

    def grouped(self, node: exp.Select, outer, ctes) -> Query:
        group = node.args.get("group")
        group_exprs = []
        if group is not None:
            for key in ("grouping_sets", "cube", "rollup", "totals"):
                if group.args.get(key):
                    raise Unsupported("ROLLUP, CUBE or GROUPING SETS")
            if group.args.get("all"):
                raise Unsupported("GROUP BY ALL")
            group_exprs = list(group.expressions)
        select_list = [item for item in node.expressions]
        if any(isinstance(i, exp.Star) or (isinstance(i, exp.Column) and isinstance(i.this, exp.Star)) for i in select_list):
            raise Unsupported("SELECT * with GROUP BY")
        aliases = {}
        for item in select_list:
            if isinstance(item, exp.Alias) and item.alias:
                aliases.setdefault(item.alias.lower(), item.this)
        # Resolve ordinals and aliases in GROUP BY.
        resolved = []
        for g in group_exprs:
            if isinstance(g, exp.Literal) and not g.is_string:
                if self.group_by_constants:
                    resolved.append(g)
                    continue
                position = int(g.this)
                if not 1 <= position <= len(select_list):
                    raise Unsupported("GROUP BY ordinal out of range")
                target = select_list[position - 1]
                resolved.append(target.this if isinstance(target, exp.Alias) else target)
                continue
            resolved.append(g)
        where = node.args.get("where")

        def copy_rows():
            rows = self.from_rows(node, outer, ctes)
            scope = Scope(rows.sources, outer, rows.using, ctes)
            cond = self.pred(where.this, scope)[0] if where is not None else TRUE
            keys = [self._group_key(g, scope, aliases) for g in resolved]
            return rows, scope, cond, keys

        rows0, scope0, cond0, keys0 = copy_rows()
        gvars = [SVar(fresh_id(), value_kind(k)) for k in keys0]

        def member(rows, cond, keys):
            return nmul(rows.body, ind(cond), ind(conj(*[Same(Ref(g), k) for g, k in zip(gvars, keys)])))

        def make_agg(call, filter_where=None) -> object:
            func = _AGG_TYPES[type(call)]
            rows, scope, cond, keys = copy_rows()
            if filter_where is not None:
                # ``agg(..) FILTER (WHERE c)`` aggregates only the rows where c is TRUE.
                if _has_aggregate(filter_where) or any(isinstance(x, exp.Subquery) for x in filter_where.walk()):
                    raise Unsupported("aggregate FILTER with an aggregate or subquery")
                cond = conj(cond, self.pred(filter_where, scope)[0])
            arg = call.this
            distinct = False
            arguments = list(call.args.get("expressions") or [])
            if isinstance(arg, exp.Distinct):
                distinct = True
                arguments = list(arg.expressions) + arguments
                arg = arguments[0] if arguments else None
                arguments = arguments[1:]
            if arguments and not isinstance(call, exp.Count):
                raise Unsupported(f"{func} with extra arguments")
            for k in ("order", "limit", "ignore_nulls", "respect_nulls", "having_max"):
                if call.args.get(k):
                    raise Unsupported(f"{func} modifiers")
            if func == "COUNT" and not arguments and (arg is None or isinstance(arg, exp.Star)):
                if distinct:
                    raise Unsupported("COUNT(DISTINCT *)")
                value = None
            elif func == "COUNTIF":
                t, f = self.pred(arg, scope)
                value = BoolV(t, f)
            elif arguments:
                # COUNT([DISTINCT] a, b) counts the rows (distinct pairs) where every argument is non-NULL
                # (MySQL, Spark, Calcite). The pair is a strict function of its arguments, so it is NULL
                # when any of them is; the function is uninterpreted, so a proof holds for the injective one.
                parts = [arg] + arguments
                if any(isinstance(a, exp.Star) or _has_aggregate(a) for a in parts):
                    raise Unsupported("COUNT over several arguments with a star or an aggregate")
                value = self._fn(f"TUPLE{len(parts)}", [self.value(a, scope) for a in parts], True, None)
            else:
                if _has_aggregate(arg):
                    raise Unsupported("nested aggregate")
                value = self.value(arg, scope)
            if func in ("LOGICAL_AND", "LOGICAL_OR") and value_kind(value) not in ("bool", None):
                raise Unsupported("LOGICAL_AND over a non-boolean")
            return Agg(func, distinct, rows.vars, member(rows, cond, keys), value)

        gscope = Scope(scope0.sources, outer, scope0.using, ctes)
        gscope.aliases = aliases
        gscope.group = GroupCtx(keys0, gvars, frozenset(rows0.vars), make_agg, scope0)
        items = []
        for item in select_list:
            expr = item.this if isinstance(item, exp.Alias) else item
            name = item.alias_or_name.lower() if item.alias_or_name else ""
            items.append((name, self.value(expr, gscope)))
        having = node.args.get("having")
        hcond = self.pred(having.this, gscope)[0] if having is not None else TRUE
        out = tuple(SVar(fresh_id(), value_kind(v)) for _, v in items)
        bind = ind(conj(*[Same(Ref(o), v) for o, (_, v) in zip(out, items)]))
        if not resolved:
            body = nmul(ind(hcond), bind)
            single = tuple(v for _, v in items) if having is None else None
            return Query(out, body, [n for n, _ in items], single)
        exists = ind(Exists(nsum(rows0.vars, member(rows0, cond0, keys0))))
        body = nsum(tuple(gvars), nmul(exists, ind(hcond), bind))
        return Query(out, body, [n for n, _ in items])

    def _group_key(self, g, scope: Scope, aliases: dict):
        if isinstance(g, exp.Literal) and not g.is_string and self.group_by_constants:
            return self.value(g, scope)
        if isinstance(g, exp.Column) and not g.table and g.name.lower() in aliases:
            name = g.name.lower()
            has_column = any(src.has(name) is not False for src in scope.sources.values()) or name in scope.using
            alias_expr = aliases[name]
            same = isinstance(alias_expr, exp.Column) and alias_expr.name.lower() == name
            if has_column and not same:
                if any(src.has(name) is True for src in scope.sources.values()) or name in scope.using:
                    # MySQL and BigQuery resolve GROUP BY names to columns first only in some versions
                    raise Unsupported(f"{name} is both a column and a select alias")
            if not has_column or same:
                return self.value(alias_expr, scope)
        if _has_aggregate(g):
            raise Unsupported("aggregate in GROUP BY")
        return self.value(g, scope)

    # ---- columns --------------------------------------------------------

    def _unqualified(self, name: str, scope: Scope):
        if name in scope.using:
            return scope.using[name]
        found = [s for s in scope.sources.values() if s.has(name) is True]
        maybe = [s for s in scope.sources.values() if s.has(name) is None]
        if len(found) == 1 and not maybe:
            return found[0].lookup(name)
        if not found and len(maybe) == 1 and len(scope.sources) == 1:
            return maybe[0].lookup(name)
        if found or maybe:
            raise Unsupported(f"cannot resolve column {name}")
        return None

    def column(self, col: exp.Column, scope: Scope):
        if isinstance(col.this, exp.Star):
            raise Unsupported("star in expression")
        if col.args.get("db") or col.args.get("catalog"):
            raise Unsupported("column path")
        name = col.name.lower()
        table = col.table.lower()
        cur = scope
        while cur is not None:
            if table:
                if table in cur.sources:
                    v = cur.sources[table].lookup(name)
                    if v is None:
                        raise Unsupported(f"{table}.{name} is not a column")
                    return self._through_group(v, cur)
            else:
                v = self._unqualified(name, cur)
                if v is not None:
                    if cur.aliases and name in cur.aliases:
                        alias_expr = cur.aliases[name]
                        if not (isinstance(alias_expr, exp.Column) and alias_expr.name.lower() == name):
                            raise Unsupported(f"{name} is both a column and a select alias")
                    return self._through_group(v, cur)
                if cur.aliases and name in cur.aliases:
                    # A select-list alias (HAVING / ORDER BY): read its expression in this scope.
                    alias_expr = cur.aliases[name]
                    return self.value(alias_expr, cur)
            cur = cur.outer
        raise Unsupported(f"unknown column {col.sql()}")

    def _through_group(self, v, scope: Scope):
        g = scope.group
        if g is None:
            return v
        for key, var in zip(g.keys, g.gvars):
            if key == v:
                return Ref(var)
        raise Unsupported("a column that is neither grouped nor aggregated")

    # ---- values ---------------------------------------------------------

    def value(self, e, scope: Scope):
        g = scope.group
        if g is not None and not isinstance(e, tuple(_AGG_TYPES)) and not _has_aggregate(e) and not any(isinstance(x, exp.Subquery) for x in e.walk()):
            # A grouped select: an expression equal to a group key is that key.
            try:
                base = self.value(e, g.base_scope)
            except Unsupported:
                base = None
            if base is not None:
                for key, var in zip(g.keys, g.gvars):
                    if key == base:
                        return Ref(var)
                from .ir import free_vars

                if not (free_vars(base) & g.base_vars):
                    return base
        return self._value(e, scope)

    def _value(self, e, scope: Scope):
        if isinstance(e, exp.Paren):
            return self.value(e.this, scope)
        if isinstance(e, exp.Column):
            return self.column(e, scope)
        if isinstance(e, exp.Literal):
            if e.is_string:
                return Lit(e.this, "str")
            text = e.this
            try:
                number = _number(text)
            except (ValueError, ZeroDivisionError):
                raise Unsupported(f"number {text}")
            return Lit(number, "int" if re.fullmatch(r"-?\d+", text.strip()) else "num")
        if isinstance(e, exp.Null):
            return NULL
        if isinstance(e, exp.Boolean):
            return Lit(bool(e.this), "bool")
        if isinstance(e, exp.Neg):
            inner = self.value(e.this, scope)
            if isinstance(inner, Lit) and isinstance(inner.value, Fraction):
                return Lit(-inner.value, inner.kind)
            return self._arith("-", Lit(Fraction(0), "int"), inner)
        if isinstance(e, tuple(_AGG_TYPES)):
            if scope.group is None:
                raise Unsupported("aggregate outside a grouped select")
            return scope.group.make_agg(e)
        if isinstance(e, exp.Filter) and isinstance(e.this, tuple(_AGG_TYPES)) and isinstance(e.expression, exp.Where):
            if scope.group is None:
                raise Unsupported("aggregate outside a grouped select")
            return scope.group.make_agg(e.this, e.expression.this)
        if isinstance(e, (exp.Add, exp.Sub, exp.Mul, exp.Div)):
            return self._binary(e, scope)
        if isinstance(e, exp.IntDiv):
            return self._fn("DIV", [self.value(e.this, scope), self.value(e.expression, scope)], True, "int")
        if isinstance(e, exp.Mod):
            a, b = self.value(e.this, scope), self.value(e.expression, scope)
            if isinstance(a, Lit) and isinstance(b, Lit) and isinstance(a.value, Fraction) and isinstance(b.value, Fraction) and b.value != 0 and a.value.denominator == b.value.denominator == 1 and a.value >= 0 and b.value > 0:
                return Lit(Fraction(int(a.value) % int(b.value)), "int")
            return self._fn("MOD", [a, b], True, value_kind(a))
        if isinstance(e, exp.Case):
            return self._case(e, scope)
        if isinstance(e, exp.If):
            t, _ = self.pred(e.this, scope)
            a = self.value(e.args["true"], scope)
            b = self.value(e.args["false"], scope) if e.args.get("false") is not None else NULL
            return Ite(t, a, b)
        if isinstance(e, exp.Coalesce):
            args = [self.value(e.this, scope)] + [self.value(x, scope) for x in e.expressions]
            result = args[-1]
            for a in reversed(args[:-1]):
                result = Ite(neg(IsNull(a)), a, result)
            return result
        if isinstance(e, exp.Nullif):
            a, b = self.value(e.this, scope), self.value(e.expression, scope)
            return Ite(Cmp("=", a, b), Lit(None, value_kind(a)), a)
        if isinstance(e, (exp.Cast, exp.TryCast)):
            return self._cast(e, scope)
        if isinstance(e, exp.Subquery):
            return self._scalar(e, scope)
        if isinstance(e, (exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE, exp.And, exp.Or, exp.Not, exp.Is, exp.In, exp.Between, exp.Exists, exp.Like, exp.ILike, exp.NullSafeEQ, exp.NullSafeNEQ)):
            t, f = self.pred(e, scope)
            return BoolV(t, f)
        if isinstance(e, exp.Extract):
            unit = e.this.name.upper() if isinstance(e.this, (exp.Var, exp.Identifier)) else e.this.sql().upper()
            arg = self.value(e.expression, scope)
            if isinstance(arg, Lit) and arg.kind == "date" and unit in ("YEAR", "MONTH", "DAY"):
                d = _date_of(int(arg.value))
                return Lit(Fraction(getattr(d, unit.lower())), "int")
            return self._fn(f"EXTRACT_{unit}", [arg], True, "int")
        if isinstance(e, (exp.DPipe,)):
            return self._fn("CONCAT", [self.value(e.this, scope), self.value(e.expression, scope)], self.dialect in ("mysql", "bigquery", "duckdb", "postgres", "calcite"), "str")
        if isinstance(e, exp.Concat):
            args = [self.value(x, scope) for x in e.expressions]
            strict = self.dialect in ("mysql", "bigquery") and not e.args.get("coalesce")
            if not strict:
                raise Unsupported("CONCAT that skips NULLs")
            return self._fn("CONCAT", args, True, "str")
        if isinstance(e, (exp.DateAdd, exp.DateSub)):
            return self._date_shift(e, scope)
        if isinstance(e, exp.Interval):
            raise Unsupported("INTERVAL value")
        if isinstance(e, exp.Window):
            raise Unsupported("window function")
        if isinstance(e, (exp.Tuple, exp.Array, exp.Struct, exp.Bracket, exp.Dot)):
            raise Unsupported(f"{type(e).__name__} value")
        if isinstance(e, exp.Func):
            name = (e.sql_name() if not isinstance(e, exp.Anonymous) else e.name).upper()
            if name in _NONDETERMINISTIC or type(e).__name__ in _NONDETERMINISTIC_TYPES:
                raise Unsupported(f"{name} is not modeled")
            args = []
            for key, arg in e.args.items():
                if arg is None or key in ("safe", "big_int"):
                    continue
                if isinstance(arg, list):
                    args.extend(self.value(a, scope) if isinstance(a, exp.Expression) else Lit(str(a), "str") for a in arg)
                elif isinstance(arg, exp.Expression):
                    if isinstance(arg, (exp.Var, exp.DataType)):
                        args.append(Lit(f"{key}={arg.sql().upper()}", "str"))
                    else:
                        args.append(self.value(arg, scope))
                elif isinstance(arg, bool):
                    args.append(Lit(f"{key}={arg}", "str"))
                else:
                    args.append(Lit(f"{key}={arg}", "str"))
            uname = type(e).__name__.upper() if not isinstance(e, exp.Anonymous) else name
            return self._fn(uname, args, uname in _STRICT or name in _STRICT, None)
        raise Unsupported(f"expression {type(e).__name__}")

    def _fn(self, name, args, strict, kind):
        return Fn(name, tuple(args), strict, kind)

    def _binary(self, e, scope):
        a, b = self.value(e.this, scope), self.value(e.expression, scope)
        op = {exp.Add: "+", exp.Sub: "-", exp.Mul: "*", exp.Div: "/"}[type(e)]
        if isinstance(e.expression, exp.Interval) or isinstance(e.this, exp.Interval):
            return self._interval(e, scope, op)
        ka, kb = value_kind(a), value_kind(b)
        if ka in ("date", "time") or kb in ("date", "time") or ka == "str" or kb == "str":
            if ka in ("date",) and kb in ("int",) and op in "+-" and isinstance(a, Lit) and isinstance(b, Lit):
                return Lit(a.value + b.value if op == "+" else a.value - b.value, "date")
            return self._fn(f"ARITH{op}", [a, b], True, None)
        if isinstance(e, exp.Div):
            if self.dialect == "bigquery" and not e.args.get("safe"):
                pass
            if e.args.get("typed") and ka == kb == "int":
                return self._fn("DIV", [a, b], True, "int")
        return self._arith(op, a, b)

    def _arith(self, op, a, b):
        ka, kb = value_kind(a), value_kind(b)
        kind = "int" if ka == kb == "int" and op in "+-*" else ("num" if family(ka) == "num" or family(kb) == "num" else None)
        if isinstance(a, Lit) and isinstance(b, Lit):
            if a.value is None or b.value is None:
                return Lit(None, kind)
            if isinstance(a.value, Fraction) and isinstance(b.value, Fraction) and self.exact:
                if op == "+":
                    return Lit(a.value + b.value, kind)
                if op == "-":
                    return Lit(a.value - b.value, kind)
                if op == "*":
                    return Lit(a.value * b.value, kind)
                if op == "/" and b.value != 0:
                    return Lit(a.value / b.value, "num")
        if not self.exact:
            return self._fn(f"ARITH{op}", [a, b], True, kind)
        return Arith(op, a, b, kind)

    def _interval(self, e, scope, op):
        base, interval = (e.this, e.expression) if isinstance(e.expression, exp.Interval) else (e.expression, e.this)
        if op not in "+-" or (op == "-" and base is not e.this):
            raise Unsupported("interval arithmetic")
        v = self.value(base, scope)
        amount = interval.this
        unit = interval.args.get("unit")
        unit = unit.name.upper() if unit is not None else None
        text = amount.this if isinstance(amount, exp.Literal) else None
        if text is not None and unit is None:
            parts = str(text).split()
            if len(parts) == 2:
                text, unit = parts[0], parts[1].upper()
        if text is None or unit is None or not re.fullmatch(r"-?\d+", str(text).strip()):
            raise Unsupported("interval")
        n = int(str(text).strip()) * (1 if op == "+" else -1)
        return self._shift(v, n, unit.rstrip("S"))

    def _date_shift(self, e, scope):
        v = self.value(e.this, scope)
        amount = e.expression
        unit = e.args.get("unit")
        unit = unit.name.upper() if unit is not None else "DAY"
        if isinstance(amount, exp.Interval):
            unit = amount.args.get("unit").name.upper() if amount.args.get("unit") is not None else unit
            amount = amount.this
        if not isinstance(amount, exp.Literal) or not re.fullmatch(r"-?\d+", str(amount.this).strip()):
            raise Unsupported("date arithmetic")
        n = int(str(amount.this).strip()) * (-1 if isinstance(e, exp.DateSub) else 1)
        return self._shift(v, n, unit.rstrip("S"))

    def _shift(self, v, n, unit):
        if isinstance(v, Lit) and v.kind == "str":
            day = _day_number(v.value)
            if day is None:
                raise Unsupported("date literal")
            v = Lit(Fraction(day), "date")
        if isinstance(v, Lit) and v.kind == "date" and v.value is not None:
            day = int(v.value)
            if unit == "DAY":
                return Lit(Fraction(day + n), "date")
            if unit == "WEEK":
                return Lit(Fraction(day + 7 * n), "date")
            if unit in ("MONTH", "QUARTER", "YEAR"):
                months = n * {"MONTH": 1, "QUARTER": 3, "YEAR": 12}[unit]
                shifted = _add_months(day, months)
                if shifted is None:
                    raise Unsupported("date arithmetic past the end of a month")
                return Lit(Fraction(shifted), "date")
        return self._fn(f"DATE_ADD_{unit}", [v, Lit(Fraction(n), "int")], True, value_kind(v))

    def _cast(self, e, scope):
        to = e.args.get("to")
        target = to.sql(dialect="mysql").upper() if to is not None else ""
        kind = kind_of_type(target)
        inner = e.this
        v = self.value(inner, scope)
        if kind == "date" and isinstance(v, Lit) and v.kind == "str":
            day = _day_number(v.value)
            if day is None:
                raise Unsupported("date literal")
            return Lit(Fraction(day), "date")
        if isinstance(v, Lit) and v.value is None:
            return Lit(None, kind)
        src = value_kind(v)
        has_params = to is not None and bool(to.expressions)
        if kind is not None and src == kind and not (kind == "str" and has_params) and not (kind == "num" and has_params):
            return v
        if kind == "num" and src == "int" and not has_params and self.exact:
            return v
        if kind == "int" and isinstance(v, Lit) and v.kind == "int":
            return v
        if kind == "str" and isinstance(v, Lit) and v.kind == "str" and not has_params:
            return v
        return self._fn(f"CAST_{target}", [v], True, kind)

    def _case(self, e: exp.Case, scope):
        subject = e.this
        default = self.value(e.args["default"], scope) if e.args.get("default") is not None else NULL
        result = default
        ifs = e.args.get("ifs") or []
        subject_v = self.value(subject, scope) if subject is not None else None
        for branch in reversed(ifs):
            if subject_v is not None:
                cond = self._compare("=", subject_v, self.value(branch.this, scope))
            else:
                cond = self.pred(branch.this, scope)[0]
            result = Ite(cond, self.value(branch.args["true"], scope), result)
        return result

    def _scalar(self, e: exp.Subquery, scope):
        q = self.query(e.this, scope, scope.ctes)
        if len(q.out) != 1:
            raise Unsupported("scalar subquery with several columns")
        if q.single is not None:
            return q.single[0]
        return Scalar(q.out[0], q.body)

    # ---- predicates -----------------------------------------------------

    def pred(self, e, scope: Scope):
        """``(is TRUE, is FALSE)`` formulas of a SQL predicate."""

        if isinstance(e, exp.Paren):
            return self.pred(e.this, scope)
        if isinstance(e, exp.And):
            a, b = self.pred(e.this, scope), self.pred(e.expression, scope)
            return conj(a[0], b[0]), disj(a[1], b[1])
        if isinstance(e, exp.Or):
            a, b = self.pred(e.this, scope), self.pred(e.expression, scope)
            return disj(a[0], b[0]), conj(a[1], b[1])
        if isinstance(e, exp.Not):
            t, f = self.pred(e.this, scope)
            return f, t
        if isinstance(e, tuple(_CMP)):
            op = _CMP[type(e)]
            right = e.expression
            if isinstance(right, (exp.Any, exp.All)):
                return self._quantified(op, e.this, right, scope)
            if isinstance(e.this, (exp.Any, exp.All)):
                return self._quantified(_FLIP[op], right, e.this, scope)
            if isinstance(e.this, exp.Tuple) or isinstance(right, exp.Tuple):
                raise Unsupported("row comparison")
            a, b = self.value(e.this, scope), self.value(right, scope)
            return self._compare(op, a, b), self._compare(_NEGATE[op], a, b)
        if isinstance(e, exp.NullSafeEQ):
            a, b = self.value(e.this, scope), self.value(e.expression, scope)
            s = self._same(a, b)
            return s, neg(s)
        if isinstance(e, exp.NullSafeNEQ):
            a, b = self.value(e.this, scope), self.value(e.expression, scope)
            s = self._same(a, b)
            return neg(s), s
        if isinstance(e, exp.Is):
            target = e.expression
            v = self.value(e.this, scope) if not isinstance(e.this, (exp.EQ, exp.And, exp.Or, exp.Not)) else None
            if isinstance(target, exp.Null):
                if v is None:
                    t, f = self.pred(e.this, scope)
                    isn = conj(neg(t), neg(f))
                else:
                    isn = IsNull(v)
                return isn, neg(isn)
            if isinstance(target, exp.Boolean):
                t, f = self.pred(e.this, scope)
                r = t if target.this else f
                return r, neg(r)
            raise Unsupported("IS with a non-literal")
        if isinstance(e, exp.Between):
            if e.args.get("symmetric"):
                raise Unsupported("BETWEEN SYMMETRIC")
            x = self.value(e.this, scope)
            lo, hi = self.value(e.args["low"], scope), self.value(e.args["high"], scope)
            a = (self._compare(">=", x, lo), self._compare("<", x, lo))
            b = (self._compare("<=", x, hi), self._compare(">", x, hi))
            return conj(a[0], b[0]), disj(a[1], b[1])
        if isinstance(e, exp.In):
            return self._in(e, scope)
        if isinstance(e, exp.Exists):
            sub = e.this
            q = self.query(sub.this if isinstance(sub, exp.Subquery) else sub, scope, scope.ctes)
            x = Exists(nsum(q.out, q.body))
            return x, neg(x)
        if isinstance(e, exp.Boolean):
            return (TRUE, FALSE) if e.this else (FALSE, TRUE)
        if isinstance(e, exp.Null):
            return FALSE, FALSE
        if isinstance(e, (exp.Like, exp.ILike)):
            if e.args.get("escape"):
                raise Unsupported("LIKE ESCAPE")
            a, b = self.value(e.this, scope), self.value(e.expression, scope)
            v = Fn("LIKE" if isinstance(e, exp.Like) else "ILIKE", (a, b), True, "bool")
            return Truth(v), conj(neg(IsNull(v)), neg(Truth(v)))
        if isinstance(e, exp.Escape):
            raise Unsupported("LIKE ESCAPE")
        v = self.value(e, scope)
        if value_kind(v) not in ("bool", None):
            if self.dialect == "mysql" and family(value_kind(v)) == "num":
                return self._compare("<>", v, Lit(Fraction(0), "int")), self._compare("=", v, Lit(Fraction(0), "int"))
            raise Unsupported("a non-boolean value used as a condition")
        if isinstance(v, BoolV):
            return v.t, v.f
        return Truth(v), conj(neg(IsNull(v)), neg(Truth(v)))

    def _same(self, a, b):
        a, b = self._coerce(a, b)
        return Same(a, b)

    def _coerce(self, a, b):
        """A string literal compared with a date column is a date."""

        ka, kb = value_kind(a), value_kind(b)
        if ka == "date" and isinstance(b, Lit) and b.kind == "str":
            day = _day_number(b.value)
            if day is None:
                raise Unsupported("date literal")
            b = Lit(Fraction(day), "date")
        elif kb == "date" and isinstance(a, Lit) and a.kind == "str":
            day = _day_number(a.value)
            if day is None:
                raise Unsupported("date literal")
            a = Lit(Fraction(day), "date")
        ka, kb = value_kind(a), value_kind(b)
        if ka is not None and kb is not None and family(ka) != family(kb) and not (isinstance(a, Lit) and a.value is None) and not (isinstance(b, Lit) and b.value is None):
            raise Unsupported(f"comparison of {ka} with {kb}")
        return a, b

    def _compare(self, op, a, b):
        a, b = self._coerce(a, b)
        if isinstance(a, Lit) and isinstance(b, Lit):
            if a.value is None or b.value is None:
                return FALSE
            if type(a.value) is type(b.value) or (isinstance(a.value, Fraction) and isinstance(b.value, Fraction)):
                if isinstance(a.value, str) and op not in ("=", "<>"):
                    return Cmp(op, a, b)
                res = {"=": a.value == b.value, "<>": a.value != b.value, "<": a.value < b.value, "<=": a.value <= b.value, ">": a.value > b.value, ">=": a.value >= b.value}[op]
                return TRUE if res else FALSE
        return Cmp(op, a, b)

    def _in(self, e: exp.In, scope):
        x = e.this
        if isinstance(x, exp.Tuple):
            xs = [self.value(i, scope) for i in x.expressions]
        else:
            xs = [self.value(x, scope)]
        query = e.args.get("query")
        if e.args.get("unnest") or e.args.get("field"):
            raise Unsupported("IN UNNEST")
        if query is not None:
            q = self.query(query.this if isinstance(query, exp.Subquery) else query, scope, scope.ctes)
            if len(q.out) != len(xs):
                raise Unsupported("IN subquery width")
            match = conj(*[self._compare("=", a, Ref(o)) for a, o in zip(xs, q.out)])
            # FALSE: no row compares TRUE or UNKNOWN, i.e. every row compares FALSE.
            not_false = neg(disj(*[self._compare("<>", a, Ref(o)) for a, o in zip(xs, q.out)]))
            q2_body, m = freshen_free(q.body, q.out)
            not_false2 = subst(not_false, {o: Ref(m[o]) for o in q.out})
            t = Exists(nsum(q.out, nmul(q.body, ind(match))))
            f = neg(Exists(nsum(tuple(m[o] for o in q.out), nmul(q2_body, ind(not_false2)))))
            return t, f
        items = e.expressions
        if len(xs) != 1:
            raise Unsupported("row IN list")
        ts, fs = [], []
        for item in items:
            v = self.value(item, scope)
            ts.append(self._compare("=", xs[0], v))
            fs.append(self._compare("<>", xs[0], v))
        return disj(*ts), conj(*fs)

    def _quantified(self, op, left, quantified, scope):
        sub = quantified.this
        if not isinstance(sub, exp.Subquery):
            raise Unsupported("ANY/ALL over a non-subquery")
        q = self.query(sub.this, scope, scope.ctes)
        if len(q.out) != 1:
            raise Unsupported("ANY/ALL over several columns")
        x = self.value(left, scope)
        o = Ref(q.out[0])
        body2, m = freshen_free(q.body, q.out)
        o2 = Ref(m[q.out[0]])
        if isinstance(quantified, exp.Any):
            t = Exists(nsum(q.out, nmul(q.body, ind(self._compare(op, x, o)))))
            f = neg(Exists(nsum((m[q.out[0]],), nmul(body2, ind(neg(self._compare(_NEGATE[op], x, o2)))))))
            return t, f
        t = neg(Exists(nsum(q.out, nmul(q.body, ind(neg(self._compare(op, x, o)))))))
        f = Exists(nsum((m[q.out[0]],), nmul(body2, ind(self._compare(_NEGATE[op], x, o2)))))
        return t, f


def _has_aggregate(e) -> bool:
    """An aggregate call in ``e`` that belongs to this select (not inside a subquery)."""

    if e is None:
        return False
    stack = [e]
    while stack:
        cur = stack.pop()
        if isinstance(cur, tuple(_AGG_TYPES)):
            return True
        if isinstance(cur, (exp.Subquery, exp.Select)) and cur is not e:
            continue
        for child in cur.iter_expressions():
            stack.append(child)
    return False


