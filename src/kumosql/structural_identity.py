"""Structural identity for deterministic queries outside the SMT expression subset."""

import sqlglot
from sqlglot import exp
from sqlglot.optimizer.qualify import qualify
from sqlglot.optimizer.normalize_identifiers import normalize_identifiers

from .smt_equivalence import _Compiler, _canonical_aliases, Unsupported


def same_scoped_query(left, right, *, schema, dialect, compare_names):
    if not schema:
        return False
    shapes = []
    try:
        for sql in (left, right):
            tree = normalize_identifiers(sqlglot.parse_one(sql, read=dialect), dialect=dialect)
            if not isinstance(tree, exp.Select) or tree.find(exp.Limit, exp.Offset):
                return False
            _Compiler(schema, False, dialect)._check_nondeterminism(tree)
            declared = {t.lower(): cs for t, cs in schema.items()}
            query_schema = {}
            for table in tree.find_all(exp.Table):
                name = ".".join(p.name for p in table.parts)
                if name.lower() not in declared:
                    return False
                query_schema[name] = {c: "UNKNOWN" for c in declared[name.lower()]}
            tree = qualify(tree, dialect=dialect,
                           schema=query_schema,
                           infer_schema=False, validate_qualify_columns=True,
                           identify=False, quote_identifiers=False)
            # Qualification resolves GROUP BY aliases before we ignore names.
            if not compare_names:
                tree.set("expressions", [e.this.copy() if isinstance(e, exp.Alias) else e for e in tree.expressions])
                for item in tree.expressions:
                    if isinstance(item, exp.Subquery):
                        item.set("alias", None)
            shapes.append(_canonical_aliases(tree, schema))
    except (sqlglot.errors.SqlglotError, Unsupported, ValueError):
        return False
    return shapes[0] == shapes[1]
