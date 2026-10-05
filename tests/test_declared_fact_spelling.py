"""Declared facts name one spelling of a table: ``other_ds.t`` does not inherit what is declared for ``t``.

The rules that use NOT NULL columns, keys and foreign keys (a self-witnessed EXISTS, a self domain join, an EXISTS a
foreign key witnesses, a grouping or DISTINCT over a key, a foreign-key join) once looked those facts up by the
bare table name, so a column declared NOT NULL (or a key declared) for ``t`` was trusted on ``other_ds.t``. Each pair below was accepted as equivalent on that mistake while DuckDB,
with the optimizer on and off, returns different rows on data where only the bare table keeps its promises.
The near misses show the same pair still proves when the facts are declared for the spelling the query uses.
"""

from collections import Counter

import pytest

pytest.importorskip("z3")
duckdb = pytest.importorskip("duckdb")

from kumosql.algebraic_equivalence import prove_equivalent_algebraic
from kumosql.duckdb_load import run_unoptimized
from kumosql.smt_equivalence import TableConstraints

COLUMNS = {"t": ["id", "x"], "p": ["id", "tid"]}
TYPES = {t: {c: "INT64" for c in cols} for t, cols in COLUMNS.items()}
DDL = [
    "CREATE SCHEMA other_ds",
    "CREATE TABLE t(id BIGINT, x BIGINT)",
    "CREATE TABLE p(id BIGINT, tid BIGINT)",
    "CREATE SCHEMA empty_ds",
    "CREATE SCHEMA dup_ds",
    "CREATE TABLE dup_ds.t(id BIGINT, x BIGINT)",
    "CREATE TABLE dup_ds.p(id BIGINT, tid BIGINT)",
    "INSERT INTO dup_ds.t VALUES (1, 1), (1, 2), (2, 5), (3, NULL)",
    "INSERT INTO dup_ds.p VALUES (1, 1), (2, 1), (3, 9), (4, NULL)",
    "CREATE TABLE other_ds.t(id BIGINT, x BIGINT)",
    "CREATE TABLE other_ds.p(id BIGINT, tid BIGINT)",
    "INSERT INTO t VALUES (1, 1), (2, 2)",
    "INSERT INTO p VALUES (1, 1), (2, 2)",
    "INSERT INTO other_ds.t VALUES (1, NULL), (2, 5), (3, 5)",
    "INSERT INTO other_ds.p VALUES (1, 9), (2, 9)",
    "CREATE TABLE empty_ds.t(id BIGINT, x BIGINT)",
    "CREATE TABLE empty_ds.p(id BIGINT, tid BIGINT)",
    "INSERT INTO empty_ds.p VALUES (1, 9), (2, 9)",
]


def declared(prefix=""):
    """x is NOT NULL on t, p.tid is NOT NULL and references t.id; ``prefix`` names the schema they hold in."""

    return {
        f"{prefix}t": TableConstraints(not_null=frozenset({"id", "x"}), keys=(("id",),)),
        f"{prefix}p": TableConstraints(not_null=frozenset({"id", "tid"}), keys=(("id",),), foreign_keys=((("tid",), f"{prefix}t", ("id",)),)),
    }


def prove(left, right, prefix=""):
    return prove_equivalent_algebraic(
        left,
        right,
        schema={f"{prefix}{t}": cols for t, cols in COLUMNS.items()},
        types={f"{prefix}{t}": types for t, types in TYPES.items()},
        constraints=declared(prefix),
        compare_names=False,
        dialect="bigquery",
    ).proven


def rows(sql):
    """The query's rows with the optimizer off and on (they must agree for the data to count)."""

    db = duckdb.connect()
    for statement in DDL:
        db.execute(statement)
    off = Counter(run_unoptimized(db, sql)[0])
    return off, Counter(db.execute(sql).fetchall())


PAIRS = {
    "self-witnessed EXISTS": (
        "SELECT t.id FROM {t} AS t WHERE EXISTS (SELECT 1 FROM {t} AS t2 WHERE t2.x = t.x)",
        "SELECT t.id FROM {t} AS t",
    ),
    "self domain join": (
        "SELECT t.id, g.k FROM {t} AS t JOIN (SELECT t3.x AS k FROM {t} AS t3 GROUP BY t3.x) AS g ON g.k = t.x",
        "SELECT t.id, t.x AS k FROM {t} AS t",
    ),
    "foreign-key witnessed EXISTS": (
        "SELECT p.id FROM {p} AS p WHERE EXISTS (SELECT 1 FROM {t} AS t)",
        "SELECT p.id FROM {p} AS p",
    ),
    "GROUP BY a declared key": (
        "SELECT t.id, MAX(t.x) AS m FROM {t} AS t GROUP BY t.id",
        "SELECT t.id, t.x AS m FROM {t} AS t",
    ),
    "DISTINCT over a declared key": (
        "SELECT DISTINCT t.id FROM {t} AS t",
        "SELECT t.id FROM {t} AS t",
    ),
    "foreign-key join": (
        "SELECT p.id FROM {p} AS p JOIN {t} AS t ON t.id = p.tid",
        "SELECT p.id FROM {p} AS p",
    ),
    "COUNT(DISTINCT key)": (
        "SELECT COUNT(DISTINCT t.id) AS c FROM {t} AS t",
        "SELECT COUNT(t.id) AS c FROM {t} AS t",
    ),
    "DISTINCT key join to EXISTS": (
        "SELECT DISTINCT t.id FROM {t} AS t JOIN {p} AS p ON p.tid = t.id",
        "SELECT t.id FROM {t} AS t WHERE EXISTS (SELECT 1 FROM {p} AS p WHERE p.tid = t.id)",
    ),
    "SUM grouped by a declared key": (
        "SELECT t.id, SUM(t.x) AS s FROM {t} AS t GROUP BY t.id",
        "SELECT t.id, t.x AS s FROM {t} AS t",
    ),
}


# the foreign-key pair needs a parent table that is empty while its child is not
DATASET = {
    "foreign-key witnessed EXISTS": "empty_ds",
    "GROUP BY a declared key": "dup_ds",
    "DISTINCT over a declared key": "dup_ds",
    "foreign-key join": "dup_ds",
    "SUM grouped by a declared key": "dup_ds",
    "COUNT(DISTINCT key)": "dup_ds",
    "DISTINCT key join to EXISTS": "dup_ds",
}


def spelled(name, **tables):
    return [sql.format(**tables) for sql in PAIRS[name]]


@pytest.mark.parametrize("name", PAIRS)
def test_facts_declared_for_a_bare_table_do_not_apply_to_a_qualified_one(name):
    ds = DATASET.get(name, "other_ds")
    left, right = spelled(name, t=f"{ds}.t", p=f"{ds}.p")
    assert not prove(left, right)
    # the qualified tables break the bare table's promises, so the pair really differs, optimizer on and off
    off_left, on_left = rows(left)
    off_right, on_right = rows(right)
    assert off_left != off_right and on_left != on_right


@pytest.mark.parametrize("name", PAIRS)
def test_the_same_pair_proves_on_the_spelling_the_facts_name(name):
    ds = DATASET.get(name, "other_ds")
    assert prove(*spelled(name, t="t", p="p"))
    assert prove(*spelled(name, t=f"{ds}.t", p=f"{ds}.p"), prefix=f"{ds}.")
