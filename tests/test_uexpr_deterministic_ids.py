"""A proof must not depend on how the bag-equivalence backend numbered its variables.

Sorting terms by their text put ``s10`` before ``s9``, so whether a proof was found could depend on how many
variables the process had numbered before (the counter was global). The counter is now scoped to each proof
and ordering keys read numbers by value. The pairs here are synthetic.
"""

from __future__ import annotations

import pytest

from kumosql.uexpr import prove_bag_equivalent
from kumosql.uexpr.ir import fresh_id, id_scope, rkey

SCHEMA = {"t": ["a", "b", "c"], "u": ["a", "d"], "v": ["a", "e"]}

PAIRS = [
    ("SELECT t.a, u.d FROM t JOIN u ON t.a = u.a JOIN v ON v.a = u.a", "SELECT t.a, u.d FROM v JOIN u ON v.a = u.a JOIN t ON u.a = t.a", True),
    ("SELECT a, SUM(b), COUNT(*) FROM t GROUP BY a", "SELECT x.a, SUM(x.b), COUNT(*) FROM t AS x GROUP BY x.a", True),
    ("SELECT a FROM t WHERE EXISTS (SELECT 1 FROM u WHERE u.a = t.a)", "SELECT a FROM t WHERE a IN (SELECT a FROM u)", True),
    ("SELECT a FROM t UNION ALL SELECT a FROM u", "SELECT a FROM u UNION ALL SELECT a FROM t", True),
    ("SELECT a, b FROM t WHERE b > 1 AND c < 3", "SELECT a, b FROM t WHERE c < 3 AND b > 1", True),
    ("SELECT a, COUNT(*) FROM t GROUP BY a", "SELECT a, COUNT(b) FROM t GROUP BY a", False),
    ("SELECT t.a FROM t JOIN u ON t.a = u.a", "SELECT t.a FROM t LEFT JOIN u ON t.a = u.a", False),
]


def prove(left: str, right: str) -> bool:
    return prove_bag_equivalent(left, right, schema=SCHEMA, dialect="mysql", exact_arithmetic=True, compare_names=False).proven


def test_pairs_are_decided_as_expected():
    for left, right, expected in PAIRS:
        assert prove(left, right) is expected, (left, right)


@pytest.mark.parametrize("start", [9, 99, 100, 5000, 123456, 10**9])
def test_answers_do_not_depend_on_where_the_numbering_starts(start):
    with id_scope(start):
        assert [prove(left, right) for left, right, _ in PAIRS] == [expected for *_, expected in PAIRS]


def test_answers_do_not_depend_on_what_was_translated_before():
    for _ in range(40):  # advance the default counter by a few thousand
        prove("SELECT a, b FROM t WHERE b > 1", "SELECT a, b FROM t WHERE b > 1")
    assert [prove(left, right) for left, right, _ in reversed(PAIRS)] == [expected for *_, expected in reversed(PAIRS)]


def test_each_proof_numbers_its_variables_from_one(monkeypatch):
    seen = []
    import kumosql.uexpr.translate as translate

    real = translate.fresh_id

    def spy():
        value = real()
        seen.append(value)
        return value

    monkeypatch.setattr(translate, "fresh_id", spy)
    prove("SELECT a FROM t", "SELECT a FROM t")
    first = list(seen)
    seen.clear()
    prove("SELECT a FROM t", "SELECT a FROM t")
    assert first and first == seen and first[0] == 1


def test_a_scope_numbers_from_its_start_and_restores_the_default():
    outside = fresh_id()
    with id_scope(500):
        assert [fresh_id(), fresh_id()] == [500, 501]
    assert fresh_id() == outside + 1


def test_the_sort_key_reads_numbers_by_value():
    assert rkey("s9") < rkey("s10") < rkey("s100")
    assert rkey("t#-2") < rkey("t#-1") < rkey("t#3")
    assert rkey("s9 + s10") == rkey("s9 + s10")
    # the order of ids is kept when every id moves by the same amount
    ids = ["s3", "s12", "s7", "s100", "s21"]
    assert sorted(ids, key=rkey) == sorted(ids, key=lambda s: int(s[1:]))
    assert sorted((f"s{int(i[1:]) + 1000}" for i in ids), key=rkey) == [f"s{int(i[1:]) + 1000}" for i in sorted(ids, key=lambda s: int(s[1:]))]
