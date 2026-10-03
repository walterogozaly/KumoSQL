"""Row loading for DuckDB test databases (``kumosql.duckdb_load``): the loader that skips repeated statements must
leave every table exactly as a plain ``DELETE`` and ``INSERT`` would."""

import random

import pytest

duckdb = pytest.importorskip("duckdb")

from kumosql.duckdb_load import TableLoader, insert_rows, rows_key  # noqa: E402

TABLES = {'"t"': "a BIGINT, b VARCHAR, c DOUBLE", '"u"': "x VARCHAR"}
# Values that compare equal in Python but load differently (1, True and 1.0 into VARCHAR; -0.0 and 0.0 into
# DOUBLE), and NaN, which has no plain literal and takes the bound path.
VALUES = {
    "a": [None, 0, 1, True, 1.0, "1"],
    "b": [None, 1, True, 1.0, -0.0, 0.0, "a", "it's"],
    "c": [None, 0, 1, True, -0.0, 0.0, 2.5, float("nan")],
    "x": [None, "a", "b", 1, True],
}


class _Counting:
    """A connection that records the kind of each statement sent to it."""

    def __init__(self, db):
        self.db, self.statements = db, []

    def execute(self, sql, *args):
        self.statements.append(sql.split()[0])
        return self.db.execute(sql, *args)

    def executemany(self, sql, rows):
        self.statements.append("INSERT")
        return self.db.executemany(sql, rows)


def _database():
    db = duckdb.connect()
    for name, columns in TABLES.items():
        db.execute(f"CREATE TABLE {name} ({columns})")
    return db


def _contents(db):
    """Every table's rows in storage order, each value with its exact spelling (so -0.0 is not 0.0)."""

    return {name: [tuple(map(repr, row)) for row in db.execute(f"SELECT * FROM {name}").fetchall()] for name in TABLES}


def _rows(rng, columns):
    return [[rng.choice(VALUES[c]) for c in columns] for _ in range(rng.choice([0, 0, 1, 1, 2, 3]))]


def test_loader_leaves_the_same_tables_as_delete_and_insert():
    rng = random.Random(5)
    plain, skipping = _database(), _database()
    loader = TableLoader(_Counting(skipping), empty=TABLES)
    sent = 0
    for _ in range(300):
        tables = {'"t"': _rows(rng, "abc"), '"u"': _rows(rng, "x")}
        for name, rows in tables.items():
            plain.execute(f"DELETE FROM {name}")
            insert_rows(plain, name, rows)
            sent += 1 + bool(rows)
        loader.load(tables)
        assert _contents(skipping) == _contents(plain)
    assert len(loader.db.statements) < sent


def test_unchanged_and_empty_tables_get_no_statement():
    db = _Counting(_database())
    loader = TableLoader(db, empty=['"t"'])
    loader.load({'"t"': [], '"u"': []})
    assert db.statements == ["DELETE"]  # "u" was never loaded, so it may hold rows
    loader.load({'"t"': [[1, "a", 2.5]], '"u"': []})
    assert db.statements == ["DELETE", "INSERT"]  # "t" was empty: no DELETE
    loader.load({'"t"': [[1, "a", 2.5]], '"u"': []})
    loader.load({'"t"': [[True, "a", 2.5]], '"u"': []})  # TRUE is not 1 to the loader
    assert db.statements == ["DELETE", "INSERT", "DELETE", "INSERT"]


def test_a_failed_load_leaves_the_table_unknown():
    db = _Counting(_database())
    loader = TableLoader(db, empty=TABLES)
    with pytest.raises(duckdb.Error):
        loader.load({'"t"': [["not a number", "a", 1]]})
    loader.load({'"t"': []})
    assert db.statements == ["INSERT", "DELETE"]


def test_rows_key_tells_values_apart_and_marks_values_without_a_literal():
    assert len({rows_key({"t": [[value]]}) for value in (1, True, 1.0, "1", None)}) == 5
    assert rows_key({"t": [[0.0]]}) != rows_key({"t": [[-0.0]]})
    assert rows_key({"t": [], "u": [[float("nan")]]}) == ("", None)
