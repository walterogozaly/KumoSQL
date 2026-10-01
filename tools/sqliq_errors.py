"""SQL-IQ's SQL Error Classification task answered with deterministic rules and no language model.

Given a question, evidence, schema and a candidate SQLite query, the task asks
whether the query is correct and, if not, which of nine error families it has.
Each rule reads only those inputs and raises a flag for one family:

* Attribute-Related: a column in no table of the schema, an evidence column the query never reads,
  more columns selected than the question asks for;
* Table-Related: a table not in the schema, a join with no ``ON`` condition;
* Value-Related: a literal found in no part of the question, evidence or example values;
* Condition-Related: more conditions than the question and evidence mention;
* Clause-Related: a ranking word in the question but no ``ORDER BY``/``MAX``/``MIN``, or a ``LIMIT``
  without one;
* Function-Related: an aggregate in the query where the question asks for none.

The query is called wrong only when at least ``THRESHOLD`` flags are raised, so the answer is
"No error" unless the evidence of a problem is real.
"""

from __future__ import annotations

import re

import sqlglot
from sqlglot import exp

from sqliq_judge import RANKING, evidence_terms, parse_schema, words, _parse

THRESHOLD = 1
ASKS_AGGREGATE = re.compile(r"\b(how many|number of|count|total|sum|average|avg|mean|percentage|ratio|maximum|minimum|highest|lowest)\b", re.I)


def flags(question: str, evidence: str, schema_text: str, sql: str) -> list[str]:
    tree = _parse(sql)
    if tree is None:
        return ["Other Errors"]
    schema = parse_schema(schema_text)
    support_text = f"{question} {evidence}".lower()
    support = words(question) | words(evidence)
    evidence_columns, evidence_values = evidence_terms(evidence)
    raised: list[str] = []

    used = {c.name.lower() for c in tree.find_all(exp.Column)}
    aliases = {a.alias.lower() for a in tree.find_all(exp.Alias) if a.alias}
    if used - schema.columns - aliases:
        raised.append("Attribute-Related Errors")
    ctes = {c.alias.lower() for c in tree.find_all(exp.CTE)}
    if any(t.name.lower() not in schema.tables and t.name.lower() not in ctes for t in tree.find_all(exp.Table)):
        raised.append("Table-Related Errors")
    if any(j.args.get("on") is None and not j.args.get("using") and not j.args.get("kind") for j in tree.find_all(exp.Join)):
        raised.append("Table-Related Errors")
    sql_lower = sql.lower()
    if any(c not in sql_lower for c in evidence_columns):
        raised.append("Attribute-Related Errors")
    select = tree.find(exp.Select)
    if select is not None and len(select.expressions) > 2:
        raised.append("Attribute-Related Errors")

    for literal in tree.find_all(exp.Literal):
        value = literal.name.lower()
        if literal.is_string:
            if value.strip("%") not in support_text and value.strip("%") not in schema.examples:
                raised.append("Value-Related Errors")
                break
        elif value not in {"0", "1", "-1", "2"} and value not in support_text and not isinstance(literal.parent, exp.Limit):
            raised.append("Value-Related Errors")
            break

    if len(list(tree.find_all(exp.Predicate))) > 3:
        raised.append("Condition-Related Errors")
    ranked = tree.args.get("limit") is not None or bool(tree.find(exp.Order)) or bool(tree.find(exp.Max) or tree.find(exp.Min))
    if RANKING.search(question) and not ranked:
        raised.append("Clause-Related Errors")
    if tree.args.get("limit") is not None and not RANKING.search(question):
        raised.append("Clause-Related Errors")
    if tree.find(exp.AggFunc) and not ASKS_AGGREGATE.search(question):
        raised.append("Function-Related Errors")
    del support
    return raised


def classify(question: str, evidence: str, schema_text: str, sql: str) -> list[str]:
    """[] for "No error", otherwise the error families found."""

    raised = flags(question, evidence, schema_text, sql)
    if len(raised) < THRESHOLD:
        return []
    return sorted(set(raised))
