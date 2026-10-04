"""Execution-only guards against arbitrary aggregate picks in candidate witnesses."""

import sqlglot
from sqlglot import exp


_PICKS = (exp.AnyValue, exp.First, exp.Last)
_GUARD = """CASE WHEN COUNT(DISTINCT __pick_argument__) <= 1
    AND (COUNT(__pick_argument__) = 0 OR COUNT(__pick_argument__) = COUNT(*))
    THEN ANY_VALUE(__pick_argument__) ELSE ERROR('arbitrary pick') END"""


def guard_arbitrary_picks(sql: str) -> str | None:
    """A stability-only copy, or None for an unsupported pick (including windows).

    A value is forced only for an empty/all-NULL group or one uniform non-NULL
    value. Mixed NULL/non-NULL groups are conservatively refused as well.
    Original queries and the reported witness results are never rewritten.
    """

    try:
        tree = sqlglot.parse_one(sql, read="duckdb")
        picks = [node for node in tree.walk() if isinstance(node, _PICKS)]
        if not picks:
            return sql
        for node in reversed(picks):
            if node.find_ancestor(exp.Window, exp.Filter):
                return None
            if any(value for key, value in node.args.items() if key != "this"):
                return None
            argument = node.this
            if not isinstance(argument, exp.Expression) or isinstance(argument, (exp.Order, exp.Distinct)):
                return None
            guarded = sqlglot.parse_one(_GUARD, read="duckdb")
            for placeholder in list(guarded.find_all(exp.Column)):
                if placeholder.name == "__pick_argument__":
                    placeholder.replace(argument.copy())
            target = node.parent if isinstance(node.parent, (exp.IgnoreNulls, exp.RespectNulls)) else node
            target.replace(guarded)
        return tree.sql(dialect="duckdb")
    except sqlglot.errors.SqlglotError:
        return None
