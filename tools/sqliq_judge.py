"""SQL-IQ's SQL Judge task answered with deterministic rules and no language model.

The task shows a schema, a question with optional evidence and two candidate SQLite
queries, and asks which one answers the question. The rules below read only
those inputs (never the candidates' results or the benchmark's labels) and score
each candidate; the higher score wins and a tie answers "A".

A candidate is penalized for:

* naming a column that is in no table of the schema;
* comparing against a literal that appears nowhere in the question, the evidence
  or the schema's example values;
* leaving out a condition the evidence spells out (``X = 1``, a backticked column);
* selecting more columns than the question asks for;
* ``LIMIT`` or ``ORDER BY`` where the question has no ranking word, and the
  reverse: a ranking word with neither ``ORDER BY`` nor ``MAX``/``MIN``;
* conditions beyond the ones the question and evidence support.

It is rewarded for using the evidence's columns and for covering the question's
words with the columns it reads.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import re

import sqlglot
from sqlglot import exp

RANKING = re.compile(
    r"\b(highest|lowest|largest|smallest|most|least|top|bottom|maximum|minimum|max|min|best|worst|"
    r"oldest|youngest|latest|earliest|first|last|longest|shortest|biggest|greatest|fewest)\b",
    re.I,
)
STOP = set(
    "the of in a an and or to for is are was were what which who whom how many much list give "
    "show please name names number total all each every by with from that this those these at on as "
    "their its it be has have had do does did than more less not no among between per whose where when".split()
)


@dataclass
class Schema:
    tables: dict[str, set[str]] = field(default_factory=dict)
    examples: set[str] = field(default_factory=set)

    @property
    def columns(self) -> set[str]:
        return set().union(*self.tables.values()) if self.tables else set()


def parse_schema(text: str) -> Schema:
    schema = Schema()
    table = None
    for line in text.splitlines():
        line = line.strip()
        header = re.match(r"# Table: (?:\w+\.)?(.+)$", line)
        if header:
            table = header.group(1).strip().lower()
            schema.tables[table] = set()
            continue
        column = re.match(r"\((.+?):[A-Za-z]+[(\d,\s)]*,", line)
        if table and column:
            schema.tables[table].add(column.group(1).strip().lower())
            example = re.search(r"Examples: \[(.*)\]\)?,?$", line)
            if example:
                for value in re.split(r",\s*", example.group(1)):
                    value = value.strip().strip("'\"").lower()
                    if value:
                        schema.examples.add(value)
    return schema


def words(text: str) -> set[str]:
    found = {w for w in re.findall(r"[a-z0-9]+", text.lower()) if w not in STOP}
    return found | {w[:-1] for w in found if w.endswith("s") and len(w) > 3}


def _parse(sql: str):
    try:
        return sqlglot.parse_one(sql, read="sqlite")
    except sqlglot.errors.SqlglotError:
        return None


def evidence_terms(evidence: str) -> tuple[set[str], set[str]]:
    """(backticked or dotted column names, quoted or numeric values) the evidence mentions."""

    columns = {c.lower() for c in re.findall(r"`([^`]+)`", evidence)}
    values = {v.lower() for v in re.findall(r"'([^']*)'", evidence)}
    values |= set(re.findall(r"(?<![\w.])-?\d+(?:\.\d+)?(?![\w])", evidence))
    return columns, values


def score(sql: str, question: str, evidence: str, schema: Schema) -> float:
    tree = _parse(sql)
    if tree is None:
        return -100.0
    support = words(question) | words(evidence)
    support_text = f"{question} {evidence}".lower()
    evidence_columns, evidence_values = evidence_terms(evidence)
    known = schema.columns
    total = 0.0

    # columns that exist, and tables that exist
    used_columns = {c.name.lower() for c in tree.find_all(exp.Column)}
    aliases = {a.alias.lower() for a in tree.find_all(exp.Alias) if a.alias}
    for name in used_columns - known - aliases:
        total -= 4
    for table in tree.find_all(exp.Table):
        if schema.tables and table.name.lower() not in schema.tables:
            cte = {c.alias.lower() for c in tree.find_all(exp.CTE)}
            if table.name.lower() not in cte:
                total -= 4

    # literals grounded in the question, evidence or example values
    unsupported = 0
    for literal in tree.find_all(exp.Literal):
        value = literal.name.lower()
        if literal.is_string:
            stripped = value.strip("%")
            if stripped in support_text or stripped in schema.examples or value in evidence_values:
                continue
            unsupported += 1
        else:
            if value in {"0", "1", "-1", "2"} or value in evidence_values or value in support_text:
                continue
            if literal.parent is not None and isinstance(literal.parent, exp.Limit):
                continue
            unsupported += 1
    total -= 2 * unsupported

    # evidence coverage
    sql_lower = sql.lower()
    for column in evidence_columns:
        total += 1.5 if column in sql_lower else -1.5
    for value in evidence_values:
        if value and value in sql_lower:
            total += 0.5

    # shape: columns selected, ranking
    select = tree.find(exp.Select)
    if select is not None:
        total -= 1.0 * max(0, len(select.expressions) - 1)
    wants_rank = bool(RANKING.search(question))
    has_rank = tree.args.get("limit") is not None or bool(tree.find(exp.Order)) or bool(tree.find(exp.Max) or tree.find(exp.Min))
    if wants_rank and not has_rank:
        total -= 1.5
    if not wants_rank and tree.args.get("limit") is not None:
        total -= 1.5

    # simplicity: every predicate and join beyond the minimum is something the question must justify
    total -= 0.5 * len(list(tree.find_all(exp.Predicate)))
    total -= 0.5 * len(list(tree.find_all(exp.Join)))
    total -= 0.002 * len(sql)
    return total


def judge(question: str, evidence: str, schema_text: str, candidate_a: str, candidate_b: str) -> str:
    schema = parse_schema(schema_text)
    a = score(candidate_a, question, evidence, schema)
    b = score(candidate_b, question, evidence, schema)
    return "B" if b > a else "A"
