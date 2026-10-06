"""Integer casts must retain their SQL signedness and range semantics."""

from kumosql.uexpr import prove_bag_equivalent


def proves(left: str, right: str, *, schema=None, types=None) -> bool:
    return prove_bag_equivalent(
        left,
        right,
        schema=schema,
        types=types,
        dialect="mysql",
        exact_arithmetic=True,
        compare_names=False,
    ).proven


def test_unsigned_and_signed_remainder_casts_are_not_collapsed_to_integer_identity():
    left = "SELECT 5 % CAST(-2 AS UNSIGNED)"
    right = "SELECT 5 % CAST(-2 AS SIGNED)"

    assert not proves(left, right)


def test_narrow_integer_cast_is_not_collapsed_to_wide_integer_identity():
    left = "SELECT CAST(-2 AS SMALLINT)"
    right = "SELECT CAST(-2 AS SIGNED)"

    assert not proves(left, right)


def test_unsigned_source_cast_to_signed_integer_is_not_assumed_to_be_identity():
    schema = {"t": ["i"]}
    types = {"t": {"i": "BIGINT UNSIGNED"}}

    assert not proves(
        "SELECT CAST(i AS SIGNED) FROM t",
        "SELECT i FROM t",
        schema=schema,
        types=types,
    )


def test_known_in_range_signed_integer_casts_remain_identity():
    assert proves("SELECT CAST(-2 AS SIGNED)", "SELECT -2")
    assert proves(
        "SELECT CAST(i AS SIGNED) FROM t",
        "SELECT i FROM t",
        schema={"t": ["i"]},
        types={"t": {"i": "BIGINT"}},
    )
