# Query-to-query relation declarations

Relation declarations are explicit, versioned records that say two SQL queries describe the same named-row relation. A record keeps the original SQL on each side, the resolved output schema, name-based output mapping, optional preferred side, evidence label, applicability scope, provenance and a stable ID.

Preparing or saving a declaration does **not** prove its equivalence. It does not run either query, connect to BigQuery, rewrite SQL or change the existing equivalence prover. Any future consumer must treat the relation as a premise and label its result as conditional on the declaration and its scope.

## Prepare a declaration

Schema resolution uses a caller-supplied map from table names to column names and SQL types. Direct projections and stars over known tables are supported. For example:

```python
from kumosql.relation_declarations import Evidence, Scope, prepare

schemas = {
    "table_y": {"col_id": "INT64", "col_old": "STRING", "keep": "DATE"},
    "table_x": {"col_new": "STRING", "keep": "DATE", "col_id": "INT64"},
}

declaration = prepare(
    "SELECT * EXCEPT (col_old), col_old AS col_new FROM table_y",
    "SELECT keep, col_id, col_new FROM table_x",
    schemas,
    preferred_side="right",
    evidence=Evidence("assertion", "warehouse owner"),
    scope=Scope("snapshot", "warehouse snapshot 2026-10-05"),
    provenance={"ticket": "MIG-42"},
)
```

The output columns match by name, even when their SELECT-list order differs. `preferred_side` says which side future rewrite discovery should prefer; it is separate from the equivalence assertion. Preference cycles are rejected when records are saved.

Types are normalized by case and whitespace, then compared exactly. No implicit casts or compatibility guesses are made. Missing names, duplicate output names, ambiguous or unknown columns, unknown star schemas and computed expressions whose types cannot be resolved raise `DeclarationError`; its `code` field is stable for callers.

## Save and inspect

`prepare(...)` returns an in-memory `RelationDeclaration`. `declare(...)` prepares and saves it. The local, versioned JSON store is `relation_declarations.json` in KumoSQL's selected data directory. It uses a stable UUID and an atomic write. `load()`, `get(id)` and `remove(id)` inspect or revoke records; revocation is idempotent. Editing is currently done by revoking and creating a replacement record.

Evidence is one of:

- `assertion`: a user-supplied premise; this is the default and needs no proof.
- `snapshot_validation`: a validation run, identified by a reference.
- `independent_proof`: a separate proof, identified by a reference.

Scope may be `unspecified`, `all_snapshots`, `snapshot`, `refresh` or `incremental_state`. The last three require a reference. The declaration keeps both SQL strings intact, so filters remain part of each declared query; a consumer must not extend a filtered relation to its unfiltered source.

## Current limits

This is the declaration format and local schema-resolution foundation. It does not yet migrate existing saved table equivalences, expose authoring in the UI or CLI, validate declarations against a warehouse snapshot, or attach IDs and scope to proof reports. It also does not invalidate proofs in downstream caches because no proof consumer reads these records yet.

Schema resolution currently accepts SELECT statements over direct table sources, with direct column projections and star expansion. Filters and joins can be stored as part of a query, but expression outputs such as aggregates, CTEs, derived tables, unions and other computed types are rejected until a type resolver can establish their output schemas. Snapshot scope and assertion metadata describe the user's premise; they are not evidence that the premise is true.

For proof verdicts that are already available, see [equivalent under conditions](conditional-equivalence.md). That feature is separate from relation declarations. The declaration requirements and acceptance example are tracked in [issue #713](https://github.com/walterogozaly/KumoSQL/issues/713).
