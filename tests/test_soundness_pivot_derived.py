"""PIVOT and UNPIVOT on a derived table (or table).

``_peel_star_wrappers``, ``_expand_stars``, ``_fold_filter_into_grouping`` and ``trim_redundant_row_clauses`` read the
derived table under ``SELECT * FROM (..) PIVOT (..)`` as if the pivot were not there, so the pivoted query normalized
to its source (``SELECT y, x FROM t``) and was proven equal to it, to a pivot over other values, and to a LIMIT-cut
UNPIVOT that has twice the rows. ``check_modeled`` now declines a query carrying a pivot, as it does the other
table modifiers neither prover reads.
"""

import pytest

pytest.importorskip("z3")

from kumosql.algebraic_equivalence import normalize, prove_equivalent_algebraic
from kumosql.ast_utils import UnmodeledConstruct

SCHEMA = {"t": ["id", "x", "y"]}
PIVOT = "SELECT * FROM (SELECT y, x FROM t) PIVOT (SUM(x) FOR y IN (1, 2))"
UNPIVOT = "SELECT * FROM (SELECT COUNT(*) AS c, MAX(x) AS m FROM t) AS d UNPIVOT (v FOR k IN (c, m))"


def prove(left, right, schema=SCHEMA):
    return prove_equivalent_algebraic(left, right, schema=schema, dialect="bigquery")


PAIRS = [
    pytest.param(PIVOT, "SELECT y, x FROM t", id="pivot-is-not-its-source"),
    pytest.param(PIVOT, "SELECT * FROM (SELECT y, x FROM t) PIVOT (SUM(x) FOR y IN (3, 4))", id="pivot-over-other-values"),
    pytest.param(UNPIVOT, UNPIVOT + " LIMIT 1", id="unpivot-with-a-limit-it-does-not-need"),
    pytest.param(UNPIVOT, "SELECT COUNT(*) AS c, MAX(x) AS m FROM t", id="unpivot-is-not-its-source"),
    pytest.param("SELECT * FROM t PIVOT (SUM(x) FOR y IN (1, 2))", "SELECT * FROM t", id="table-pivot"),
    pytest.param("SELECT id FROM t UNPIVOT (v FOR k IN (x, y))", "SELECT id FROM t", id="table-unpivot"),
]


@pytest.mark.parametrize("left, right", PAIRS)
def test_a_pivot_is_not_proven_equal_to_anything(left, right):
    assert not prove(left, right).proven


@pytest.mark.parametrize("sql", [PIVOT, UNPIVOT, "SELECT * FROM t PIVOT (SUM(x) FOR y IN (1, 2))"])
def test_normalize_declines_a_pivot(sql):
    with pytest.raises(UnmodeledConstruct):
        normalize(sql, schema=SCHEMA)


def test_a_case_pivot_still_proves():
    left = "SELECT id, SUM(CASE WHEN y = 1 THEN x END) AS a FROM t GROUP BY id"
    right = "SELECT id, SUM(IF(y = 1, x, NULL)) AS a FROM t GROUP BY id"
    assert prove(left, right).proven
