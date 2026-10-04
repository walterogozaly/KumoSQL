"""The SMT compiler declines every sqlglot argument it does not model.

``CAST(x AS NUMBER DEFAULT 0 ON CONVERSION ERROR)`` returns 0 where ``CAST(x AS NUMBER)`` fails, yet the
``default`` argument never reached the model, so the two were proven equal. ``smt_args.check_args`` now
refuses a populated argument outside each node type's allowlist. Clauses that cannot change a result
(``FOR UPDATE``, ``CLUSTER BY``, ``SETTINGS``, a ``SELECT`` modifier) are declined along with the rest:
the guard does not argue about which ignored clauses are harmless.
"""

import pytest
import sqlglot

pytest.importorskip("z3")

from kumosql.algebraic_equivalence import prove_equivalent_algebraic
from kumosql.ast_utils import UnmodeledConstruct
from kumosql.smt_args import ALLOWED, check_args
from kumosql.smt_equivalence import prove_equivalent_smt

PROVERS = pytest.mark.parametrize("prove", [prove_equivalent_smt, prove_equivalent_algebraic], ids=["smt", "algebraic"])

UNMODELED = [
    pytest.param("oracle", "SELECT CAST(x AS NUMBER DEFAULT 0 ON CONVERSION ERROR) AS s FROM t", "SELECT CAST(x AS NUMBER) AS s FROM t", id="cast-default-on-error"),
    pytest.param("mysql", "SELECT x FROM t FOR UPDATE", "SELECT x FROM t", id="locking-read"),
    pytest.param("mysql", "SELECT SQL_CALC_FOUND_ROWS x FROM t", "SELECT x FROM t", id="select-modifier"),
    pytest.param("spark", "SELECT x FROM t CLUSTER BY x", "SELECT x FROM t", id="cluster-by"),
    pytest.param("clickhouse", "SELECT x FROM t SETTINGS max_threads = 1", "SELECT x FROM t", id="settings"),
]


@PROVERS
@pytest.mark.parametrize("dialect,left,right", UNMODELED)
def test_an_unmodeled_argument_is_never_proven_away(prove, dialect, left, right):
    assert prove(left, right, dialect=dialect).status.name != "PROVEN_EQUIVALENT"


@pytest.mark.parametrize("dialect,left,right", UNMODELED)
def test_check_args_names_the_argument(dialect, left, right):
    with pytest.raises(UnmodeledConstruct, match="not modeled"):
        check_args(sqlglot.parse_one(left, read=dialect))


STILL_PROVEN = [
    pytest.param("SELECT t.x FROM t JOIN u ON t.x = u.x WHERE t.y > 1", "SELECT t.x FROM t JOIN u ON u.x = t.x WHERE 1 < t.y", id="join-on"),
    pytest.param("SELECT x, COUNT(*) AS n FROM t GROUP BY x HAVING COUNT(*) > 1 ORDER BY x", "SELECT x, COUNT(*) AS n FROM t GROUP BY x HAVING 1 < COUNT(*) ORDER BY x", id="group-having-order"),
    pytest.param("SELECT DISTINCT x FROM t WHERE x = 1", "SELECT DISTINCT x FROM t WHERE 1 = x", id="distinct"),
    pytest.param("SELECT CAST(x AS INT64) AS s FROM t WHERE x = 1", "SELECT CAST(x AS INT64) AS s FROM t WHERE 1 = x", id="plain-cast"),
    pytest.param("SELECT d.x FROM (SELECT x FROM t WHERE x = 1) AS d", "SELECT d.x FROM (SELECT x FROM t WHERE 1 = x) AS d", id="derived-table"),
    pytest.param("WITH c AS (SELECT x FROM t) SELECT x FROM c UNION ALL SELECT x FROM c", "WITH c AS (SELECT x FROM t) SELECT x FROM c UNION ALL SELECT x FROM c", id="cte-union-all"),
]


@PROVERS
@pytest.mark.parametrize("left,right", STILL_PROVEN)
def test_modeled_arguments_stay_proven(prove, left, right):
    assert prove(left, right, dialect="bigquery").status.name == "PROVEN_EQUIVALENT"


def test_only_the_modeled_star_modifiers_are_allowed():
    # _star_columns models EXCEPT, REPLACE and RENAME; any other star argument (ILIKE) must be declined
    assert ALLOWED[sqlglot.exp.Star] == {"except", "except_", "replace", "rename"}
    for sql in ("SELECT * EXCEPT (y) FROM t", "SELECT t.* REPLACE (x + 1 AS x) FROM t"):
        check_args(sqlglot.parse_one(sql, read="bigquery"))
    with pytest.raises(UnmodeledConstruct):
        check_args(sqlglot.parse_one("SELECT * ILIKE 'x%' FROM t", read="snowflake"))
