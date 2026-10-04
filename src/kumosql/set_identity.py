"""Expose mixed bag/distinct set trees to the existing scoped identity fallback.

SELECT * over a derived relation preserves its complete row bag, column order
and output names. This does not distribute, reorder or deduplicate branches.
Cuts and WITH bindings are intentionally outside this small identity bridge.
"""
from sqlglot import exp

def expose_set_identity(tree, schema):
    if not schema or not isinstance(tree,exp.Union) or tree.args.get("distinct") is not False or tree.find(exp.Limit,exp.Offset,exp.CTE):
        return tree
    # The bridge is needed for a DISTINCT branch read by a bag union. Leave
    # ordinary ALL and root-DISTINCT trees in their existing compiler paths;
    # wrapping them can hide constant/empty folds or create asymmetric shapes.
    if not any(u.args.get("distinct") is not False for u in tree.find_all(exp.Union)):
        return tree
    return exp.select("*").from_(exp.Subquery(this=tree,alias=exp.TableAlias(this=exp.to_identifier("kumosql_set_identity"))))
