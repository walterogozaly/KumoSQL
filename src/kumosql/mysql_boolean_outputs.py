"""Spell MySQL's predicate output values explicitly as 1, 0 and NULL."""

from sqlglot import exp

INTEGER_TYPES = {
    exp.DataType.Type.INT,
    exp.DataType.Type.BIGINT,
    exp.DataType.Type.SMALLINT,
    exp.DataType.Type.TINYINT,
}


def boolean_value(node):
    while isinstance(node, exp.Paren):
        node = node.this
    if isinstance(
        node, (exp.Boolean, exp.Predicate, exp.And, exp.Or, exp.Not, exp.Exists)
    ):
        return True
    if isinstance(node, exp.Case):
        values = [i.args.get("true") for i in node.args.get("ifs") or []] + [
            node.args.get("default")
        ]
        return (
            node.this is None
            and any(v is not None and not isinstance(v, exp.Null) for v in values)
            and all(
                v is None or isinstance(v, exp.Null) or boolean_value(v) for v in values
            )
        )
    return False


def canonical_mysql_boolean_outputs(
    tree: exp.Expression, dialect: str
) -> exp.Expression:
    from .counted_membership import _unstable

    if dialect != "mysql" or not isinstance(tree, exp.Select) or _unstable(tree):
        return tree
    for item in tree.expressions:
        value = item.unalias()
        operand = value
        if (
            isinstance(value, exp.Cast)
            and not isinstance(value, exp.TryCast)
            and value.to.this in INTEGER_TYPES
            and not value.to.expressions
        ):
            operand = value.this
        if not boolean_value(operand):
            continue
        # MySQL predicate values already belong to {1,0,NULL}; integer CAST
        # preserves that domain. Other casts and arbitrary BOOLEAN/TINYINT
        # columns are deliberately not classified as Boolean values.
        plain = operand.unnest() if isinstance(operand, exp.Paren) else operand
        branches = [exp.If(this=operand.copy(), true=exp.Literal.number(1))]
        never_null = isinstance(
            plain, (exp.Exists, exp.Is, exp.NullSafeEQ, exp.NullSafeNEQ, exp.Boolean)
        )
        if isinstance(plain, exp.Not):
            inner = plain.this.unnest()
            never_null = isinstance(inner, (exp.Exists, exp.Is, exp.NullSafeEQ, exp.NullSafeNEQ, exp.Boolean))
        if not never_null:
            branches.append(
                exp.If(
                    this=exp.Not(this=exp.Paren(this=operand.copy())),
                    true=exp.Literal.number(0),
                )
            )
        replacement = exp.Case(
            ifs=branches, default=exp.Literal.number(0) if never_null else exp.Null()
        )
        if isinstance(item, exp.Alias):
            item.set("this", replacement)
        else:
            item.replace(replacement)
    return tree
