"""The structural guard: a sqlglot argument the provers do not model is declined, never ignored.

sqlglot keeps a clause on a node whether or not KumoSQL reads it, so a query carrying an argument the
provers ignore was read as a query without it and proven against that reading. On master
(``f26d7f4a``) each pair below came back ``PROVEN_EQUIVALENT`` from the SMT prover, and all but the
two marked also from the algebraic prover, whose normalizer drops the same clause before the SMT
compiler sees it. ``ast_utils._MODELED_ARGS`` lists what is read; ``ast_utils.check_modeled`` refuses
everything else on those node types, so a clause a newer sqlglot adds is declined until it is
modeled. The near misses keep the same shape with no extra argument, and stay proven.
"""

import re
from collections import Counter

import pytest
import sqlglot
from sqlglot import exp

from kumosql.algebraic_equivalence import prove_equivalent_algebraic
from kumosql.ast_utils import UnmodeledConstruct, _MODELED_ARGS, check_modeled
from kumosql.duckdb_load import run_unoptimized
from kumosql.smt_equivalence import Unsupported, _check_star_args, prove_equivalent_smt

SCHEMA = {"t": ["x", "y"]}

# (id, dialect, left, right, why the two are not equivalent)
WRONG_PROOFS = [
    pytest.param(
        "bigquery",
        "SELECT x FROM t WHERE CAST(x AS INT64 DEFAULT 5 ON CONVERSION ERROR) > 1",
        "SELECT x FROM t WHERE CAST(x AS INT64) > 1",
        "BigQuery returns 5 for the text a plain cast rejects, so the fallback keeps the row",
        id="cast-default-on-conversion-error",
    ),
    pytest.param(
        "bigquery",
        'SELECT CAST(x AS STRING DEFAULT "z" ON CONVERSION ERROR) AS v FROM t',
        "SELECT CAST(x AS STRING) AS v FROM t",
        'the same fallback for a string cast',
        id="cast-default-string",
    ),
    pytest.param(
        "bigquery",
        "SELECT * EXCEPT (y) FROM t",
        "SELECT * FROM t",
        "the star drops a column, so the two have different column counts",
        id="star-except",
    ),
    pytest.param(
        "bigquery",
        "SELECT t.* EXCEPT (y) FROM t",
        "SELECT t.* FROM t",
        "the qualified star keeps its modifiers on the child star",
        id="qualified-star-except",
    ),
    pytest.param(
        "bigquery",
        "SELECT * REPLACE (1 AS x) FROM t",
        "SELECT * FROM t",
        "the replacement rewrites a value, not a column",
        id="star-replace",
    ),
    pytest.param(
        "bigquery",
        "SELECT * RENAME (x AS z) FROM t",
        "SELECT * FROM t",
        "the rename changes the output name",
        id="star-rename",
    ),
    pytest.param(
        "hive",
        "SELECT x FROM t SORT BY x",
        "SELECT x FROM t",
        "SORT BY is Hive's order-sensitivity marker, read here as no ORDER BY at all",
        id="select-sort-by",
    ),
    pytest.param(
        "hive",
        "SELECT x FROM t CLUSTER BY x",
        "SELECT x FROM t",
        "CLUSTER BY is Hive's distribution, read here as no clause at all",
        id="select-cluster-by",
    ),
    pytest.param(
        "hive",
        "SELECT x FROM t UNION ALL SELECT y FROM t SORT BY x",
        "SELECT x FROM t UNION ALL SELECT y FROM t",
        "the set operation's own SORT BY tail",
        id="set-operation-sort-by",
    ),
    pytest.param(
        "postgres",
        "SELECT x FROM t FOR UPDATE",
        "SELECT x FROM t",
        "a locking read is not the query the compiler compiled",
        id="select-for-update",
    ),
    pytest.param(
        "postgres",
        "WITH c AS (SELECT 1 AS a) SEARCH DEPTH FIRST BY a SET z SELECT a FROM c",
        "WITH c AS (SELECT 1 AS a) SELECT a FROM c",
        "SEARCH adds a search column to the CTE",
        id="with-search-clause",
    ),
    pytest.param(
        "bigquery",
        "SELECT * FROM UNNEST([STRUCT(1 AS x)])",
        "SELECT * FROM UNNEST([STRUCT(1 AS x, 2 AS y)])",
        "unnesting an array of structs explodes the struct fields; the compiler models one value column",
        id="unnest-array-of-structs",
    ),
]

# Pairs with no extra argument: the same reading, so they must stay proven.
STILL_PROVEN = [
    pytest.param("bigquery", "SELECT * FROM t", "SELECT t.* FROM t", id="star-and-qualified-star"),
    pytest.param("bigquery", "SELECT x FROM t", "SELECT t.x FROM t", id="qualified-column"),
    pytest.param("bigquery", "SELECT x FROM t WHERE x > 1", "SELECT x FROM t WHERE 1 < x", id="predicate-commuted"),
    pytest.param("bigquery", "SELECT CAST(x AS INT64) AS v FROM t", "SELECT CAST(t.x AS INT64) AS v FROM t", id="plain-cast"),
    pytest.param("bigquery", "SELECT CAST(x AS STRING) AS v FROM t", "SELECT CAST(x AS STRING) AS v FROM t WHERE TRUE", id="cast-with-a-true-filter"),
    pytest.param("bigquery", "SELECT x FROM t ORDER BY x", "SELECT x FROM t ORDER BY x ASC", id="order-by-default-asc"),
    pytest.param("bigquery", "SELECT x FROM t GROUP BY x", "SELECT x FROM t GROUP BY 1", id="group-by-ordinal"),
    pytest.param("bigquery", "SELECT x FROM t WHERE x IS NOT NULL", "SELECT x FROM t WHERE NOT x IS NULL", id="is-not-null"),
    pytest.param(
        "bigquery", "SELECT COUNT(*) AS n FROM t", "SELECT COUNT(*) AS n FROM t WHERE x IS NOT NULL OR x IS NULL",
        id="count-star-over-a-tautology",
    ),
    pytest.param("bigquery", "SELECT x FROM UNNEST([1, 2]) AS x", "SELECT x FROM UNNEST([1, 2]) AS x", id="unnest-array-literal"),
    pytest.param("bigquery", "SELECT COUNT(DISTINCT x) AS n FROM t", "SELECT COUNT(DISTINCT x) AS n FROM t", id="count-distinct"),
    pytest.param("postgres", "SELECT x FROM t", "SELECT x FROM t", id="no-argument-at-all"),
]


def _status(prove, left: str, right: str, dialect: str) -> str:
    return prove(left, right, schema=SCHEMA, dialect=dialect).status.value


@pytest.mark.parametrize("dialect,left,right,why", WRONG_PROOFS, ids=[str(p.id) for p in WRONG_PROOFS])
def test_an_unmodeled_argument_is_never_proven(dialect, left, right, why):
    """Neither prover may prove a pair whose difference is a clause it does not read.

    A refutation (``not_equivalent``) is as good a verdict here as a decline; what must never happen
    is ``proven_equivalent``.
    """

    assert _status(prove_equivalent_smt, left, right, dialect) != "proven_equivalent", why
    assert _status(prove_equivalent_algebraic, left, right, dialect) != "proven_equivalent", why


@pytest.mark.parametrize(
    "dialect,left,right",
    [(p.values[0], p.values[1], p.values[2]) for p in WRONG_PROOFS if p.values[0] != "bigquery" or "UNNEST" not in p.values[2]],
    ids=[str(p.id) for p in WRONG_PROOFS if p.values[0] != "bigquery" or "UNNEST" not in p.values[2]],
)
def test_the_smt_prover_declines_rather_than_guesses(dialect, left, right):
    assert _status(prove_equivalent_smt, left, right, dialect) == "not_proven"


@pytest.mark.parametrize("dialect,left,right", STILL_PROVEN, ids=[str(p.id) for p in STILL_PROVEN])
def test_a_pair_with_no_unmodeled_argument_stays_proven(dialect, left, right):
    assert _status(prove_equivalent_smt, left, right, dialect) == "proven_equivalent"
    assert _status(prove_equivalent_algebraic, left, right, dialect) == "proven_equivalent"


def test_the_star_modifiers_differ_on_every_database():
    """The star witnesses are real wrong answers, not only unread clauses."""

    duckdb = pytest.importorskip("duckdb")
    db = duckdb.connect()
    db.execute("CREATE TABLE t (x BIGINT, y BIGINT)")
    db.executemany("INSERT INTO t VALUES (?, ?)", [(1, 2), (3, 4)])
    keep, drop = run_unoptimized(db, "SELECT x FROM t", "SELECT x, y FROM t")
    assert Counter(keep) != Counter(drop)


def _modeled(sql: str, dialect: str = "bigquery"):
    return check_modeled(sqlglot.parse_one(sql, read=dialect))


@pytest.mark.parametrize(
    "sql,dialect",
    [
        ("SELECT x FROM t WHERE CAST(x AS INT64 DEFAULT 5 ON CONVERSION ERROR) > 1", "bigquery"),
        ("SELECT x FROM t SORT BY x", "hive"),
        ("SELECT x FROM t FOR UPDATE", "postgres"),
        ("WITH c AS (SELECT 1 AS a) SEARCH DEPTH FIRST BY a SET z SELECT a FROM c", "postgres"),
        ("SELECT * FROM UNNEST([STRUCT(1 AS x)])", "bigquery"),
    ],
    ids=["cast-default", "sort-by", "for-update", "with-search", "unnest-array-of-structs"],
)
def test_check_modeled_names_the_argument_it_refuses(sql, dialect):
    with pytest.raises(UnmodeledConstruct, match=r"is not modeled"):
        _modeled(sql, dialect)


@pytest.mark.parametrize(
    "sql,message",
    [
        ("SELECT * EXCEPT (y) FROM t", "SELECT * EXCEPT is not modeled"),
        ("SELECT * REPLACE (1 AS x) FROM t", "SELECT * REPLACE is not modeled"),
        ("SELECT * RENAME (x AS z) FROM t", "SELECT * RENAME is not modeled"),
        ("SELECT t.* EXCEPT (y) FROM t", "SELECT * EXCEPT is not modeled"),
    ],
    ids=["except", "replace", "rename", "qualified-except"],
)
def test_the_smt_compiler_refuses_an_unexpanded_star_modifier(sql, message):
    """The algebraic prover expands these into explicit columns; the SMT compiler has no such step."""

    tree = sqlglot.parse_one(sql, read="bigquery")
    with pytest.raises(Unsupported, match=re.escape(message)):
        _check_star_args(tree)
    # A query compared with itself is the cheapest way to see the refusal reach a result.
    assert prove_equivalent_smt(sql, sql, schema=SCHEMA).status.value == "not_proven"


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT x FROM t",
        "SELECT * FROM t",
        "SELECT CAST(x AS INT64) AS v FROM t",
        "SELECT COUNT(DISTINCT x) AS n FROM t",
        "SELECT x FROM t ORDER BY x NULLS LAST",
        "SELECT x, y FROM t GROUP BY ROLLUP (x)",
        "SELECT x FROM UNNEST([1, 2]) AS x",
        "WITH c AS (SELECT 1 AS a) SELECT a FROM c",
        "SELECT x FROM t WHERE EXISTS (SELECT 1 FROM t AS u WHERE u.x = t.x)",
        # BigQuery's star sugar is read: the algebraic prover expands it into explicit columns.
        "SELECT * EXCEPT (y) FROM t",
        "SELECT * REPLACE (1 AS x) FROM t",
    ],
    ids=[
        "plain-select", "star", "cast", "count-distinct", "nulls-last", "rollup", "unnest", "with", "exists",
        "star-except", "star-replace",
    ],
)
def test_check_modeled_accepts_what_the_provers_read(sql):
    assert _modeled(sql) is not None


def test_the_algebraic_prover_still_expands_bigquery_star_sugar():
    """The guard must not cost the rewrite the sugar rules already had."""

    result = prove_equivalent_algebraic(
        "SELECT * EXCEPT (y) REPLACE (x + 1 AS x) FROM t", "SELECT x + 1 AS x FROM t", schema=SCHEMA, compare_names=False
    )
    assert result.proven, result.reason


def test_an_argument_sqlglot_did_not_have_yet_is_declined_not_assumed():
    """The guard is an allowlist, so a new clause cannot be read as a query without it."""

    tree = sqlglot.parse_one("SELECT x FROM t", read="bigquery")
    tree.set("a_clause_added_by_a_newer_sqlglot", exp.Literal.number(1))
    with pytest.raises(UnmodeledConstruct, match="a_clause_added_by_a_newer_sqlglot"):
        check_modeled(tree)


def test_an_unset_or_empty_argument_is_not_an_argument():
    """sqlglot stores an absent flag as ``False`` and an empty list as ``[]``; neither is a clause."""

    tree = sqlglot.parse_one("SELECT x FROM t", read="bigquery")
    select = tree
    select.set("locks", None)
    select.set("hint", False)
    select.set("pivots", [])
    check_modeled(tree)  # no raise


def test_every_guarded_node_type_lists_only_real_sqlglot_arguments():
    """A typo in an allowlist entry would silently stop guarding that node.

    ``except`` and ``replace`` are the pre-30 spellings of the star modifiers, kept so the guard also
    works on the older sqlglot versions the matrix tests run.
    """

    legacy = {"except", "replace"}
    for node_type, allowed in _MODELED_ARGS.items():
        assert issubclass(node_type, exp.Expression), node_type
        unknown = allowed - set(node_type.arg_types) - (legacy if node_type is exp.Star else set())
        assert not unknown, f"{node_type.__name__} has no argument {sorted(unknown)}"


def test_sqlglot_parsers_keep_their_own_markers():
    """``[1, 2]`` and ``UNNEST`` carry flags the BigQuery parser sets; they are not clauses."""

    array = sqlglot.parse_one("SELECT [1, 2] AS a", read="bigquery").find(exp.Array)
    assert array is not None
    check_modeled(sqlglot.parse_one("SELECT [1, 2] AS a", read="bigquery"))
    unnest = sqlglot.parse_one("SELECT x FROM UNNEST([1, 2]) AS x", read="bigquery").find(exp.Unnest)
    assert unnest is not None
