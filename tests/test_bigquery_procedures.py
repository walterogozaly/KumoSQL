"""BigQuery procedural syntax: procedures with parameter modes, labels, REPEAT, the CASE statement, RETURN and RAISE.

Two layers are covered. ``parse_statements`` must not raise on a script sqlglot cannot read when the only problem is a
procedural block: each block becomes one opaque command that prints back as written. The scripts reader must cut the same
text into the right statements and give each the right context (a statement that may not run is a possible one).
"""

from __future__ import annotations

import pytest
from sqlglot import exp
from sqlglot.errors import ParseError

from kumosql.ast_utils import parse_statements
from kumosql.scripts import KEPT, UNKNOWN, analyse_script, parse_script, split_script

PROCEDURE = """CREATE OR REPLACE PROCEDURE ds.p(IN a INT64, OUT b INT64, INOUT c STRING)
BEGIN
  SET b = (SELECT MAX(x) FROM src WHERE y = a);
  SET c = CONCAT(c, 'x');
END"""


def _sql(sql: str) -> list[str]:
    return [tree.sql(dialect="bigquery") for tree in parse_statements(sql)]


def _writes(analysis) -> list[tuple[str, list[str], bool]]:
    return [(w.table.name, sorted(source.name for source in w.sources), w.conditional) for w in analysis.writes]


def _reads(analysis) -> list[str]:
    return sorted(table.name for table in analysis.all_reads())


# ----------------------------------------------------------------------------- parse layer


@pytest.mark.parametrize(
    "sql",
    [
        PROCEDURE,
        "outer_loop: LOOP\n  SELECT 'a;b';\n  BREAK outer_loop;\nEND LOOP outer_loop",
        "DECLARE i INT64 DEFAULT 0;\nREPEAT\n  SET i = i + 1;\nUNTIL i >= 3\nEND REPEAT",
        "CASE n\n  WHEN 1 THEN SELECT 'one';\n  ELSE SELECT 'many';\nEND CASE",
    ],
    ids=["procedure_modes", "labelled_loop", "repeat_until", "case_statement"],
)
def test_procedural_block_is_kept_whole_instead_of_a_parse_error(sql):
    statements = parse_statements(sql)
    blocks = [tree for tree in statements if isinstance(tree, exp.Command)]
    assert len(blocks) == 1, [type(tree).__name__ for tree in statements]
    # nothing is split at a ``;`` inside the block, and every word of it prints back
    printed = blocks[0].sql(dialect="bigquery")
    for word in blocks[0].expression.name.replace(";", " ").split():
        assert word in printed


def test_procedure_modes_survive_the_round_trip():
    (printed,) = _sql(PROCEDURE)
    assert "IN a INT64, OUT b INT64, INOUT c STRING" in printed
    assert printed.rstrip().endswith("END")
    assert "SELECT MAX(x) FROM src WHERE y = a" in printed


def test_statements_around_a_block_keep_their_own_trees():
    statements = parse_statements("DECLARE i INT64 DEFAULT 0;\nlp: LOOP\n  SET i = i + 1;\n  IF i > 2 THEN LEAVE lp; END IF;\nEND LOOP lp;\nSELECT i")
    assert [type(tree).__name__ for tree in statements] == ["Declare", "Command", "Select"]
    assert statements[1].this.startswith("lp")


def test_a_broken_statement_next_to_a_block_is_still_a_parse_error():
    with pytest.raises(ParseError):
        parse_statements("SELECT FROM WHERE;\nlp: LOOP BREAK lp; END LOOP lp")


def test_text_without_a_block_is_still_a_parse_error():
    with pytest.raises(ParseError):
        parse_statements("SELECT * FROM")
    with pytest.raises(ParseError):
        parse_statements("SELECT 1; SELECT FROM WHERE")


def test_recover_mode_never_goes_through_the_block_reader():
    # error recovery asks sqlglot for whatever it can read; the block reader only replaces a raised ParseError
    assert parse_statements("SELECT 1; SELECT 2", recover=True)


# ----------------------------------------------------------------------------- splitting


def test_label_case_and_repeat_do_not_split_inside_a_block():
    text = (
        "lp: LOOP\n  INSERT INTO t SELECT 1 FROM a;\n  BREAK lp;\nEND LOOP lp;\n"
        "CASE (SELECT MAX(x) FROM c)\n  WHEN 1 THEN INSERT INTO t SELECT 2 FROM b; WHEN 2 THEN SELECT 3 FROM d;\n  ELSE SELECT 4 FROM e;\nEND CASE;\n"
        "DECLARE i INT64 DEFAULT 0;\nREPEAT SET i = i + 1; UNTIL CASE WHEN i > 3 THEN TRUE ELSE FALSE END END REPEAT;\n"
        "SELECT 5 FROM z"
    )
    assert [part.text for part in split_script(text)] == [
        "INSERT INTO t SELECT 1 FROM a",
        "BREAK lp",
        "INSERT INTO t SELECT 2 FROM b",
        "SELECT 3 FROM d",
        "SELECT 4 FROM e",
        "DECLARE i INT64 DEFAULT 0",
        "SET i = i + 1",
        "SELECT 5 FROM z",
    ]


def test_labelled_block_text_starts_at_its_label():
    text = "outer_loop: LOOP\n  BREAK outer_loop;\nEND LOOP outer_loop"
    (node,) = parse_script(text)
    assert node.label == "outer_loop"
    assert text[node.start : node.end] == text


@pytest.mark.parametrize(
    "opener,closer",
    [("blk: BEGIN", "END blk"), ("blk: WHILE TRUE DO", "END WHILE blk"), ("blk: FOR r IN (SELECT 1 AS c FROM q) DO", "END FOR blk"), ("blk: REPEAT", "UNTIL TRUE END REPEAT blk")],
)
def test_every_labelled_block_is_found(opener, closer):
    text = f"{opener}\n  INSERT INTO t SELECT 1 FROM a;\n{closer};\nINSERT INTO u SELECT 1 FROM b"
    assert [part.text for part in split_script(text)] == ["INSERT INTO t SELECT 1 FROM a", "INSERT INTO u SELECT 1 FROM b"]


# ----------------------------------------------------------------------------- context of a statement


def test_a_leave_of_a_label_makes_only_the_later_statements_of_that_block_possible():
    analysis = analyse_script(
        "blk: BEGIN\n  INSERT INTO t SELECT 1 FROM a;\n  IF (SELECT COUNT(*) FROM g) = 0 THEN LEAVE blk; END IF;\n  INSERT INTO u SELECT 1 FROM b;\nEND blk;\nINSERT INTO v SELECT 1 FROM c"
    )
    writes = {name: conditional for name, _sources, conditional in _writes(analysis)}
    assert writes == {"t": False, "u": True, "v": False}


def test_return_inside_an_if_makes_the_rest_of_the_script_possible():
    analysis = analyse_script("IF (SELECT COUNT(*) FROM g) = 0 THEN RETURN; END IF;\nINSERT INTO t SELECT 1 FROM a")
    assert _writes(analysis) == [("t", ["a", "g"], True)]


def test_return_in_a_procedure_ends_the_call_not_the_caller():
    analysis = analyse_script(
        "CREATE PROCEDURE ds.p(n INT64)\nBEGIN\n  IF n = 0 THEN RETURN; END IF;\n  INSERT INTO t SELECT 1 FROM a;\nEND;\n"
        "CALL ds.p(1);\nINSERT INTO u SELECT 1 FROM b"
    )
    assert _writes(analysis) == [("t", ["a"], True), ("u", ["b"], False)]


def test_raise_caught_by_the_handler_does_not_make_what_follows_the_block_possible():
    analysis = analyse_script(
        "BEGIN\n  INSERT INTO t SELECT 1 FROM a;\n  RAISE USING MESSAGE = 'x';\n  INSERT INTO u SELECT 1 FROM b;\nEXCEPTION WHEN ERROR THEN\n  INSERT INTO e SELECT 1 FROM c;\nEND;\n"
        "INSERT INTO v SELECT 1 FROM d"
    )
    assert _writes(analysis) == [("t", ["a"], False), ("u", ["b"], True), ("e", ["c"], True), ("v", ["d"], False)]


def test_an_inner_loop_label_does_not_end_the_outer_loop_but_the_outer_label_does():
    analysis = analyse_script(
        "o: LOOP\n  i: LOOP\n    LEAVE i;\n  END LOOP i;\n  INSERT INTO t SELECT 1 FROM a;\n  LEAVE o;\n  INSERT INTO u SELECT 1 FROM b;\nEND LOOP o;\nINSERT INTO v SELECT 1 FROM c"
    )
    # every loop body is a possible path; after the outer loop the script goes on for certain
    assert _writes(analysis) == [("t", ["a"], True), ("u", ["b"], True), ("v", ["c"], False)]


def test_case_statement_arms_and_the_tables_that_pick_them():
    analysis = analyse_script(
        "CASE (SELECT MAX(x) FROM c)\n  WHEN 1 THEN INSERT INTO t SELECT 1 FROM a;\n  ELSE INSERT INTO t SELECT 2 FROM b;\nEND CASE;\nINSERT INTO u SELECT 1 FROM d"
    )
    assert _writes(analysis) == [("t", ["a", "c"], True), ("t", ["b", "c"], True), ("u", ["d"], False)]


def test_repeat_until_reads_its_condition_and_runs_the_body_possibly_never_again():
    analysis = analyse_script(
        "DECLARE i INT64 DEFAULT 0;\nREPEAT\n  INSERT INTO t SELECT 1 FROM a;\n  SET i = i + 1;\nUNTIL i >= (SELECT COUNT(*) FROM g)\nEND REPEAT;\nSELECT 1 FROM z"
    )
    assert _writes(analysis) == [("t", ["a", "g"], True)]
    assert _reads(analysis) == ["a", "g", "z"]


def test_assert_and_raise_read_the_tables_their_expressions_name_but_feed_nothing():
    analysis = analyse_script(
        "ASSERT (SELECT COUNT(*) FROM g) > 0 AS 'none; found';\nBEGIN\n  RAISE USING MESSAGE = (SELECT MAX(m) FROM msg);\nEXCEPTION WHEN ERROR THEN SELECT 1 FROM e;\nEND;\nINSERT INTO t SELECT 1 FROM a"
    )
    assert _reads(analysis) == ["a", "e", "g", "msg"]
    assert _writes(analysis) == [("t", ["a"], False)]
    assert not analysis.unknown


# ----------------------------------------------------------------------------- procedures


def test_in_out_inout_parameters_are_not_tables_and_the_out_value_reaches_the_caller():
    analysis = analyse_script(
        PROCEDURE + ";\nDECLARE v INT64;\nDECLARE s STRING DEFAULT 'q';\nCALL ds.p((SELECT MIN(k) FROM g), v, s);\nINSERT INTO u SELECT v, s FROM w"
    )
    assert not analysis.unknown
    assert _reads(analysis) == ["g", "src", "w"]  # a, b and c are never tables
    assert _writes(analysis) == [("u", ["g", "src", "w"], False)]


def test_a_parameter_named_like_a_table_is_still_the_table_after_from():
    analysis = analyse_script("CREATE PROCEDURE ds.p(src INT64)\nBEGIN\n  INSERT INTO t SELECT src FROM src;\nEND;\nCALL ds.p(1)")
    assert _writes(analysis) == [("t", ["src"], False)]


def test_struct_and_array_parameters_split_on_their_own_commas():
    analysis = analyse_script(
        "CREATE PROCEDURE ds.p(IN s STRUCT<a INT64, b STRING>, OUT r ARRAY<STRUCT<x INT64, y INT64>>)\nBEGIN\n  INSERT INTO t SELECT s.a FROM src;\nEND;\n"
        "DECLARE q ARRAY<STRUCT<x INT64, y INT64>>;\nCALL ds.p((1, 'a'), q)"
    )
    assert not analysis.unknown
    assert _writes(analysis) == [("t", ["src"], False)]
    (procedure,) = analysis.procedures.values() if isinstance(analysis.procedures, dict) else analysis.procedures
    assert procedure.params == ("s", "r")
    assert procedure.modes == ("IN", "OUT")


def test_procedure_options_do_not_hide_the_body():
    analysis = analyse_script(
        "CREATE OR REPLACE PROCEDURE ds.p(IN x INT64) OPTIONS (strict_mode = FALSE, description = 'a; b')\nBEGIN\n  INSERT INTO t SELECT x FROM src;\nEND;\nCALL ds.p(1)"
    )
    assert _writes(analysis) == [("t", ["src"], False)]


def test_labels_inside_a_procedure_body_gate_its_statements():
    analysis = analyse_script(
        "CREATE PROCEDURE ds.p(n INT64)\nBEGIN\n  lp: LOOP\n    INSERT INTO t SELECT 1 FROM src;\n    IF n > 1 THEN LEAVE lp; END IF;\n  END LOOP lp;\nEND;\nCALL ds.p(1)"
    )
    assert _writes(analysis) == [("t", ["src"], True)]


def test_a_spark_procedure_is_a_definition_and_a_call_of_it_is_unknown():
    analysis = analyse_script(
        "CREATE PROCEDURE ds.sp() WITH CONNECTION `p.us.c` OPTIONS (engine = 'SPARK') LANGUAGE PYTHON AS r\"\"\"\nprint(1); x = 'END'\n\"\"\";\n"
        "CALL ds.sp();\nSELECT 1 FROM a"
    )
    assert [(s.kind, s.disposition) for s in analysis.statements if s.kind in {"create_procedure", "call"}] == [
        ("create_procedure", "ignored"),
        ("call", UNKNOWN),
    ]
    assert _reads(analysis) == ["a"]  # nothing of the Python text is read as SQL
    assert KEPT == "kept"


def test_a_call_whose_out_argument_is_not_a_variable_is_unknown_not_guessed():
    analysis = analyse_script(PROCEDURE + ";\nCALL ds.p(1, 2, 'x')")
    assert [s.kind for s in analysis.unknown] == ["call"]
