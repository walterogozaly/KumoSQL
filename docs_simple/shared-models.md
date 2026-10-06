# Moving a repeated CTE into a shared model

[All simple guides](README.md) · [Full reference](../docs/shared-models.md)

If several Dataform models copy the same WITH query, KumoSQL can propose a new shared model and change those copies to read it. It generates a patch for review; it does not apply it to your files or BigQuery.

## Try it

In the app, open **Shared models**, select a repeated CTE, choose the new model name and View or Table, then **Generate patch**. Inspect the edited models' verdicts and the diff before applying it.

From a project checkout:

```sh
python -m kumosql shared-model path/to/project
```

This lists candidates with their IDs. Use an ID to generate a patch:

```sh
python -m kumosql shared-model path/to/project CANDIDATE_ID --patch shared.diff
```

Replace the path and ID. The output includes proof checks.

## View or table?

A view shares SQL: readers still execute its definition. A table computes and stores the rows, adding storage and a build step. Neither choice establishes measured savings by itself.

Matching fingerprints only finds candidates. The checker reloads the patched project and compares each edited model with the original, expanding the new shared definition. The patch's verdict reflects its weakest result, including any assumptions.

Unsupported templates, external CTE dependencies, unstable values such as RAND, and ambiguous columns can block extraction. One near-duplicate shape is supported: same-output CTE copies where at least one has only the common filters and every extra filter reads a simple projected column. KumoSQL keeps the source CTE's Dataform refs, moves it into a shared view, reapplies each copy's filter, reloads the edited project, and checks the changed models. Literal changes, extra columns, hidden filter inputs, functions, subqueries, and other shapes remain unsupported. Apply the patch only when its result is proven or proven with the listed assumptions.
