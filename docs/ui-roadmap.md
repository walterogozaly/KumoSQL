# UI for the roadmap

The browser UI already has the pages the roadmap needs. The query graph shows the loaded project (see below); other views without a backend yet show sample data behind a "Preview with sample data" banner. This note says which issue owns each area, and what data it expects, so that each issue ends with a working screen.

## How the preview data works

- `src/kumosql/preview_data.py` returns the sample payloads. Every payload has `"preview": true` and an `issues` list.
- `src/kumosql/ui.py` serves those payloads at `/api/cost` and `/api/changes` (see `INSIGHTS`). `/api/graph` serves the loaded project and uses the sample only when nothing is loaded.
- `src/kumosql/static/insights.js` renders `/graph`, `/cost` and `/changes`. It shows the banner whenever `preview` is true.

To wire up a view, return the same shape from real code in `INSIGHTS` and drop `preview`. The field lists below are the contract, and `tests/test_ui.py` checks the sample data against it. If an issue needs a different shape, change the sample data and the renderer in the same PR.

All names in the sample data are generic. Per the roadmap's reporting rules, real payloads that end up in screenshots or reports should not include internal names or identifying SQL.

## Evidence labels (workspace, change reports, proposals)

The labels and check chips are defined once in `static/evidence.js` (`KumoEvidence`) and used on every page.

| UI area | Issue | Expected data |
| --- | --- | --- |
| One label per step and per pipeline: proven, planner checked, unproven, failed, unchanged. Shown in the verdict bar, timeline and legend | #15 (landed) | `verification.status` on the pipeline and on each step |
| Supporting checks shown as chips under each step and the pipeline. Checks that did not pass also list their detail | #15 (landed) | `verification.checks[]`: `{kind, outcome, detail}`. Kinds so far: `rewrite`, `change_detection`, `equivalence_proof`, `planner` |
| Planner (dry run) chip, and the "Planner checked: not proven" verdict | #16 | check `kind: "planner"` |
| "Unchanged text kept" chip | #17 | check `kind: "source_spans"` |
| Idempotence chip | #18 | check `kind: "idempotence"` |
| Structural proof, SMT proof and synthetic-results chips | #19, #20, #21 | check kinds `structural_proof`, `smt` and `synthetic_results`. An `inconclusive` outcome covers a baseline that differs from itself. |

Check outcomes are `passed`, `failed`, `not_proven`, `inconclusive`, `unsupported` and `not_run`. A new kind or outcome only needs an entry in `CHECKS` or `OUTCOMES` in `evidence.js`; unknown kinds still render under their raw name. Proof kinds and planner checks are always separate chips.

`ws-future-*.png` shows the workspace with a mocked response that includes the planned check kinds.

## Query graph: `/graph` → `/api/graph`

`kumosql.live_graph` holds the project the server has loaded (in memory; it is gone after a restart) and builds the payload from a `Pipeline`. Load one by:

- entering a public Dataform repository URL on the `/graph` page (`POST /api/github/load`, limited to 100 SQLX files because unauthenticated GitHub requests are rate limited),
- `kumosql-ui --project DIR`, or
- `POST /api/project` with `{"files": {relative path: text}, "label"}` (only `.sqlx`, `.sql` and Dataform config files; relative paths only).

`POST /api/project/clear` (the "Use sample data" button) goes back to the sample.

Live payloads have no `preview` flag and carry `source {kind: "project", label}`; the sample carries `source {kind: "sample"}` and the banner. Live payloads add `completeness {complete, views, assets_not_analyzed}` and `blocking` on each gap. Tables the project reads but does not define are `source` nodes; a model that failed to parse is listed as a gap and its node is drawn as "Not analyzed". `coverage.complete` is false whenever a blocking gap exists, so the "Partial graph" strip cannot be missed. Observed edges, last seen and the sampled accuracy need job history, which the server does not have yet; they stay empty (`sampled_impact_accuracy` is null and shown as "Not reviewed").

| UI area | Issue | Fields |
| --- | --- | --- |
| Node cards and the detail header (identity, kind, how it was matched) | #23 | `nodes[]`: `id`, `dataset`, `name`, `kind` (`source`, `model`, `view`, `observed`, `unmatched`), `source`, `columns[]`, `note?` |
| Edge line styles (declared, observed, both, parsed only), confidence and last seen | #24 | `edges[]`: `from`, `to`, `source`, `confidence` (`high`, `medium`, `low`), `last_seen?`, `observed_count` |
| Find readers tab | #25 | Computed on the page from `edges` |
| Assess a change tab (drop, rename, change expression). Readers whose use can't be traced are listed as Unknown; readers seen only in job history are listed separately as Observed | #26 | `GET /api/impact?node=&column=&change=drop|rename|expression` returns the server's `Pipeline.assess_change` result (`kumosql.impact`): `affected[] {model, effect, via, depth, columns}`, `unknown[] {model, reason}`, `observed[] {model, effect (may_break | may_change), via, depth, last_seen, confidence, observed_count, source}`, `observed_checked`, `complete`, `incomplete_reasons` and `safe_to_delete` (always `unknown`). `observed` holds tables that job history shows reading the target or an affected model but that no compiled model declares; a reader that is already affected or unknown is not repeated. Job history names tables, not columns, so those effects are "may", never "breaks". With no project loaded the endpoint answers from the labeled sample data; the page no longer computes impact itself |
| Explain lineage tab. Columns that can't be traced are marked Unknown | #27 | `column_lineage[]`: `node`, `column`, `sources[] {node, column}`, `transform`, plus `status` (`traced`, `constant`, `unknown`), `reason?` and `complete`. Built by `Pipeline.lineage_report()`; the sample data does not include the extra fields yet |
| Gaps table, dashed "Not analyzed" nodes, the "Partial graph" strip | #28 | `gaps[]`: `asset`, `kind` (`parse_error`, `inaccessible`, `unmatched_reference`, `unattributed_reads`, and other diagnostic codes), `message`. Built by `Pipeline.completeness()` (also `report()["completeness"]`, which adds `complete`, per-view flags and `blocking`); `/api/graph` serves it for a loaded project |
| Coverage strip | #29 | `coverage`: `assets_total`, `assets_analyzed`, `statements_total`, `statements_matched`, `sampled_impact_accuracy` (null until reviewed), `sample_size`, `complete`; `window {start, end}`. Also ratios, column, edge-source/confidence and gap counts, and an `accuracy` block with precision/recall intervals. `scoped` is true when computed within a saved scope; `gate {passed, failed, checks}` is present when release thresholds are supplied. Built by `Pipeline.coverage()` (also `report()["coverage"]`); anonymized aggregates only |

The traversals for readers and lineage run in the browser, which is fine at MVP size; if the graph gets large, add `?node=` endpoints and keep the result shapes in `insights.js` (`readersOf`, `lineageOf`). Impact already runs on the server (`/api/impact`) and the page only fetches and draws it. The view has no "safe to delete" action, per the roadmap.

## Cost: `/cost` → `/api/cost`

| UI area | Issue | Fields |
| --- | --- | --- |
| Measured, attributed and unattributed tiles; cost by asset | #30 | `totals {measured, attributed, unattributed}`, `nodes[] {node, measured, runs, bytes_processed}`, `currency`, `window` |
| "Where the work repeats" | #31 | `opportunities[].repeats[] {node, where}`; built by `kumosql.repeated_work_report(pipeline)` (locations only, no cost until #30 lands); the preview data stays until the page has real input |
| Ranked opportunities list | #32 | `opportunities[]`: `rank`, `title`, `savings {value, basis, range?}`, `measured_cost`, `frequency`, `downstream_reach` |
| Four-part recommendation (where, who, what, how verified) | #33 | `consumers[]`, `proposed_change`, `rule`, `verification {required, plan[]}` |
| Cost rule catalog | #34 | `rules[]`: `id`, `name`, `state` (`shipped` or `not_implemented`), `safe_when`, `requires`, `outcome`, `measured_outcome` (null until measured); built by `cost_rules.rule_catalog()` |
| Validated savings and open estimates tiles; Measured, Estimate and Upper bound tags | #35 | `validated {accepted_changes, validated_savings, pending_estimates}`; `savings.basis` is one of `measured`, `estimate`, `upper_bound` |

`kumosql.costs.build_cost(pipeline, jobs)` returns this shape from real job history (`load_jobs` reads a JSON, JSON lines or CSV export). It also adds `unit`, `counts`, `unattributed[]` (reason codes) and `edges[]`. `/api/cost` still serves the preview because no job history is configured for the server. Values are billed bytes unless an explicit `usd_per_tib` rate is given. Reader cost on a view lands on the view, never split across base tables.

## Change reports: `/changes` → `/api/changes`

| UI area | Issue | Fields |
| --- | --- | --- |
| Evidence coverage bar (proof and planner reported separately) | #22 | `evidence_coverage {changed, proven, planner_checked, unproven, failed, synthetic_agreed, useful_evidence}`; headline "Useful evidence" is `useful_evidence / changed` (proof plus synthetic agreement, each output once); built from `kumosql.summarize_evidence(results).to_json()` or `kumosql-evidence-summary` (labels and counts only). The API keeps preview data until a corpus run is supplied |
| Change report table: behavior, cost and consumers side by side | #36 | `report {title, base, head, generated_at, changes[]}`; each change has `model`, `kind`, `verification {label, reason, checks[]}`, `cost {basis, before?, after?}`, `consumers {models[], complete}` |
| Code review check preview | #37 | `ci {check_name, conclusion, summary}`; built by `kumosql.ci_check` from a change report (`kumosql-ci-check`); example workflow in `docs/change-report-workflow.example.yml` |
| Query sources list | #38 | `sources[] {name, kind, state, matched}`; `state` is `connected`, `not_enabled` or `error`, `matched` is a 0-1 fraction or null. Built by `query_sources.SourceRegistry.to_json(pipeline)`, which also adds `assets_total`, `assets_matched`, `assets_unmatched`. The API keeps preview data until a pipeline and source records are supplied |
| "N assets could not be analyzed. The rest of this report is complete." | #39 | `report.diagnostics[] {asset, message}` |
| Guided refactors with a result for every consumer, including Unknown, and Ready or Not ready | #40, #41, #42 | `proposals[] {id, kind, title, cost_rationale, consumers[] {node, label}, ready}` |

`kumosql.shared_logic.propose_shared_logic` (#40) builds the `shared_logic` proposals with this shape plus `consumers_complete`, `incomplete_reasons` and a consumer `role`. `cost_rationale` is `unknown` and `ready` is false until #41 and #42 fill them. The `/api/changes` payload is still preview data.
`kumosql.change_report.build_change_report` (and the `kumosql-change-report BASE HEAD [--cost FILE]` command) produces `report` from two project snapshots. `/api/changes` still serves the preview because the UI has no base and head input yet; wire it to the builder once it does.

`ready` is true only when every consumer is `proven` or `unchanged`. This is the strictest reading of #42; relax it in `preview_data.changes()` and in the "need a proof" message if planner checked should count.

## Screenshots

Screenshots are in `docs/images/ui-roadmap/`: `graph-readers`, `graph-impact`, `graph-lineage`, `graph-mobile`, `cost`, `changes`, `changes-dark`, `ws-current-report`, `ws-future-report` and `ws-future-verdict`.
