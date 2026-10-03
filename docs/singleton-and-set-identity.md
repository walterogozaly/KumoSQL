# Singleton aggregation and set identity

Two algebraic bridges cover shapes beyond the SMT compiler's direct subset.
Neither modifies a frozen aggregation or set-operation rule.

`set_identity.py` wraps a cut-free, CTE-free root UNION ALL tree containing a
nested UNION DISTINCT in a SELECT-star over the same derived relation.
Ordinary ALL and root-DISTINCT trees keep their existing compilation paths;
wrapping them can hide constant/empty folds or create asymmetric normal forms. The existing scoped SELECT identity check validates
bindings before comparing relation aliases. Complete row bags, NULLs, duplicate
multiplicities, empty branches, column order, output names, ALL/DISTINCT flags,
branch order, types and outer padding are preserved. It does not distribute
or reorder branches. Unknown schemas, volatile queries and cuts remain subject
to refusal. Window NULLS FIRST and NULLS LAST remain different.

`singleton_aggregate_rules.py` recognizes a plain left input fixing every member
of a declared integer key to a non-NULL small integer literal. Its right input
must be plain grouped COUNT(*) with every grouping key equality-joined to a
passed-through left column. Key comparisons require integer/integer types or
identical supported STRING/TEXT/VARCHAR declarations. Number/string and
integer/float comparisons are refused. The weight must be an integer column.
Partial keys, disjunctions, extra grouping keys, grouping extensions, outer
joins, cuts, DISTINCT counts and COUNT(column) are outside this bridge.

Those conditions give zero or one joined row. A grouped SUM of the integer
weighted count preserves that row bag: NULL stays NULL; zero stays zero; empty
input stays zero rows. A global SUM would add an empty-input row and is not
used. The existing weighted-count rule can then flatten the grouped SUM. Its
exact-arithmetic and no-runtime-error conditions still apply. Result column
types are not compared by this proof API.

String keys require an additional condition because public type metadata does
not encode column collation. A coarser join comparison can match two distinct
right groups: a NOCASE left value `a` matches ordinary groups `a` and `A`. The
public algebraic result reports this condition explicitly:

> string equality in singleton/group joins uses the same collation as right-side GROUP BY (no implicit collation coercion)

Callers must verify this condition in their actual schema. Default columns
created together without differing COLLATE declarations meet it in the Calcite
replay. Explicit COLLATE expressions are refused by the plain-column guards.
Normalization without an assumption collector declines the string bridge;
integer-only joins need no additional collation condition. Conditions are
collected separately for each public proof call and both query sides.

The rules were developed with SQLSolver Calcite cases 142 and 231 visible
(tuned on test). Focused tests cover NULL/empty inputs, duplicate groups,
missing/partial keys, coercions, implicit-collation witness data, actual API
assumption reporting, call isolation and nearby unequal set trees.
