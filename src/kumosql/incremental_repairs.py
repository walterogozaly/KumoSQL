"""Repairs for an incremental model that diverges from its full refresh under a contract.

:func:`propose_repairs` takes the SQLX of an incremental model, the sources it reads and a change contract
(see :mod:`kumosql.incremental`) and returns the repairs that make the model equal its full refresh under
that contract. A repair is a *patch*: a unified diff of the SQLX. Nothing is written anywhere; the person
who owns the model decides whether to apply it.

Four atomic edits are tried, alone and in combination (smallest combination first, and no combination
that contains a smaller one already offered):

* ``dedup``: keep one row per source key. Every source whose key the contract can re-deliver is read
  through ``(SELECT * FROM src WHERE TRUE QUALIFY ROW_NUMBER() OVER (PARTITION BY key ORDER BY ts DESC) =
  1)``; a model over one table gets the ``QUALIFY`` on its own query instead, the shape rule R3 reads.
* ``drop_update_partition_filter``: remove ``bigquery.updatePartitionFilter``, which stops the ``MERGE``
  from matching rows outside the filter (their changed versions then land beside the old ones).
* ``full_rerun_merge``: make every incremental run execute the full query (drop the
  ``when(incremental(), ...)`` filter) and merge on the model's existing ``uniqueKey`` (rule R7).
* ``watermark_lookback``: change a strict ``>`` watermark to ``>=`` with a lookback and add the source's
  key as ``uniqueKey``, so a run re-reads the newest rows and merges them instead of appending (rules
  R2 and R3).

**A repair is offered only when both of these hold.**

1. *Safe under the contract.* The repaired SQLX is parsed again and :func:`kumosql.incremental.prove`
   proves it equal to its full refresh under the same contract. Proof rules only: a repair for which no
   rule applies is not offered, however likely it looks to work. As a last guard, a short counterexample
   search over the repaired model must find nothing (a hit would mean a wrong proof, and the repair is
   dropped).
2. *Full refresh unchanged.* The repaired model's full query returns what the original's does on every
   source state in which each declared key is unique and non-NULL. When the repair leaves the full query
   text alone (it changes only the incremental query or the configuration), that is immediate. When it
   changes it (``dedup``), the proof has two steps. The lemma, checked by :func:`eliminate_key_dedups`:
   ``SELECT ... FROM t [WHERE p] QUALIFY ROW_NUMBER() OVER (PARTITION BY k ...) = 1`` returns every row of
   ``SELECT ... FROM t [WHERE p]`` when ``k`` contains a declared non-NULL key of ``t``, because each
   partition then holds one row, whose row number is 1, whatever the ``ORDER BY``. Then the algebraic
   prover (:func:`kumosql.algebraic_equivalence.prove_equivalent_algebraic`, with every source's declared
   key and NOT NULL columns as constraints) proves the original full query equal to the repaired one with
   those de-duplications removed. The prover alone cannot do this: it keeps a window computation whole
   and does not know a window over a unique key is the identity.

What the second condition does not say: on a source state where the contract *has* re-delivered rows (the
declared key is then no longer unique), a de-duplicated model's full refresh is the original's over the
distinct rows. That is the intent of ``dedup`` (the original's full refresh would hold the same row
twice), and it is stated in the repair's ``assumptions``.

Costs a repair does not model: dropping ``updatePartitionFilter`` makes each merge scan the whole table
instead of the partitions the filter names, and a full re-run reads every source row on every run.
"""

from __future__ import annotations

import difflib
import itertools
import re
from collections.abc import Iterable
from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp

from .incremental import (
    _STRING,
    IncrementalError,
    IncrementalModel,
    SourceTable,
    _evaluate,
    _js_string,
    _split_args,
    parse_incremental_sqlx,
    prove,
    search_divergence,
)
from .incremental_monotone import source_schema
from .sqlx import _find_interpolation_end, split_sqlx_sections

EDITS = ("drop_update_partition_filter", "watermark_lookback", "full_rerun_merge", "dedup")
# Edits that cannot be combined: both rewrite how an incremental run reads its rows.
_EXCLUSIVE = (frozenset({"watermark_lookback", "full_rerun_merge"}),)

LOOKBACK = "INTERVAL 1 HOUR"
_DEDUP_ASSUMPTION = (
    "on a source state where the contract re-delivers rows, the repaired full refresh is the original's over the distinct rows"
)
_KEYWORDS = frozenset(
    ["where", "left", "right", "inner", "outer", "full", "cross", "join", "on", "using", "group", "order", "having", "qualify", "window", "limit", "union", "intersect", "except", "for", "tablesample", "unnest", "natural", "lateral", "offset", "as", "set", "when", "then", "else", "and", "or"]
)


@dataclass(frozen=True)
class Repair:
    """One repair: its edits, the patch, and the two proofs that justify offering it."""

    edits: tuple[str, ...]
    summary: str
    diff: str  # unified diff of the SQLX, never applied
    sqlx: str  # the repaired text
    rule: str  # the proof rule that shows the repaired model safe under the contract
    detail: str
    full_refresh: str  # how the full refresh is shown unchanged
    assumptions: tuple[str, ...] = ()

    def to_json(self) -> dict:
        return {
            "edits": list(self.edits),
            "summary": self.summary,
            "rule": self.rule,
            "full_refresh": self.full_refresh,
            "assumptions": list(self.assumptions),
            "diff": self.diff,
        }


@dataclass(frozen=True)
class Refusal:
    """An edit or combination that was not offered, and why."""

    edits: tuple[str, ...]
    reason: str


@dataclass
class RepairReport:
    repairs: list[Repair] = field(default_factory=list)
    refused: list[Refusal] = field(default_factory=list)
    already_safe: bool = False  # the original is proven safe, so there is nothing to repair


# ---------------------------------------------------------------------------
# Reading the SQLX
# ---------------------------------------------------------------------------


def _sections(sqlx: str) -> list[list[str]]:
    return [[kind, text] for kind, text in split_sqlx_sections(sqlx)]


def _join(sections: list[list[str]]) -> str:
    return "".join(text for _, text in sections)


def _interpolations(text: str) -> Iterable[tuple[int, int, str]]:
    """``(start, end, expression)`` of each top-level ``${...}`` in ``text`` (``end`` is after the brace)."""

    cursor = 0
    while True:
        opening = text.find("${", cursor)
        if opening < 0:
            return
        closing = _find_interpolation_end(text, opening)
        yield opening, closing + 1, text[opening + 2 : closing].strip()
        cursor = closing + 1


def _partition_text(table: SourceTable) -> tuple[str, str]:
    key = ", ".join(table.key)
    order = f"{table.time_column} DESC" if table.time_column else ", ".join(table.key)
    return key, order


def _dedup_clause(table: SourceTable) -> str:
    key, order = _partition_text(table)
    return f"QUALIFY ROW_NUMBER() OVER (PARTITION BY {key} ORDER BY {order}) = 1"


# ---------------------------------------------------------------------------
# Atomic edits: each takes the SQLX and returns (new SQLX, None) or (None, why not)
# ---------------------------------------------------------------------------

Edit = tuple[str | None, str | None]


def _drop_update_partition_filter(sqlx: str, model: IncrementalModel, **_) -> Edit:
    if not model.update_partition_filter:
        return None, "the model sets no updatePartitionFilter"
    value = re.compile(rf"updatePartitionFilter\s*:\s*(?:{_STRING})")
    sections = _sections(sqlx)
    for section in sections:
        if section[0] == "block" and section[1].lstrip().startswith("config"):
            found = value.search(section[1])
            if found is None:
                continue
            text = section[1]
            start, end = found.start(), found.end()
            before = text[:start].rstrip()
            if before.endswith(","):  # a property before it: take the comma that joined them
                start = len(before) - 1
                after = text[end:].lstrip()
                if after.startswith(",") and after[1:].lstrip().startswith("}"):
                    end = len(text) - len(after) + 1  # a trailing comma it leaves behind
            else:  # the first property: take the comma after it
                after = text[end:].lstrip()
                if after.startswith(","):
                    end = len(text) - len(after) + 1
                    end += len(text[end:]) - len(text[end:].lstrip(" \t"))
            text = text[:start] + text[end:]
            text = re.sub(r"(?m)^[ \t]*bigquery\s*:\s*\{\s*\},?[ \t]*\n", "", text)  # an emptied bigquery block goes too
            text = re.sub(r"\bbigquery\s*:\s*\{\s*\}\s*,?\s*", "", text)
            section[1] = re.sub(r",(\s*)\}\s*$", r"\1}", text)
            return _join(sections), None
    return None, "updatePartitionFilter is not a plain string in the config"


def _with_unique_key(sqlx: str, key: tuple[str, ...]) -> str | None:
    sections = _sections(sqlx)
    for section in sections:
        if section[0] == "block" and section[1].lstrip().startswith("config"):
            if re.search(r"\buniqueKey\s*:", section[1]):
                return None
            listed = ", ".join(f'"{k}"' for k in key)
            found = re.search(r"\btype\s*:\s*[\"']incremental[\"']", section[1])
            if found is None:
                return None
            section[1] = f'{section[1][: found.end()]}, uniqueKey: [{listed}]{section[1][found.end() :]}'
            return _join(sections)
    return None


def _matching_paren(text: str, opening: int) -> int | None:
    depth, quote = 0, None
    for index in range(opening, len(text)):
        char = text[index]
        if quote:
            if char == "\\":
                continue
            if char == quote:
                quote = None
        elif char in "'\"`":
            quote = char
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return index
    return None


_WATERMARK = re.compile(r"(?P<col>[A-Za-z_][\w.]*)\s*(?P<op>>=|>)\s*(?P<head>(?:TIMESTAMP_SUB\s*\(\s*)?COALESCE\s*\()", re.IGNORECASE)


def _watermark_lookback(sqlx: str, model: IncrementalModel, sources: dict[str, SourceTable], **_) -> Edit:
    """``ts > COALESCE((SELECT MAX(ts) FROM self), d)`` becomes ``ts >= TIMESTAMP_SUB(COALESCE(...), INTERVAL 1 HOUR)``,
    and the source's key becomes the ``uniqueKey`` when the model has none (a ``>=`` watermark only gets the key)."""

    if model.incremental_sql == model.full_sql:
        return None, "the incremental query has no watermark"
    tables = list(sources.values())
    if len(tables) != 1 or not tables[0].key or tables[0].time_column is None:
        return None, "needs one source with a declared key and an event-time column"
    table = tables[0]
    if model.unique_key and set(model.unique_key) != set(table.key):
        return None, "the uniqueKey is not the source's key"
    sections = _sections(sqlx)
    found_watermark = changed = False
    for section in sections:
        if section[0] != "sql" or found_watermark:
            continue
        text = section[1]
        for start, end, expression in _interpolations(text):
            body = text[start:end]
            found = _WATERMARK.search(body) if expression.startswith("when(") else None
            if found is None or found.group("col").split(".")[-1].lower() != table.time_column.lower():
                continue
            opening = body.index("(", found.start("head"))
            closing = _matching_paren(body, opening)
            if closing is None:
                continue
            call = body[found.start("head") : closing + 1]
            if not re.search(r"MAX\s*\(\s*(?:\w+\.)?" + re.escape(table.time_column) + r"\s*\)\s*FROM\s*\$\{self\(\)\}", call, re.IGNORECASE):
                continue
            found_watermark = True
            if found.group("op") == ">":
                lookback = call if call.upper().startswith("TIMESTAMP_SUB") else f"TIMESTAMP_SUB({call}, {LOOKBACK})"
                body = f"{body[: found.start()]}{found.group('col')} >= {lookback}{body[closing + 1 :]}"
                section[1] = text[:start] + body + text[end:]
                changed = True
            break
    if not found_watermark:
        return None, "no `ts > COALESCE((SELECT MAX(ts) FROM self), ...)` watermark on the source's event time"
    result = _join(sections)
    if not model.unique_key:
        result = _with_unique_key(result, table.key)
        if result is None:
            return None, "could not add a uniqueKey to the config"
    elif not changed:
        return None, "the watermark already reads with >= and the model already merges"
    return result, None


def _full_rerun_merge(sqlx: str, model: IncrementalModel, **_) -> Edit:
    if not model.unique_key:
        return None, "no uniqueKey: a full re-run would append every row again"
    if model.pre_operations:
        return None, "incremental pre_operations run first and are not rewritten"
    sections = _sections(sqlx)
    changed = False
    for section in sections:
        if section[0] != "sql":
            continue
        text, out, cursor = section[1], [], 0
        for start, end, expression in _interpolations(text):
            when = re.fullmatch(r"when\((.*)\)", expression, re.DOTALL)
            if when is None:
                continue
            args = _split_args(when.group(1))
            if len(args) not in (2, 3):
                continue
            condition = args[0].replace(" ", "")
            if condition == "incremental()":
                chosen = _js_string(args[2]) if len(args) == 3 else ""
            elif condition == "!incremental()":
                chosen = _js_string(args[1])
            else:
                continue
            out.append(text[cursor:start])
            out.append(chosen)
            cursor = end
            changed = True
        out.append(text[cursor:])
        section[1] = "".join(out)
    if not changed:
        return None, "the incremental run already executes the full query"
    return _join(sections), None


def _source_occurrences(text: str, names: dict[str, SourceTable], target: str) -> list[tuple[int, int, str, str | None]]:
    """``(start, end, source name, alias)`` for each top-level ``${ref(...)}`` of a wanted source in a FROM or JOIN."""

    found = []
    for start, end, expression in _interpolations(text):
        if not expression.startswith("ref("):
            continue
        try:
            name = _evaluate(expression, target, False)
        except IncrementalError:
            continue
        if name not in names or not re.search(r"(?i)\b(?:from|join)\s*$", text[:start]):
            continue
        tail = re.match(r"\s+(?:(?i:as)\s+)?([A-Za-z_]\w*)", text[end:])
        alias = tail.group(1) if tail and tail.group(1).lower() not in _KEYWORDS else None
        found.append((start, end, name, alias))
    return found


def _single_table_select(model: IncrementalModel) -> bool:
    try:
        trees = [sqlglot.parse_one(sql, read=model.dialect) for sql in (model.full_sql, model.incremental_sql)]
    except sqlglot.errors.SqlglotError:
        return False
    for tree in trees:
        if not isinstance(tree, exp.Select):
            return False
        source = tree.args.get("from_") or tree.args.get("from")
        if source is None or not isinstance(source.this, exp.Table) or tree.args.get("joins"):
            return False
        if any(tree.args.get(k) for k in ("group", "having", "qualify", "limit", "order", "distinct", "windows")):
            return False
        if any(isinstance(n, (exp.Window, exp.AggFunc, exp.Subquery, exp.Select)) for e in tree.expressions for n in e.walk()):
            return False
    return True


def _dedup(sqlx: str, model: IncrementalModel, sources: dict[str, SourceTable], kinds: frozenset[str], tables, **_) -> Edit:
    if "duplicate" not in kinds:
        return None, "the contract does not re-deliver rows"
    if "null_key" in kinds:
        return None, "the contract allows NULL keys, which no de-duplication on the key can absorb"
    changing = {t.lower() for t in tables} if tables else {n.lower() for n in sources}
    wanted = {n: t for n, t in sources.items() if n.lower() in changing}
    if not wanted:
        return None, "no source table can be re-delivered"
    missing = [n for n, t in wanted.items() if not t.key]
    if missing:
        return None, "source without a declared key: " + ", ".join(sorted(missing))
    sections = _sections(sqlx)
    if len(sources) == 1 and _single_table_select(model):  # one table: the QUALIFY goes on the query itself (rule R3's shape)
        table = next(iter(wanted.values()))
        for section in reversed(sections):
            if section[0] == "sql" and section[1].strip():
                body = section[1].rstrip().rstrip(";").rstrip()
                section[1] = f"{body}\n{_dedup_clause(table)}\n"
                return _join(sections), None
    replaced: set[str] = set()
    for section in sections:
        if section[0] != "sql":
            continue
        text = section[1]
        for start, end, name, alias in reversed(_source_occurrences(text, wanted, model.target)):
            table = wanted[name]
            inner = f"(SELECT * FROM {text[start:end]} WHERE TRUE {_dedup_clause(table)})"
            tail = "" if alias else f" AS {name}"
            text = text[:start] + inner + tail + text[end:]
            replaced.add(name)
        section[1] = text
    if replaced != set(wanted):
        return None, "could not find where the model reads " + ", ".join(sorted(set(wanted) - replaced))
    return _join(sections), None


_APPLY = {
    "drop_update_partition_filter": _drop_update_partition_filter,
    "watermark_lookback": _watermark_lookback,
    "full_rerun_merge": _full_rerun_merge,
    "dedup": _dedup,
}


# ---------------------------------------------------------------------------
# The lemma and the full-refresh proof
# ---------------------------------------------------------------------------


def _is_key_dedup(select: exp.Select, sources: dict[str, SourceTable]) -> bool:
    qualify = select.args.get("qualify")
    if qualify is None or select.args.get("joins") or not isinstance(qualify.this, exp.EQ):
        return False
    window, one = qualify.this.this, qualify.this.expression
    if isinstance(one, exp.Window):
        window, one = one, window
    if not (isinstance(window, exp.Window) and isinstance(window.this, exp.RowNumber)):
        return False
    if not (isinstance(one, exp.Literal) and not one.is_string and one.name == "1"):
        return False
    source = select.args.get("from_") or select.args.get("from")
    if source is None or not isinstance(source.this, exp.Table):
        return False
    table = next((t for n, t in sources.items() if n.lower() == source.this.name.lower()), None)
    partition = window.args.get("partition_by") or []
    if table is None or not table.key or not partition or not all(isinstance(p, exp.Column) for p in partition):
        return False
    order = window.args.get("order")
    if order is not None and not all(isinstance(o.this, exp.Column) for o in order.expressions):
        return False  # an ORDER BY expression could fail on the one row of a partition
    return {k.lower() for k in table.key} <= {p.name.lower() for p in partition}


def eliminate_key_dedups(sql: str, sources: dict[str, SourceTable], dialect: str = "bigquery") -> str | None:
    """``sql`` with every ``QUALIFY ROW_NUMBER() OVER (PARTITION BY k ...) = 1`` removed where ``k`` contains
    a declared key of the single table the select reads (see the module docstring for why that keeps every
    row), and a ``(SELECT * FROM t) AS a`` left behind written as ``t AS a``. None when nothing was removed."""

    try:
        tree = sqlglot.parse_one(sql, read=dialect)
    except sqlglot.errors.SqlglotError:
        return None
    touched: list[exp.Select] = []
    for select in list(tree.find_all(exp.Select)):
        if _is_key_dedup(select, sources):
            select.set("qualify", None)
            where = select.args.get("where")
            if where is not None and isinstance(where.this, exp.Boolean) and where.this.this:
                select.set("where", None)
            touched.append(select)
    if not touched:
        return None
    for select in touched:
        parent = select.parent
        plain = (
            isinstance(parent, exp.Subquery)
            and len(select.expressions) == 1
            and isinstance(select.expressions[0], exp.Star)
            and not any(v for k, v in select.args.items() if k not in ("expressions", "from", "from_"))
            and not any(v for k, v in parent.args.items() if k not in ("this", "alias"))
        )
        if plain:
            source = select.args.get("from_") or select.args.get("from")
            table = source.this.copy()
            if parent.args.get("alias") is not None:
                table.set("alias", parent.args["alias"].copy())
            parent.replace(table)
    return tree.sql(dialect=dialect)


def _normal(sql: str, dialect: str) -> str | None:
    try:
        return sqlglot.parse_one(sql, read=dialect).sql(dialect=dialect)
    except sqlglot.errors.SqlglotError:
        return None


def prove_full_refresh_unchanged(
    original: IncrementalModel, repaired: IncrementalModel, sources: dict[str, SourceTable]
) -> tuple[bool, str, tuple[str, ...]]:
    """``(proven, how, assumptions)``: the repaired full query equals the original's on every state where
    each declared key is unique and non-NULL."""

    dialect = original.dialect
    before, after = _normal(original.full_sql, dialect), _normal(repaired.full_sql, dialect)
    if before is None or after is None:
        return False, "the full query could not be read", ()
    if before == after:
        return True, "the full query is untouched", ()
    reduced = eliminate_key_dedups(repaired.full_sql, sources, dialect)
    if reduced is None:
        return False, "the full query changed and no lemma explains the change", ()
    from .algebraic_equivalence import prove_equivalent_algebraic
    from .incremental_monotone import contract_constraints
    from .smt_equivalence import SmtStatus

    constraints = contract_constraints(sources, (), None)
    try:
        result = prove_equivalent_algebraic(
            original.full_sql,
            reduced,
            schema=source_schema(sources),
            constraints=constraints,
            dialect=dialect,
            timeout_ms=10000,
        )
    except Exception as error:  # noqa: BLE001 - a prover crash is never a proof
        return False, f"the prover failed: {type(error).__name__}", ()
    if result.status is not SmtStatus.PROVEN_EQUIVALENT:
        return False, f"the algebraic prover did not prove it: {result.reason[:100]}", ()
    return (
        True,
        "key de-duplication keeps every row (declared key), then the algebraic prover proves the original full query equal under the keys",
        (_DEDUP_ASSUMPTION, *dict.fromkeys(result.assumptions)),
    )


# ---------------------------------------------------------------------------
# Proposing repairs
# ---------------------------------------------------------------------------

_SUMMARY = {
    "drop_update_partition_filter": "drop updatePartitionFilter",
    "watermark_lookback": "read the watermark with >= and a lookback, merge on the source key",
    "full_rerun_merge": "re-run the full query on every run and merge on uniqueKey",
    "dedup": "keep one row per source key",
}


def _combinations(names: list[str]) -> list[tuple[str, ...]]:
    out: list[tuple[str, ...]] = []
    for size in range(1, len(names) + 1):
        for combo in itertools.combinations(names, size):
            if not any(group <= set(combo) and len(group) > 1 for group in _EXCLUSIVE):
                out.append(combo)
    return out


def _diff(before: str, after: str, path: str) -> str:
    return "".join(
        difflib.unified_diff(before.splitlines(keepends=True), after.splitlines(keepends=True), f"a/{path}", f"b/{path}")
    )


def propose_repairs(
    sqlx: str,
    target: str,
    sources: dict[str, SourceTable],
    kinds: Iterable[str],
    *,
    tables: tuple[str, ...] | None = None,
    path: str = "model.sqlx",
    verify_seeds: int = 8,
) -> RepairReport:
    """Repairs of the SQLX model ``target`` that are proven safe under ``kinds`` and leave its full refresh unchanged.

    ``tables`` limits which source tables the contract changes (default all). The report lists each
    minimal repair with its patch, and the edits that could not be applied or proven, with the reason.
    """

    kinds = frozenset(kinds)
    report = RepairReport()
    try:
        model = parse_incremental_sqlx(sqlx, target)
    except IncrementalError as error:
        report.refused.append(Refusal((), f"the model is not supported: {error}"))
        return report
    if model.unmodelled:
        report.refused.append(Refusal((), "not modelled: " + ", ".join(model.unmodelled)))
        return report
    if prove(model, sources, kinds, tables) is not None:
        report.already_safe = True
        return report
    context = {"model": model, "sources": sources, "kinds": kinds, "tables": tables}
    applicable: dict[str, str] = {}
    for name in EDITS:
        edited, why = _APPLY[name](sqlx, **context)
        if edited is None:
            report.refused.append(Refusal((name,), why or "not applicable"))
        else:
            applicable[name] = edited
    offered: list[frozenset[str]] = []
    for combo in _combinations([n for n in EDITS if n in applicable]):
        if any(found <= set(combo) for found in offered):
            continue
        text = sqlx
        why = None
        for name in combo:  # edits compose in the order of EDITS, each reading the text the last one left
            nxt, why = _APPLY[name](text, **dict(context, model=_reparse(text, target) or model))
            if nxt is None:
                break
            text = nxt
        else:
            repair, why = _check(sqlx, text, combo, model, sources, kinds, tables, target, path, verify_seeds)
            if repair is not None:
                report.repairs.append(repair)
                offered.append(frozenset(combo))
                continue
        if len(combo) == 1 or why is not None:
            report.refused.append(Refusal(combo, why or "not proven"))
    return report


def _reparse(sqlx: str, target: str) -> IncrementalModel | None:
    try:
        return parse_incremental_sqlx(sqlx, target)
    except IncrementalError:
        return None


def _check(
    original_text: str,
    text: str,
    combo: tuple[str, ...],
    model: IncrementalModel,
    sources: dict[str, SourceTable],
    kinds: frozenset[str],
    tables,
    target: str,
    path: str,
    verify_seeds: int,
) -> tuple[Repair | None, str | None]:
    repaired = _reparse(text, target)
    if repaired is None or repaired.unmodelled:
        return None, "the repaired model cannot be read back"
    verdict = prove(repaired, sources, kinds, tables)
    if verdict is None:
        return None, "no proof rule shows the repaired model safe under the contract"
    unchanged, how, assumptions = prove_full_refresh_unchanged(model, repaired, sources)
    if not unchanged:
        return None, "the full refresh is not proven unchanged: " + how
    if verify_seeds:
        try:
            found = search_divergence(repaired, sources, kinds, seeds=verify_seeds, batches=4, tables=tables, time_limit=20.0)
        except (IncrementalError, TimeoutError):
            found = None
        if found is not None:
            return None, "a counterexample search diverged on the repaired model, so its proof is not trusted"
    summary = " and ".join(_SUMMARY[n] for n in combo)
    return (
        Repair(
            edits=combo,
            summary=summary,
            diff=_diff(original_text, text, path),
            sqlx=text,
            rule=verdict.rule,
            detail=verdict.detail,
            full_refresh=how,
            assumptions=assumptions,
        ),
        None,
    )
