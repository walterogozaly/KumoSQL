# Making a pipeline simpler

[All simple guides](README.md) · [Full reference](../docs/refactor.md)

Refactoring changes how a pipeline is organized while preserving the outputs you care about. For example, two staging tables may be folded into the report that reads them.

## Tell KumoSQL what matters

| Class | Meaning |
| --- | --- |
| Protected | Keep the model and its results; its SQL may change |
| Editable | Allow the model to be dropped, merged, or put inside its readers |
| Neither | Keep it as written |

A model read by a model in “Neither” is also checked, because that reader cannot change. Protected takes priority over Editable. Assertions, declarations, and operations are not rearranged.

In the UI, use **Refactor**, classify the models, save, and choose **Find simpler pipelines**. From the command line:

```sh
python -m kumosql refactor path/to/project --protect report --editable stg1 --editable stg2
```

Use your model names. The result is JSON containing candidate pipelines, changed SQL, moves, and proof assumptions.

## Understand the choices

KumoSQL only keeps a move when it proves the protected and exposed outputs equivalent. A move it cannot prove is rejected with a reason.

Fewer models can mean larger individual queries. The result therefore shows choices balancing model count and SQL complexity. The technical term “Pareto front” means choices where improving one of those measurements would worsen the other among the candidates found.

Search limits bound how much work it does. This is a search for good candidates, not a guarantee of the simplest possible pipeline.

## Fold a chain yourself

If `A` feeds `B` and `C`, which feed `D`, you can ask to place their SQL inside `D`:

```sh
python -m kumosql consolidate-tables path/to/project D A B C
```

The command is a read-only preview: it prints proposed SQL and an equivalence result on screen, and it has no option that writes, moves or deletes your files (it only adds timing lines to KumoSQL's own log in its data folder). To see its usage without running anything, run `python -m kumosql consolidate-tables --help`. It refuses folds that leave a known outside reader or involve unsupported model types or operations. It does not write the result into your SQLX files.

The loaded project does not reveal dashboards or other projects reading these tables. Include those consumers in your review before removing a table. Missing source columns, especially with `SELECT *`, can also prevent a proof. See [pipeline analysis](pipeline-analysis.md) and [table minimization tests](evals/table-minimization.md).
