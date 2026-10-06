"""A select-list ``IN`` becomes an EXISTS only where its NOT NULL column is never NULL: not in a grand-total row."""

import pytest

pytest.importorskip("z3")
duckdb = pytest.importorskip("duckdb")

from kumosql.algebraic_equivalence import prove_equivalent_algebraic
from kumosql.smt_equivalence import TableConstraints

SCHEMA = {"t": ["id", "x"], "u": ["k", "w"]}
CONSTRAINTS = {
    "t": TableConstraints(not_null=frozenset({"id", "x"}), keys=(("id",),)),
    "u": TableConstraints(not_null=frozenset({"k", "w"}), keys=(("k",),)),
}
IN = "SELECT t.x IN (SELECT u.w FROM u) AS m FROM t GROUP BY {group}"
EXISTS = "SELECT EXISTS (SELECT 1 FROM u WHERE u.w = t.x) AS m FROM t GROUP BY {group}"


def _rows(sql: str):
    con = duckdb.connect()
    con.execute("PRAGMA disable_optimizer")
    con.execute("CREATE TABLE t (id INT NOT NULL, x INT NOT NULL); CREATE TABLE u (k INT NOT NULL, w INT NOT NULL)")
    con.execute("INSERT INTO u VALUES (1, 3)")
    return con.execute(sql).fetchall()


def _proves(group: str) -> bool:
    result = prove_equivalent_algebraic(IN.format(group=group), EXISTS.format(group=group), schema=SCHEMA, constraints=CONSTRAINTS, dialect="bigquery")
    return result.proven


def test_a_grand_total_row_keeps_the_unknown_membership():
    # t is empty, so the only row is the grand total, where t.x is NULL: NULL IN (3) is NULL, not FALSE.
    group = "GROUPING SETS (t.x, ())"
    assert _rows(IN.format(group=group)) == [(None,)]
    assert _rows(EXISTS.format(group=group)) == [(False,)]
    assert not _proves(group)


def test_a_plain_grouping_still_reads_the_membership_as_exists():
    assert _proves("t.x")
