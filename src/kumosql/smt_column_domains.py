"""What a declared column type says about its values, for the SMT prover outside BigQuery.

The prover reads every value as a number, a string or a Boolean and every number as a real. Without a type fact a
column declared ``INT`` can hold 10.5, so ``x > 10`` and ``x >= 11`` differ and ``x > 1 AND x < 2`` is satisfiable;
and a column declared ``BOOLEAN`` can hold the number 0, so ``b OR b`` (NULL for a number) and ``b`` differ. The
BigQuery dialect already states such facts (``_Compiler.typed_facts``); this module states the ones that hold in the
other dialects, as hypotheses on the occurrences of a proof, never as rewrites:

* **Integer columns** (``INT``, ``INTEGER``, ``BIGINT``, ``SMALLINT``, ``TINYINT``, ``MEDIUMINT``, ``INT64``): a
  non-NULL value is a number with no fraction. That makes a strict bound a closed one (``x > 10`` is ``x >= 11``), an
  open interval with no integer in it (``x > 1 AND x < 2``) empty, and ``BETWEEN 11 AND 19`` the same as ``> 10 AND < 20``,
  all by the solver, in any combination with the rest of the query. Only the *integrality* is stated, not the width: an
  integer past a declared ``INT``'s range is not excluded, so no proof depends on an overflow either way (the
  assumption ``cast_rules`` makes about widths is untouched). A value that is not an integer is still read exactly:
  ``x > 10.5`` keeps its real meaning, and a DECIMAL, FLOAT or untyped column gets no fact.
* **Boolean columns**, only when the caller passes ``boolean_columns=True``: a non-NULL value is a Boolean, so the column
  has the domain {FALSE, TRUE, NULL} and ``b = TRUE`` is ``b``, ``b <> TRUE`` is ``NOT b``, ``b OR b`` is ``b`` and
  ``b OR (b AND c)`` is ``b``. It is opt-in because in MySQL (and SQLite) a ``BOOLEAN`` column is a ``TINYINT(1)``
  that holds 2 or -3 without complaint, where ``b = TRUE`` and ``b`` really do differ; a caller that stores real booleans
  (PostgreSQL, DuckDB, CockroachDB) says so.

SQLite is left out of the integer facts: an ``INTEGER`` column there has type affinity and may hold text. Facts are stated for
NULL-free values only (``NOT null IMPLIES ...``), as in BigQuery's.
"""

from __future__ import annotations

import re


_INTEGER_TYPES = frozenset(("INT", "INTEGER", "BIGINT", "SMALLINT", "TINYINT", "MEDIUMINT", "INT64", "INT2", "INT4", "INT8"))
_BOOLEAN_TYPES = frozenset(("BOOL", "BOOLEAN"))
_NO_INTEGER_FACTS = frozenset(("sqlite",))


def _type_name(declared: str | None) -> str:
    return re.sub(r"[(<\s].*", "", declared or "").strip().upper()


def is_integer_type(declared: str | None) -> bool:
    return _type_name(declared) in _INTEGER_TYPES


def is_boolean_type(declared: str | None) -> bool:
    return _type_name(declared) in _BOOLEAN_TYPES


def facts(compiler, occs, z3, V) -> list:
    """The type facts for the columns of the table occurrences ``occs`` (empty for BigQuery, whose facts are the compiler's own)."""

    if compiler.dialect == "bigquery":
        return []
    out = []
    integers = compiler.dialect not in _NO_INTEGER_FACTS
    for occ in occs:
        declared = compiler.types.get(compiler.occ_tables.get(occ.uid, ""), {})
        for name, v in occ.cols.items():
            type_sql = declared.get(name)
            if integers and is_integer_type(type_sql):
                typed = z3.And(V.is_Num(v.val), z3.IsInt(V.num(v.val)))
            elif compiler.boolean_columns and is_boolean_type(type_sql):
                typed = V.is_Bool(v.val)
            else:
                continue
            out.append(z3.Implies(z3.Not(v.null), typed))
    return out
