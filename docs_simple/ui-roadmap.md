# Where the UI gets its data

[All simple guides](README.md) · [Full reference](../docs/ui-roadmap.md)

This guide is for people working on the browser app. The user-facing introduction is [the UI guide](ui.md).

Each page asks the local server for JSON: structured data the browser turns into a graph, table, or report.

| Page | Endpoint | What supplies the data |
| --- | --- | --- |
| Query graph | `/api/graph` | Loaded models, lineage, scopes, and impact analysis |
| Cost | `/api/cost` | Loaded project, repeated-work analysis, and any loaded job history |
| Change reports | `/api/changes` | Shared-logic proposals and any project comparison |

The project lives in `ProjectSession`. `live_insights.py` builds Cost and Change report data, `ui.py` serves it, and browser code in `static/insights.js` renders those pages.

## Empty pages are useful information

If no project is loaded, the API returns an empty state explaining what to load. It does not fill the page with sample measurements. With no job history, the page can describe repeated work but cannot show measured usage for it.

Keep a payload's source, scope, counts, and completeness information together so the user understands what a result covers.

## Evidence should mean the same thing everywhere

The shared `static/evidence.js` code renders the evidence labels across views. A planner-only check must not look like an equivalence proof. A proposed change with incomplete consumers or an unknown rationale must still show that limitation.

The graph has Explorer and Simple renderers over the same data. Explorer uses `static/lineage-view.js` for layout and interaction; switching views should not change the analysis.

The full reference contains endpoint payloads, proposal fields, evidence chips, and screenshots. Use it when adding or changing a field; this guide explains the overall flow.
