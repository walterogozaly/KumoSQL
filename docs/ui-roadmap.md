# UI for the roadmap

The browser UI already has the pages the roadmap needs. Views without a backend yet show sample data behind a "Preview with sample data" banner. This note says which issue owns each area, and what data it expects, so that each issue ends with a working screen.

## How the preview data works

- `src/kumosql/preview_data.py` returns the sample payloads. Every payload has `"preview": true` and an `issues` list.
- `src/kumosql/ui.py` serves those payloads at `/api/graph`, `/api/cost` and `/api/changes` (see `INSIGHTS`).
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

| UI area | Issue | Fields |
| --- | --- | --- |
| Node cards and the detail header (identity, kind, how it was matched) | #23 | `nodes[]`: `id`, `dataset`, `name`, `kind` (`source`, `model`, `view`, `observed`, `unmatched`), `source`, `columns[]`, `note?` |
| Edge line styles (declared, observed, both, parsed only), confidence and last seen | #24 | `edges[]`: `from`, `to`, `source`, `confidence` (`high`, `medium`, `low`), `last_seen?`, `observed_count` |
| Find readers tab | #25 | Computed on the page from `edges` |
| Assess a change tab (drop, rename, change expression). Readers whose use can't be traced are listed as Unknown | #26 | Computed on the page from `column_lineage` plus `gaps` |
| Explain lineage tab. Columns that can't be traced are marked Unknown | #27 | `column_lineage[]`: `node`, `column`, `sources[] {node, column}`, `transform` |
| Gaps table, dashed "Not analyzed" nodes, the "Partial graph" strip | #28 | `gaps[]`: `asset`, `kind` (`parse_error`, `inaccessible`, `unmatched_reference`, `unattributed_reads`), `message` |
| Coverage strip | #29 | `coverage`: `assets_total`, `assets_analyzed`, `statements_total`, `statements_matched`, `sampled_impact_accuracy`, `sample_size`, `complete`; `window {start, end}` |

The traversals for readers, impact and lineage run in the browser, which is fine at MVP size. If the graph gets large, add `?node=` endpoints and keep the result shapes in `insights.js` (`readersOf`, `impactOf`, `lineageOf`). The view has no "safe to delete" action, per the roadmap.

## Cost: `/cost` → `/api/cost`

| UI area | Issue | Fields |
| --- | --- | --- |
| Measured, attributed and unattributed tiles; cost by asset | #30 | `totals {measured, attributed, unattributed}`, `nodes[] {node, measured, runs, bytes_processed}`, `currency`, `window` |
| "Where the work repeats" | #31 | `opportunities[].repeats[] {node, where}` |
| Ranked opportunities list | #32 | `opportunities[]`: `rank`, `title`, `savings {value, basis, range?}`, `measured_cost`, `frequency`, `downstream_reach` |
| Four-part recommendation (where, who, what, how verified) | #33 | `consumers[]`, `proposed_change`, `rule`, `verification {required, plan[]}` |
| Cost rule catalog | #34 | `rules[]`: `id`, `name`, `state`, `safe_when`, `requires`, `outcome` |
| Validated savings and open estimates tiles; Measured, Estimate and Upper bound tags | #35 | `validated {accepted_changes, validated_savings, pending_estimates}`; `savings.basis` is one of `measured`, `estimate`, `upper_bound` |

## Change reports: `/changes` → `/api/changes`

| UI area | Issue | Fields |
| --- | --- | --- |
| Evidence coverage bar (proof and planner reported separately) | #22 | `evidence_coverage {changed, proven, planner_checked, unproven, failed}` |
| Change report table: behavior, cost and consumers side by side | #36 | `report {title, base, head, generated_at, changes[]}`; each change has `model`, `kind`, `verification {label, reason, checks[]}`, `cost {basis, before?, after?}`, `consumers {models[], complete}` |
| Code review check preview | #37 | `ci {check_name, conclusion, summary}` |
| Query sources list | #38 | `sources[] {name, kind, state, matched}` |
| "N assets could not be analyzed. The rest of this report is complete." | #39 | `report.diagnostics[] {asset, message}` |
| Guided refactors with a result for every consumer, including Unknown, and Ready or Not ready | #40, #41, #42 | `proposals[] {id, kind, title, cost_rationale, consumers[] {node, label}, ready}` |

`kumosql.change_report.build_change_report` (and the `kumosql-change-report BASE HEAD [--cost FILE]` command) produces `report` from two project snapshots. `/api/changes` still serves the preview because the UI has no base and head input yet; wire it to the builder once it does.

`ready` is true only when every consumer is `proven` or `unchanged`. This is the strictest reading of #42; relax it in `preview_data.changes()` and in the "need a proof" message if planner checked should count.

## Screenshots

Screenshots are in `docs/images/ui-roadmap/`: `graph-readers`, `graph-impact`, `graph-lineage`, `graph-mobile`, `cost`, `changes`, `changes-dark`, `ws-current-report`, `ws-future-report` and `ws-future-verdict`.
