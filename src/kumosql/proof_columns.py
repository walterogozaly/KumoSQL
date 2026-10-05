"""Independent re-derivation of which FROM item a bare column belongs to, for the provers' own resolution.

``proof_qualify`` checks the ``qualify_columns`` rule. The algebraic and SMT provers resolve bare columns too:
the SMT compiler picks a source for every unqualified name, the algebraic normalizer writes ``a.x`` for a bare
``x`` before its rewrites read a derived table as its base table, and the scoped-identity check hands the
question to sqlglot's qualifier. A wrong owner chosen there (an outer column read instead of a select alias, a
name another source shares) would be agreed on by both sides of a proof. This module re-reads the statement's
text, builds the scopes itself and answers, for one column, which FROM item owns it. The provers hand over what
they chose, and a disagreement makes the proof ``not_proven``.

It imports no rule, normalizer, lifter or prover: only the record types of ``proof_steps`` and the scope helpers
of ``proof_qualify`` (which has the same independence).

What is decided, per bare column, from the text and the supplied table columns alone:

* the select that reads it, the sources that place can read (a JOIN ... ON reads the sources up to and including
  its own join, an UNNEST argument the sources before it) and, outward, the enclosing selects of a subquery;
* the sources' columns: a CTE or derived table that names every output, an aliased UNNEST (its element and offset;
  a struct element may expose more names), a physical table whose columns the caller supplies. A source whose
  columns are not known cannot be excluded, so the name is ``undecided`` unless elimination leaves one candidate;
* ``owner`` when exactly one source of the innermost scope that has any has the name; ``ambiguous`` when several
  do; ``output`` when GROUP BY, HAVING, QUALIFY, ORDER BY or a named window reads a select alias first;
  ``using`` for a merged USING column; ``undecided`` for anything else (a name that is a source's name, a correlated
  reference past a source of unknown columns, a lambda, a pivot, a NATURAL join).

It assumes that the original statement runs on BigQuery (so an ambiguous name is an error the engine would
raise, and elimination is sound) and that the supplied column lists are complete. Only BigQuery is read.

Three ways the provers use it, all of which can only remove a proof:

* **a choice** (``StatementReader.judge``): the SMT compiler tags the columns and FROM items of the statement it
  parses (:func:`tag`), and says which FROM item it resolved a bare column to, or that it read a select alias;
* **a qualification pass** (:func:`guarded_qualification`): a normalizer pass that only adds qualifiers is checked
  from the text before and after, as ``proof_qualify`` checks a rule;
* **a qualified tree** (:meth:`StatementReader.judge_qualified`): sqlglot's qualifier ran on a tagged tree and
  every tagged column that gained a qualifier is compared with the independent owner.

* **a column-rebuilding rewrite** (:func:`begin` and :meth:`Rewrite.verify`): a rewrite that reads a derived table as
  its base table, flattens a join, a CTE or a derived table, renames an alias or pushes a qualifier through a
  subquery rebuilds columns without choosing among sources by name. The statement it rewrote is numbered (every
  column and FROM item), printed and read before the rewrite and again after it, and every column that survived
  (and every column the rewrite says it rebuilt from another) must read the same *leaves* afterwards: the base
  tables' columns it chases through plain derived tables and CTEs, naming the table by the number of the FROM item
  it came from. A name that now binds to another source, a projection read from the wrong output, a table
  instance swapped for another, all change the leaves.

A column the text cannot decide (a source whose columns nobody listed, a name that is also a source's name) is
``unchecked``: the proof stands, because turning a proof the prover reached into a refusal on missing knowledge
would lose correct proofs (the check is evidence, not a second prover). An error inside the check is not a column
it could not decide: it is a refusal, so a broken checker never certifies anything.
:func:`recording` collects every verdict, so the share of decided columns can be measured.
"""

from __future__ import annotations

import itertools
import secrets
from collections import Counter
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

import sqlglot
from sqlglot import exp

from .proof_qualify import (
    _DATE_PARTS,
    _HARMLESS_TABLE_ARGS,
    _NAME_CLAUSES,
    _Rejected,
    _cte_named,
    _empty,
    _locate,
    _names_of_query,
    _readable,
    _walk,
)
from .proof_steps import RewriteStep, StepCheck

COLUMN_RESOLUTION_FAMILY = "prover_column_resolution"
COLUMN_RESOLUTION_ASSUMPTIONS = (
    "only_qualifiers_added",
    "bare_column_has_one_owner_in_its_scope",
    "source_columns_known_exactly",
    "output_names_read_before_source_columns",
    "supplied_table_columns_complete_and_input_valid",
)

COLUMN_TAG = "kq_column_ordinal"
SOURCE_TAG = "kq_source_ordinal"

OWNER, AMBIGUOUS, OUTPUT, USING, UNDECIDED = "owner", "ambiguous", "output", "using", "undecided"
AGREE, DISAGREE, UNCHECKED = "agree", "disagree", "unchecked"

_ENABLED: ContextVar[bool] = ContextVar("kumosql_proof_columns_enabled", default=True)
_RECORD: ContextVar[list | None] = ContextVar("kumosql_proof_columns_record", default=None)


class _Undecided(ValueError):
    """The text does not settle the question (a construct or a missing fact), which is not a disagreement."""


class ColumnResolutionRefused(Exception):
    """Raised by a prover hook when the independent reading names a different owner than the prover chose."""


@contextmanager
def disabled():
    """Turn the provers' column-resolution check off inside the block (fault-injection tests, measurements)."""

    token = _ENABLED.set(False)
    try:
        yield
    finally:
        _ENABLED.reset(token)


def enabled() -> bool:
    return _ENABLED.get()


@contextmanager
def recording():
    """Collect ``(site, verdict kind, reason)`` for every column the check looks at inside the block."""

    log: list = []
    token = _RECORD.set(log)
    try:
        yield log
    finally:
        _RECORD.reset(token)


def _note(site: str, verdict: "Verdict") -> None:
    log = _RECORD.get()
    if log is not None:
        log.append((site, verdict.kind, verdict.reason))


def summarize(log: list) -> dict[str, dict[str, int]]:
    """``{site: {kind: count}}`` of a :func:`recording` log."""

    result: dict[str, Counter] = {}
    for site, kind, _ in log:
        result.setdefault(site, Counter())[kind] += 1
    return {site: dict(counts) for site, counts in result.items()}


# --- identity of columns and FROM items -----------------------------------------------------------------

def _columns(statement: exp.Expression) -> list[exp.Column]:
    return [node for node in statement.walk() if isinstance(node, exp.Column)]


def _from_items(statement: exp.Expression) -> list[exp.Expression]:
    return [
        node for node in statement.walk()
        if isinstance(node.parent, (exp.From, exp.Join)) and node.arg_key == "this"
    ]


def tag(statement: exp.Expression) -> exp.Expression:
    """Number the bare columns and the FROM items of a freshly parsed statement, in place, and return it.

    The numbers live in ``meta`` and survive ``copy()``. They are positions in this statement's own text, so
    the reader of the same text finds the same column at the same number.
    """

    for index, column in enumerate(_columns(statement)):
        if not column.args.get("table") and not isinstance(column.this, exp.Star):
            column.meta[COLUMN_TAG] = index
    for index, item in enumerate(_from_items(statement)):
        item.meta[SOURCE_TAG] = index
    return statement


def column_tag(column: exp.Expression) -> int | None:
    meta = getattr(column, "_meta", None)
    return meta.get(COLUMN_TAG) if meta else None


def source_tag(node: exp.Expression) -> int | None:
    meta = getattr(node, "_meta", None)
    return meta.get(SOURCE_TAG) if meta else None


# --- what a column was resolved to ----------------------------------------------------------------------

@dataclass(frozen=True)
class Claim:
    """What a prover decided for one bare column: the FROM item it read (by tag), the qualifier it wrote, or a select alias."""

    source: int | None = None
    qualifier: str | None = None
    alias: bool = False


@dataclass(frozen=True)
class Resolution:
    status: str
    source: int | None = None
    name: str = ""
    target: exp.Expression | None = None
    detail: str = ""


@dataclass(frozen=True)
class Verdict:
    kind: str
    reason: str = ""
    agreed: int = 0
    unchecked: int = 0

    @property
    def refused(self) -> bool:
        return self.kind == DISAGREE


@dataclass(frozen=True)
class _Src:
    node: exp.Expression
    name: str
    columns: frozenset[str] | None
    open: bool = False  # an UNNEST of structs: names beyond ``columns`` may be fields of its element


class StatementReader:
    """The scopes of one statement, read from its text, answering which FROM item owns a bare column."""

    def __init__(self, statement: exp.Expression, known: dict[str, object] | None):
        self.statement = statement
        self.known = {
            str(table).lower(): frozenset(str(column).lower() for column in columns)
            for table, columns in (known or {}).items()
        }
        self.columns = _columns(statement)
        self.items = _from_items(statement)
        self._item_index = {id(item): index for index, item in enumerate(self.items)}
        self._sources: dict[int, list[_Src] | _Undecided] = {}
        self._resolved: dict[int, Resolution] = {}

    # -- sources ---------------------------------------------------------------------------------------

    def _source_of(self, relation: exp.Expression) -> _Src:
        if isinstance(relation, exp.Table):
            alias = relation.args.get("alias")
            name = (alias.this.name if alias is not None and alias.this is not None else relation.name).lower()
            carried = {key for key, value in relation.args.items() if not _empty(value) and value is not False}
            if carried - _HARMLESS_TABLE_ARGS or not isinstance(relation.this, exp.Identifier):
                return _Src(relation, name, None)
            if alias is not None and alias.args.get("columns"):
                return _Src(relation, name, None)
            try:
                cte = _cte_named(relation)
            except _Rejected:
                return _Src(relation, name, None)
            if cte is not None:
                listed = cte.args["alias"].args.get("columns") if cte.args.get("alias") is not None else None
                names = [c.name.lower() for c in listed] if listed else _names_of_query(cte.this)
                return _Src(relation, name, None if names is None else frozenset(names))
            parts = [p.name.lower() for p in (relation.args.get("catalog"), relation.args.get("db"), relation.this) if p is not None]
            return _Src(relation, name, self.known.get(".".join(parts)))
        if isinstance(relation, exp.Subquery):
            alias = relation.args.get("alias")
            name = alias.this.name.lower() if alias is not None and alias.this is not None else ""
            extra = {key for key, value in relation.args.items() if not _empty(value)} - {"this", "alias", "comments"}
            if extra or alias is None or alias.this is None or alias.args.get("columns"):
                return _Src(relation, name, None)
            names = _names_of_query(relation.this)
            return _Src(relation, name, None if names is None else frozenset(names))
        if isinstance(relation, exp.Unnest):
            alias = relation.args.get("alias")
            columns = alias.args.get("columns") if alias is not None else None
            extra = {key for key, value in relation.args.items() if not _empty(value)} - {"expressions", "alias", "offset", "comments"}
            if extra or not columns or len(columns) != 1 or alias.this is not None:
                return _Src(relation, "", None)
            element = columns[0].name.lower()
            names = {element}
            offset = relation.args.get("offset")
            if offset is not None:
                names.add(offset.name.lower() if isinstance(offset, exp.Identifier) else "offset")
            return _Src(relation, element, frozenset(names), open=True)
        alias = relation.args.get("alias") if isinstance(relation, exp.Expression) else None
        name = alias.this.name.lower() if alias is not None and getattr(alias, "this", None) is not None else ""
        return _Src(relation, name, None)

    def _select_sources(self, select: exp.Select) -> list[_Src]:
        cached = self._sources.get(id(select))
        if cached is None:
            try:
                cached = self._build_sources(select)
            except _Undecided as exc:
                cached = exc
            self._sources[id(select)] = cached
        if isinstance(cached, _Undecided):
            raise cached
        return cached

    def _build_sources(self, select: exp.Select) -> list[_Src]:
        from_ = select.args.get("from_") or select.args.get("from")
        if from_ is None:
            raise _Undecided("the select has no FROM")
        joins = select.args.get("joins") or []
        for join in joins:
            if str(join.args.get("method") or "").upper() == "NATURAL":
                raise _Undecided("the select has a NATURAL join")
            if str(join.args.get("kind") or "").upper() in ("SEMI", "ANTI"):
                raise _Undecided("the select has a SEMI or ANTI join")
        sources = [self._source_of(item) for item in [from_.this] + [join.this for join in joins]]
        names = [source.name for source in sources if source.name]
        if len(set(names)) != len(names):
            raise _Undecided("two sources of the select share a name")
        return sources

    @staticmethod
    def _outputs(select: exp.Select) -> dict[str, exp.Expression | None]:
        """Names GROUP BY, HAVING, QUALIFY, ORDER BY and a named window read before a source column, with their value."""

        found: dict[str, list] = {}
        for item in select.expressions:
            value = item
            while isinstance(value, exp.Paren):
                value = value.this
            if isinstance(item, exp.Alias):
                found.setdefault(item.alias.lower(), []).append(item.this)
            elif isinstance(value, exp.Column) and not isinstance(value.this, exp.Star):
                if not _empty(value.args.get("table")):
                    found.setdefault(value.name.lower(), []).append(value)
            elif isinstance(value, exp.Dot):
                found.setdefault(value.text("expression").lower(), []).append(value)
            for star in value.find_all(exp.Star):
                for part in (*(star.args.get("replace") or []), *(star.args.get("rename") or [])):
                    for identifier in part.find_all(exp.Identifier):
                        found.setdefault(identifier.name.lower(), []).append(None)
        return {name: (values[0] if len(values) == 1 else None) for name, values in found.items()}

    @staticmethod
    def _enclosing(select: exp.Select):
        """``(outer select, its child holding the subquery, the node just below that child)``, or ``None`` when
        the select cannot read an outer column (a CTE body, a derived table) or has no outer select."""

        current, below = select, None
        while True:
            parent = current.parent
            if parent is None or isinstance(parent, exp.CTE):
                return None
            if isinstance(parent, exp.Subquery) and isinstance(parent.parent, (exp.From, exp.Join)) and current.arg_key == "this":
                return None
            if isinstance(parent, exp.Subquery) and current.arg_key != "this":
                raise _Undecided("the column sits under a parenthesized query's own ORDER BY or LIMIT")
            if isinstance(parent, exp.Select):
                return parent, current, below
            below, current = current, parent

    # -- one column ------------------------------------------------------------------------------------

    def resolve(self, column: exp.Column) -> Resolution:
        """Which FROM item owns ``column`` (bare or qualified), from this statement's text."""

        cached = self._resolved.get(id(column))
        if cached is not None:
            return cached
        try:
            result = self._qualified(column) if column.args.get("table") else self._bare(column)
        except (_Undecided, _Rejected) as exc:
            result = Resolution(UNDECIDED, detail=str(exc))
        self._resolved[id(column)] = result
        return result

    def _owner(self, source: _Src, detail: str = "") -> Resolution:
        index = self._item_index.get(id(source.node))
        return Resolution(OWNER, source=index, name=source.name, detail=detail)

    def _qualified(self, column: exp.Column) -> Resolution:
        if column.args.get("db") or column.args.get("catalog") or isinstance(column.this, exp.Star):
            raise _Undecided("a column path")
        select, top, below = _locate(column)
        qualifier = column.table.lower()
        while True:
            sources = self._select_sources(select)
            _, readable = _readable(select, top, below, len(sources))
            hits = [source for source in sources[:readable] if source.name == qualifier]
            if len(hits) > 1:
                raise _Undecided("two sources have the qualifier")
            if hits:
                return self._owner(hits[0])
            outer = self._enclosing(select)
            if outer is None:
                raise _Undecided("the qualifier names no source")
            select, top, below = outer

    def _bare(self, column: exp.Column) -> Resolution:
        if isinstance(column.this, exp.Star) or not isinstance(column.this, exp.Identifier):
            raise _Undecided("not a plain column")
        name = column.name.lower()
        if isinstance(column.parent, exp.Func) and name.upper() in _DATE_PARTS:
            raise _Undecided("a date part spelled as a function argument")
        select, top, below = _locate(column)
        first = True
        while True:
            sources = self._select_sources(select)
            clause, readable = _readable(select, top, below, len(sources))
            using = {i.name.lower() for join in select.args.get("joins") or [] for i in join.args.get("using") or []}
            if name in using:
                if first:
                    return Resolution(USING, detail=f"{name} is a USING column")
                raise _Undecided("a correlated name that is also a USING column")
            if name in {source.name for source in sources if not isinstance(source.node, exp.Unnest)}:
                raise _Undecided(f"{name} is the name of a source")
            if clause in _NAME_CLAUSES:
                outputs = self._outputs(select)
                if name in outputs:
                    if first:
                        return Resolution(OUTPUT, name=name, target=outputs[name], detail=f"{name} is an output of the select")
                    raise _Undecided("a correlated name that is also an output of the outer select")
            readable_sources = sources[:readable]
            owners = [s for s in readable_sources if s.columns is not None and name in s.columns]
            unknown = [s for s in readable_sources if s.columns is None]
            opened = [s for s in readable_sources if s.open]
            outer = self._enclosing(select)
            if owners and not unknown:
                if len(owners) == 1:
                    return self._owner(owners[0])
                return Resolution(AMBIGUOUS, detail=f"{name} is a column of more than one source it can read")
            if owners or unknown or opened:
                if not owners and len(unknown) == 1 and not opened and outer is None:
                    return self._owner(unknown[0], detail="the only source whose columns are unknown")
                raise _Undecided(f"{name} may belong to a source whose columns are not known")
            if outer is None:
                raise _Undecided(f"no source has the column {name}")
            select, top, below = outer
            first = False

    # -- comparing with a prover's decision -------------------------------------------------------------

    def judge(self, column: exp.Column, claim: Claim) -> Verdict:
        """Compare what a prover decided for a bare column of this statement with the independent reading."""

        return self._judge(self.resolve(column), claim, column.name.lower())

    def _judge(self, resolution: Resolution, claim: Claim, name: str, depth: int = 0) -> Verdict:
        status = resolution.status
        if status == UNDECIDED:
            return Verdict(UNCHECKED, resolution.detail)
        if status == USING:
            return Verdict(UNCHECKED, resolution.detail)
        if status == AMBIGUOUS:
            return Verdict(DISAGREE, f"{resolution.detail}, so the engine rejects the bare name; the prover chose one owner")
        if status == OUTPUT:
            if claim.alias:
                return Verdict(AGREE, "a select alias", agreed=1)
            target = resolution.target
            if isinstance(target, exp.Column) and target.name.lower() == name and depth == 0:
                return self._judge(self.resolve(target), claim, name, depth + 1)
            return Verdict(DISAGREE, f"{name!r} reads the select alias there, but the prover read a source column")
        if claim.alias:
            return Verdict(DISAGREE, f"{name!r} is a source column there, but the prover read a select alias")
        if claim.source is not None:
            if resolution.source == claim.source:
                return Verdict(AGREE, "the owner", agreed=1)
            return Verdict(DISAGREE, f"{name!r} belongs to the source {resolution.name or '?'!r} but the prover chose another")
        if claim.qualifier is not None:
            if not resolution.name:
                return Verdict(UNCHECKED, "the owner has no name to compare")
            if resolution.name == claim.qualifier.lower():
                return Verdict(AGREE, "the owner", agreed=1)
            return Verdict(DISAGREE, f"{name!r} belongs to {resolution.name!r} but was qualified with {claim.qualifier!r}")
        return Verdict(UNCHECKED, "the prover's choice was not traced")

    def judge_tagged(self, column: exp.Column, claim: Claim, site: str) -> Verdict:
        """Judge a column of the prover's tree by the number :func:`tag` gave it; records the verdict."""

        ordinal = column_tag(column)
        try:
            if ordinal is None or ordinal >= len(self.columns):
                verdict = Verdict(UNCHECKED, "the column is not one of the statement's own")
            else:
                mine = self.columns[ordinal]
                if mine.name.lower() != column.name.lower() or mine.args.get("table"):
                    verdict = Verdict(UNCHECKED, "the column does not line up with the text")
                else:
                    verdict = self.judge(mine, claim)
        except Exception as exc:  # noqa: BLE001 - a checker that fails refuses; it never guesses that the prover was right
            verdict = Verdict(DISAGREE, f"the independent check failed ({type(exc).__name__}: {exc}), so the owner is not confirmed")
        _note(site, verdict)
        return verdict

    def judge_qualified(self, tree: exp.Expression, site: str) -> Verdict:
        """After a qualifier ran on a tagged tree: each tagged column that now has one must name its independent owner."""

        agreed = unchecked = 0
        first = None
        for column in tree.find_all(exp.Column):
            qualifier = column.args.get("table")
            if qualifier is None:
                continue
            ordinal = column_tag(column)
            if ordinal is None or ordinal >= len(self.columns):
                continue
            verdict = self.judge_tagged(column, Claim(qualifier=qualifier.name), site)
            if verdict.kind == DISAGREE and first is None:
                first = verdict
            agreed += verdict.kind == AGREE
            unchecked += verdict.kind == UNCHECKED
        if first is not None:
            return Verdict(DISAGREE, first.reason, agreed, unchecked)
        return Verdict(AGREE if agreed else UNCHECKED, "", agreed, unchecked)


def reader_for(sql: str, known: dict | None, dialect: str = "bigquery") -> StatementReader | None:
    """A reader of ``sql`` (one statement, BigQuery), or ``None`` when the check is off or the text is not one it reads."""

    if dialect != "bigquery" or not enabled():
        return None
    try:
        nodes = [n for n in sqlglot.parse(sql, read="bigquery", error_level=sqlglot.ErrorLevel.RAISE) if n is not None]
    except Exception:  # noqa: BLE001 - text this reader cannot parse is left unchecked
        return None
    if len(nodes) != 1:
        return None
    try:
        return StatementReader(nodes[0], known)
    except Exception as exc:  # noqa: BLE001 - a reader that cannot be built refuses, it does not skip the check
        raise ColumnResolutionRefused(f"the independent reader could not be built ({type(exc).__name__}: {exc})") from None


def parse_tagged(sql: str, dialect: str, reader: StatementReader | None) -> list[exp.Expression]:
    """``sqlglot.parse`` of ``sql``, with the statement numbered for ``reader`` when there is one."""

    statements = [n for n in sqlglot.parse(sql, read=dialect) if n is not None]
    if reader is not None:
        try:
            for statement in statements:
                tag(statement)
        except Exception as exc:  # noqa: BLE001 - numbering that fails refuses
            raise ColumnResolutionRefused(f"the statement could not be numbered for the check ({type(exc).__name__}: {exc})") from None
    return statements


# --- a pass that only adds qualifiers ----------------------------------------------------------------------

def check_added_qualifiers(before_sql: str, after_sql: str, known: dict | None, site: str = "qualification") -> Verdict:
    """Compare the qualifiers a normalizer pass added with the owners read from the text before it.

    ``disagree`` when any added qualifier names a source other than the independent owner (or the bare name has
    no single owner); ``unchecked`` when the texts differ by more than qualifiers or the owner cannot be decided;
    ``agree`` when every added qualifier is the independent owner's.
    """

    try:
        try:  # the statement the prover was given is text this reader may not read (left unchecked, as ``reader_for`` does)
            first = [n for n in sqlglot.parse(before_sql, read="bigquery", error_level=sqlglot.ErrorLevel.RAISE) if n is not None]
        except Exception:  # noqa: BLE001
            return _noted(site, Verdict(UNCHECKED, "the statement is not one this reader parses"))
        try:  # the prover's own output that cannot be read back is not confirmed
            second = [n for n in sqlglot.parse(after_sql, read="bigquery", error_level=sqlglot.ErrorLevel.RAISE) if n is not None]
        except Exception as exc:  # noqa: BLE001
            return _noted(site, Verdict(DISAGREE, f"the prover's output cannot be read back ({type(exc).__name__}), so its qualifiers are not confirmed"))
        if len(first) != 1 or len(second) != 1:
            return _noted(site, Verdict(UNCHECKED, "not one statement on each side"))
        before, after = first[0], second[0]
        gained: list = []
        try:
            _walk(before, after, gained, "statement")
        except _Rejected as exc:
            return _noted(site, Verdict(UNCHECKED, f"more than qualifiers changed: {exc}"))
        if not gained:
            return Verdict(AGREE, "no qualifier was added")
        reader = StatementReader(before, known)
        agreed = unchecked = 0
        first = None
        for old, new in gained:
            verdict = reader.judge(old, Claim(qualifier=new.args["table"].name))
            _note(site, verdict)
            if verdict.kind == DISAGREE and first is None:
                first = f"the qualifier on {old.sql(dialect='bigquery')!r}: {verdict.reason}"
            agreed += verdict.kind == AGREE
            unchecked += verdict.kind == UNCHECKED
        if first is not None:
            return Verdict(DISAGREE, first, agreed, unchecked)
        return Verdict(AGREE if agreed else UNCHECKED, "", agreed, unchecked)
    except Exception as exc:  # noqa: BLE001 - a checker that fails refuses; it never guesses that the prover was right
        return _noted(site, Verdict(DISAGREE, f"the independent check failed ({type(exc).__name__}: {exc}), so the qualifiers are not confirmed"))


def _noted(site: str, verdict: Verdict) -> Verdict:
    _note(site, verdict)
    return verdict


def guarded_qualification(tree: exp.Expression, apply, known: dict | None, dialect: str, site: str) -> exp.Expression:
    """Run ``apply(tree)``, a pass that writes qualifiers in place, and check what it added.

    Raises :class:`ColumnResolutionRefused` when an added qualifier disagrees with the independent owner. Passes
    that add nothing, other dialects and a disabled check cost nothing.
    """

    if dialect != "bigquery" or not enabled():
        return apply(tree)
    bare = [c for c in tree.find_all(exp.Column) if not c.args.get("table") and not isinstance(c.this, exp.Star)]
    result = apply(tree)
    gained = [c for c in bare if c.args.get("table")]
    if not gained:
        return result
    try:
        after = result.sql(dialect="bigquery")
        saved = [(c, c.args["table"]) for c in gained]
        for c, _ in saved:
            c.set("table", None)
        try:
            before = result.sql(dialect="bigquery")
        finally:
            for c, qualifier in saved:
                c.set("table", qualifier)
    except Exception as exc:  # noqa: BLE001 - a pass whose result cannot be printed for the check is not confirmed
        raise ColumnResolutionRefused(f"the independent check could not print the tree ({type(exc).__name__}: {exc})") from None
    verdict = check_added_qualifiers(before, after, known, site)
    if verdict.refused:
        raise ColumnResolutionRefused(verdict.reason)
    return result


# --- a rewrite that rebuilds columns -----------------------------------------------------------------------------

STAMP_COLUMN = "kq_stamp_column"
STAMP_SOURCE = "kq_stamp_source"
REBUILT_FROM = "kq_rebuilt_from"

_SUPPLIED: ContextVar[tuple[dict | None, str | None]] = ContextVar("kumosql_proof_columns_supplied", default=(None, None))


class _Ambiguous(ValueError):
    """The independent reading finds a bare name in more than one source it can read."""


@contextmanager
def supplying(known: dict | None, dialect: str | None):
    """Name the columns the prover was given and its dialect, for the rewrites that call :func:`begin` inside the block."""

    token = _SUPPLIED.set((known, dialect))
    try:
        yield
    finally:
        _SUPPLIED.reset(token)


def _stamp(root: exp.Expression, token: int) -> None:
    for index, column in enumerate(_columns(root)):
        column.meta[STAMP_COLUMN] = (token, index)
    for index, item in enumerate(_from_items(root)):
        item.meta[STAMP_SOURCE] = (token, index)


def _meta_of(node: exp.Expression, key: str):
    meta = getattr(node, "_meta", None)
    return meta.get(key) if meta else None


def rebuilt(new: exp.Expression, old: exp.Expression) -> exp.Expression:
    """Say that ``new`` stands where ``old`` (a column, or an expression over columns) stood, so the check compares
    the base columns they read. Returns ``new``. Nothing is claimed when a column of ``old`` was not numbered."""

    stamps = []
    for column in old.find_all(exp.Column):
        stamp = _meta_of(column, STAMP_COLUMN)
        if stamp is None:
            return new
        stamps.append(stamp)
    new.meta[REBUILT_FROM] = tuple(stamps)
    return new


def carry_source(new: exp.Expression, old: exp.Expression) -> exp.Expression:
    """Say that the FROM item ``new`` is the FROM item ``old`` (a table rebuilt with another alias, a derived table read as its base)."""

    stamp = _meta_of(old, STAMP_SOURCE)
    if stamp is not None:
        new.meta[STAMP_SOURCE] = stamp
    return new


def _read_back(root: exp.Expression) -> tuple[exp.Expression, list[exp.Column], list[exp.Expression]]:
    """Print ``root``, read the text again, and line the printed statement's columns and FROM items up with ``root``'s."""

    text = root.sql(dialect="bigquery")
    nodes = [n for n in sqlglot.parse(text, read="bigquery", error_level=sqlglot.ErrorLevel.RAISE) if n is not None]
    if len(nodes) != 1:
        raise _Undecided("the printed statement is not one statement")
    parsed = nodes[0]
    columns, items = _columns(root), _from_items(root)
    again, again_items = _columns(parsed), _from_items(parsed)
    if (
        len(columns) != len(again)
        or len(items) != len(again_items)
        or any(a.name.lower() != b.name.lower() for a, b in zip(columns, again))
        or any(type(a) is not type(b) for a, b in zip(items, again_items))
    ):
        raise _Undecided("the printed statement does not line up with the tree")
    return parsed, columns, items


class _Origins:
    """The base columns a column reads, chased through plain derived tables and CTEs, from one statement's text.

    A leaf is ``(kind, number, name)``: ``table`` (a FROM item that is a physical table), ``unnest`` or ``derived``
    (the output of a set operation, which is not chased), the FROM item's number and the column's name.
    """

    def __init__(self, reader: StatementReader, number):
        self.reader = reader
        self.number = number  # a FROM item of the reader's text -> its number, or None when the rewrite made it

    def column(self, column: exp.Column, depth: int = 0) -> list[tuple]:
        if depth > 24:
            raise _Undecided("too many derived tables to chase")
        resolution = self.reader.resolve(column)
        status = resolution.status
        if status == AMBIGUOUS:
            raise _Ambiguous(resolution.detail)
        if status == OWNER:
            if resolution.source is None:
                raise _Undecided("the owner is not a FROM item")
            return self._item(self.reader.items[resolution.source], column.name.lower(), depth)
        if status == OUTPUT:
            if resolution.target is None:
                raise _Undecided("a select alias with no single value")
            return self.expression(resolution.target, depth + 1)
        raise _Undecided(resolution.detail or status)

    def expression(self, node: exp.Expression, depth: int = 0) -> list[tuple]:
        leaves: list[tuple] = []
        for column in node.find_all(exp.Column):
            if isinstance(column.this, exp.Star):
                raise _Undecided("a star")
            leaves.extend(self.column(column, depth + 1))
        return leaves

    def _item(self, item: exp.Expression, name: str, depth: int) -> list[tuple]:
        if isinstance(item, exp.Table):
            try:
                cte = _cte_named(item)
            except _Rejected as exc:
                raise _Undecided(str(exc)) from None
            if cte is None:
                return [("table", self._number(item), name)]
            alias = cte.args.get("alias")
            if alias is not None and alias.args.get("columns"):
                raise _Undecided("a CTE with a column list")
            return self._body(cte.this, name, item, depth)
        if isinstance(item, exp.Subquery):
            alias = item.args.get("alias")
            if alias is not None and alias.args.get("columns"):
                raise _Undecided("a derived table with a column list")
            return self._body(item.this, name, item, depth)
        if isinstance(item, exp.Unnest):
            return [("unnest", self._number(item), name)]
        raise _Undecided("a FROM item that is not a table or a query")

    def _number(self, item: exp.Expression) -> int:
        number = self.number(item)
        if number is None:
            raise _Undecided("a FROM item the rewrite made")
        return number

    def _body(self, query: exp.Expression, name: str, item: exp.Expression, depth: int) -> list[tuple]:
        while isinstance(query, exp.Subquery):
            query = query.this
        if isinstance(query, exp.Select):
            if any(isinstance(e, exp.Star) or (isinstance(e, exp.Column) and isinstance(e.this, exp.Star)) for e in query.expressions):
                raise _Undecided("a star in the projection")
            matches = [e for e in query.expressions if e.alias_or_name.lower() == name]
            if len(matches) != 1:
                raise _Undecided(f"{name} is not exactly one output")
            value = matches[0].this if isinstance(matches[0], exp.Alias) else matches[0]
            return self.expression(value, depth + 1)
        if isinstance(query, exp.SetOperation):
            return [("derived", self._number(item), name)]
        raise _Undecided("a derived table that is not a SELECT")


def _show(leaves: list[tuple]) -> str:
    return ", ".join(f"{kind} #{number} column {name}" for kind, number, name in leaves) or "no column"


class Rewrite:
    """One rewrite of a statement, checked: its columns are numbered and read before it, and again after it.

    Every column that survives (and every node the rewrite says it rebuilt from a column, :func:`rebuilt`) must
    read the same base columns after the rewrite as the column it came from read before it. FROM items the rewrite
    makes itself have no number (:func:`carry_source` hands one over), so a column that reads one is left unchecked.

    ``begin`` numbers the columns and FROM items of the node the rewrite works on (cheap, and done before the
    rewrite copies it); :meth:`snapshot` numbers the rest of the statement and reads it, and must run while the
    statement is still as it was before the rewrite (:meth:`verify` takes the snapshot itself when the rewrite
    worked on a copy and left the statement alone).
    """

    active = True

    def __init__(self, node: exp.Expression, site: str, known: dict | None):
        self.site = site
        self.known = known
        self.token = secrets.randbits(60)
        self.root = node.root()
        self._count = itertools.count()
        self.problem: str | None = None
        self.before: _Origins | None = None
        self.before_columns: list[exp.Column] = []
        self.before_index: dict[int, int] = {}
        self._snapped = False
        for item in (*_columns(node), *_from_items(node)):
            self._number(item)

    def _number(self, node: exp.Expression) -> None:
        key = STAMP_COLUMN if isinstance(node, exp.Column) else STAMP_SOURCE
        node.meta[key] = (self.token, next(self._count))

    def snapshot(self) -> None:
        if self._snapped:
            return
        self._snapped = True
        try:
            for column in _columns(self.root):
                if self._stamped(column, STAMP_COLUMN) is None:
                    self._number(column)
            for item in _from_items(self.root):
                if self._stamped(item, STAMP_SOURCE) is None:
                    self._number(item)
            parsed, columns, items = _read_back(self.root)
            reader = StatementReader(parsed, self.known)
            numbers = {id(item): self._stamped(items[index], STAMP_SOURCE) for index, item in enumerate(reader.items)}
            self.before = _Origins(reader, lambda item: numbers.get(id(item)))
            self.before_columns = reader.columns
            self.before_index = {self._stamped(column, STAMP_COLUMN): index for index, column in enumerate(columns)}
        except Exception as exc:  # noqa: BLE001 - a statement the reader cannot read is left unchecked
            self.problem = f"the statement before the rewrite is not one the reader reads ({type(exc).__name__})"

    def _stamped(self, node: exp.Expression, key: str) -> int | None:
        stamp = _meta_of(node, key)
        return stamp[1] if stamp is not None and stamp[0] == self.token else None

    def verify(self, after: exp.Expression) -> Verdict:
        self.snapshot()
        if self.problem is not None or self.before is None:
            return _noted(self.site, Verdict(UNCHECKED, self.problem or "no reading of the statement before"))
        try:
            parsed, columns, items = _read_back(after)
        except _Undecided as exc:
            return _noted(self.site, Verdict(UNCHECKED, str(exc)))
        except Exception as exc:  # noqa: BLE001 - the prover's own output that cannot be read back is not confirmed
            return _noted(self.site, Verdict(DISAGREE, f"the rewritten statement cannot be read back ({type(exc).__name__}), so its columns are not confirmed"))
        try:
            reader = StatementReader(parsed, self.known)
            numbers = {id(item): self._stamped(items[index], STAMP_SOURCE) for index, item in enumerate(reader.items)}
            origins = _Origins(reader, lambda item: numbers.get(id(item)))
            position = {id(column): index for index, column in enumerate(columns)}
            claims: list[tuple[list[int], list[int], str]] = []
            for index, column in enumerate(columns):
                before = self._stamped(column, STAMP_COLUMN)
                if before is not None and before in self.before_index:
                    claims.append(([self.before_index[before]], [index], column.name))
            for node in after.walk():
                made = _meta_of(node, REBUILT_FROM)
                if made and all(token == self.token and number in self.before_index for token, number in made):
                    indexes = [position[id(c)] for c in node.find_all(exp.Column) if id(c) in position]
                    claims.append(([self.before_index[number] for _, number in made], indexes, node.sql(dialect="bigquery")[:60]))
            agreed = unchecked = 0
            first = None
            for before, indexes, label in claims:
                verdict = self._compare(origins, reader, before, indexes, label)
                _note(self.site, verdict)
                if verdict.kind == DISAGREE and first is None:
                    first = verdict
                agreed += verdict.kind == AGREE
                unchecked += verdict.kind == UNCHECKED
        except Exception as exc:  # noqa: BLE001 - a checker that fails refuses; it never guesses that the prover was right
            return _noted(self.site, Verdict(DISAGREE, f"the independent check failed ({type(exc).__name__}: {exc}), so the rewritten columns are not confirmed"))
        if first is not None:
            return Verdict(DISAGREE, first.reason, agreed, unchecked)
        return Verdict(AGREE if agreed else UNCHECKED, "", agreed, unchecked)

    def _compare(self, origins: _Origins, reader: StatementReader, before: list[int], indexes: list[int], label: str) -> Verdict:
        try:
            expected = [leaf for index in before for leaf in self.before.column(self.before_columns[index])]
        except (_Undecided, _Ambiguous, IndexError) as exc:
            return Verdict(UNCHECKED, f"before the rewrite: {exc}")
        try:
            actual = [leaf for index in indexes for leaf in origins.column(reader.columns[index])]
        except _Undecided as exc:
            return Verdict(UNCHECKED, str(exc))
        except _Ambiguous as exc:
            return Verdict(DISAGREE, f"{label!r} is ambiguous after the rewrite ({exc}) and was not before")
        if actual == expected:
            return Verdict(AGREE, "the same base columns", agreed=1)
        return Verdict(DISAGREE, f"{label!r} reads {_show(actual)} after the rewrite but read {_show(expected)} before")

    def check(self, after: exp.Expression) -> None:
        """:meth:`verify`, raising :class:`ColumnResolutionRefused` on a disagreement."""

        verdict = self.verify(after)
        if verdict.refused:
            raise ColumnResolutionRefused(f"{self.site}: {verdict.reason}")


class _NoRewrite:
    """What :func:`begin` hands back when there is nothing to check (another dialect, or the check is off)."""

    active = False

    def snapshot(self) -> None:
        return None

    def check(self, after: exp.Expression) -> None:
        return None

    def verify(self, after: exp.Expression) -> None:
        return None


def begin(node: exp.Expression, site: str) -> "Rewrite | _NoRewrite":
    """Start checking a rewrite of ``node`` (a part of a statement), before it copies or edits anything.

    Numbers the columns and FROM items of ``node``. A rewrite that edits in place calls ``.snapshot()`` before its
    first edit; either way ``.check(after)`` reads the statement the rewrite made. Does nothing outside
    :func:`supplying` (the algebraic normalizer's own call), for other dialects and with the check off.
    """

    known, dialect = _SUPPLIED.get()
    if dialect != "bigquery" or not enabled():
        return _NoRewrite()
    return Rewrite(node, site, known)


# --- the registry's view: a transition accepted only when every added qualifier is re-derived ------------------

def check_column_resolution_transition(step: RewriteStep, before: exp.Expression, after: exp.Expression) -> StepCheck:
    """Accept a step only when each qualifier it adds is the independent owner of its bare column.

    Unlike the provers' hook (which leaves an undecided column unchecked), a registered step is accepted only
    when everything is re-derived, as for every other family. The caller's trees are not read.
    """

    if step.family != COLUMN_RESOLUTION_FAMILY:
        return StepCheck(step, False, f"no independent checker for the {step.family!r} family")
    if step.assumptions != COLUMN_RESOLUTION_ASSUMPTIONS:
        return StepCheck(step, False, "the step's assumptions are not the assumptions of its family")
    verdict = check_added_qualifiers(step.before_sql, step.after_sql, dict(step.known_columns), "registry")
    if verdict.kind == AGREE and verdict.agreed and not verdict.unchecked:
        return StepCheck(step, True, "each added qualifier is the independent owner of its bare column", verdict.agreed)
    reason = verdict.reason or (
        f"{verdict.unchecked} column(s) could not be decided" if verdict.kind == UNCHECKED else "no qualifier was added"
    )
    return StepCheck(step, False, reason)


__all__ = [
    "AGREE", "COLUMN_RESOLUTION_ASSUMPTIONS", "COLUMN_RESOLUTION_FAMILY", "Claim", "ColumnResolutionRefused", "DISAGREE",
    "Resolution", "StatementReader", "UNCHECKED", "Verdict", "check_added_qualifiers",
    "check_column_resolution_transition", "disabled", "enabled", "guarded_qualification", "parse_tagged", "reader_for",
    "Rewrite", "begin", "carry_source", "rebuilt", "recording", "source_tag", "summarize", "supplying", "tag",
]
