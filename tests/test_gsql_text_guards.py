"""Text-level refusals for names and arities the sqlglot tree cannot be trusted on (gsql_eval/text_guards.py)."""

import pytest

from kumosql.gsql_eval import Unsupported, evaluate
from kumosql.gsql_eval.text_guards import calls


def test_calls_skip_strings_comments_and_quoted_names():
    sql = "SELECT 'len(x)', `len`(1) -- hex(1)\n, \"split(a,b,c,d)\", f(g(1, 2), 3)"
    assert [(n, a) for n, a, _ in calls(sql)] == [("g", 2), ("f", 2)]


def test_dotted_calls_are_marked():
    assert calls("SELECT NET.HOST('x')") == [("host", 1, True)]


@pytest.mark.parametrize("sql", ["SELECT LEN('ab')", "SELECT HEX('ab')", "SELECT CHARINDEX('a', 'ab')", "SELECT LENGTH('ab', 'cd')",
                                 "SELECT SPLIT('a,b', ',', 1, 2)"])
def test_unreadable_calls_are_refused(sql):
    with pytest.raises(Unsupported):
        evaluate(sql)


def test_ordinary_calls_still_run():
    assert evaluate("SELECT LENGTH('ab'), '(' || ')'").rows == [(2, "()")]
