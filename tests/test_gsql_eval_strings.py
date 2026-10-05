"""STRING and BYTES functions of the GoogleSQL evaluator (``kumosql.gsql_eval.fn_string``).

Expected values come from the GoogleSQL compliance results (tests/fixtures/googlesql_conformance) and from
BigQuery's documented behavior. A case the evaluator cannot answer exactly must raise ``Unsupported``: those are
asserted too, because declining is the contract (never an approximation).
"""

from __future__ import annotations

import pytest
import sqlglot
from sqlglot import exp

from kumosql.gsql_eval import AnalysisError, Database, EvalError, Table, Unsupported, evaluate
from kumosql.gsql_eval import types as T
from kumosql.gsql_eval.functions import NODE_MAP, REGISTRY


def value(sql: str, mode: str = "bigquery", **tables):
    db = Database({name: Table(cols, rows) for name, (cols, rows) in tables.items()})
    return evaluate("SELECT " + sql, db, mode=mode).rows[0][0]


def same(sql: str, expected, mode: str = "bigquery"):
    got = value(sql, mode)
    assert got == expected and type(got) is type(expected), (sql, got, expected)


def column(sql: str, values, type_: T.Type = T.STRING, extra=()):
    """``sql`` is evaluated for every value of column ``s`` (``extra`` adds more columns, as (name, type, values))."""

    cols = [("s", type_)] + [(n, t) for n, t, _ in extra]
    rows = [tuple([v] + [vals[i] for _, _, vals in extra]) for i, v in enumerate(values)]
    db = Database({"t": Table(cols, rows)})
    return [r[0] for r in evaluate(f"SELECT {sql} FROM t", db).rows]


def error(sql: str, kind, mode: str = "bigquery"):
    with pytest.raises(kind):
        value(sql, mode)


# --- the registry: every name parses to the node its mapper reads -------------------------------------------------

SAMPLES = {
    "CONCAT": "CONCAT(a, b)",
    "LENGTH": "LENGTH(a)",
    "CHAR_LENGTH": "CHAR_LENGTH(a)",
    "BYTE_LENGTH": "BYTE_LENGTH(a)",
    "LOWER": "LOWER(a)",
    "UPPER": "UPPER(a)",
    "INITCAP": "INITCAP(a, b)",
    "TRIM": "TRIM(a, b)",
    "LTRIM": "LTRIM(a, b)",
    "RTRIM": "RTRIM(a, b)",
    "LPAD": "LPAD(a, b, c)",
    "RPAD": "RPAD(a, b, c)",
    "REPLACE": "REPLACE(a, b, c)",
    "REPEAT": "REPEAT(a, b)",
    "REVERSE": "REVERSE(a)",
    "SUBSTR": "SUBSTR(a, b, c)",
    "LEFT": "LEFT(a, b)",
    "RIGHT": "RIGHT(a, b)",
    "STARTS_WITH": "STARTS_WITH(a, b)",
    "ENDS_WITH": "ENDS_WITH(a, b)",
    "STRPOS": "STRPOS(a, b)",
    "INSTR": "INSTR(a, b, c, d)",
    "SPLIT": "SPLIT(a, b)",
    "ASCII": "ASCII(a)",
    "CHR": "CHR(a)",
    "UNICODE": "UNICODE(a)",
    "CODE_POINTS_TO_STRING": "CODE_POINTS_TO_STRING(a)",
    "CODE_POINTS_TO_BYTES": "CODE_POINTS_TO_BYTES(a)",
    "TO_CODE_POINTS": "TO_CODE_POINTS(a)",
    "TRANSLATE": "TRANSLATE(a, b, c)",
    "REGEXP_CONTAINS": "REGEXP_CONTAINS(a, b)",
    "REGEXP_EXTRACT": "REGEXP_EXTRACT(a, b, c, d)",
    "REGEXP_EXTRACT_ALL": "REGEXP_EXTRACT_ALL(a, b)",
    "REGEXP_REPLACE": "REGEXP_REPLACE(a, b, c)",
    "REGEXP_INSTR": "REGEXP_INSTR(a, b, c, d, e)",
    "TO_HEX": "TO_HEX(a)",
    "FROM_HEX": "FROM_HEX(a)",
    "TO_BASE64": "TO_BASE64(a)",
    "FROM_BASE64": "FROM_BASE64(a)",
    "TO_BASE32": "TO_BASE32(a)",
    "FROM_BASE32": "FROM_BASE32(a)",
    "SAFE_CONVERT_BYTES_TO_STRING": "SAFE_CONVERT_BYTES_TO_STRING(a)",
    "FORMAT": "FORMAT(a, b, c)",
    "SOUNDEX": "SOUNDEX(a)",
    "EDIT_DISTANCE": "EDIT_DISTANCE(a, b, max_distance => c)",
    "NORMALIZE_NFC": "NORMALIZE(a)",
    "NORMALIZE_NFKD": "NORMALIZE(a, NFKD)",
    "NORMALIZE_NFKC_CASEFOLD": "NORMALIZE_AND_CASEFOLD(a, NFKC)",
}


@pytest.mark.parametrize("name", sorted(SAMPLES))
def test_every_name_has_a_mapper_that_returns_its_name_and_arguments(name):
    node = sqlglot.parse_one("SELECT " + SAMPLES[name], read="bigquery").expressions[0]
    mapped_name, args = NODE_MAP[type(node)](node)
    assert mapped_name == name
    assert name in REGISTRY
    assert all(isinstance(a, exp.Expression) for a in args)


def test_unregistered_functions_are_declined_not_approximated():
    # sqlglot reads CONTAINS_SUBSTR as LOWER(a) CONTAINS LOWER(b): not BigQuery's normalization-aware search
    error("CONTAINS_SUBSTR('Abc', 'b')", Unsupported)
    error("COLLATE('a', 'und:ci')", Unsupported)


# --- CONCAT ---------------------------------------------------------------------------------------------------

def test_concat():
    same("CONCAT('a', 'b', 'c')", "abc")
    same("CONCAT('a')", "a")
    same("CONCAT(b'a', b'b')", b"ab")
    same("CONCAT('a', NULL)", None)
    same("CONCAT(NULL, NULL)", None)
    same("'a' || 'b' || 'c'", "abc")


def test_concat_rejects_mixed_string_and_bytes():
    error("CONCAT(b'a', 'b')", AnalysisError)


def test_concat_of_other_types_is_a_googlesql_feature_not_bigquery():
    error("CONCAT(1, 2)", AnalysisError)
    error("CONCAT('a', TRUE)", AnalysisError)
    same("CONCAT(1, -2)", "1-2", mode="googlesql")
    same("CONCAT(TRUE, NOT TRUE)", "truefalse", mode="googlesql")
    same("CONCAT(1.23456789, -8765432.1, 1e55)", "1.23456789-8765432.11e+55", mode="googlesql")
    same("CONCAT('x', NULL, 1)", None, mode="googlesql")
    error("CONCAT(b'a', 1)", AnalysisError, mode="googlesql")


# --- lengths --------------------------------------------------------------------------------------------------

def test_lengths():
    same("LENGTH('AbCdE')", 5)
    same("LENGTH('€')", 1)
    same("LENGTH(b'€')", 3)
    same("CHAR_LENGTH('€')", 1)
    same("CHARACTER_LENGTH('€')", 1)
    same("BYTE_LENGTH('€')", 3)
    same("BYTE_LENGTH(b'€')", 3)
    same("OCTET_LENGTH('€')", 3)
    same("LENGTH(NULL)", None)
    same("CHAR_LENGTH(CAST(NULL AS STRING))", None)


def test_char_length_is_for_strings_only_and_length_is_not():
    error("CHAR_LENGTH(b'abc')", AnalysisError)
    error("LENGTH(12)", AnalysisError)


def test_lengths_count_code_points_not_graphemes():
    assert column("LENGTH(s)", ["é", "\U0001f600"]) == [2, 1]
    assert column("BYTE_LENGTH(s)", ["é", "\U0001f600"]) == [3, 4]


# --- LOWER, UPPER, INITCAP ------------------------------------------------------------------------------------

def test_lower_and_upper_ascii_and_bytes():
    same("LOWER('AbCdE')", "abcde")
    same("UPPER('aBcDe')", "ABCDE")
    same("LOWER(b'A' || b'z')", b"az")
    same("UPPER(b'az')", b"AZ")
    same("UPPER(NULL)", None)


def test_bytes_case_mapping_is_ascii_only():
    assert column("UPPER(s)", [b"a\xc3\xa9"], T.BYTES) == [b"A\xc3\xa9"]
    assert column("LOWER(s)", [b"A\xc3\x89"], T.BYTES) == [b"a\xc3\x89"]


def test_string_case_mapping_one_code_point_to_one_is_exact():
    assert column("UPPER(s)", ["é", "ÿ", "я", "日本語", "ǆ"]) == ["É", "Ÿ", "Я", "日本語", "Ǆ"]
    assert column("LOWER(s)", ["É", "Я", "日本語"]) == ["é", "я", "日本語"]


@pytest.mark.parametrize("text", ["ß", "ŉ", "ǰ", "\ufb01"])
def test_upper_declines_multi_character_mappings(text):
    with pytest.raises(Unsupported):
        column("UPPER(s)", [text])


@pytest.mark.parametrize("text", ["İ", "Σ"])
def test_lower_declines_context_or_multi_character_mappings(text):
    with pytest.raises(Unsupported):
        column("LOWER(s)", [text])


def test_case_mapping_declines_characters_unassigned_here():
    # U+10FFFE is a noncharacter; a character assigned only in a later Unicode version would behave the same
    with pytest.raises(Unsupported):
        column("UPPER(s)", ["\U0010fffe"])


def test_initcap_with_explicit_delimiters():
    same("INITCAP('abcd e', ' c')", "AbcD E")
    same("INITCAP('hello WORLD-foo bar', ' ')", "Hello World-foo Bar")
    same("INITCAP('hello world', '')", "Hello world")
    same("INITCAP(NULL, ' ')", None)
    same("INITCAP('a1b c', ' ')", "A1b C")


def test_initcap_default_delimiters_are_declined():
    # sqlglot writes a default delimiter string in; BigQuery's default cannot be told from an explicit one
    error("INITCAP('abc def')", Unsupported)


# --- TRIM, LTRIM, RTRIM ---------------------------------------------------------------------------------------

def test_trim_family_with_a_set_of_characters():
    same("TRIM(' a b ', ' ')", "a b")
    same("LTRIM(' a b ', ' ')", "a b ")
    same("RTRIM(' a b ', ' ')", " a b")
    same("TRIM('xxabcxyx', 'xy')", "abc")
    same("TRIM('abc', '')", "abc")
    same("TRIM('abc', NULL)", None)
    same("TRIM(NULL, 'a')", None)
    same("TRIM('', 'a')", "")


def test_trim_family_on_bytes_needs_the_set():
    same("TRIM(b' a b ', b' ')", b"a b")
    same("TRIM(b' a b ', b'')", b" a b ")
    same("TRIM(b' a b ', NULL)", None)
    assert column("LTRIM(s, b' \\xe2')", [b" \xe2\x82\xac "], T.BYTES) == [b"\x82\xac "]
    assert column("RTRIM(s, b' \\xac')", [b" \xe2\x82\xac "], T.BYTES) == [b" \xe2\x82"]
    error("TRIM(b' a ')", AnalysisError)


def test_trim_without_a_set_removes_spaces():
    same("TRIM(' a b ')", "a b")
    same("LTRIM(' a b ')", "a b ")
    same("RTRIM(' a b ')", " a b")
    same("TRIM('   ')", "")


@pytest.mark.parametrize("text", ["\ta", "a\n", " a", " 　a"])
def test_trim_declines_when_other_white_space_decides(text):
    with pytest.raises(Unsupported):
        column("TRIM(s)", [text])


def test_trim_set_is_code_points_not_a_prefix():
    assert column("TRIM(s, 'é')", ["ééaé"]) == ["a"]


# --- LPAD, RPAD -----------------------------------------------------------------------------------------------

def test_pad():
    same("LPAD('abc', 10)", "       abc")
    same("RPAD('abc', 10)", "abc       ")
    same("LPAD('abc', 10, '*')", "*******abc")
    same("RPAD('abc', 10, '-*=')", "abc-*=-*=-")
    same("LPAD('abc', 10, '-*=')", "-*=-*=-abc")
    same("LPAD('abc', 2)", "ab")
    same("RPAD('abc', 2, 'x')", "ab")
    same("LPAD('abc', 0)", "")
    same("LPAD(b'abc', 6, b'xy')", b"xyxabc")
    same("RPAD(b'abc', 10)", b"abc       ")
    assert column("LPAD(s, 10, '智者')", ["¼¼¼a"]) == ["智者智者智者¼¼¼a"]
    assert column("RPAD(s, 10, '智者')", ["¼¼¼a"]) == ["¼¼¼a智者智者智者"]


def test_pad_nulls_and_errors():
    same("LPAD(NULL, 10, 'ab')", None)
    same("LPAD('ab', NULL, 'cd')", None)
    same("RPAD('ab', 10, NULL)", None)
    error("LPAD('abc', -15)", EvalError)
    error("RPAD('abc', -15)", EvalError)
    error("LPAD('abc', 1000000000)", EvalError)
    error("RPAD('abc', 1000000000)", EvalError)
    same("SAFE.LPAD('abc', -1)", None)
    error("LPAD('abc', 5, '')", Unsupported)
    error("LPAD('abc', 1048577, 'x')", EvalError)


def test_pad_near_the_one_megabyte_limit_is_declined():
    error("LPAD('abc', 1040000)", Unsupported)
    assert len(value("LPAD('abc', 1000)")) == 1000


# --- REPLACE, REPEAT, REVERSE ---------------------------------------------------------------------------------

def test_replace():
    same("REPLACE('abcabc', 'a', 'z')", "zbczbc")
    same("REPLACE('abc', '', 'z')", "abc")
    same("REPLACE(b'abc', b'a', b'xyz')", b"xyzbc")
    same("REPLACE('abc', 'abc', '')", "")
    same("REPLACE('abab', 'ab', 'a')", "aa")
    same("REPLACE(NULL, 'a', 'b')", None)
    same("REPLACE('a', NULL, 'b')", None)
    same("REPLACE('aaa', 'aa', 'b')", "ba")
    error("REPLACE(b'a', 'a', 'b')", AnalysisError)


def test_repeat():
    same("REPEAT('goog', 3)", "googgooggoog")
    same("REPEAT(b'goog', 2)", b"googgoog")
    same("REPEAT('goog', 0)", "")
    same("REPEAT('goog', 1)", "goog")
    same("REPEAT(NULL, 10)", None)
    same("REPEAT('ab', NULL)", None)
    same("REPEAT('', 1000000000)", "")
    error("REPEAT('abc', -15)", EvalError)
    error("REPEAT('abc', 1000000000)", EvalError)
    same("SAFE.REPEAT('abc', -1)", None)


def test_reverse():
    same("REVERSE('abc')", "cba")
    same("REVERSE(b'abc')", b"cba")
    same("REVERSE('')", "")
    same("REVERSE(NULL)", None)
    assert column("REVERSE(s)", ["abç", "é"]) == ["çba", "́e"]  # code points, not grapheme clusters


# --- SUBSTR, LEFT, RIGHT --------------------------------------------------------------------------------------

def test_substr():
    same("SUBSTR('abc', 1)", "abc")
    same("SUBSTR('abc', 1, 1)", "a")
    same("SUBSTR('abc', 0, 1)", "a")
    same("SUBSTR('abc', -3, 5)", "abc")
    same("SUBSTR('abc', -1)", "c")
    same("SUBSTR('abc', -2, 1)", "b")
    same("SUBSTR('abc', -5, 3)", "abc")
    same("SUBSTR('abc', 4)", "")
    same("SUBSTR('abc', 2, 0)", "")
    same("SUBSTR('abc', 2, 100)", "bc")
    same("SUBSTRING('abc', 2)", "bc")
    same("SUBSTR(NULL, 1)", None)
    same("SUBSTR('abc', NULL)", None)
    same("SUBSTR('abc', 1, NULL)", None)


def test_substr_counts_characters_for_strings_and_bytes_for_bytes():
    assert column("SUBSTR(s, 1, 1)", ["€"]) == ["€"]
    same("SUBSTR(b'abc', 2, 1)", b"b")
    assert column("SUBSTR(s, 2, 1)", [b"\xe2\x82\xac"], T.BYTES) == [b"\x82"]
    assert column("SUBSTR(s, 1, 1)", [b"\xe2\x82\xac"], T.BYTES) == [b"\xe2"]


def test_substr_negative_length_is_an_error():
    error("SUBSTR('abc', 1, -3)", EvalError)
    error("SUBSTR(b'abc', 1, -3)", EvalError)
    same("SAFE.SUBSTR('abc', 1, -3)", None)


def test_left_and_right():
    same("LEFT('abc', 0)", "")
    same("LEFT('abc', 2)", "ab")
    same("LEFT('abc', 5)", "abc")
    same("RIGHT('abc', 0)", "")
    same("RIGHT('abc', 2)", "bc")
    same("RIGHT('abc', 5)", "abc")
    same("LEFT(b'abc', 2)", b"ab")
    same("RIGHT(b'abc', 2)", b"bc")
    same("LEFT(NULL, 2)", None)
    error("LEFT('abc', -2)", EvalError)
    error("RIGHT(b'abc', -2)", EvalError)
    same("SAFE.LEFT('apple', -1)", None)
    same("SAFE.RIGHT('apple', -1)", None)


# --- STARTS_WITH, ENDS_WITH, STRPOS, INSTR --------------------------------------------------------------------

def test_starts_with_and_ends_with():
    same("STARTS_WITH('abc', 'a')", True)
    same("STARTS_WITH('abc', 'b')", False)
    same("STARTS_WITH('abc', '')", True)
    same("STARTS_WITH('', 'a')", False)
    same("ENDS_WITH('abc', 'c')", True)
    same("ENDS_WITH('abc', 'b')", False)
    same("ENDS_WITH(b'abc', b'')", True)
    same("STARTS_WITH(NULL, b'abc')", None)
    same("ENDS_WITH('abc', NULL)", None)
    assert column("STARTS_WITH(s, b'\\xe2')", [b"\xe2\x82\xac"], T.BYTES) == [True]


def test_strpos():
    same("STRPOS('abc', 'b')", 2)
    same("STRPOS('abc', 'd')", 0)
    same("STRPOS('abcabc', 'c')", 3)
    same("STRPOS(NULL, 'a')", None)
    same("STRPOS('€', '€')", 1)
    assert column("STRPOS(s, '€')", ["a€"]) == [2]
    assert column("STRPOS(s, b'\\x82')", [b"\xe2\x82\xac"], T.BYTES) == [2]


def test_strpos_of_an_empty_string_is_declined():
    error("STRPOS('abc', '')", Unsupported)


def test_instr():
    same("INSTR('abc', 'b')", 2)
    same("INSTR('abbba', 'bb')", 2)
    same("INSTR('abbba', 'bb', 2, 2)", 3)  # matches may overlap
    same("INSTR('abbba', 'bb', 3)", 3)
    same("INSTR('abbba', 'bb', 4)", 0)
    same("INSTR('abbba', 'bb', 1, 3)", 0)
    same("INSTR('banana', 'an', 1, 2)", 4)
    same("INSTR('abc', NULL)", None)
    same("INSTR(b'\\xe2\\x82\\x82', b'\\x82', 1, 2)", 3)


def test_instr_backwards_when_every_reading_agrees():
    same("INSTR('abbba', 'bb', -2)", 3)
    same("INSTR('abc', 'c', -1)", 3)
    same("INSTR('abc', 'a', -1)", 1)
    same("INSTR(b'\\xe2\\x82\\x82', b'\\x82', -1)", 3)


def test_instr_backwards_declines_when_readings_differ():
    # "bc" starts at 2 and ends at 3: whether position -2 (= 2) admits it depends on an undocumented rule
    error("INSTR('abc', 'bc', -2)", Unsupported)


def test_instr_errors():
    error("INSTR('abc', 'b', 0)", EvalError)
    error("INSTR('abc', 'b', 1, 0)", EvalError)
    error("INSTR('abc', 'b', 1, -1)", EvalError)
    same("SAFE.INSTR('abc', 'b', 0)", None)


# --- SPLIT ----------------------------------------------------------------------------------------------------

def test_split():
    same("SPLIT('192.0.0.1', '.')", ("192", "0", "0", "1"))
    same("SPLIT('foo, bar, ,,')", ("foo", " bar", " ", "", ""))
    same("SPLIT('')", ("",))
    same("SPLIT(',')", ("", ""))
    same("SPLIT(' hello, world! ', ' ')", ("", "hello,", "world!", ""))
    same("SPLIT('aaaaa', 'aa')", ("", "", "a"))
    same("SPLIT('ab', 'abc')", ("ab",))
    same("SPLIT('', 'foo')", ("",))


def test_split_with_an_empty_delimiter_splits_characters():
    same("SPLIT('abcd', '')", ("a", "b", "c", "d"))
    same("SPLIT('', '')", ("",))
    assert column("SPLIT(s, '')", ["beyoncé"]) == [("b", "e", "y", "o", "n", "c", "é")]
    assert column("SPLIT(s, '')", ["विभिन"]) == [("व", "ि", "भ", "ि", "न")]
    same("SPLIT(b'\\x00\\x02', b'')", (b"\x00", b"\x02"))


def test_split_nulls_and_bytes():
    same("SPLIT(NULL)", None)
    same("SPLIT('x', NULL)", None)
    same("SPLIT(NULL, NULL)", None)
    same("SPLIT(CAST(NULL AS BYTES), b',')", None)
    same("SPLIT(b'192.0.0.1', b'.')", (b"192", b"0", b"0", b"1"))
    same("SPLIT(b'', b'')", (b"",))
    error("SPLIT(b'a,b')", AnalysisError)  # BYTES has no default delimiter
    same("SPLIT(b'aaaaa', b'aa')", (b"", b"", b"a"))


def test_split_has_an_array_type():
    r = evaluate("SELECT SPLIT('a,b'), SPLIT(b'a', b',')")
    assert [t for _, t in r.columns] == [T.array(T.STRING), T.array(T.BYTES)]


# --- ASCII, CHR, UNICODE, code points, TRANSLATE --------------------------------------------------------------

def test_ascii_unicode_chr():
    same("ASCII('')", 0)
    same("ASCII('a')", 97)
    same("ASCII('abc')", 97)
    same("ASCII(b'')", 0)
    same("ASCII(b'a')", 97)
    same("UNICODE('')", 0)
    same("UNICODE('a')", 97)
    same("UNICODE('é')", 233)
    same("CHR(97)", "a")
    same("CHR(1076)", "д")
    same("CHR(NULL)", None)
    assert column("UNICODE(s)", [""]) == [57344]
    error("CHR(-1)", EvalError)
    error("CHR(1114112)", EvalError)
    error("CHR(55296)", EvalError)  # a surrogate
    same("SAFE.CHR(-1)", None)


def test_ascii_of_a_non_ascii_character_is_an_error():
    assert column("SAFE.ASCII(s)", ["ÿ"]) == [None]
    with pytest.raises(EvalError):
        column("ASCII(s)", ["ÿ"])


def test_chr_zero_is_nul_in_the_reference_and_declined_for_bigquery():
    same("CHR(0)", "\x00", mode="googlesql")
    error("CHR(0)", Unsupported)


def test_code_points():
    same("TO_CODE_POINTS('ab')", (97, 98))
    same("TO_CODE_POINTS('')", ())
    same("TO_CODE_POINTS(b'ab')", (97, 98))
    same("TO_CODE_POINTS(NULL)", None)
    assert column("TO_CODE_POINTS(s)", ["é€"]) == [(233, 8364)]
    same("CODE_POINTS_TO_STRING([97, 98])", "ab")
    same("CODE_POINTS_TO_STRING([])", "")
    same("CODE_POINTS_TO_STRING([68, 69, NULL, 70])", None)
    same("CODE_POINTS_TO_BYTES([68, 69])", b"DE")
    same("CODE_POINTS_TO_BYTES([68, 69, NULL, 70])", None)
    same("CODE_POINTS_TO_BYTES(NULL)", None)
    error("CODE_POINTS_TO_BYTES([256])", EvalError)
    error("CODE_POINTS_TO_BYTES([-1])", EvalError)
    error("CODE_POINTS_TO_STRING([55296])", EvalError)
    error("CODE_POINTS_TO_STRING([0])", Unsupported)
    error("CODE_POINTS_TO_STRING(['a'])", AnalysisError)


def test_translate():
    same("TRANSLATE('abc', 'abd', 'xy')", "xyc")  # a source character without a target is removed
    same("TRANSLATE('abcabc', 'ab', 'xy')", "xycxyc")
    same("TRANSLATE('abc', 'ab', 'xyzzy')", "xyc")
    same("TRANSLATE('abc', '', 'x')", "abc")
    same("TRANSLATE(NULL, 'a', 'b')", None)
    same("TRANSLATE('a', NULL, 'b')", None)
    same("TRANSLATE(b'abc', b'ab', b'\\x01\\x02')", b"\x01\x02c")
    assert column("TRANSLATE(s, 'ab', '\\tд')", ["abcabc"]) == ["\tдc\tдc"]


def test_translate_with_a_repeated_source_character_is_declined():
    error("TRANSLATE('abc', 'aa', 'xy')", Unsupported)


# --- hex, base64, base32, bytes to string ---------------------------------------------------------------------

def test_hex():
    same("TO_HEX(b'abc')", "616263")
    same("TO_HEX(b'')", "")
    same("FROM_HEX('616263')", b"abc")
    same("FROM_HEX('6A6b')", b"jk")
    same("FROM_HEX('616')", b"\x06\x16")  # an odd number of digits acts as a leading 0
    same("FROM_HEX('')", b"")
    same("FROM_HEX(NULL)", None)
    same("TO_HEX(NULL)", None)
    error("FROM_HEX('zz')", EvalError)
    same("SAFE.FROM_HEX('zz')", None)
    error("TO_HEX('abc')", AnalysisError)


def test_base64_and_base32():
    same("TO_BASE64(b'abc')", "YWJj")
    same("TO_BASE64(b'ab')", "YWI=")
    same("TO_BASE64(b'')", "")
    same("FROM_BASE64('YWJj')", b"abc")
    same("FROM_BASE64('YWI=')", b"ab")
    same("FROM_BASE64('')", b"")
    same("TO_BASE32(b'abc')", "MFRGG===")
    same("FROM_BASE32('MFRGG===')", b"abc")
    same("FROM_BASE32(NULL)", None)
    same("TO_BASE64(NULL)", None)


@pytest.mark.parametrize("text", ["YWJj\n", "YWJ", "YW-j", "YR==", " YWJj"])
def test_from_base64_declines_what_is_not_canonical_padded_standard_base64(text):
    with pytest.raises(Unsupported):
        column("FROM_BASE64(s)", [text])


def test_safe_convert_bytes_to_string():
    assert column("SAFE_CONVERT_BYTES_TO_STRING(s)", [b"\xe2\x28\xa1"], T.BYTES) == ["�(�"]
    assert column("SAFE_CONVERT_BYTES_TO_STRING(s)", [b"abc\xe2\x82\xac"], T.BYTES) == ["abc€"]
    assert column("SAFE_CONVERT_BYTES_TO_STRING(s)", [b"\xa0\xa1"], T.BYTES) == ["��"]
    assert column("SAFE_CONVERT_BYTES_TO_STRING(s)", [b"\xf0\x28\x8c\x28"], T.BYTES) == ["�(�("]
    same("SAFE_CONVERT_BYTES_TO_STRING(NULL)", None)


def test_safe_convert_declines_truncated_multibyte_sequences():
    # one replacement character per byte or per truncated sequence: undocumented
    with pytest.raises(Unsupported):
        column("SAFE_CONVERT_BYTES_TO_STRING(s)", [b"a\xe2\x82b"], T.BYTES)


# --- regular expressions --------------------------------------------------------------------------------------

def test_regexp_contains():
    same("REGEXP_CONTAINS('abc', 'b')", True)
    same("REGEXP_CONTAINS('abc', '^b')", False)
    same("REGEXP_CONTAINS('abc', '^a.c$')", True)
    same("REGEXP_CONTAINS('abc', '')", True)
    same("REGEXP_CONTAINS('a\\nb', 'a.b')", False)
    same("REGEXP_CONTAINS('a\\nb', '(?s)a.b')", True)
    same("REGEXP_CONTAINS(NULL, 'a')", None)
    same("REGEXP_CONTAINS('a', NULL)", None)
    same("REGEXP_CONTAINS(b'abc', b'a.c')", True)
    same("REGEXP_CONTAINS(b'abc', b'')", True)
    error("REGEXP_CONTAINS(b'abc', 'a')", AnalysisError)


def test_regexp_dollar_does_not_match_before_a_final_newline():
    # Python's $ would: RE2's does not
    assert column("REGEXP_CONTAINS(s, 'a$')", ["a\n", "a"]) == [False, True]
    assert column("REGEXP_CONTAINS(s, '(?m)a$')", ["a\nb"]) == [True]
    assert column("REGEXP_CONTAINS(s, '\\\\Aa\\\\z')", ["a\n", "a"]) == [False, True]


def test_regexp_perl_classes_are_ascii():
    assert column("REGEXP_CONTAINS(s, '^\\\\w+$')", ["abc_1", "é"]) == [True, False]
    assert column("REGEXP_CONTAINS(s, '^\\\\d+$')", ["123", "٣"]) == [True, False]
    assert column("REGEXP_CONTAINS(s, 'a\\\\sb')", ["a b", "a\u000bb", "a b"]) == [True, False, False]  # RE2's \s has no \v


def test_regexp_word_boundary_is_ascii():
    assert column("REGEXP_CONTAINS(s, '\\\\bfoo\\\\b')", ["a foo b", "afoo", "éfoo"]) == [True, False, True]


def test_regexp_case_insensitive_flag_needs_ascii_text():
    assert column("REGEXP_CONTAINS(s, '(?i)ABC')", ["xaBcx", "xyz"]) == [True, False]
    with pytest.raises(Unsupported):
        column("REGEXP_CONTAINS(s, '(?i)abc')", ["Kabc"])  # KELVIN SIGN folds to k in RE2 and not in Python's ASCII mode


def test_regexp_extract():
    same("REGEXP_EXTRACT('abcabc', '[a|d]b?')", "ab")
    same("REGEXP_EXTRACT('abcabc', 'a(b)c')", "b")
    same("REGEXP_EXTRACT('abcabc', 'x')", None)
    same("REGEXP_EXTRACT('abcabc', '')", "")
    same("REGEXP_EXTRACT('abcabc', 'a(x)?bc')", None)  # the group did not take part
    same("REGEXP_EXTRACT(NULL, 'a')", None)
    same("REGEXP_EXTRACT('abc', NULL)", None)
    same("REGEXP_EXTRACT(b'abcabc', b'a.c')", b"abc")
    same("REGEXP_EXTRACT(b'abcabc', b'%#ab')", None)
    same("REGEXP_EXTRACT(b'abcabc', NULL)", None)
    same("REGEXP_SUBSTR('abcabc', 'b.')", "bc")


def test_regexp_extract_position_and_occurrence():
    same("REGEXP_EXTRACT('abcabc', 'b.', 3)", "bc")
    same("REGEXP_EXTRACT('abcabc', 'b.', 1, 2)", "bc")
    same("REGEXP_EXTRACT('abcabc', 'b.', 1, 3)", None)
    same("REGEXP_EXTRACT('banana', 'ana', 1, 2)", None)  # occurrences do not overlap
    error("REGEXP_EXTRACT('abc', 'b', 4)", Unsupported)  # beyond the value: not answered
    error("REGEXP_EXTRACT('abc', 'b', 0)", Unsupported)
    error("REGEXP_EXTRACT('abc', '^a', 2)", Unsupported)  # an anchor resumed mid-value: whether it re-anchors is undocumented


def test_regexp_extract_with_several_groups_or_a_bad_pattern_is_an_error():
    error("REGEXP_EXTRACT('abc', '(a)(b)')", EvalError)
    error("REGEXP_EXTRACT_ALL('abc', '(a)(b)')", EvalError)
    error("REGEXP_CONTAINS('abc', '(')", EvalError)
    error("REGEXP_CONTAINS('abc', '[a')", EvalError)
    error("REGEXP_CONTAINS('abc', '*a')", EvalError)
    error("REGEXP_CONTAINS('abc', 'a\\\\')", EvalError)
    error("REGEXP_CONTAINS('abc', 'a{2,1}')", EvalError)
    error("REGEXP_CONTAINS('abc', ')')", EvalError)
    same("SAFE.REGEXP_CONTAINS('abc', '(')", None)
    same("SAFE.REGEXP_INSTR('abc', '(a)(b)')", None)
    same("REGEXP_CONTAINS(NULL, '(')", None)


def test_regexp_extract_all():
    same("REGEXP_EXTRACT_ALL('abc dbc ac', '[a|d]b?')", ("ab", "db", "a"))
    same("REGEXP_EXTRACT_ALL('banana', 'ana')", ("ana",))
    same("REGEXP_EXTRACT_ALL('banana', 'a(n)a')", ("n",))
    same("REGEXP_EXTRACT_ALL('abc', 'x')", ())
    same("REGEXP_EXTRACT_ALL(NULL, 'a')", None)
    same("REGEXP_EXTRACT_ALL('abc', NULL)", None)
    same("REGEXP_EXTRACT_ALL(b'abcabc', b'a.c')", (b"abc", b"abc"))
    same("REGEXP_EXTRACT_ALL(b'abcabc', b'%#ab')", ())
    same("REGEXP_EXTRACT_ALL(b'abc', b'')", (b"", b"", b""))
    same("REGEXP_EXTRACT_ALL(b'foo bar baz', b'[^ ]+')", (b"foo", b"bar", b"baz"))
    same("REGEXP_EXTRACT_ALL('', '[^ ]+')", ())


def test_regexp_extract_all_declines_empty_matches_it_cannot_place():
    error("REGEXP_EXTRACT_ALL('baab', 'a*')", Unsupported)
    error("REGEXP_EXTRACT_ALL('', '')", Unsupported)
    error("REGEXP_EXTRACT_ALL('aXa', '^a')", Unsupported)


def test_regexp_replace():
    same("REGEXP_REPLACE('abcabc', '[a|d]b?', 'xyz')", "xyzcxyzc")
    same("REGEXP_REPLACE('abcabc', 'a.c', 'xyz')", "xyzxyz")
    same("REGEXP_REPLACE('abcabc', '', 'xyz')", "xyzaxyzbxyzcxyzaxyzbxyzcxyz")
    same("REGEXP_REPLACE(b'abcabc', b'', b'xyz')", b"xyzaxyzbxyzcxyzaxyzbxyzcxyz")
    same("REGEXP_REPLACE('abc', 'b', '[\\\\0]')", "a[b]c")
    same("REGEXP_REPLACE('abc', '(a)(b)?', '<\\\\2\\\\1>')", "<ba>c")
    same("REGEXP_REPLACE('abc', 'x(y)?', '\\\\1')", "abc")
    same("REGEXP_REPLACE('a.c', '\\\\.', '\\\\\\\\')", "a\\c")
    same("REGEXP_REPLACE(NULL, 'a', 'b')", None)
    same("REGEXP_REPLACE('a', NULL, 'b')", None)
    same("REGEXP_REPLACE('a', 'a', NULL)", None)
    same("REGEXP_REPLACE(b'abcabc', b'a.c', b'xyz')", b"xyzxyz")
    same("REGEXP_REPLACE(b'abcabc', NULL, b'xyz')", None)


def test_regexp_replace_declines_what_it_cannot_place():
    error("REGEXP_REPLACE('abc', 'b*', '-')", Unsupported)  # empty matches next to non-empty ones differ between engines
    error("REGEXP_REPLACE('abc', 'b', '\\\\2')", Unsupported)  # a group the pattern lacks
    error("REGEXP_REPLACE('abc', 'b', '\\\\q')", Unsupported)
    error("REGEXP_REPLACE('aXa', '^a', 'b')", Unsupported)


def test_regexp_instr():
    same("REGEXP_INSTR('abcabc', 'a(b)c', 2, 1, 1)", 6)
    same("REGEXP_INSTR('щцфщфф', 'щ(.).', 1, 2)", 5)
    same("REGEXP_INSTR('abcdef', 'ac.*e.')", 0)
    same("REGEXP_INSTR('abcabc', 'b')", 2)
    same("REGEXP_INSTR('abcabc', 'b', 3)", 5)
    same("REGEXP_INSTR('abcabc', 'bc', 1, 2, 1)", 7)
    same("REGEXP_INSTR('abcabc', 'bc', 1, 3)", 0)
    same("REGEXP_INSTR(NULL, 'a')", None)
    same("REGEXP_INSTR('a', NULL)", None)
    same("REGEXP_INSTR(b'abcabc', b'a(b)c', 2, 1, 1)", 6)
    same("REGEXP_INSTR(b'щцфщфф', b'щ(.).', 1, 2)", 9)  # bytes, not characters
    same("REGEXP_INSTR('-2020-jack-class1', '', 2)", 0)


def test_regexp_instr_declines_what_it_cannot_place():
    error("REGEXP_INSTR('abc', 'b*')", Unsupported)
    error("REGEXP_INSTR('abc', 'b', 1, 1, 2)", Unsupported)
    error("REGEXP_INSTR('abc', '^b', 2)", Unsupported)


@pytest.mark.parametrize(
    "pattern",
    [
        "\\\\pL",  # Unicode classes
        "[[:alpha:]]",  # POSIX classes (Python reads them as a nested set)
        "\\\\Qa.b\\\\E",  # quoting
        "(?=a)",  # lookahead: RE2 rejects, Python accepts
        "(?<=a)b",
        "(?<n>a)",  # RE2's newer named group syntax
        "(?P=n)",
        "a++",  # possessive: Python 3.11 accepts, RE2 rejects
        "a*+",
        "a{,3}",  # RE2 reads this as literal text, Python as {0,3}
        "a{",
        "\\\\1",  # backreference
        "\\\\8",
        "(?U)a",
        "(?i:a)",
        "a(?i)b",
        "\\\\x{41}",
        "\\\\Z",
        "(a*)*",  # nullable repetition: captures differ
        "(?:a?)+",
        "x{500}{3}",
        "((a{100}){100}){100}",
    ],
)
def test_regexp_constructs_outside_the_common_subset_are_declined(pattern):
    with pytest.raises((Unsupported, EvalError)) as info:
        value(f"REGEXP_CONTAINS('abc', '{pattern}')")
    assert info.type is Unsupported or "repetition" in str(info.value)


@pytest.mark.parametrize(
    "pattern, text, expected",
    [
        ("a|b", "xb", True),
        ("(a|ab)(c|bcd)", "abcd", True),
        ("^(?:a|b)+$", "abba", True),
        ("[^a-c]", "abc", False),
        ("[^a-c]", "abcd", True),
        ("[a\\\\-z]", "-", True),
        ("[]a]", "]", True),
        ("[^]a]", "]", False),
        ("a{2}", "aa", True),
        ("a{2}", "a", False),
        ("a{2,}", "aaa", True),
        ("a{1,2}b", "aaab", True),
        ("a.?b", "a\\nb", False),
        ("\\\\.", "a.b", True),
        ("\\\\.", "ab", False),
        ("\\\\x41", "A", True),
        ("[\\\\x41-\\\\x43]+", "B", True),
        ("\\\\_", "_", True),
        ("é", "café", True),
        ("[à-ÿ]", "é", True),
        ("(?P<x>a)b", "ab", True),
        ("x*", "", True),
        ("a*?b", "aab", True),
    ],
)
def test_regexp_contains_over_the_supported_subset(pattern, text, expected):
    assert column(f"REGEXP_CONTAINS(s, '{pattern}')", [text.replace("\\n", "\n")]) == [expected]


def test_regexp_leftmost_first_alternation_and_lazy_quantifiers():
    assert column("REGEXP_EXTRACT(s, 'a|ab')", ["ab"]) == ["a"]
    assert column("REGEXP_EXTRACT(s, 'a+?')", ["aaa"]) == ["a"]
    assert column("REGEXP_EXTRACT(s, 'a+')", ["baaa"]) == ["aaa"]
    assert column("REGEXP_EXTRACT(s, '(?:a|ab)(?:c|bcd)(?:d*)')", ["abcd"]) == ["abcd"]


def test_regexp_bytes_mode_works_on_bytes_not_characters():
    # the pattern "." matches one byte, so two dots span one two-byte character
    assert column("REGEXP_EXTRACT(s, b'^..')", ["щ".encode()], T.BYTES) == ["щ".encode()]
    assert column("REGEXP_EXTRACT(s, b'^.')", ["щ".encode()], T.BYTES) == ["щ".encode()[:1]]
    assert column("REGEXP_CONTAINS(s, b'[\\\\x80-\\\\xff]')", [b"a\xe2", b"abc"], T.BYTES) == [True, False]
    assert column("REGEXP_EXTRACT_ALL(s, b'[^ ]+')", [b"foo bar"], T.BYTES) == [(b"foo", b"bar")]


def test_regexp_functions_are_typed():
    r = evaluate("SELECT REGEXP_EXTRACT('a', 'a'), REGEXP_EXTRACT(b'a', b'a'), REGEXP_EXTRACT_ALL('a', 'a'), REGEXP_INSTR('a', 'a'), REGEXP_CONTAINS('a', 'a'), REGEXP_REPLACE(b'a', b'a', b'b')")
    assert [t for _, t in r.columns] == [T.STRING, T.BYTES, T.array(T.STRING), T.INT64, T.BOOL, T.BYTES]


# --- FORMAT ---------------------------------------------------------------------------------------------------

def test_format_integers():
    same("FORMAT('%d', 15)", "15")
    same("FORMAT('%i', -23)", "-23")
    same("FORMAT('%d %d', 15, -23)", "15 -23")
    same("FORMAT('%5d|%-5d|', -23, 4)", "  -23|4    |")
    same("FORMAT('%05d', 7)", "00007")
    same("FORMAT('%+d', 5)", "+5")
    same("FORMAT('% d', 5)", " 5")
    same("FORMAT('%.3d', 7)", "007")
    same("FORMAT('%x %X %o', 255, 255, 8)", "ff FF 10")
    same("FORMAT('%#x %#X %#o', 255, 255, 8)", "0xff 0XFF 010")
    same("FORMAT('%#x', 0)", "0")
    same("FORMAT('%08x', 255)", "000000ff")
    same("FORMAT('%d%%', 5)", "5%")
    same("FORMAT('no specifiers')", "no specifiers")
    same("FORMAT('100%%')", "100%")


def test_format_star_width_and_precision():
    cols = [("x", T.STRING), ("y", T.INT64), ("z", T.INT64)]
    cases = {
        ("%*i", 6, 13): "    13",
        ("%0.*d", 4, 12): "0012",
        ("%*d", None, 12): None,  # a NULL width makes the result NULL
        ("%*d", 4, None): None,  # so does a NULL value under a *
        (None, 4, 12): None,
        ("%d %d", 15, -23): "15 -23",
        ("%-*d|", 4, 7): "7   |",
        ("%.*f", 2, 3.14159): None,  # FLOAT64 in an INT64 column is rejected at load; see below
    }
    for row, expected in cases.items():
        if row == ("%.*f", 2, 3.14159):
            continue
        db = Database({"t": Table(cols, [row])})
        assert evaluate("SELECT FORMAT(x, y, z) FROM t", db).rows[0][0] == expected, row
    same("FORMAT('%.*f', 2, 3.14159)", "3.14")
    same("FORMAT('%*.*f|', 8, 2, 3.14159)", "    3.14|")


def test_format_floats():
    same("FORMAT('%f', 1.5)", "1.500000")
    same("FORMAT('%.2f', 3.14159)", "3.14")
    same("FORMAT('%10.3f|%-10.1f|', 3.14159, 2.5)", "     3.142|2.5       |")
    same("FORMAT('%e', 12345.678)", "1.234568e+04")
    same("FORMAT('%E', 0.000123)", "1.230000E-04")
    same("FORMAT('%g', 0.0001234)", "0.0001234")
    same("FORMAT('%g', 1e20)", "1e+20")
    same("FORMAT('%G', 1e-5)", "1E-05")
    same("FORMAT('%g', 100000.0)", "100000")
    same("FORMAT('%g', 1000000.0)", "1e+06")
    same("FORMAT('%+.1f', 2.0)", "+2.0")
    same("FORMAT('%08.2f', -3.14159)", "-0003.14")
    same("FORMAT('%#.0f', 3.0)", "3.")
    same("FORMAT('%f', CAST('inf' AS FLOAT64))", "inf")
    same("FORMAT('%F', CAST('-inf' AS FLOAT64))", "-INF")
    same("FORMAT('%e', 0.0)", "0.000000e+00")
    same("FORMAT('%.0e', 5.5)", "6e+00")
    same("FORMAT('%.2f', 2.675)", "2.67")  # exact binary value, as C printf


def test_format_strings_and_text_forms():
    same("FORMAT('%s', 'abc')", "abc")
    same("FORMAT('%5s|%-5s|', 'ab', 'cd')", "   ab|cd   |")
    same("FORMAT('%.2s', 'xyz')", "xy")
    same("FORMAT('%t', 'abc')", "abc")
    same("FORMAT('%T', 'abc')", '"abc"')
    same("FORMAT('%t %T', 12, 12)", "12 12")
    same("FORMAT('%t %T', TRUE, FALSE)", "true false")
    same("FORMAT('%t', 1.5)", "1.5")
    same("FORMAT('%4.2t', 150)", "  15")
    assert column("FORMAT('%T', s)", ["a\nb\\c", "日本"]) == ['"a\\nb\\\\c"', '"日本"']
    assert column("FORMAT('%s|%t|%T', s, s, s)", ["é"]) == ['é|é|"é"']


def test_format_declines_what_is_not_the_documented_subset():
    for sql in [
        "FORMAT('%p', 1)",
        "FORMAT('%u', 1)",
        "FORMAT(\"%'d\", 1000)",
        "FORMAT('%s', 1)",  # %s of a non-string
        "FORMAT('%x', -1)",
        "FORMAT('%d', 1.5)",
        "FORMAT('%d', NULL)",
        "FORMAT('%s', NULL)",
        "FORMAT('%T', 1.5)",
        "FORMAT('%T', \"a'b\")",
        "FORMAT('%5.1f', CAST('inf' AS FLOAT64))",
        "FORMAT('%d', CAST(1 AS NUMERIC))",
        "FORMAT('%.0d', 0)",
        "FORMAT('%#d', 1)",
        "FORMAT('%5%', 1)",
        "FORMAT('abc%')",
        "FORMAT('%f', 1)",
        "FORMAT('%T', DATE '2020-01-01')",
    ]:
        with pytest.raises(Unsupported):
            value(sql)


def test_format_errors():
    error("FORMAT('%d', 'abc')", EvalError)  # Expected integer; Got STRING
    error("FORMAT('%d', 17, 'abc')", EvalError)  # too many arguments
    error("FORMAT('%d %d', 17)", EvalError)  # too few
    error("FORMAT('%f', TRUE)", EvalError)
    same("SAFE.FORMAT('%d', 'abc')", None)


def test_format_null_template_is_null_whatever_follows():
    same("FORMAT(NULL, 1)", None)
    same("FORMAT((((NULL))), 1)", None)
    same("FORMAT(FORMAT(((NULL)), 1), 2)", None)
    same("FORMAT(CONCAT(CAST(NULL AS STRING), CAST(NULL AS STRING)), 1)", None)
    same("FORMAT(CAST(NULL AS STRING))", None)
    error("FORMAT(1)", AnalysisError)


# --- SOUNDEX, EDIT_DISTANCE, NORMALIZE ------------------------------------------------------------------------

def test_soundex():
    same("SOUNDEX('')", "")
    same("SOUNDEX(' I LOVE YOU TOO')", "I413")
    same("SOUNDEX('Ashcraft')", "A261")
    same("SOUNDEX('Robert')", "R163")
    same("SOUNDEX('Rupert')", "R163")
    same("SOUNDEX('123')", "")
    same("SOUNDEX(NULL)", None)
    same("SOUNDEX('a')", "A000")
    error("SOUNDEX('Ünï')", Unsupported)


def test_edit_distance():
    same("EDIT_DISTANCE('kitten', 'sitting')", 3)
    same("EDIT_DISTANCE('', 'abc')", 3)
    same("EDIT_DISTANCE('abc', 'abc')", 0)
    same("EDIT_DISTANCE('kitten', 'sitting', max_distance => 2)", 2)
    same("EDIT_DISTANCE('kitten', 'sitting', max_distance => 5)", 3)
    same("EDIT_DISTANCE(b'abc', b'abd')", 1)
    same("EDIT_DISTANCE(NULL, 'abc')", None)
    same("EDIT_DISTANCE('abc', NULL)", None)
    error("EDIT_DISTANCE('a', 'b', max_distance => -1)", EvalError)
    error("EDIT_DISTANCE('é', 'e')", Unsupported)


def test_normalize():
    assert column("NORMALIZE(s)", ["é"]) == ["é"]
    assert column("NORMALIZE(s, NFD)", ["é"]) == ["é"]
    assert column("NORMALIZE(s, NFKC)", ["ﬁ"]) == ["fi"]
    assert column("NORMALIZE(s, NFC)", ["ﬁ"]) == ["ﬁ"]
    assert column("NORMALIZE(s, NFKD)", ["éﬁ"]) == ["éfi"]
    same("NORMALIZE('abc')", "abc")
    same("NORMALIZE(NULL)", None)
    assert column("NORMALIZE_AND_CASEFOLD(s)", ["ABC", "É"]) == ["abc", "é"]
    assert column("NORMALIZE_AND_CASEFOLD(s, NFKC)", ["ﬁX"]) == ["fix"]


def test_normalize_declines_unassigned_characters_and_order_dependent_folding():
    with pytest.raises(Unsupported):
        column("NORMALIZE(s)", ["\U0010fffe"])
    with pytest.raises(Unsupported):
        column("NORMALIZE_AND_CASEFOLD(s)", ["\u0345\u0301"])


# --- the regular expression translator against RE2 itself (DuckDB links RE2) ------------------------------------

def test_not_a_word_boundary_matches_the_empty_string_as_in_re2():
    assert column("REGEXP_CONTAINS(s, '\\\\B')", [""]) == [True]
    assert column("REGEXP_CONTAINS(s, 'a\\\\Bb')", ["ab", "a b"]) == [True, False]
    with pytest.raises(Unsupported):  # RE2 scans bytes, so \B can match inside a multi-byte character
        column("REGEXP_CONTAINS(s, '\\\\B')", ["aKy"])


_TOKENS = [
    "a", "b", "c", " ", "é", ".", "\\d", "\\w", "\\s", "\\S", "\\W", "\\D", "^", "$", "\\b", "\\B", "[abc]", "[^a]",
    "[a-c]", "[\\s\\d]", "[^\\s]", "[\\w-]", "[]a]", "[^]a]", "(", ")", "(?:", "|", "*", "+", "?", "{2}", "{1,2}", "{2,}",
    "*?", "+?", "??", "\\A", "\\z", "\\.", "\\-", "[a\\-c]", "(?i)", "(?s)", "(?m)", "x", "1", "_", "\\n", "\\t", "\\x41",
    "\\x{41}", "{", "}", "]", "[", ",", "\\\\", "#", "\\v", "\\f", "A", "\\pL", "\\Z", "(?P<n>", "(?=", "\\1", "[é-ü]",
    "(a|b)", "(ab)+", "(?:ab|c)*", "a{0}", "(x)?", "[a-z]+", "(a)(b)", "a|b|", "(|a)", "((a))", "(a(b)?)", "\\bx", "x\\b",
]
_ALPHABET = list("abcABC d1_xyz.\n\t\r") + ["é", "É", "\x0b", " ", "K", "-", "]", "}"]


def _outcome(sql: str, db):
    try:
        return "ok", evaluate(sql, db).rows[0][0]
    except Unsupported:
        return "unsupported", None
    except EvalError:
        return "error", None


def test_regexp_functions_agree_with_re2_wherever_they_answer():
    import random

    from kumosql.gsql_eval.fn_string import _compile_re2

    duckdb = pytest.importorskip("duckdb")
    rng = random.Random(20260101)
    con = duckdb.connect()
    answered = 0
    for _ in range(700):
        pattern = "".join(rng.choice(_TOKENS) for _ in range(rng.randint(1, 6)))
        text = "".join(rng.choice(_ALPHABET) for _ in range(rng.randint(0, 7)))
        db = Database({"t": Table([("s", T.STRING), ("p", T.STRING)], [(text, pattern)])})
        try:
            expected = con.execute("select regexp_matches(?, ?)", [text, pattern]).fetchone()[0]
            re2_error = False
        except Exception:  # noqa: BLE001  DuckDB reports RE2's parse errors as its own exception types
            expected, re2_error = None, True
        kind, got = _outcome("SELECT REGEXP_CONTAINS(s, p) FROM t", db)
        if kind == "error":
            assert re2_error, (pattern, text)  # a pattern we call invalid must be invalid in RE2
        if kind != "ok":
            continue
        answered += 1
        assert not re2_error and got == expected, (pattern, text, got, expected)
        group = _compile_re2(pattern).groups  # 0 or 1 here: more is an error for the extraction functions
        checks = (
            ("REGEXP_EXTRACT(s, p)", "select regexp_extract(?, ?, ?)", (text, pattern, group)),
            ("REGEXP_EXTRACT_ALL(s, p)", "select regexp_extract_all(?, ?, ?)", (text, pattern, group)),
            ("REGEXP_REPLACE(s, p, '<\\\\0>')", "select regexp_replace(?, ?, '<\\0>', 'g')", (text, pattern)),
        )
        for sql, duck_sql, params in checks:
            kind2, got2 = _outcome(f"SELECT {sql} FROM t", db)
            if kind2 != "ok" or (got2 is None and sql.startswith("REGEXP_EXTRACT(")):
                continue  # declined, or no match (RE2's wrappers answer "" where BigQuery answers NULL)
            want = con.execute(duck_sql, list(params)).fetchone()[0]
            assert (list(got2) if isinstance(got2, tuple) else got2) == want, (sql, pattern, text, got2, want)
    assert answered > 150
