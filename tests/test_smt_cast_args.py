"""Cast options must not disappear on the specialized SMT compilation path."""

import pytest
import sqlglot
from sqlglot import exp

pytest.importorskip("z3")

from kumosql.smt_equivalence import SmtStatus, Unsupported, _Compiler, prove_equivalent_smt


@pytest.mark.parametrize("fallback", ["0", "-1", "NULL"])
def test_cast_default_is_declined(fallback):
    before = f"SELECT CAST(x AS NUMBER DEFAULT {fallback} ON CONVERSION ERROR) AS x FROM t"
    after = "SELECT CAST(x AS NUMBER) AS x FROM t"
    assert sqlglot.parse_one(before, read="oracle").find(exp.Cast).args["default"] is not None
    result = prove_equivalent_smt(before, after, schema={"t": ["x"]}, dialect="oracle")
    assert result.status is SmtStatus.NOT_PROVEN
    assert "Cast.default" in result.reason


def test_ordinary_cast_keeps_its_proof():
    result = prove_equivalent_smt("SELECT CAST(x AS INT64) AS x FROM t",
                                  "SELECT CAST(x AS INT64) AS x FROM t WHERE TRUE",
                                  schema={"t": ["x"]})
    assert result.proven


def test_identical_unsupported_cast_is_still_declined():
    sql = "SELECT CAST(x AS NUMBER DEFAULT 0 ON CONVERSION ERROR) AS x FROM t"
    result = prove_equivalent_smt(sql, sql, schema={"t": ["x"]}, dialect="oracle")
    assert result.status is SmtStatus.NOT_PROVEN
    assert "Cast.default" in result.reason


def test_unknown_cast_argument_is_not_silently_compiled(monkeypatch):
    original_parse = sqlglot.parse

    def parse_with_new_option(*args, **kwargs):
        statements = original_parse(*args, **kwargs)
        for statement in statements:
            for cast in statement.find_all(exp.Cast):
                cast.set("future_option", exp.Literal.number(0))
        return statements

    monkeypatch.setattr(sqlglot, "parse", parse_with_new_option)
    with pytest.raises(Unsupported, match="Cast.future_option"):
        _Compiler({"t": ["x"]}, False).compile("SELECT CAST(x AS INT64) FROM t")
