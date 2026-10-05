"""Scalar folding: string, integer-division, constant, date, cast, LIKE and boolean-constant folds.

Rules aimed at: ``fold_string_literals``, ``fold_literal_int_div``, ``extract_to_ranges``,
``fold_casts_and_constant_cases``, ``drop_subsumed_like`` and, in ``algebraic_equivalence``, ``_fold_constants``,
``_fold_dates``, ``_fold_boolean_constants``, ``_fold_trivia``, ``_fold_identity_casts``, ``_bigquery_sugar`` and
``_lowercase_columns``. Each template either makes a fold fire or is a near miss one of its guards must decline
(NULL operands, zero and negative operands, INT64 extremes, empty strings, leap days, month ends, non-ASCII text).

A template is a SQL string or ``(sql, spec)`` where ``spec`` may set ``dialect``, ``schema`` (replacing the generated
one) and ``constraints`` (replacing the generated ones); see :func:`build`.
"""

from __future__ import annotations

import random

from rule_fuzz_gen import make_schema

from ._base import fill

# a typed schema with a nullable value column everywhere and a key: for null-guard shapes that need a column
# the schema does not declare NOT NULL
NULLABLE = {"t": {"not_null": ["id"], "keys": [["id"]]}, "u": {"not_null": ["k"], "keys": [["k"]]}}
NOT_NULL_XY = {"t": {"not_null": ["id", "x", "y"], "keys": [["id"]]}, "u": {"not_null": ["k", "w"], "keys": [["k"]]}}
# a table with TIMESTAMP and DATETIME columns, for EXTRACT ranges over them
TIMES = {
    "schema": {"e": [["id", "INT64"], ["ts", "TIMESTAMP"], ["dt", "DATETIME"], ["d", "DATE"]], "t": [["id", "INT64"], ["x", "INT64"]]},
    "constraints": {"e": {"not_null": ["id"], "keys": [["id"]]}, "t": {"not_null": ["id"], "keys": [["id"]]}},
}


def build(templates: list, seed: int, count: int, source: str) -> list[dict]:
    """``count`` cases cycling through ``templates``; each choice group is drawn at random.

    ``make_schema`` supplies the schema and constraints unless the template's ``spec`` replaces them.
    """

    rng = random.Random(seed)
    cases = []
    for index in range(count):
        item = templates[index % len(templates)]
        sql, spec = (item, {}) if isinstance(item, str) else item
        schema, constraints = make_schema(rng)
        cases.append(
            {
                "sql": fill(sql, rng),
                "dialect": fill(spec.get("dialect", "bigquery"), rng),
                "schema": spec.get("schema", schema),
                "constraints": spec.get("constraints", constraints),
                "options": {},
                "source": f"{source}:{seed}:{index % len(templates)}:{index}",
            }
        )
    return cases


STRINGS = [
    # case maps over literals: ASCII folds, non-ASCII text (and its special casings) must not
    "SELECT {UPPER|LOWER}({'abc'|'Mixed Case'|''|'a1 b2'|'ÀÉ'|'straße'|'İ'|'ΑΣ'}) AS a, t.id FROM t",
    "SELECT t.id FROM t WHERE t.s = {UPPER|LOWER}({'a'|'A'|'ab'|''}) {AND t.x > 0|OR t.s IS NULL|}",
    "SELECT {UPPER|LOWER}(UPPER({'aB'|'Ab'|'x'})) AS a, LOWER(UPPER(t.s)) AS b, UPPER(UPPER(t.s)) AS c, LOWER(LOWER(t.s)) AS d FROM t",
    # SUBSTR of a literal: start at 1 or later with a non-empty result folds; start 0 or negative, empty result, NULL do not
    "SELECT SUBSTR({'table'|'abc'|''|'x'}, {1|2|3|0|-1|-2|6|10}{, 2|, 0|, 10|, 1|}) AS a FROM t",
    "SELECT t.id FROM t WHERE SUBSTR('table', {1|2|3}, {1|2|3}) = {'t'|'ta'|'tab'|'a'|'abl'|'b'|''} {AND t.x > 0|}",
    "SELECT t.id FROM t WHERE t.s = SUBSTR({'aab'|'abc'|'a'}, {1|2|3}){, 1|}",
    "SELECT SUBSTR(NULL, 1, 2) AS a, SUBSTR('abc', NULL, 2) AS b, SUBSTR('abc', 2, NULL) AS c FROM t",
    # CONCAT / || of literals: NULL operands and a column operand keep the call
    "SELECT CONCAT({'a'|''|'ab'}, {'b'|''|' '|'ab'}{, 'c'|, ''|}) AS a, t.id FROM t",
    "SELECT CONCAT({'a'|''}, {NULL|t.s|CAST(t.x AS STRING)|'b'}) AS a FROM t",
    "SELECT {'a'|''} || {'b'|''|' '} AS a, t.s || {'x'|''} AS b, {'a'|''} || t.s || {'z'|''} AS c FROM t",
    "SELECT t.id FROM t WHERE CONCAT('a', 'b') = {'ab'|'AB'|'a'} {AND|OR} CONCAT(t.s, '') = t.s",
    "SELECT CONCAT(CONCAT(t.s, {'a'|''}), {'b'|t.s}) AS a, CONCAT({'a'|t.s}, CONCAT({'b'|t.s}, 'c'), t.s) AS b FROM t",
    # equality and inequality of two literals: exact comparison, so case and trailing blanks matter
    "SELECT t.id FROM t WHERE {'a'|'A'|'a '|''|' '|'é'|'e'|'ab'} {=|<>} {'a'|'A'|'a '|''|'é'|'ab'} {AND t.x > 0|OR t.x > 0|}",
    "SELECT t.id, {'a'|''|'é'} {=|<>} {'a'|'A'|''|'é'} AS e FROM t",
    "SELECT CASE WHEN {'a'|'b'} = 'a' THEN t.x ELSE t.y END AS c, IF('x' <> 'x', 1, 2) AS d FROM t",
    "SELECT t.id FROM t WHERE {UPPER|LOWER}('Ab') = {'AB'|'ab'|'Ab'} AND CONCAT('a', 'b') <> 'ab'",
]

INT_DIV = [
    "SELECT DIV({10|0|7|1|100|-10|-7|9223372036854775807|9223372036854775806|4611686018427387904}, {3|2|1|7|9223372036854775807|-3|-1|0}) AS q, t.id FROM t",
    "SELECT DIV(DIV({20|7|-20}, {2|3}), {5|1|0}) AS q, DIV(({20|-20}), {3|-3}) AS r FROM t",
    "SELECT t.id FROM t WHERE DIV(t.x, {2|3|-2}) = DIV({10|7|-7}, {3|2|-2}) {AND t.x > 0|}",
    "SELECT DIV({10|-10}, t.x) AS a, DIV(t.x, 0) AS b, DIV(t.x, {2|3}) + DIV(10, 4) AS c FROM t",
    "SELECT DIV(CAST({10|7} AS NUMERIC), {3|2}) AS a, DIV({10.5|10}, 2) AS b, DIV('10', 2) AS c FROM t",
    ("SELECT {10|7|0|-10|-7} DIV {3|2|-3|1} AS q, t.x DIV {2|3} AS r, 7 / 2 AS d, {10|-10} % 3 AS m FROM t", {"dialect": "mysql"}),
    ("SELECT t.id FROM t WHERE t.x = {10|9|-3} DIV {3|2|-3}", {"dialect": "mysql"}),
]

CONSTANTS = [
    "SELECT t.id FROM t WHERE t.x = {1 + 2|10 / 2|10 / 4|7 - 10|2 * 3|0 / 5|-7 / 7|3 * 0|10 / 0|(2 + 3) * 4|2 - -3|100 / 5 / 2|1 + 1 + 1}",
    "SELECT {9007199254740992 - 1|9007199254740993 - 1|9007199254740991 + 1|9007199254740992 + 1|4503599627370496 * 2|9223372036854775807 / 1|-9223372036854775807 - 1|3037000500 * 3037000500|9223372036854775807 + 0} AS a, t.id FROM t",
    "SELECT t.id FROM t WHERE {1|2|-1|0} {<|<=|=|<>|>|>=} {1|2|-1|0}",
    "SELECT t.id FROM t WHERE t.f {=|<|>=} {0.10|1.00|0.20|100.0|1.0|0.0|2.50|0.30|0.1 + 0.2}",
    "SELECT t.id, t.f * {1.50|2.0|0.10} AS a, {1.00|0.50} + t.f AS b FROM t",
    "SELECT CASE WHEN {TRUE|FALSE|1 = 1|1 > 2|NULL} THEN t.x {ELSE t.y|} END AS a, CASE WHEN TRUE THEN 1 ELSE 2 END AS b, CASE WHEN FALSE THEN 1 END AS c FROM t",
    "SELECT CASE WHEN t.y > 0 THEN 1 WHEN {TRUE|FALSE|1 < 2|NOT TRUE} THEN 2 WHEN t.y < 0 THEN 3 ELSE 4 END AS a FROM t",
    "SELECT t.id FROM t WHERE t.x {=|>} {1 + 1|2 * 1|2 - 0} {AND|OR} t.y {<|=} {3 - 1|4 / 2|6 / 4}",
    "SELECT t.id FROM t WHERE t.x * 1 = {2 * 3|6 / 3} AND t.y + 0 > {4 - 4|2 - 3}",
]

DATES = [
    "SELECT t.id FROM t WHERE t.d {>=|<|=|>|<=} {DATE '2024-01-31' + INTERVAL 1 MONTH|DATE '2024-03-31' - INTERVAL 1 MONTH|DATE '2024-02-29' + INTERVAL 1 YEAR|DATE '2023-12-31' + INTERVAL 1 DAY|DATE '2024-03-01' - INTERVAL 1 DAY|DATE '2024-01-01' + INTERVAL 6 WEEK|DATE '2024-02-28' + INTERVAL 2 DAY}",
    "SELECT t.id FROM t WHERE t.d {>=|<|=|>|<=} {DATE_ADD(DATE '2024-01-31', INTERVAL 1 MONTH)|DATE_ADD(DATE '2024-02-29', INTERVAL 1 YEAR)|DATE_SUB(DATE '2024-03-31', INTERVAL 1 MONTH)|DATE_ADD(DATE '2023-12-31', INTERVAL 1 DAY)|DATE_SUB(DATE '2024-03-01', INTERVAL 1 DAY)|DATE_ADD(DATE '2024-01-01', INTERVAL 5 WEEK)|DATE_ADD(DATE '2024-02-29', INTERVAL -1 YEAR)|DATE_SUB(DATE '2024-02-29', INTERVAL 4 YEAR)|DATE_ADD(DATE '2024-01-31', INTERVAL 1 QUARTER)|DATE_ADD(DATE '2024-01-31', INTERVAL 12 MONTH)}",
    "SELECT DATE_ADD(DATE '2024-01-31', INTERVAL {1|-1|13|0} MONTH) AS a, DATE_SUB(DATE '2024-03-31', INTERVAL {1|2|12} MONTH) AS b, DATE '2024-12-31' + INTERVAL 1 DAY AS c, t.id FROM t",
    "SELECT t.id FROM t WHERE t.d {=|>=|<} {DATE '2024-1-5'|DATE('2024-01-31')|CAST('2024-01-31' AS DATE)|DATE '2024-02-29'|DATE '2023-12-31'|CAST('2024-3-1' AS DATE)}",
    # boundaries of the calendar and invalid dates (errors on both sides are unchecked)
    "SELECT {DATE '0001-01-01'|DATE '9999-12-31'|DATE '2023-02-29'|DATE '2024-02-30'|DATE '2024-13-01'} {+ INTERVAL 1 DAY|- INTERVAL 1 DAY|+ INTERVAL 1 YEAR|- INTERVAL 1 MONTH} AS a FROM t",
    "SELECT DATE_ADD(DATE '2024-01-01', INTERVAL {99999999|3652059|-3652059|1000000000} DAY) AS a FROM t",
    "SELECT t.id FROM t WHERE t.d BETWEEN DATE_SUB(DATE '2024-03-31', INTERVAL 1 MONTH) AND DATE_ADD(DATE '2024-01-31', INTERVAL 1 MONTH)",
    "SELECT t.id FROM t WHERE DATE_ADD(t.d, INTERVAL 1 DAY) {>|=|<=} DATE_ADD(DATE '2024-02-28', INTERVAL 1 DAY)",
]

RANGES = [
    "SELECT t.id FROM t WHERE EXTRACT(YEAR FROM t.d) = {2024|2023|2025|1|0|9998|9999|02024}",
    "SELECT t.id FROM t WHERE EXTRACT(YEAR FROM t.d) = {2024|2023} AND EXTRACT(MONTH FROM t.d) = {1|2|3|12|0|13}",
    "SELECT t.id FROM t WHERE {2024|2023} = EXTRACT(YEAR FROM t.d) AND {2|1|12} = EXTRACT(MONTH FROM t.d) {AND t.x > 0|}",
    "SELECT t.id FROM t WHERE EXTRACT(YEAR FROM t.d) = 2024 AND EXTRACT(YEAR FROM t.d) = {2023|2024}",
    "SELECT t.id FROM t WHERE EXTRACT(MONTH FROM t.d) = {1|2} AND EXTRACT(MONTH FROM t.d) = {2|3} AND EXTRACT(YEAR FROM t.d) = 2024",
    "SELECT t.id FROM t WHERE EXTRACT(YEAR FROM t.d) = 2024 {OR t.x > 0|AND (t.x > 0 OR t.y > 0)|AND NOT (t.x > 0)|AND t.d IS NOT NULL}",
    "SELECT t.id FROM t WHERE NOT (EXTRACT(YEAR FROM t.d) = 2024 AND EXTRACT(MONTH FROM t.d) = {1|2})",
    "SELECT t.id FROM t WHERE (EXTRACT(YEAR FROM t.d) = 2024 AND EXTRACT(MONTH FROM t.d) = {1|2|12}) OR t.x > 0",
    "SELECT t.id FROM t WHERE EXTRACT(YEAR FROM t.d) = 2024 AND EXTRACT(DAY FROM t.d) = {29|1|31}",
    "SELECT t.id FROM t WHERE EXTRACT(YEAR FROM t.d) {<>|<|>=} 2024 AND EXTRACT(MONTH FROM t.d) = 2",
    "SELECT t.id FROM t WHERE EXTRACT(YEAR FROM t.d) = {2024.0|2024.5|-2024} AND EXTRACT(MONTH FROM t.d) = 1",
    "SELECT t.d, COUNT(*) AS c FROM t GROUP BY t.d HAVING EXTRACT(YEAR FROM t.d) = 2024 {AND EXTRACT(MONTH FROM t.d) = 2|AND COUNT(*) > 1|}",
    "SELECT t.id FROM t WHERE t.id IN (SELECT t2.id FROM t AS t2 WHERE EXTRACT(YEAR FROM t2.d) = 2024 AND EXTRACT(MONTH FROM t2.d) = {1|2}) AND t.x > 0",
    "SELECT t.id FROM t WHERE EXTRACT(YEAR FROM DATE_ADD(t.d, INTERVAL 1 DAY)) = 2024 AND EXTRACT(MONTH FROM t.d) = {1|2}",
    "SELECT t.id FROM t WHERE EXTRACT(YEAR FROM t.d) = 2024 AND EXTRACT(YEAR FROM t.d) = 2024 AND EXTRACT(MONTH FROM t.d) = 2",
    ("SELECT e.id FROM e WHERE EXTRACT(YEAR FROM e.{ts|dt|d}) = {2024|2023} {AND EXTRACT(MONTH FROM e.ts) = 2|AND EXTRACT(MONTH FROM e.dt) = 1|}", TIMES),
    ("SELECT e.id FROM e WHERE EXTRACT(YEAR FROM e.ts) = 2024 AND EXTRACT(MONTH FROM e.ts) = {1|2} AND EXTRACT(YEAR FROM e.d) = 2024", TIMES),
]

CASTS = [
    # identity casts: an INT64-valued expression cast to an integer type, a DATE to DATE, a BOOL to BOOL
    "SELECT CAST(t.x AS {INT64|SMALLINT|INTEGER|BIGINT}) AS a, CAST(t.d AS DATE) AS b, CAST(t.x > 0 AS BOOL) AS c FROM t",
    "SELECT CAST(t.x + {1|t.y} AS INT64) AS a, CAST(t.x * {2|t.y} AS INT64) AS b, CAST(-t.x AS INT64) AS c, CAST(DIV(t.x, 2) AS INT64) AS d FROM t",
    "SELECT CAST(SUM(t.x) AS INT64) AS a, CAST(MIN(t.x) AS INT64) AS b, CAST(MAX(t.y) AS INT64) AS c, CAST(COUNT(*) AS INT64) AS d FROM t",
    "SELECT t.y, CAST(SUM(t.x) AS INT64) AS a, CAST(COUNT(t.x) AS INT64) AS b FROM t GROUP BY t.y",
    "SELECT CAST(COALESCE(t.x, 0) AS INT64) AS a, CAST(CASE WHEN t.y > 0 THEN t.x ELSE 1 END AS INT64) AS b, CAST(IFNULL(t.x, t.y) AS INT64) AS c FROM t",
    "SELECT t.id FROM t WHERE CAST(t.x AS INT64) {=|<|>} CAST(t.y AS INT64) AND CAST(t.d AS DATE) {>|<=} DATE '2024-01-31'",
    "SELECT t.id FROM t ORDER BY CAST(t.x AS INT64), CAST(t.y AS FLOAT64), t.id",
    "SELECT t.id FROM t WHERE CAST(t.x AS FLOAT64) IS {NOT |}NULL AND CAST(t.y AS NUMERIC) IS NOT NULL",
    "SELECT t.id FROM t ORDER BY CAST({1|7|2147483647|2147483648|9007199254740993} AS {FLOAT64|NUMERIC|INT64}), t.id",
    "SELECT d.a FROM (SELECT CAST(t.x AS INT64) AS a, t.id FROM t) AS d WHERE CAST(d.a AS INT64) {>|=} 0",
    # casts that change the value: floats, strings, booleans, numerics, narrower targets
    "SELECT CAST(t.f AS {INT64|NUMERIC|STRING}) AS a, CAST(t.s AS {INT64|FLOAT64}) AS b, CAST(t.x > 0 AS INT64) AS c, CAST(t.x AS {STRING|FLOAT64|NUMERIC|BOOL}) AS d FROM t",
    "SELECT CAST(CAST(t.x AS {STRING|NUMERIC|FLOAT64}) AS {STRING|NUMERIC|FLOAT64|INT64}) AS a, CAST(CAST(t.f AS INT64) AS INT64) AS b, CAST(CAST(t.x AS INT64) AS FLOAT64) AS c FROM t",
    # casts of literals: integers that fit, integer strings, DECIMAL(p, s) that holds the literal exactly
    "SELECT CAST({0|5|12|123456789012|9223372036854775807|9223372036854775808|-5|-0} AS {INT64|NUMERIC|FLOAT64|STRING}) AS a FROM t",
    "SELECT CAST({'12'|'012'|'-5'|'+5'|' 5'|'1.5'|'abc'|''|'9223372036854775807'|'9223372036854775808'|'-0'|'-9223372036854775808'|'1e3'|'00'} AS INT64) AS a FROM t",
    "SELECT CAST({5|0|5.5|5.55|1.5|-1.5|123456789.5|0.05|100|99.99} AS NUMERIC({11, 1|3, 1|2, 1|5, 2|38, 9|4, 0})) AS a FROM t",
    "SELECT t.id FROM t WHERE CAST({5|-5|10} AS NUMERIC(11, 1)) {=|<} t.x AND CAST('12' AS INT64) > t.y",
    # IS NULL of a literal, NOT of a boolean literal, CASE with constant conditions
    "SELECT t.id FROM t WHERE {1|'a'|''|NULL|TRUE|FALSE|-1|(NULL)|(1)|1.5} IS {NOT |}NULL {AND|OR} t.x > 0",
    "SELECT CASE WHEN {NULL|1|'a'} IS NULL THEN t.x ELSE t.y END AS a, CASE WHEN (NULL IS NULL) AND t.x > 0 THEN 1 END AS b FROM t",
    "SELECT t.id FROM t WHERE {NOT TRUE|NOT FALSE|NOT (NOT TRUE)} {OR|AND} t.x > 0",
    "SELECT CASE WHEN {FALSE|TRUE|NOT FALSE|NOT TRUE|NULL} THEN t.x WHEN t.y > 0 THEN 2 ELSE 3 END AS a, CASE WHEN t.y > 0 THEN 1 WHEN {TRUE|FALSE} THEN 2 WHEN FALSE THEN 3 ELSE 4 END AS b FROM t",
    "SELECT CASE WHEN FALSE THEN 1 END AS a, CASE WHEN FALSE THEN 1 WHEN FALSE THEN 2 END AS b, CASE WHEN t.x > 0 THEN 1 WHEN TRUE THEN 2 END AS c FROM t",
    "SELECT CASE t.x WHEN 1 THEN 'a' WHEN NULL THEN 'n' END AS a, CASE WHEN t.x IS NULL THEN 'n' WHEN t.x = 1 THEN 'a' ELSE 'z' END AS b FROM t",
    # COUNT strict comparison: COUNT(x) > 1 is COUNT(x) >= 2, the integer literal on either side
    "SELECT t.y, COUNT({*|t.x}) AS c FROM t GROUP BY t.y HAVING COUNT({*|t.x}) {>|<|>=|<=|=} {1|0|-1|2|5}",
    "SELECT t.y FROM t GROUP BY t.y HAVING {1|2|0|-1} {<|>} COUNT({*|t.x}) {AND t.y > 0|}",
    "SELECT t.y FROM t GROUP BY t.y HAVING COUNT(*) {>|<} 1.5 OR COUNT(DISTINCT t.x) {>|<} 1 OR SUM(t.x) {>|<} 1 OR COUNT(*) > t.y",
    "SELECT COUNT(*) {>|<} {1|0|9223372036854775807} AS m, COUNT(t.x) {>|<} {0|2} AS n FROM t",
    "SELECT t.id FROM t WHERE (SELECT COUNT(*) FROM u WHERE u.k = t.y) {>|<} {0|1|2}",
    "SELECT t.id FROM t WHERE t.id IN (SELECT u.k FROM u GROUP BY u.k HAVING COUNT(*) {>|<} {1|2})",
    # ROUND(x) is ROUND(x, 0); 1 * x is x only where x is already a number
    "SELECT ROUND(t.f) AS a, ROUND(t.x) AS b, ROUND(t.f, {1|0|-1}) AS c, ROUND(AVG(t.x)) AS d FROM t",
    "SELECT {1|1.0} * COUNT(*) AS a, 1 * SUM(t.x) AS b, 1 * AVG(t.x) AS c, 1 * t.x AS d, t.x * 1 AS e, 1 * t.f AS f FROM t",
    "SELECT t.x * {1|1.0} + t.y AS a, t.x * 1.0 / 2 AS b, (t.x * 1) * 1 AS c, 1 * (t.x + t.y) AS d, 2 * 1 AS e, t.f * 1 - t.f AS g FROM t",
    "SELECT SUM({1|1.0} * t.x) AS a, AVG(t.x * {1|1.0}) AS b, SUM(DISTINCT t.x * 1) AS c, COUNT(t.x * 1) AS d, MIN(t.x * 1) AS e FROM t",
    "SELECT t.id FROM t WHERE t.x * {1|1.0} {=|<} t.y AND t.y * 1 >= {0|1} * t.id",
    "SELECT t.id FROM t WHERE t.x * 1.0 {=|<} t.f",
]

LIKES = [
    "SELECT t.id FROM t WHERE t.s LIKE '{a|ab|abc|}%' {OR|AND} t.s LIKE '{a|ab|abc|b|}{%|}'",
    "SELECT t.id FROM t WHERE t.s LIKE '{a|ab|}%' {OR|AND} t.s LIKE '{a|ab|abc}%' {OR|AND} t.s LIKE '{a|abc|}%'",
    "SELECT t.id FROM t WHERE t.s LIKE '%' {OR|AND} t.s LIKE '{a|}{%|}'",
    "SELECT t.id FROM t WHERE t.s LIKE 'a%' {OR|AND} t.s LIKE 'ab%' {OR|AND} t.x > {0|1}",
    "SELECT t.id FROM t WHERE (t.s LIKE 'a%' OR t.s LIKE 'ab%') AND (t.s LIKE 'a%' AND t.s LIKE 'ab%' OR t.x > 0)",
    "SELECT t.id FROM t WHERE t.s LIKE 'a%' {OR|AND} t.s LIKE 'a%'",
    "SELECT t.id FROM t WHERE t.s LIKE '{a|a_|%a|a%b|%a%}' {OR|AND} t.s LIKE '{ab|ab%|a}'",
    "SELECT t.id FROM t WHERE t.s NOT LIKE 'a%' {OR|AND} t.s LIKE 'ab%'",
    "SELECT t.id FROM t WHERE t.s LIKE 'a%' {OR|AND} NOT t.s LIKE 'ab%'",
    "SELECT t.id FROM t JOIN u ON u.k = t.y WHERE t.s LIKE 'a%' {OR|AND} u.v LIKE 'ab%'",
    "SELECT t.id FROM t JOIN u ON u.k = t.y WHERE t.s LIKE 'a%' {OR|AND} t.s LIKE 'ab%' {OR|AND} u.v LIKE 'a%' {OR|AND} u.v LIKE 'ab%'",
    "SELECT t.id, t.s LIKE 'a%' {OR|AND} t.s LIKE 'ab%' AS m, CASE WHEN t.s LIKE 'b%' OR t.s LIKE 'bb%' THEN 1 END AS n FROM t",
    "SELECT t.id FROM t WHERE t.s LIKE 'a%' OR t.s LIKE 'ab%' OR t.s IS NULL",
    "SELECT t.id FROM t WHERE EXISTS (SELECT 1 FROM u WHERE u.v LIKE 'a%' {OR|AND} u.v LIKE 'ab%' {OR|AND} u.k = t.y)",
    "SELECT t.id FROM t WHERE t.s LIKE 'a%' AND t.s LIKE 'ab%' AND t.s LIKE 'abc%' AND t.s IS NOT NULL",
    ("SELECT t.id FROM t WHERE t.s LIKE 'a%' {ESCAPE '!'|} {OR|AND} t.s LIKE 'ab%'", {"dialect": "mysql"}),
    ("SELECT t.id FROM t WHERE t.s ILIKE 'a%' {OR|AND} t.s LIKE 'ab%' {OR|AND} t.s ILIKE 'ab%'", {"dialect": "duckdb"}),
    ("SELECT t.id FROM t WHERE t.s LIKE 'a%' {OR|AND} t.s LIKE 'ab%'", {"dialect": "{mysql|postgres|duckdb}"}),
]

SUGAR = [
    "SELECT COUNTIF(t.x > 0) AS a, COUNTIF(t.x IS NULL) AS b, COUNTIF(FALSE) AS c FROM t",
    "SELECT t.y, COUNTIF(t.x {>|<|=} {0|1}) AS a FROM t GROUP BY t.y",
    "SELECT SAFE_DIVIDE(t.x, {t.y|0|t.f|t.x - t.x|2|NULL}) AS a, SAFE_DIVIDE({1|t.x|NULL}, {t.y|0}) AS b FROM t",
    "SELECT t.id FROM t WHERE SAFE_DIVIDE(t.x, t.y) {>|=|<} {0|1|0.5}",
    "SELECT STARTS_WITH(t.s, {'a'|''|'ab'|'a%'|'a_'|'A'}) AS a, ENDS_WITH(t.s, {'a'|''|'b'|'%a'|'_a'}) AS b FROM t",
    "SELECT t.id FROM t WHERE {STARTS_WITH|ENDS_WITH}(t.s, {'a'|'b'|' '|''}) {AND|OR} t.x > 0",
    "SELECT t.id FROM t WHERE STARTS_WITH(t.s, t.s) AND ENDS_WITH(t.s, CONCAT('a', ''))",
    "SELECT LOWER(TRIM(t.s)) AS a, UPPER(TRIM(t.s)) AS b, LOWER(TRIM(t.s, 'a')) AS c, TRIM(LOWER(t.s)) AS d, LOWER(LTRIM(t.s)) AS e FROM t",
    "SELECT t.s || {'a'|''|t.s} AS a, CONCAT(t.s, {'a'|''|t.s}) AS b, t.s || NULL AS c FROM t",
    "SELECT t.id FROM t WHERE t.s || {'a'|''} = {'aa'|'a'|''} {AND|OR} (t.s || t.s) = CONCAT(t.s, t.s)",
]

LOWERCASE = [
    "SELECT T.ID, t.X, t.Y FROM t WHERE T.Y > 0 AND t.S LIKE 'a%'",
    "SELECT t.x AS Foo FROM t WHERE t.X > 0 ORDER BY FOO",
    "SELECT d.FOO FROM (SELECT t.x AS Foo FROM t) AS D WHERE d.foo > 0 AND D.Foo < 3",
    "SELECT t.ID FROM t WHERE t.id = (SELECT MAX(U.K) FROM u WHERE U.k = T.Y)",
    "SELECT t.Y, COUNT(*) AS C FROM t GROUP BY t.y HAVING COUNT(*) > 0 ORDER BY c",
    "SELECT X FROM (SELECT t.x AS x, t.id AS Id FROM t) AS d WHERE ID > 0",
    "SELECT `T`.`X` FROM t WHERE `T`.`Id` > 0",
]

TRIVIA = [
    "SELECT t.id FROM t WHERE t.x = (SELECT {1|0|-1}) AND t.s = (SELECT {'a'|''}) AND t.b IS NULL",
    "SELECT t.id FROM t WHERE t.x = (SELECT {NULL|1 AS a FROM u|1 + 1|t.y|MAX(1)})",
    "SELECT t.id FROM t WHERE (SELECT {TRUE|1}) = {TRUE|1} AND t.x > 0",
    "SELECT t.id FROM t WHERE t.x {IN|NOT IN} (SELECT u.w FROM u WHERE {FALSE|1 = 2|FALSE AND u.k > 0|u.k > 0 AND FALSE|NULL})",
    "SELECT t.id FROM t WHERE t.x {IN|NOT IN} (SELECT u.w FROM u LIMIT 0)",
    "SELECT t.id FROM t WHERE {EXISTS|NOT EXISTS} (SELECT 1 FROM u WHERE {FALSE|u.k = t.y AND FALSE})",
    "SELECT t.id FROM t WHERE {EXISTS|NOT EXISTS} (SELECT {COUNT(*)|MAX(u.w)|u.k} FROM u WHERE FALSE {GROUP BY u.k|})",
    "SELECT t.id FROM t WHERE t.x {IN|NOT IN} (SELECT {MAX|MIN|COUNT}(u.w) FROM u WHERE FALSE)",
    "SELECT t.id FROM t WHERE t.x IN (SELECT u.w FROM u WHERE FALSE LIMIT 5) OR t.y IN (SELECT u.k FROM u LIMIT 0 OFFSET 1)",
    "SELECT t.id FROM t WHERE NOT EXISTS (SELECT 1 FROM u LIMIT 0) AND t.x IN (SELECT u.w FROM u UNION ALL SELECT u.k FROM u WHERE FALSE)",
    "SELECT 1 AS a, 'x' AS b FROM t GROUP BY TRUE",
    "SELECT {1|NULL|'x'} AS a, 2 AS b FROM t {WHERE t.x > 0|WHERE FALSE|} GROUP BY {TRUE|FALSE}",
    "SELECT 1 AS a, COUNT(*) AS c FROM t GROUP BY TRUE",
    "SELECT 1 AS a FROM t GROUP BY TRUE HAVING COUNT(*) > 1",
    "SELECT DISTINCT 1 AS a FROM t GROUP BY TRUE",
    ("SELECT {COUNT|SUM|MIN|MAX|AVG}({t.x|DISTINCT t.x|*}) FILTER (WHERE {t.y > 0|t.y IS NULL|t.y = 1|t.y > 1}) AS a FROM t", {"dialect": "duckdb"}),
    ("SELECT t.y, SUM(t.x) FILTER (WHERE t.y > 0) AS a, COUNT(*) FILTER (WHERE FALSE) AS b, MAX(t.x) FILTER (WHERE FALSE) AS c FROM t GROUP BY t.y", {"dialect": "duckdb"}),
    ("SELECT t.id FROM t WHERE {YEAR|MONTH|DAY}(t.d) = {2024|1|2|29} {AND t.x > 0|}", {"dialect": "mysql"}),
    ("SELECT {YEAR|MONTH|DAY}(DATE('2024-02-29')) AS a, {YEAR|MONTH}(t.d) AS b FROM t", {"dialect": "mysql"}),
    "SELECT t.id FROM t WHERE CONCAT(CONCAT(t.s, 'a'), 'b') = 'ab' AND UPPER(UPPER(t.s)) = 'A'",
]

BOOLEANS = [
    "SELECT t.id FROM t WHERE {(t.x IS NULL) IS NULL|(t.x IS NOT NULL) IS NULL|NOT ((t.x IS NULL) IS NULL)|(t.x IS NULL) IS NOT NULL|(NOT t.x IS NULL) IS NULL|(t.x > 0) IS NULL|(t.x IS NULL OR t.y IS NULL) IS NULL}",
    "SELECT t.id, (t.x IS NULL) IS NULL AS a, (t.x = t.y) IS NULL AS b FROM t",
    "SELECT t.id FROM t WHERE {TRUE|FALSE|NULL|t.x > 0} {OR|AND} {t.y > 0|t.x IS NULL|t.y = NULL} {OR|AND} {TRUE|FALSE|NULL}",
    "SELECT t.id FROM t WHERE (TRUE OR t.x > 0) AND (FALSE AND t.y > 0 OR t.y IS NULL)",
    "SELECT t.id, TRUE OR t.x > 0 AS a, FALSE AND t.x > 0 AS b, NULL AND FALSE AS c, NULL OR TRUE AS d, t.x > 0 OR TRUE AS e FROM t",
    "SELECT t.id FROM t WHERE CAST(t.x IS NULL AS INT64) {>|>=|<|<=|=|<>} {2|-1|0|1|5|3}",
    "SELECT t.id FROM t WHERE {2|-1|0|1|5} {>|>=|<|<=|=|<>} CAST(t.x IS NULL AS INT64) {AND|OR} t.y > 0",
    "SELECT t.id FROM t WHERE (CAST(t.x IS NOT NULL AS {INT64|BOOL|STRING}) {>|=} 2) OR CAST(t.y > 0 AS INT64) = 5",
    "SELECT CAST(NULL AS {INT64|STRING|FLOAT64|DATE|BOOL|NUMERIC}) AS a, COALESCE(CAST(NULL AS STRING), 'x') AS b, IF(CAST(NULL AS BOOL), 1, 2) AS c FROM t",
    "SELECT t.id FROM t WHERE {CAST(NULL AS INT64) = 0|CAST(NULL AS INT64) = t.x|CAST(NULL AS INT64) < NULL|CAST(NULL AS INT64) IS NULL|CAST(NULL AS INT64) IS NOT NULL} OR t.x IN (CAST(NULL AS INT64), 1)",
    "SELECT CAST(NULL AS INT64) AS a, 1 AS b UNION ALL SELECT t.x, t.y FROM t",
    "SELECT t.id FROM t WHERE TIMESTAMP(NULL) IS NULL AND t.x > 0",
]

TEMPLATES = STRINGS + INT_DIV + CONSTANTS + DATES + RANGES + CASTS + LIKES + SUGAR + LOWERCASE + TRIVIA + BOOLEANS


def cases(seed: int, count: int) -> list[dict]:
    return build(TEMPLATES, seed, count, "folding")
