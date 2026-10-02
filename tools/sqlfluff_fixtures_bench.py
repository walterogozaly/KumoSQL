"""Score KumoSQL on sqlfluff's rule fixtures: about 850 ``fail_str`` to ``fix_str`` pairs, no LLM.

Source: https://github.com/sqlfluff/sqlfluff (MIT licence, Alan Cruickshank and contributors),
version 4.3.0, commit ``2e2275713d078f29529250f7ff883fbf66f83e0d``,
``test/fixtures/rules/std_rule_cases/*.yml``. Every case with a ``fix_str`` is copied into
``benchmarks/sqlfluff_rule_cases/rule-fixtures.json`` (the licence is next to it); ``extract`` rebuilds
that file from a checkout of the pinned commit.

Each pair is one query a lint rule flags and the exact query sqlfluff's fixer returns. Three
questions are asked of them, kept apart in the report:

* **Semantic fixes** (rules AL, AM, CV, RF, ST and the rest that rewrite structure): does the fix
  keep the meaning? The algebraic prover tries to prove ``fail`` and ``fix`` equivalent, with the
  tables and columns the two queries mention as the schema. A proof is re-run on random DuckDB
  databases (a database that separates the pair makes the verdict ``wrong``). Fixes that change
  meaning by design (``= NULL`` to ``IS NULL``, reordering the select list, dropping an unused join)
  are labelled as their own category: those must be refuted or left unknown, never proven.
* **Layout fixes** (LT, CP, JJ): do the parse trees, comments and literals stay the same? Checked
  with sqlglot's parser in the case's dialect.
* **KumoSQL's own rules** where they overlap: ``format_sql`` (sqlfluff behind KumoSQL's verified
  wrapper) against the layout fixtures, and the structural rewrite rules against the semantic ones.

Outcomes per pair: ``proven``, ``refuted`` (a database separates the pair), ``unknown``,
``unsupported`` (the parser or prover cannot read the SQL, or no dialect mapping), ``timeout``,
``error`` (a crash) and ``wrong`` (a false proof). Rules whose code hashes into one fifth are
held out of development runs.

    python tools/sqlfluff_fixtures_bench.py semantic              # prover over the semantic fixes
    python tools/sqlfluff_fixtures_bench.py layout                # parse-tree, comment and literal checks
    python tools/sqlfluff_fixtures_bench.py kumosql               # format_sql and rewrite rules vs the fixtures
    python tools/sqlfluff_fixtures_bench.py semantic --split dev  # development rules only
    python tools/sqlfluff_fixtures_bench.py extract CHECKOUT      # rebuild the data file
"""

from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import sys
import time

import sqlglot
from sqlglot import exp

logging.getLogger("sqlglot").setLevel(logging.ERROR)

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "benchmarks" / "sqlfluff_rule_cases" / "rule-fixtures.json"
SOURCE = {
    "repository": "https://github.com/sqlfluff/sqlfluff",
    "version": "4.3.0",
    "commit": "2e2275713d078f29529250f7ff883fbf66f83e0d",
    "path": "test/fixtures/rules/std_rule_cases/*.yml",
    "licence": "MIT (Copyright (c) 2018-2026 Alan Cruickshank), see benchmarks/sqlfluff_rule_cases/LICENSE.md",
}

# --- cases -------------------------------------------------------------------------


def extract(checkout: Path, out: Path = DATA) -> int:
    """Rebuild the data file from a checkout of the pinned commit."""

    import yaml

    cases = []
    for path in sorted((checkout / SOURCE["path"].rsplit("/", 1)[0]).glob("*.yml")):
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        for name, case in data.items():
            if name == "rule" or "fix_str" not in case:
                continue
            configs = case.get("configs") or {}
            cases.append({
                "id": f"{path.stem}/{name}",
                "rule": str(data.get("rule") or case.get("rules") or path.stem),
                "dialect": (configs.get("core") or {}).get("dialect", "ansi"),
                "fail": case["fail_str"],
                "fix": case["fix_str"],
                "configs": configs,
            })
    out.write_text(json.dumps({"source": SOURCE, "cases": cases}, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    return len(cases)


@dataclass
class Case:
    id: str
    rule: str
    dialect: str
    fail: str
    fix: str
    configs: dict

    @property
    def codes(self) -> list[str]:
        return [c.strip() for c in self.rule.split(",")]

    @property
    def family(self) -> str:
        return self.codes[0][:2]

    @property
    def held_out(self) -> bool:
        return any(int(hashlib.sha1(f"sqlfluff-rule:{c}".encode()).hexdigest(), 16) % 5 == 0 for c in self.codes)


def load_cases() -> list[Case]:
    data = json.loads(DATA.read_text(encoding="utf-8"))
    if data["source"]["commit"] != SOURCE["commit"]:
        raise SystemExit("the data file is not from the pinned commit")
    return [Case(**c) for c in data["cases"]]


def split(cases: list, name: str) -> list:
    if name == "dev":
        return [c for c in cases if not c.held_out]
    if name == "held-out":
        return [c for c in cases if c.held_out]
    return cases


SEMANTIC_FAMILIES = {"AL", "AM", "CV", "RF", "ST"}
LAYOUT_FAMILIES = {"LT", "CP", "JJ"}

OUTCOMES = ("proven", "refuted", "unknown", "unsupported", "timeout", "error", "wrong")

# sqlfluff dialect name -> sqlglot dialect ("" is sqlglot's default, close to ANSI). A dialect sqlglot lacks
# makes the case ``unsupported`` rather than being read in the wrong dialect.
DIALECTS = {
    "ansi": "", "bigquery": "bigquery", "snowflake": "snowflake", "tsql": "tsql", "postgres": "postgres",
    "oracle": "oracle", "mysql": "mysql", "mariadb": "mysql", "redshift": "redshift", "trino": "trino",
    "duckdb": "duckdb", "athena": "athena", "sqlite": "sqlite", "databricks": "databricks", "hive": "hive",
    "sparksql": "spark", "clickhouse": "clickhouse", "teradata": "teradata", "starrocks": "starrocks", "doris": "doris",
}


class Unsupported(Exception):
    """The SQL cannot be read faithfully (no dialect, a parse error, or a fallback to a raw command)."""


def parse_all(sql: str, dialect: str) -> list[exp.Expression]:
    """The statements of ``sql`` in the sqlfluff dialect, or ``Unsupported``."""

    if dialect not in DIALECTS:
        raise Unsupported(f"no sqlglot dialect for {dialect}")
    try:
        trees = sqlglot.parse(sql, read=DIALECTS[dialect], error_level=sqlglot.ErrorLevel.RAISE)
    except sqlglot.errors.SqlglotError as error:
        raise Unsupported(f"parse error: {str(error).splitlines()[0][:90]}") from error
    trees = [t for t in trees if t is not None and not isinstance(t, exp.Semicolon)]
    if not trees:
        raise Unsupported("no statement")
    if any(tree.find(exp.Command) for tree in trees):
        raise Unsupported("syntax sqlglot reads only as a raw command")
    return trees


# --- labels ------------------------------------------------------------------------


def output_names(tree: exp.Expression) -> list[str] | None:
    """The top-level select list as lower-case names (``*`` for a star); None when the query is not a plain select."""

    node = tree
    if isinstance(node, (exp.Create, exp.Insert)) and node.expression is not None:
        node = node.expression
    while isinstance(node, exp.Subquery):
        node = node.this
    if isinstance(node, exp.Union):
        return output_names(node.this)
    if not isinstance(node, exp.Select):
        return None
    names = []
    for projection in node.expressions:
        inner = projection.this if isinstance(projection, exp.Alias) else projection
        if isinstance(inner, exp.Star) or (isinstance(inner, exp.Column) and isinstance(inner.this, exp.Star)):
            names.append(inner.sql().lower())
        else:
            names.append(projection.alias_or_name.lower() or projection.sql().lower())
    return names


def _has_star(tree: exp.Expression) -> bool:
    node = tree
    if isinstance(node, (exp.Create, exp.Insert)) and node.expression is not None:
        node = node.expression
    while isinstance(node, (exp.Subquery, exp.Union)):
        node = node.this
    return isinstance(node, exp.Select) and any(
        isinstance(p, exp.Star) or (isinstance(p, exp.Column) and isinstance(p.this, exp.Star)) for p in node.expressions
    )


def meaning_label(case: Case, left: list[exp.Expression] | None, right: list[exp.Expression] | None) -> str:
    """``""`` when sqlfluff intends the fix to keep the meaning, else why the fix changes it by design.

    Decided from what the rule does (and the shape of the pair), never from a prover verdict:

    * CV05 turns ``= NULL`` into ``IS NULL``: the first never holds, the second does.
    * ST06 moves complex select targets after simple ones: the result's columns come back in a new order (also
      through a bare ``*`` over a reordered CTE).
    * ST07 (``USING`` to ``ON``) and CV08 (``RIGHT`` to ``LEFT JOIN``) change which columns a bare ``*`` returns
      and in what order.
    """

    first = case.codes[0]
    if first == "CV05":
        return "compares to NULL with = (never true) and the fix uses IS (can be true)"
    if first == "ST06" and left and right:
        a, b = output_names(left[-1]), output_names(right[-1])
        if a is not None and b is not None and a != b:
            return "reorders the output columns"
        if _has_star(left[-1]):
            return "a bare * returns the reordered columns in a different order"
    if first in ("ST07", "CV08") and left and _has_star(left[-1]):
        return "a bare * returns its columns in a different order or set"
    return ""


# --- schema ------------------------------------------------------------------------

PLACEHOLDERS = ("zz_extra1", "zz_extra2")  # two columns nobody mentions, so ``SELECT *`` has a width


def _sources(select: exp.Select) -> list[exp.Expression]:
    found = []
    from_ = select.args.get("from_") or select.args.get("from")
    if from_ is not None:
        found.append(from_.this)
    for join in select.args.get("joins") or []:
        found.append(join.this)
    return found


def infer_schema(trees: list[exp.Expression]) -> tuple[dict[str, list[str]], dict[str, str]]:
    """Tables and columns the statements mention, plus a column type guess for each (``int``, ``text`` or ``date``).

    A fixture has no schema, so one is read off the queries: a column appears when some query refers to it
    (qualified by a table or alias, or unqualified in a select over exactly one table), and every table gets
    two placeholder columns so a ``SELECT *`` has a width. A column read through a derived table or CTE that is
    just ``SELECT * FROM t`` belongs to ``t``. A verdict therefore holds for databases with these columns, not
    for every possible schema.
    """

    ctes = {cte.alias.lower(): cte.this for tree in trees for cte in tree.find_all(exp.CTE)}
    columns: dict[str, dict[str, None]] = {}
    kinds: dict[tuple[str, str], str] = {}

    def resolve(source: exp.Expression, depth: int = 0) -> str | None:
        """The real table a source passes its columns through from, ``None`` if it computes them."""

        if depth > 6:
            return None
        if isinstance(source, exp.Table):
            name = source.name.lower() if source.name else ""
            if not name or isinstance(source.this, exp.Func):
                return None
            if name in ctes and not source.db:
                return resolve_query(ctes[name], depth + 1)
            return name
        if isinstance(source, exp.Subquery):
            return resolve_query(source.this, depth + 1)
        return None

    def resolve_query(query: exp.Expression, depth: int) -> str | None:
        if isinstance(query, exp.Select) and len(query.expressions) == 1 and isinstance(query.expressions[0], exp.Star):
            sources = _sources(query)
            if len(sources) == 1:
                return resolve(sources[0], depth)
        return None

    for tree in trees:
        for table in tree.find_all(exp.Table):
            if table.name and not (table.name.lower() in ctes and not table.db):
                owner = resolve(table)
                if owner:
                    columns.setdefault(owner, {})
        for column in tree.find_all(exp.Column):
            if isinstance(column.this, exp.Star):
                continue
            node = column.find_ancestor(exp.Select)
            qualifier = column.table.lower() if column.table else ""
            owner = None
            while node is not None and owner is None:
                sources = _sources(node)
                if qualifier:
                    for source in sources:
                        if source.alias_or_name.lower() == qualifier:
                            owner = resolve(source)
                else:
                    own = {p.alias.lower() for p in node.expressions if isinstance(p, exp.Alias)}
                    if column.name.lower() in own or len(sources) != 1:
                        break  # an output alias, or ambiguous between several sources
                    owner = resolve(sources[0])
                node = node.find_ancestor(exp.Select)
            if owner is not None and column.name:
                name = column.name.lower()
                columns.setdefault(owner, {}).setdefault(name)
                parent = column.parent
                if isinstance(parent, (exp.Binary, exp.Between, exp.In, exp.Like)):
                    others = [parent.args.get(k) for k in ("this", "expression", "low", "high")] + list(parent.args.get("expressions") or [])
                    if any(isinstance(o, exp.Literal) and o.is_string for o in others):
                        kinds.setdefault((owner, name), "text")
    schema = {t: list(cols) + list(PLACEHOLDERS) for t, cols in columns.items()}
    return schema, {f"{t}.{c}": k for (t, c), k in kinds.items()}


# --- semantic fixes ----------------------------------------------------------------


@dataclass
class Verdict:
    outcome: str  # one of OUTCOMES
    detail: str = ""
    label: str = ""  # why the fix changes meaning by design; "" when it should keep the meaning
    witness: dict | None = None
    adapted: bool = False

    @property
    def correct(self) -> bool:
        """Proven for a meaning-keeping fix, or refuted for one that changes meaning by design."""

        return self.outcome == ("refuted" if self.label else "proven")


def _outcome_of(reason: str) -> str:
    if "timed out" in reason or "timeout" in reason:
        return "timeout"
    if reason.startswith(("unsupported", "parse error")):
        return "unsupported"
    return "unknown"


def _check_schema(schema: dict[str, list[str]], kinds: dict[str, str]):
    from kumosql.random_check import Column, Schema, Table

    return Schema([
        Table(name, [Column(c, kinds.get(f"{name}.{c}", "int")) for c in cols]) for name, cols in schema.items()
    ])


def _isolated(fn, *args, timeout: float = 90.0):
    """Run ``fn`` in a child process: DuckDB can crash the interpreter on odd joins, and that must not end the run.

    Returns ``False`` (not checkable) when the child dies or overruns.
    """

    import multiprocessing

    context = multiprocessing.get_context("fork")
    queue = context.SimpleQueue()

    def target():
        import faulthandler

        faulthandler.disable()  # a DuckDB crash is expected and handled; keep the traceback out of the output
        queue.put(fn(*args))

    child = context.Process(target=target)
    child.start()
    child.join(timeout)
    if child.is_alive():
        child.kill()
        child.join()
        return False
    if child.exitcode != 0 or queue.empty():
        return False
    return queue.get()


def execute_difference(left: str, right: str, dialect: str, schema: dict, kinds: dict, trials: int):
    """A random database on which the two statements return different row bags, ``None`` if none is found, ``False`` if DuckDB cannot run them."""

    return _isolated(_execute_difference, left, right, dialect, schema, kinds, trials)


def _execute_difference(left: str, right: str, dialect: str, schema: dict, kinds: dict, trials: int):

    from kumosql.random_check import CheckError, find_difference

    import duckdb

    try:
        witness = find_difference(_check_schema(schema, kinds), left, right, mode="bag", trials=trials, dialect=DIALECTS[dialect])
    except (CheckError, duckdb.Error, sqlglot.errors.SqlglotError, KeyError, ValueError):
        return False
    if witness is None:
        return None
    return {"tables": {t: [list(r) for r in rows] for t, rows in witness.tables.items()}, "only_left": list(map(list, witness.only_left)), "only_right": list(map(list, witness.only_right))}


def decide_statement_pair(left_sql: str, right_sql: str, dialect: str, label: str, trials: int, timeout_ms: int) -> Verdict:
    """Prove, refute or leave unknown one pair of single statements. Never reads the published expectation twice."""

    from kumosql.algebraic_equivalence import prove_equivalent_algebraic

    try:
        left, right = parse_all(left_sql, dialect), parse_all(right_sql, dialect)
    except Unsupported as error:
        return Verdict("unsupported", str(error), label)
    if len(left) != 1 or len(right) != 1:
        return Verdict("unsupported", f"{len(left)} and {len(right)} statements", label)
    schema, kinds = infer_schema(left + right)
    try:
        result = prove_equivalent_algebraic(left_sql, right_sql, schema=schema, compare_names=True, dialect=DIALECTS[dialect], timeout_ms=timeout_ms)
    except Exception as error:  # a crash is a failure to prove, never a proof
        return Verdict("error", f"{type(error).__name__}: {str(error)[:80]}", label)
    if result.proven:
        witness = execute_difference(left_sql, right_sql, dialect, schema, kinds, trials * 3)
        if witness:
            return Verdict("wrong", "proved equivalent but DuckDB separates the queries", label, witness)
        if label:
            return Verdict("wrong", f"proved equivalent although the fix {label}", label)
        return Verdict("proven", "proved" + ("" if witness is None else " (not re-checkable in DuckDB)"), label)
    witness = execute_difference(left_sql, right_sql, dialect, schema, kinds, trials)
    if witness:
        return Verdict("refuted", "counterexample", label, witness)
    return Verdict(_outcome_of(result.reason), result.reason[:120], label)


def decide_semantic(case: Case, trials: int = 120, timeout_ms: int = 4000) -> Verdict:
    try:
        left, right = parse_all(case.fail, case.dialect), parse_all(case.fix, case.dialect)
    except Unsupported as error:
        label = meaning_label(case, None, None)
        return Verdict("unsupported", str(error), label)
    label = meaning_label(case, left, right)
    verdict = decide_statement_pair(case.fail, case.fix, case.dialect, label, trials, timeout_ms)
    verdict.label = label
    return verdict


# --- adapted cases -----------------------------------------------------------------
#
# The prover reads one query. A fixture that is a script, or a query inside ``INSERT ... SELECT`` or
# ``CREATE TABLE ... AS``, is therefore unsupported as written. Its adapted form compares each changed
# statement's query on its own, after checking the statement around the query is the same on both sides.


SAME_TREES = "the statements parse to identical trees"


def _split_statement(tree: exp.Expression) -> tuple[str, exp.Expression] | None:
    """``(the statement around its query, the query)``; the whole statement is the query for a plain SELECT."""

    if isinstance(tree, exp.Query):
        return "", tree
    if isinstance(tree, (exp.Insert, exp.Create)) and isinstance(tree.expression, exp.Query):
        copy = tree.copy()
        query = copy.expression
        with_ = copy.args.pop("with_", None) or copy.args.pop("with", None)
        if with_ is not None:
            query.set("with_" if "with_" in query.arg_types else "with", with_)
        copy.set("expression", exp.var("QUERY"))
        return copy.sql(), query
    return None


def adapted_pairs(case: Case) -> list[tuple[str, str]] | str:
    """The query pairs of an adapted case, or why the case cannot be adapted."""

    try:
        left, right = parse_all(case.fail, case.dialect), parse_all(case.fix, case.dialect)
    except Unsupported as error:
        return str(error)
    if len(left) != len(right):
        return f"{len(left)} statements become {len(right)}"
    dialect = DIALECTS[case.dialect]
    pairs = []
    for a, b in zip(left, right):
        if a == b:
            continue
        x, y = _split_statement(a), _split_statement(b)
        if x is None or y is None or x[0] != y[0]:
            return "a changed statement is not a query"
        pairs.append((x[1].sql(dialect=dialect), y[1].sql(dialect=dialect)))
    return pairs or SAME_TREES


def decide_adapted(case: Case, trials: int = 120, timeout_ms: int = 4000) -> Verdict | None:
    """Verdict for the adapted form of a case, ``None`` when the case needs no adaptation (a single plain query)."""

    if "{{" in case.fail or "{%" in case.fail:
        masked = Case(case.id, case.rule, case.dialect, mask_templates(case.fail), mask_templates(case.fix), case.configs)
        verdict = decide_semantic(masked, trials, timeout_ms)
        verdict.adapted = True
        return verdict
    try:
        left, right = parse_all(case.fail, case.dialect), parse_all(case.fix, case.dialect)
    except Unsupported:
        return None
    label = meaning_label(case, left, right)
    if len(left) == 1 and len(right) == 1 and isinstance(left[0], exp.Query) and isinstance(right[0], exp.Query):
        return None
    pairs = adapted_pairs(case)
    if pairs == SAME_TREES:
        return Verdict("wrong" if label else "proven", "identical parse trees, statement by statement", label, adapted=True)
    if isinstance(pairs, str):
        return Verdict("unsupported", pairs, label, adapted=True)
    verdicts = [decide_statement_pair(a, b, case.dialect, label, trials, timeout_ms) for a, b in pairs]
    rank = ["wrong", "error", "timeout", "unsupported", "unknown", "refuted", "proven"]
    worst = min(verdicts, key=lambda v: rank.index(v.outcome))
    if any(v.outcome == "refuted" for v in verdicts) and not any(v.outcome in ("wrong", "error") for v in verdicts):
        worst = next(v for v in verdicts if v.outcome == "refuted")
    return Verdict(worst.outcome, f"{len(verdicts)} statement(s): {worst.detail}", label, worst.witness, adapted=True)


# --- layout fixes ------------------------------------------------------------------
#
# A layout fix may change spacing, line breaks, indentation and the case of keywords, and nothing else: the
# parse tree, the comments (text and order) and the string literals must come out the same. Identifier case
# is not layout, so rules that rename identifiers (CP02) are labelled as changing meaning and must not pass.

LAYOUT_BY_DESIGN = {
    "CP02": "re-cases or re-spells identifiers, which a case-sensitive engine reads as different names",
}


def _fold_function_names(tree: exp.Expression) -> exp.Expression:
    """Function names are case-insensitive in every dialect read here; only quoted names would not be."""

    tree = tree.copy()
    for node in tree.find_all(exp.Anonymous):
        if isinstance(node.this, str):
            node.set("this", node.this.lower())
    for node in tree.walk():
        node.comments = None
    return tree


def _tokens(sql: str, dialect: str):
    return sqlglot.tokenize(sql, read=DIALECTS[dialect])


def layout_differences(case: Case) -> list[str]:
    """What a fix changes beyond layout: any of ``tree``, ``comments``, ``literals``; ``[]`` when only layout changed."""

    left, right = parse_all(case.fail, case.dialect), parse_all(case.fix, case.dialect)
    changed = []
    if len(left) != len(right) or any(_fold_function_names(a) != _fold_function_names(b) for a, b in zip(left, right)):
        changed.append("tree")
    before, after = _tokens(case.fail, case.dialect), _tokens(case.fix, case.dialect)

    def comments(tokens):
        # re-indenting the lines of a block comment is layout; its words and their order are not
        return ["\n".join(line.strip() for line in c.strip().splitlines()) for t in tokens for c in (t.comments or [])]

    def literals(tokens):
        return [t.text for t in tokens if t.token_type in (sqlglot.TokenType.STRING, sqlglot.TokenType.NATIONAL_STRING, sqlglot.TokenType.BYTE_STRING, sqlglot.TokenType.RAW_STRING)]

    if comments(before) != comments(after):
        changed.append("comments")
    if literals(before) != literals(after):
        changed.append("literals")
    return changed


_JINJA = re.compile(r"\{\{[-+]?\s*(.*?)\s*[-+]?\}\}|\{%[-+]?\s*(.*?)\s*[-+]?%\}|\{#.*?#\}", re.S)


def mask_templates(sql: str) -> str:
    """Replace each Jinja tag by a placeholder that depends only on the tag's text, so padding inside the tag
    (what JJ01 fixes) cannot show and everything outside it is compared as usual. Expressions become identifiers,
    statements and comments become block comments."""

    def mask(match: re.Match) -> str:
        tag = hashlib.sha1(re.sub(r"\s+", " ", match.group(1) or match.group(2) or match.group(0)).encode()).hexdigest()[:8]
        return f"jinja_{tag}" if match.group(1) is not None else f"/*jinja_{tag}*/"

    return _JINJA.sub(mask, sql)


def decide_layout_adapted(case: Case) -> Verdict | None:
    """The layout check on a templated fixture with its Jinja tags masked; ``None`` for a fixture without templating."""

    if "{{" not in case.fail and "{%" not in case.fail:
        return None
    label = layout_label(case)
    masked = Case(case.id, case.rule, case.dialect, mask_templates(case.fail), mask_templates(case.fix), case.configs)
    if "{{" in masked.fail or "{%" in masked.fail:
        return Verdict("unsupported", "unbalanced templating", label, adapted=True)
    verdict = decide_layout(masked)
    verdict.adapted = True
    return verdict


def renames_identifiers(case: Case) -> bool | None:
    """Whether the fix changes the spelling or case of some identifier; ``None`` when unreadable."""

    try:
        left, right = parse_all(case.fail, case.dialect), parse_all(case.fix, case.dialect)
    except Unsupported:
        return None
    a = [i.name for tree in left for i in tree.find_all(exp.Identifier)]
    b = [i.name for tree in right for i in tree.find_all(exp.Identifier)]
    return len(a) == len(b) and a != b


def layout_label(case: Case) -> str:
    """Why a layout rule's fix changes meaning by design: only CP02 does, and only when it actually renames an identifier."""

    reason = next((LAYOUT_BY_DESIGN[c] for c in case.codes if c in LAYOUT_BY_DESIGN), "")
    if reason and renames_identifiers(case) is False:
        return ""
    return reason


def decide_layout(case: Case) -> Verdict:
    """``proven`` = tree, comments and literals unchanged (a syntactic proof, no solver involved); ``refuted`` = something else changed."""

    label = layout_label(case)
    if "{{" in case.fail or "{%" in case.fail:
        return Verdict("unsupported", "templated (Jinja) SQL", label)
    try:
        changed = layout_differences(case)
    except Unsupported as error:
        return Verdict("unsupported", str(error), label)
    except Exception as error:  # noqa: BLE001 - a crash is not evidence
        return Verdict("error", f"{type(error).__name__}: {str(error)[:80]}", label)
    if changed:
        return Verdict("refuted", "changes " + ", ".join(changed), label)
    return Verdict("wrong" if label else "proven", "tree, comments and literals unchanged", label)


# --- KumoSQL's own rules against the fixtures ---------------------------------------
#
# ``format_sql`` is sqlfluff behind KumoSQL's wrapper (BigQuery dialect, a repeat-until-stable loop, quoted names
# restored, every result verified). Each layout fixture that KumoSQL's preferences can express is run through it
# with only that fixture's rule switched on: does it reproduce sqlfluff's fix, and does KumoSQL's verification
# accept the result? The structural rewrite rules (``lift_subqueries`` is sqlfluff's ST05 in reverse, and so on) are
# run over the semantic fixtures: how often does each fire, is the result verified, and does it agree with the fix?

CAP_RULES = ("capitalisation.keywords", "capitalisation.functions", "capitalisation.literals", "capitalisation.types")


def format_preferences(case: Case):
    """KumoSQL format preferences equal to the fixture's configuration, ``None`` when they cannot express it."""

    from kumosql.formatting import FormatPreferences

    configs = case.configs or {}
    extra = {k: v for k, v in configs.items() if k not in ("core", "indentation", "layout")}
    core = {k: v for k, v in (configs.get("core") or {}).items() if k != "dialect"}
    length, unit, tabs, comma = 80, "space", 4, "trailing"
    if set(core) - {"max_line_length"}:
        return None
    if "max_line_length" in core:
        length = core["max_line_length"]
        if not isinstance(length, int) or not 20 <= length <= 500:
            return None
    indentation = configs.get("indentation") or {}
    if set(indentation) - {"indent_unit", "tab_space_size"}:
        return None
    unit, tabs = indentation.get("indent_unit", unit), indentation.get("tab_space_size", tabs)
    if unit not in ("space", "tab") or not isinstance(tabs, int) or not 1 <= tabs <= 8:
        return None
    layout = configs.get("layout")
    if layout is not None:
        position = ((layout.get("type") or {}).get("comma") or {}).get("line_position")
        if layout != {"type": {"comma": {"line_position": position}}} or position not in ("leading", "trailing"):
            return None
        comma = position
    policy = "consistent"
    if extra:
        rules = extra.get("rules")
        if set(extra) != {"rules"} or not isinstance(rules, dict) or len(rules) != 1:
            return None
        (name, options), = rules.items()
        if name not in CAP_RULES or set(options) - {"capitalisation_policy", "extended_capitalisation_policy"} or len(options) != 1:
            return None
        policy = next(iter(options.values()))
        if policy not in ("upper", "lower", "consistent", "capitalise"):
            return None
    codes = tuple(c for c in case.codes)
    return FormatPreferences(rules=codes, keyword_case=policy, max_line_length=length, indent_unit=unit, tab_space_size=tabs, comma_position=comma)


@dataclass
class FormatVerdict:
    outcome: str  # reproduced | different | unsupported | error | wrong
    detail: str = ""
    verified: str = ""  # KumoSQL's verification status of the run


def decide_format(case: Case) -> FormatVerdict:
    from kumosql import formatting
    from kumosql.rewrite import apply_rule

    if case.dialect not in ("ansi", "bigquery"):
        return FormatVerdict("unsupported", f"KumoSQL formats BigQuery, the fixture is {case.dialect}")
    if "{{" in case.fail or "{%" in case.fail:
        return FormatVerdict("unsupported", "templated (Jinja) SQL")
    prefs = format_preferences(case)
    if prefs is None:
        return FormatVerdict("unsupported", "configuration KumoSQL's preferences cannot express")
    try:
        result = apply_rule("format_sql", case.fail, overrides={"format_sql": formatting.FormatSqlRule(prefs)})
    except Exception as error:  # noqa: BLE001
        return FormatVerdict("error", f"{type(error).__name__}: {str(error)[:80]}")
    if any(d.code in ("parse_error", "unsupported_sqlx") for d in result.diagnostics):
        return FormatVerdict("unsupported", "; ".join(d.message for d in result.diagnostics)[:80])
    status = result.verification.status.value
    same = result.sql.strip() == case.fix.strip()
    # an independent check of the output, whatever KumoSQL's verification said
    try:
        changed = layout_differences(Case(case.id, case.rule, "bigquery", case.fail, result.sql, {}))
    except Unsupported:
        changed = ["unreadable"]
    if status in ("proven", "unchanged") and changed and changed != ["unreadable"]:
        return FormatVerdict("wrong", f"verified {status} but the output changes {', '.join(changed)}", status)
    return FormatVerdict("reproduced" if same else "different", "" if same else "output differs from the fixture's fix", status)


def _decide_format_job(args):
    position, case = args
    return position, decide_format(case)


STRUCTURAL_RULES = ("lift_subqueries", "inline_single_use_ctes", "remove_trivial_predicates", "remove_redundant_parentheses",
                    "deduplicate_ctes", "remove_unused_ctes", "remove_redundant_distinct")


def decide_rules(case: Case) -> list[dict]:
    """One record per structural rule that changes the fixture's failing query."""

    from kumosql.algebraic_equivalence import prove_equivalent_algebraic
    from kumosql.rewrite import apply_rule

    if case.dialect not in ("ansi", "bigquery") or "{{" in case.fail:
        return []
    try:
        left = parse_all(case.fail, case.dialect)
    except Unsupported:
        return []
    if len(left) != 1 or not isinstance(left[0], exp.Query):
        return []
    records = []
    for name in STRUCTURAL_RULES:
        try:
            result = apply_rule(name, case.fail)
        except Exception as error:  # noqa: BLE001
            records.append({"rule": name, "outcome": "error", "detail": f"{type(error).__name__}: {str(error)[:60]}"})
            continue
        if result.sql.strip() == case.fail.strip():
            continue
        status = result.verification.status.value
        record = {"rule": name, "status": status, "outcome": "fired"}
        try:
            fixed = parse_all(case.fix, case.dialect)
            out = parse_all(result.sql, "bigquery")
            record["agrees"] = bool(len(fixed) == 1 and (_fold_function_names(fixed[0]) == _fold_function_names(out[0]) or prove_equivalent_algebraic(case.fix, result.sql, compare_names=True, dialect="bigquery", timeout_ms=3000).proven))
        except Unsupported:
            record["agrees"] = None
        # an independent look at the rewrite: a database that separates input and output is a false verification
        try:
            schema, kinds = infer_schema(left + parse_all(result.sql, "bigquery"))
        except Unsupported:
            records.append(record)
            continue
        witness = execute_difference(case.fail, result.sql, "ansi", schema, kinds, 120)
        if status in ("proven", "unchanged") and witness:
            record["outcome"] = "wrong"
            record["detail"] = "verified but a database separates input and output"
        records.append(record)
    return records


def _decide_rules_job(args):
    position, case = args
    return position, decide_rules(case)


def run_kumosql(cases: list[Case], workers: int | None = None) -> dict:
    out = {"format": {}, "rules": {}, "seconds": 0.0}
    start = time.time()
    layout = layout_cases(cases)
    semantic = semantic_cases(cases)
    with ProcessPoolExecutor(max_workers=workers or os.cpu_count()) as pool:
        for position, verdict in pool.map(_decide_format_job, list(enumerate(layout)), chunksize=8):
            out["format"][layout[position].id] = verdict
        for position, records in pool.map(_decide_rules_job, list(enumerate(semantic)), chunksize=4):
            if records:
                out["rules"][semantic[position].id] = records
    out["seconds"] = time.time() - start
    return out


def kumosql_summary(result: dict) -> str:
    fmt = Counter(v.outcome for v in result["format"].values())
    verified = Counter(v.verified for v in result["format"].values() if v.outcome in ("reproduced", "different"))
    lines = [
        f"format_sql over {len(result['format'])} layout fixtures ({result['seconds']:.0f} s): "
        + ", ".join(f"{k} {fmt[k]}" for k in ("reproduced", "different", "unsupported", "error", "wrong") if fmt[k]),
        "  verification of the runs: " + ", ".join(f"{k} {n}" for k, n in sorted(verified.items())),
    ]
    by_rule: dict[str, Counter] = {}
    for records in result["rules"].values():
        for r in records:
            counter = by_rule.setdefault(r["rule"], Counter())
            counter["fired"] += r["outcome"] in ("fired", "wrong")
            counter["verified"] += r.get("status") in ("proven", "unchanged")
            counter["agrees with the fix"] += bool(r.get("agrees"))
            counter["wrong"] += r["outcome"] == "wrong"
            counter["error"] += r["outcome"] == "error"
    for name in STRUCTURAL_RULES:
        if name in by_rule:
            lines.append(f"  {name}: " + ", ".join(f"{k} {n}" for k, n in by_rule[name].items() if n))
    return "\n".join(lines)


def semantic_cases(cases: list[Case]) -> list[Case]:
    return [c for c in cases if c.family in SEMANTIC_FAMILIES or c.family in ("TQ", "OR")]


def layout_cases(cases: list[Case]) -> list[Case]:
    return [c for c in cases if c.family in LAYOUT_FAMILIES]


def _decide_semantic_job(args):
    position, case, trials = args
    original = decide_semantic(case, trials)
    adapted = decide_adapted(case, trials) if original.outcome == "unsupported" else None
    return position, original, adapted


@dataclass
class Report:
    total: int = 0
    outcomes: Counter = field(default_factory=Counter)
    verdicts: dict = field(default_factory=dict)  # position -> Verdict
    adapted: dict = field(default_factory=dict)  # position -> Verdict, for cases that needed adapting
    seconds: float = 0.0

    def count(self, outcome: str, label: bool | None = None) -> int:
        """Cases with this outcome; ``label`` True keeps only by-design fixes, False only meaning-keeping ones."""

        return sum(1 for v in self.verdicts.values() if v.outcome == outcome and (label is None or bool(v.label) == label))


def run_semantic(cases: list[Case], trials: int = 120, workers: int | None = None) -> Report:
    report = Report(total=len(cases))
    start = time.time()
    jobs = [(i, c, trials) for i, c in enumerate(cases)]
    with ProcessPoolExecutor(max_workers=workers or os.cpu_count()) as pool:
        for position, verdict, adapted in pool.map(_decide_semantic_job, jobs, chunksize=4):
            report.verdicts[position] = verdict
            report.outcomes[verdict.outcome] += 1
            if adapted is not None:
                report.adapted[position] = adapted
                report.outcomes["adapted " + adapted.outcome] += 1
    report.seconds = time.time() - start
    return report


def _decide_layout_job(args):
    position, case = args
    return position, decide_layout(case), decide_layout_adapted(case)


def run_layout(cases: list[Case], workers: int | None = None) -> Report:
    report = Report(total=len(cases))
    start = time.time()
    with ProcessPoolExecutor(max_workers=workers or os.cpu_count()) as pool:
        for position, verdict, adapted in pool.map(_decide_layout_job, list(enumerate(cases)), chunksize=16):
            report.verdicts[position] = verdict
            report.outcomes[verdict.outcome] += 1
            if adapted is not None:
                report.adapted[position] = adapted
                report.outcomes["adapted " + adapted.outcome] += 1
    report.seconds = time.time() - start
    return report


def layout_summary(cases: list[Case], report: Report) -> str:
    keep = [v for v in report.verdicts.values() if not v.label]
    return "\n".join([
        f"{len(cases)} layout pairs ({report.seconds:.0f} s): {len(keep)} should change layout only, {len(cases) - len(keep)} rename identifiers",
        "layout only: " + ", ".join(f"{o} {report.count(o, False)}" for o in OUTCOMES if report.count(o, False)),
        "rename identifiers: " + ", ".join(f"{o} {report.count(o, True)}" for o in OUTCOMES if report.count(o, True)),
        f"adapted (Jinja tags masked): {len(report.adapted)} cases, " + ", ".join(f"{o} {report.outcomes['adapted ' + o]}" for o in OUTCOMES if report.outcomes["adapted " + o]),
    ])


def counts(report: Report) -> dict[str, Counter]:
    """Outcome counts for the original cases that keep the meaning (``keep``), those that change it by design
    (``design``), and the adapted cases (``adapted``)."""

    return {
        "keep": Counter(v.outcome for v in report.verdicts.values() if not v.label),
        "design": Counter(v.outcome for v in report.verdicts.values() if v.label),
        "adapted": Counter(v.outcome for v in report.adapted.values()),
    }


def semantic_summary(cases: list[Case], report: Report) -> str:
    keep = [i for i, v in report.verdicts.items() if not v.label]
    design = [i for i, v in report.verdicts.items() if v.label]
    supported = [i for i in keep if report.verdicts[i].outcome not in ("unsupported", "error")]
    lines = [
        f"{len(cases)} pairs: {len(keep)} keep the meaning, {len(design)} change it by design ({report.seconds:.0f} s)",
        f"meaning kept: proven {report.count('proven', False)}, refuted {report.count('refuted', False)}, unknown {report.count('unknown', False)}, "
        f"unsupported {report.count('unsupported', False)}, timeout {report.count('timeout', False)}, error {report.count('error', False)}, wrong {report.count('wrong', False)}"
        f"  (proven on the {len(supported)} supported: {report.count('proven', False)}/{len(supported)})",
        f"changes by design: refuted {report.count('refuted', True)}, unknown {report.count('unknown', True)}, unsupported {report.count('unsupported', True)}, "
        f"timeout {report.count('timeout', True)}, error {report.count('error', True)}, wrong (proven) {report.count('wrong', True)}",
        f"adapted (scripts and INSERT/CREATE ... SELECT, compared query by query): {len(report.adapted)} cases, "
        + ", ".join(f"{o} {report.outcomes['adapted ' + o]}" for o in OUTCOMES if report.outcomes["adapted " + o]),
        f"wrong in all: {report.outcomes['wrong'] + report.outcomes['adapted wrong']}",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("part", choices=("semantic", "layout", "kumosql", "extract"))
    parser.add_argument("checkout", nargs="?", type=Path, help="extract: a checkout of the pinned sqlfluff commit")
    parser.add_argument("--split", choices=("all", "dev", "held-out"), default="all", help="held-out rules are for final scoring only")
    parser.add_argument("--workers", type=int)
    parser.add_argument("--trials", type=int, default=120)
    parser.add_argument("--rule", help="only cases of this rule code")
    parser.add_argument("--json", type=Path, help="write per-case verdicts here")
    parser.add_argument("--show", choices=("unknown", "unsupported", "refuted", "wrong", "different", "reproduced", "all"), help="print the cases with this outcome")
    args = parser.parse_args(argv)
    if args.part == "extract":
        print(f"{extract(args.checkout)} cases written to {DATA}")
        return 0
    cases = split(load_cases(), args.split)
    if args.rule:
        cases = [c for c in cases if args.rule in c.codes]
    if args.part == "semantic":
        cases = semantic_cases(cases)
        report = run_semantic(cases, args.trials, args.workers)
        print(semantic_summary(cases, report))
        if args.show:
            for i, v in sorted(report.verdicts.items()):
                if args.show == "all" or v.outcome == args.show:
                    print(f"{v.outcome:11} {'[design] ' if v.label else ''}{cases[i].id}: {v.detail}")
        if args.json:
            args.json.write_text(json.dumps({cases[i].id: {"outcome": v.outcome, "detail": v.detail, "label": v.label, "adapted": (report.adapted[i].outcome if i in report.adapted else None)} for i, v in report.verdicts.items()}, indent=1))
        return 1 if report.outcomes["wrong"] or report.outcomes["adapted wrong"] else 0
    if args.part == "layout":
        cases = layout_cases(cases)
        report = run_layout(cases, args.workers)
        print(layout_summary(cases, report))
        if args.show:
            for i, v in sorted(report.verdicts.items()):
                if args.show == "all" or v.outcome == args.show:
                    print(f"{v.outcome:11} {'[design] ' if v.label else ''}{cases[i].id}: {v.detail}")
        return 1 if report.outcomes["wrong"] else 0
    if args.part == "kumosql":
        result = run_kumosql(cases, args.workers)
        print(kumosql_summary(result))
        if args.show:
            for case_id, v in sorted(result["format"].items()):
                if args.show == "all" or v.outcome == args.show:
                    print(f"{v.outcome:11} {case_id}: {v.detail}")
        wrong = sum(v.outcome == "wrong" for v in result["format"].values()) + sum(r["outcome"] == "wrong" for rs in result["rules"].values() for r in rs)
        return 1 if wrong else 0
    raise SystemExit(f"{args.part} is not implemented yet")


if __name__ == "__main__":
    sys.exit(main())
