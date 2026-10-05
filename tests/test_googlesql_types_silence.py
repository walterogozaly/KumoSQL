"""The GoogleSQL typer stays silent when it cannot be sure: the false findings the corpus scan
(``tools/googlesql_types_scan.py``) turned up, and the findings that must still fire."""

from kumosql.googlesql_types import Catalog, infer

TABLE = {"p.d.t": {"id": "INT64", "v": "STRING"}}


def codes(sql: str, catalog: Catalog) -> list[str]:
    return [f.code for f in infer(sql, catalog).findings]


def test_a_table_the_catalog_does_not_list_is_unknown_not_an_error():
    assert codes("SELECT * FROM p.d.other", Catalog.from_types(TABLE)) == []
    assert codes("SELECT * FROM p.d.other", Catalog()) == []


def test_a_missing_table_is_an_error_only_in_a_complete_catalog():
    assert codes("SELECT * FROM p.d.other", Catalog.from_types(TABLE, complete=True)) == ["unknown_table"]


def test_having_and_qualify_may_name_a_select_alias_but_where_may_not():
    catalog = Catalog.from_types(TABLE)
    assert codes("SELECT id, COUNT(*) AS n FROM p.d.t GROUP BY 1 HAVING n > 1", catalog) == []
    assert codes("SELECT id, ROW_NUMBER() OVER (ORDER BY v) AS rn FROM p.d.t QUALIFY rn = 1", catalog) == []
    assert codes("SELECT id AS x FROM p.d.t WHERE x > 1", catalog) == ["unknown_column"]
    assert codes("SELECT id FROM p.d.t HAVING nope > 1", catalog) == ["unknown_column"]


def test_a_dataform_placeholder_makes_the_query_untyped():
    typed = infer("SELECT __sqlx_token_000__ FROM p.d.t WHERE __sqlx_token_001__", Catalog.from_types(TABLE))
    assert typed.findings == () and typed.columns is None and typed.error
