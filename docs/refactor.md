# Refactor: keep some tables, rearrange the rest

Say which tables must keep existing and keep returning the same rows, which may be dropped, merged or rewritten, and let KumoSQL look for simpler pipelines. Every option it returns has every protected table **proved** equal to the original; a move it cannot prove is rejected, never accepted.

## Classes

Each model is in one class, saved like scopes (a list of saved scopes plus explicit model names, in the data folder, never written to BigQuery):

| Class | Meaning |
| --- | --- |
| Protected | Must keep existing and return the same rows. Its SQL may change. |
| Editable | May be dropped, merged into another model, inlined into its readers or rewritten. |
| Neither | Read as it is, never modified. A model a "neither" model reads is *exposed*: it is checked like a protected one, because its reader cannot change. |

Protected wins when a model is in both. Models that are not queries (declarations, operations) and assertions are always "neither". Dataform models are what the search rearranges; other objects (views, functions and procedures seen in the BigQuery explorer) can be named in a class but are only read.

## Use it

- **UI:** the Refactor page. Tick saved scopes for each class or set a class per model (the filter and "Set shown to" classify many at once), Save, then *Find simpler pipelines*. The result is the Pareto front: for each number of models, the lowest total complexity found, with the moves, the changed SQL and the assumptions the proofs used.
- **CLI:** `python -m kumosql refactor DIR --protect report --editable stg1 --editable stg2` (also `--protect-scope`, `--editable-scope`, `--save`, `--max-states`, `--max-seconds`). Without class options it uses the saved classes. Output is JSON; progress goes to stderr.
- **API:** `GET /api/refactor`, `PUT /api/settings/refactor`, `POST /api/refactor/run` (starts a background search), `GET /api/refactor/status` (progress and the front so far), `POST /api/refactor/cancel`. In the UI the page keeps working while it runs, shows the front as it grows, and has a Cancel button; a cancelled run keeps what it proved.

## What it does

Moves: drop an unread editable model; inline an editable model into all its readers (as derived tables); merge an editable model into another that returns the same columns from the same tables. A move is kept only when `prove_models` proves each protected or exposed model of the new pipeline equal to the original (layer lemmas, then inlining; saved equivalences and declared source columns apply). The search runs one greedy descent per weight on model count against complexity (`WEIGHTS` in `refactor.py`): at each step the cheapest moves are proved first and the first proved one is taken. Every proved state is a candidate, and the candidates are reduced to the Pareto front over total sqlfluff complexity (`kumosql.formatting.complexity`) and model count. Proofs are cached by the SQL of the models a protected table reads, so each is done once across descents.

## Scale

`python tools/make_refactor_project.py OUT --reports N` generates a project with duplicated filtered views, leftovers and N reports (protected), the rest editable. Measured on one core: 32 models (20 reports) 9 s, 7 front points from 32 to 26 models; 136 models (100 reports) 95 s, 7 front points from 136 to 100 models, every one proved. Cost grows with the number of proved moves times the protected tables each one touches; `--max-seconds` bounds it.

## Limits

- Evidence is proof only. A move that is not proved is listed with the prover's reason (`rejected_moves`); there is no counterexample for it yet.
- Complexity is the sqlfluff structural score summed over models, so inlining lowers the model count but raises complexity (each derived table counts); the front shows that trade.
- It cannot extract shared logic into a new model, rewrite SQL inside a model, or write the result back to `.sqlx` files; it reports the new SQL. Proof coverage is the prover's: staged LEFT JOIN chains and AVG rebuilt across a rollup are still unknown (see [pipeline-equivalence.md](pipeline-equivalence.md)).
- The search is bounded by `--max-states` and `--max-seconds`; the result says which limit stopped it.
