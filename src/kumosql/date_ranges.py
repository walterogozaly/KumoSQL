"""``EXTRACT(YEAR FROM d) = 2014`` is the range ``d >= '2014-01-01' AND d < '2015-01-01'``.

When the same AND also fixes ``EXTRACT(MONTH FROM d)``, the pair becomes the
month's range. Only equalities with integer literals are rewritten.
"""

from __future__ import annotations

import datetime

from sqlglot import exp


def _part(node: exp.Expression) -> tuple[str, str, int] | None:
    if not isinstance(node, exp.EQ):
        return None
    for extract, value in ((node.left, node.right), (node.right, node.left)):
        if (
            isinstance(extract, exp.Extract)
            and isinstance(extract.expression, exp.Column)
            and isinstance(value, exp.Literal)
            and not value.is_string
            and value.name.isdigit()
        ):
            unit = extract.this.name.upper()
            if unit in ("YEAR", "MONTH"):
                return unit, extract.expression.sql(), int(value.name)
    return None


def _conjuncts(node: exp.Expression) -> list[exp.Expression]:
    if isinstance(node, exp.And):
        return _conjuncts(node.left) + _conjuncts(node.right)
    if isinstance(node, exp.Paren) and isinstance(node.this, exp.And):
        return _conjuncts(node.this)
    return [node.unnest() if isinstance(node, exp.Paren) else node]


def _range(column: exp.Expression, start: datetime.date, end: datetime.date) -> exp.Expression:
    return exp.and_(
        exp.GTE(this=column.copy(), expression=exp.cast(exp.Literal.string(start.isoformat()), "DATE")),
        exp.LT(this=column.copy(), expression=exp.cast(exp.Literal.string(end.isoformat()), "DATE")),
    )


def _rewrite(condition: exp.Expression) -> exp.Expression | None:
    parts = _conjuncts(condition)
    found = {}
    for index, part in enumerate(parts):
        info = _part(part)
        if info is not None:
            unit, column, value = info
            found.setdefault(column, {}).setdefault(unit, []).append((index, value, part))
    if not any("YEAR" in units for units in found.values()):
        return None
    drop, added = set(), []
    for units in found.values():
        if "YEAR" not in units or len(units["YEAR"]) != 1:
            continue
        index, year, part = units["YEAR"][0]
        if not 1 <= year <= 9998:
            continue
        column = next(e for e in (part.left, part.right) if isinstance(e, exp.Extract)).expression
        months = units.get("MONTH", [])
        if len(months) == 1 and 1 <= months[0][1] <= 12:
            m_index, month, _ = months[0]
            start = datetime.date(year, month, 1)
            end = datetime.date(year + (month == 12), month % 12 + 1, 1)
            drop |= {index, m_index}
        else:
            start, end = datetime.date(year, 1, 1), datetime.date(year + 1, 1, 1)
            drop.add(index)
        added.append(_range(column, start, end))
    if not added:
        return None
    kept = [p.copy() for i, p in enumerate(parts) if i not in drop] + added
    result = kept[0]
    for part in kept[1:]:
        result = exp.and_(result, part)
    return result


def extract_to_ranges(tree: exp.Expression) -> exp.Expression:
    for where in list(tree.find_all(exp.Where, exp.Having)):
        rewritten = _rewrite(where.this)
        if rewritten is not None:
            where.set("this", rewritten)
    return tree
