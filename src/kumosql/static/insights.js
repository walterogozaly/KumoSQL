"use strict";

/* Query graph, cost and change report views.
   Each view reads one JSON endpoint (see docs/ui-roadmap.md). While the backend
   for a view is on the roadmap the server returns sample data with
   `preview: true`, and this page shows a banner saying so. */

const E = window.KumoEvidence;
const REPO_ISSUES = "https://github.com/walterogozaly/KumoSQL/issues/";

const ISSUE_TITLES = {
  22: "Evidence coverage metric", 23: "Asset and table identity", 24: "Declared and observed edges",
  25: "Find readers", 26: "Assess a change", 27: "Column lineage", 28: "Expose gaps",
  29: "Coverage and accuracy measures", 30: "Attribute cost to the graph", 31: "Repeated computation",
  32: "Rank opportunities", 33: "Recommendation format", 34: "Cost rewrite rule catalog",
  35: "Validated savings", 36: "Semantic change reports", 37: "CI check", 38: "Further query sources",
  39: "Failures stay local", 40: "Shared-logic extraction", 41: "Upstream filter changes",
  42: "Per-consumer verification",
};

const VIEWS = {
  graph: {
    endpoint: "/api/graph", eyebrow: "Query graph", title: "What reads this, and what breaks if it changes?",
    lede: "Declared model dependencies and observed job reads in one graph. Pick an asset to see its readers, the impact of a change, or where a column comes from.",
    render: renderGraph,
  },
  cost: {
    endpoint: "/api/cost", eyebrow: "Cost", title: "Where is the same work done repeatedly?",
    lede: "Measured cost attributed to the graph, ranked opportunities, and how each proposed change would be verified.",
    render: renderCost,
  },
  changes: {
    endpoint: "/api/changes", eyebrow: "Change reports", title: "What does this change do to behavior, cost and consumers?",
    lede: "One report per change set, the check posted in code review, and graph-wide proposals with a verification result for every consumer.",
    render: renderChanges,
  },
};

/* ---------- DOM helpers ---------- */

function h(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === undefined || value === null || value === false) continue;
    if (key === "class") node.className = value;
    else if (key === "style") Object.assign(node.style, value); // CSSOM: allowed under the page's CSP
    else if (key === "text") node.textContent = value;
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else node.setAttribute(key, value === true ? "" : value);
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

const $ = (id) => document.getElementById(id);

function panel(title, { note, className = "", id } = {}, ...body) {
  return h("section", { class: `card ${className}`, id },
    h("div", { class: "card-head" }, h("h2", { text: title }), note ? h("span", { class: "card-note", text: note }) : null),
    ...body);
}

function money(value, currency = "USD") {
  return new Intl.NumberFormat(undefined, { style: "currency", currency, maximumFractionDigits: 0 }).format(value);
}

function percent(part, whole) {
  return whole ? `${Math.round((part / whole) * 100)}%` : "–";
}

function shortDate(iso) {
  if (!iso) return "–";
  return new Date(iso).toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

function windowLabel(window) {
  return window ? `${shortDate(window.start)} to ${shortDate(window.end)}` : "";
}

function tag(text, tone = "idle", title) {
  return h("span", { class: `ev-pill ev-${tone}`, title, text });
}

const BASIS = {
  measured: ["Measured", "ok", "Observed from job history after the change"],
  estimate: ["Estimate", "info", "Projected from dry runs or history; not yet observed"],
  upper_bound: ["Upper bound", "warn", "The most this could save; actual savings will be lower"],
  unavailable: ["Unavailable", "idle", "No cost data for this item"],
};

function basisTag(basis) {
  const [text, tone, title] = BASIS[basis] || BASIS.unavailable;
  return tag(text, tone, title);
}

/* ---------- Page shell ---------- */

async function applySavedTheme() {
  try {
    const response = await fetch("/api/settings");
    const theme = response.ok ? (await response.json()).ui?.theme : null;
    if (theme === "light" || theme === "dark") document.documentElement.dataset.theme = theme;
  } catch {
    /* keep the system theme */
  }
}

function showPreview(data) {
  $("preview-banner").hidden = !data.preview;
  const list = $("preview-issues");
  list.replaceChildren();
  for (const number of data.issues || []) {
    list.append(h("li", {}, h("a", { href: REPO_ISSUES + number, target: "_blank", rel: "noopener" }, `#${number}`), ` ${ISSUE_TITLES[number] || ""}`));
  }
}

/* The graph page shows the loaded project, or labeled sample data when none is loaded. */
function setupProjectForm(data) {
  const form = $("project-form");
  form.hidden = false;
  const live = data.source?.kind === "project";
  $("project-source").textContent = live ? `Showing ${data.source.label}` : "Showing sample data. Load a Dataform git repository (private ones work with your own git credentials) to see your own graph.";
  $("project-clear").hidden = !live;
  if (form.dataset.bound) return;
  form.dataset.bound = "1";
  const run = async (path, body, busy) => {
    const status = $("project-status");
    status.textContent = busy;
    try {
      const response = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || "Could not load the project");
      location.reload();
    } catch (error) {
      status.textContent = error.message;
    }
  };
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    run("/api/project/git", {
      url: $("project-url").value.trim(),
      branch: $("project-branch").value.trim(),
      refresh: $("project-refresh").checked,
    }, "Loading project with git…");
  });
  $("project-clear").addEventListener("click", () => run("/api/project/clear", {}, "Clearing…"));
}

/* ---------- Active scope ---------- */

/** Add the active scope (if any) to an API path so the server limits what it returns. */
function withScope(path) {
  const params = window.KumoScopes ? new URLSearchParams(window.KumoScopes.params()) : new URLSearchParams();
  const text = params.toString();
  return text ? `${path}${path.includes("?") ? "&" : "?"}${text}` : path;
}

async function setupScopePicker() {
  if (!window.KumoScopes) {
    $("scope-bar").hidden = true;
    return;
  }
  await window.KumoScopes.mountPicker($("scope-picker"), { onChange: () => location.reload() });
}

/** Say what the active scope did to this view, including when it could not apply. */
function showScope(scope) {
  const note = $("scope-note");
  note.classList.toggle("is-warn", Boolean(scope && (!scope.applied_to.length || scope.note)));
  if (!scope) {
    note.textContent = "";
  } else if (!scope.applied_to.length) {
    note.textContent = scope.note;
  } else {
    note.textContent = `Showing only what “${scope.name}” includes (${scope.rule}).${scope.note ? ` ${scope.note}` : ""}`;
  }
}

async function start() {
  const name = location.pathname.replace(/^\/+|\/+$/g, "") || "graph";
  const view = VIEWS[name] || VIEWS.graph;
  document.title = `${view.eyebrow} · KumoSQL`;
  $("page-eyebrow").textContent = view.eyebrow;
  $("page-title").textContent = view.title;
  $("page-lede").textContent = view.lede;
  applySavedTheme();
  try {
    await setupScopePicker();
    const response = await fetch(withScope(view.endpoint));
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "Could not load this view");
    showPreview(data);
    showScope(data.scope);
    if (name === "graph") setupProjectForm(data);
    view.render(data, $("view"));
  } catch (error) {
    $("load-error").hidden = false;
    $("load-error").textContent = error.message;
  }
}

/* ======================================================================
   Query graph (#23-#29)
   ====================================================================== */

const EDGE_SOURCES = {
  both: ["Declared + observed", "Compiled model dependency, also seen in job history"],
  declared: ["Declared", "Compiled model dependency; not seen in job history in this window"],
  observed: ["Observed", "Seen in job history; no compiled model declares it"],
  parsed: ["Parsed only", "Found only by parsing SQL; lower confidence"],
};
const CONFIDENCE_TONE = { high: "ok", medium: "info", low: "warn" };
const NODE_KINDS = {
  source: "Source table", model: "Model", view: "View", observed: "Observed query", unmatched: "Unmatched",
};

function buildGraph(data) {
  const nodes = new Map(data.nodes.map((node) => [node.id, node]));
  const down = new Map();
  const up = new Map();
  for (const edge of data.edges) {
    if (!down.has(edge.from)) down.set(edge.from, []);
    if (!up.has(edge.to)) up.set(edge.to, []);
    down.get(edge.from).push(edge);
    up.get(edge.to).push(edge);
  }
  const lineage = new Map(data.column_lineage.map((item) => [`${item.node}.${item.column}`, item]));
  const consumers = new Map();
  for (const item of data.column_lineage) {
    for (const source of item.sources) {
      const key = `${source.node}.${source.column}`;
      if (!consumers.has(key)) consumers.set(key, []);
      consumers.get(key).push(item);
    }
  }
  const gaps = new Map(data.gaps.map((gap) => [gap.asset, gap]));
  const traced = new Set(data.column_lineage.map((item) => item.node));

  // Longest-path depth from the roots; cycles (possible with observed edges) stop at the first revisit.
  const depth = new Map();
  const visit = (id, trail) => {
    if (depth.has(id)) return depth.get(id);
    if (trail.has(id)) return 0;
    trail.add(id);
    const parents = (up.get(id) || []).map((edge) => visit(edge.from, trail) + 1);
    trail.delete(id);
    const value = parents.length ? Math.max(...parents) : 0;
    depth.set(id, value);
    return value;
  };
  for (const id of nodes.keys()) visit(id, new Set());
  return { data, nodes, down, up, lineage, consumers, gaps, traced, depth };
}

/** Direct and transitive readers of a node (#25). */
function readersOf(graph, id) {
  const seen = new Map();
  let frontier = [{ id, hops: 0 }];
  while (frontier.length) {
    const next = [];
    for (const item of frontier) {
      for (const edge of graph.down.get(item.id) || []) {
        if (seen.has(edge.to) || edge.to === id) continue;
        seen.set(edge.to, { id: edge.to, hops: item.hops + 1, edge });
        next.push({ id: edge.to, hops: item.hops + 1 });
      }
    }
    frontier = next;
  }
  return [...seen.values()];
}

/* Change impact (#26) is computed by the server (/api/impact): the same
   Pipeline.assess_change result the CLI returns, with readers seen only in job
   history. The page only fetches it, caches it per request and draws it. */
const IMPACT_EFFECTS = {
  breaks: ["Breaks", "bad", "The model reads this column and stops working"],
  indirect: ["Breaks upstream", "bad", "Reads a model that breaks"],
  values_change: ["Results may change", "info", "An output column is computed from the changed expression"],
  behavior_may_change: ["Rows may change", "info", "Uses the column only in a filter, join or grouping"],
  may_break: ["May break", "warn", "Job history shows the read; which columns it uses is not known"],
  may_change: ["May change", "warn", "Job history shows the read; which columns it uses is not known"],
};
const UNKNOWN_REASONS = {
  parse_error: "Could not be parsed", qualify_error: "Could not be resolved", no_query: "Has no query",
  unexpanded_star: "SELECT * over an unknown schema", unparsed_operation: "Operation, not analyzed",
  unparsed_model: "Could not be analyzed", unknown_reads: "Reads tables that could not be determined",
  downstream_of_unknown_reader: "Downstream of a reader that could not be read",
  column_use_not_traced: "Column use could not be traced",
};

async function fetchOverlaps(node) {
  const response = await fetch(withScope(`/api/overlaps?${new URLSearchParams({ node })}`));
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || "Could not compare this table");
  return data;
}

const MATCH_KINDS = {
  same_meaning: ["Same meaning", "ok", "Every attribute, the grain and the row scope match"],
  contains: ["Contains", "info", "Same attributes and grain; the table has more rows and can be filtered"],
  partial: ["Partial", "warn", "Some attributes match, or the row scope differs"],
};
const CHECK_TONE = { matched: "ok", differs: "warn", unknown: "idle" };

/** The "already done elsewhere" list (#83): ranked matches with their checks and role, then coverage. */
function overlapList(section, name) {
  const matches = section.matches || [];
  const unknown = section.unknown || [];
  const skipped = Object.entries(section.skipped || {});
  return h("div", { class: "overlap" },
    h("p", { class: "coverage-headline", "data-testid": "overlap-summary", text: section.summary }),
    section.status === "unavailable"
      ? h("p", { class: "callout callout-warn", text: "The comparison could not be completed, so this is not a “no match”. The rest of the page is unaffected." }) : null,
    matches.length ? h("ol", { class: "plain-list overlap-list" }, matches.map((match) => {
      const [label, tone, title] = MATCH_KINDS[match.kind] || [match.kind, "idle", ""];
      return h("li", { class: "reader overlap-item" },
        h("div", { class: "overlap-head" }, h("span", { class: "muted small", text: `#${match.rank}` }), name(match.table, match.key),
          tag(label, tone, title), tag(`${match.confidence} confidence`, CONFIDENCE_TONE[match.confidence] || "idle"),
          match.retiring ? tag("Retired in this change", "info", "This table is removed by the same change, so a replacement is expected") : null,
          match.in_this_change ? tag("Also in this change", "info", "This table is new or edited in the same change; neither side is settled") : null),
        h("div", { class: "ev-checks" }, (match.checks || []).map((check) =>
          h("span", { class: "overlap-check", title: check.detail || "" }, tag(`${check.kind.replace("_", " ")}: ${check.outcome}`, CHECK_TONE[check.outcome] || "idle")))),
        match.reason ? h("p", { class: "muted small", text: match.reason }) : null,
        match.role ? h("p", { class: "muted small", text: `Role: ${match.role.role} (${match.role.confidence} confidence)` +
          (match.role.evidence?.length ? `. Evidence: ${match.role.evidence.map((e) => e.detail).join("; ")}` : "") }) : null);
    })) : h("p", { class: "muted small", text: section.status === "unavailable" ? "Nothing was compared." : "No existing table in the compared set provides the same attributes." }),
    unknown.length ? h("div", {},
      h("h3", { text: `Could not be compared (${unknown.length})` }),
      h("ul", { class: "plain-list" }, unknown.map((item) => h("li", { class: "reader" }, name(item.table, item.key),
        h("span", { class: "edge-meta" }, E.pill("unknown"), h("span", { class: "muted small", text: (item.reason || "").split(":").slice(1).join(":").trim() || item.reason }))))))
      : null,
    skipped.length ? h("p", { class: "muted small", text: "Skipped: " + skipped.map(([reason, count]) => `${count} ${reason.replaceAll("_", " ")}`).join(", ") + ". A skipped table was not checked; it is not a “no match”." }) : null,
    (section.rollups || []).length ? h("div", {},
      h("h3", { text: `Held at a finer grain (${section.rollups.length})` }),
      h("ul", { class: "plain-list" }, section.rollups.map((item) => h("li", { class: "reader" }, name(item.table, item.key),
        h("span", { class: "edge-meta" }, tag(item.derivability.replaceAll("_", " "), item.derivability === "derivable_exact" ? "ok" : item.derivability === "unknown" ? "idle" : "info"),
          item.attribute ? h("span", { class: "muted small", text: item.attribute }) : null)))))
      : null);
}

async function fetchImpact(node, column, change) {
  const query = new URLSearchParams({ node, column, change });
  const response = await fetch(withScope(`/api/impact?${query}`));
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || "Could not assess this change");
  return data;
}

/** Trace a column back to its sources (#27). */
function lineageOf(graph, id, column, seen = new Set()) {
  const key = `${id}.${column}`;
  const item = graph.lineage.get(key);
  const node = graph.nodes.get(id);
  if (seen.has(key)) return { node: id, column, transform: "cycle", sources: [] };
  seen.add(key);
  if (!item) {
    const known = node && node.kind === "source";
    return { node: id, column, transform: known ? "source column" : "unknown", unknown: !known, sources: [] };
  }
  return {
    node: id, column, transform: item.transform,
    unknown: item.status === "unknown",
    sources: item.sources.map((source) => lineageOf(graph, source.node, source.column, seen)),
  };
}

function renderGraph(data, root) {
  const graph = buildGraph(data);
  const params = new URLSearchParams(location.search);
  const firstTraced = data.nodes.find((node) => graph.traced.has(node.id)) || data.nodes[0];
  const initialNode = graph.nodes.has(params.get("node")) ? params.get("node") : graph.nodes.has("staging.stg_orders") ? "staging.stg_orders" : firstTraced?.id;
  const initialColumns = graph.nodes.get(initialNode)?.columns || [];
  const state = {
    node: initialNode,
    column: params.get("column") || (initialColumns.includes("amount_usd") ? "amount_usd" : initialColumns[0] || null),
    mode: ["readers", "impact", "lineage", "overlap"].includes(params.get("mode")) ? params.get("mode") : "readers",
    change: "drop",
  };

  const coverage = data.coverage;
  const coverageStrip = h("div", { class: `coverage ${coverage.complete ? "is-complete" : "is-partial"}` },
    h("div", { class: "coverage-title" },
      tag(coverage.complete ? "Complete" : "Partial graph", coverage.complete ? "ok" : "warn"),
      h("span", { text: coverage.complete ? "Every asset in scope was analyzed." : "Some assets could not be analyzed. Results below may miss readers." })),
    h("dl", { class: "coverage-stats" },
      stat("Assets analyzed", `${coverage.assets_analyzed} of ${coverage.assets_total}`),
      stat("Statements matched", percent(coverage.statements_matched, coverage.statements_total)),
      stat("Sampled impact accuracy", coverage.sampled_impact_accuracy == null ? "Not reviewed" : `${Math.round(coverage.sampled_impact_accuracy * 100)}%`, `${coverage.sample_size} sampled reports`),
      stat("Job history", data.window?.start || data.window?.end ? windowLabel(data.window) : "None"),
      stat("Gaps", String(data.gaps.length))));

  const search = h("input", { type: "search", class: "field", placeholder: "Find an asset or asset.column", list: "asset-options", "aria-label": "Find an asset" });
  const options = h("datalist", { id: "asset-options" },
    data.nodes.flatMap((node) => [h("option", { value: node.id }), ...node.columns.map((column) => h("option", { value: `${node.id}.${column}` }))]));
  search.addEventListener("change", () => {
    const value = search.value.trim();
    const node = data.nodes.find((item) => value === item.id || value.startsWith(`${item.id}.`));
    if (!node) return;
    select(node.id, value.length > node.id.length ? value.slice(node.id.length + 1) : null);
    search.value = "";
  });

  const modeTabs = h("div", { class: "tabs", role: "tablist", "aria-label": "Graph question" },
    ...[["readers", "Find readers"], ["impact", "Assess a change"], ["lineage", "Explain lineage"], ["overlap", "Already elsewhere"]].map(([mode, label]) =>
      h("button", { class: "tab", type: "button", role: "tab", "data-mode": mode, onclick: () => { state.mode = mode; update(); } }, label)));

  const canvas = h("div", { class: "graph-canvas", tabindex: "0", "aria-label": "Graph of assets and dependencies" });
  const detail = h("aside", { class: "graph-detail", "aria-live": "polite" });
  const legend = h("div", { class: "graph-legend" },
    ...Object.entries(EDGE_SOURCES).map(([key, [label, title]]) =>
      h("span", { class: "legend-item", title }, h("span", { class: `legend-line edge-${key}` }), label)),
    h("span", { class: "legend-item" }, h("span", { class: "legend-node is-gap" }), "Not analyzed"));

  root.append(
    coverageStrip,
    h("div", { class: "graph-toolbar" }, search, options, modeTabs),
    h("div", { class: "graph-layout" },
      h("div", { class: "card graph-card" }, canvas, legend),
      detail),
    gapsPanel(data));

  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.classList.add("graph-edges");
  svg.setAttribute("aria-hidden", "true");
  const columns = [];
  for (const [id, value] of graph.depth) {
    (columns[value] ||= []).push(graph.nodes.get(id));
  }
  const grid = h("div", { class: "graph-cols" },
    columns.map((list) => h("div", { class: "graph-col" },
      list.sort((a, b) => a.id.localeCompare(b.id)).map((node) =>
        h("button", {
          type: "button", class: `gnode kind-${node.kind}${graph.gaps.has(node.id) ? " is-gap" : ""}`, "data-id": node.id,
          onclick: () => select(node.id, null),
        },
        h("span", { class: "gnode-dataset", text: node.dataset }),
        h("span", { class: "gnode-name", text: node.name }),
        h("span", { class: "gnode-kind", text: graph.gaps.has(node.id) ? "Not analyzed" : NODE_KINDS[node.kind] || node.kind }))))));
  canvas.append(svg, grid);

  function select(id, column) {
    state.node = id;
    const node = graph.nodes.get(id);
    state.column = column && node.columns.includes(column) ? column : node.columns.includes(state.column) ? state.column : node.columns[0] || null;
    update();
  }

  const impactCache = new Map();
  /** The server's impact result for the current selection, or null while it loads or failed. */
  function impactFor() {
    if (state.mode !== "impact" || !state.column) return null;
    const key = JSON.stringify([state.node, state.column, state.change]);
    const cached = impactCache.get(key);
    if (cached) return cached.result || null;
    impactCache.set(key, { pending: true });
    fetchImpact(state.node, state.column, state.change)
      .then((result) => impactCache.set(key, { result }))
      .catch((error) => impactCache.set(key, { error: error.message }))
      .finally(update);
    return null;
  }

  const overlapCache = new Map();
  /** The server's overlap result for the selected node, or null while it loads or failed. */
  function overlapFor() {
    const cached = overlapCache.get(state.node);
    if (cached) return cached.result || null;
    const node = state.node;
    overlapCache.set(node, { pending: true });
    fetchOverlaps(node)
      .then((result) => overlapCache.set(node, { result }))
      .catch((error) => overlapCache.set(node, { error: error.message }))
      .finally(update);
    return null;
  }

  function highlightSet() {
    const set = new Set([state.node]);
    if (state.mode === "overlap") for (const match of overlapFor()?.matches || []) set.add(match.key);
    if (state.mode === "readers") for (const reader of readersOf(graph, state.node)) set.add(reader.id);
    if (state.mode === "impact" && state.column) {
      const impact = impactFor();
      for (const item of [...(impact?.affected || []), ...(impact?.unknown || []), ...(impact?.observed || [])]) set.add(item.model);
    }
    if (state.mode === "lineage" && state.column) {
      const collect = (tree) => { set.add(tree.node); tree.sources.forEach(collect); };
      collect(lineageOf(graph, state.node, state.column));
    }
    return set;
  }

  function drawEdges(lit) {
    const box = canvas.getBoundingClientRect();
    const width = grid.scrollWidth;
    const height = grid.scrollHeight;
    svg.setAttribute("width", width);
    svg.setAttribute("height", height);
    svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
    svg.replaceChildren();
    const at = (id) => grid.querySelector(`[data-id="${CSS.escape(id)}"]`)?.getBoundingClientRect();
    for (const edge of data.edges) {
      const a = at(edge.from);
      const b = at(edge.to);
      if (!a || !b) continue;
      const x1 = a.right - box.left + canvas.scrollLeft;
      const y1 = a.top + a.height / 2 - box.top + canvas.scrollTop;
      const x2 = b.left - box.left + canvas.scrollLeft;
      const y2 = b.top + b.height / 2 - box.top + canvas.scrollTop;
      const bend = Math.max(24, (x2 - x1) / 2);
      const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
      path.setAttribute("d", `M${x1},${y1} C${x1 + bend},${y1} ${x2 - bend},${y2} ${x2},${y2}`);
      path.setAttribute("class", `edge edge-${edge.source}${lit.has(edge.from) && lit.has(edge.to) ? " is-lit" : ""}`);
      const title = document.createElementNS("http://www.w3.org/2000/svg", "title");
      title.textContent = `${edge.from} → ${edge.to}: ${EDGE_SOURCES[edge.source]?.[0] || edge.source}, ${edge.confidence} confidence`;
      path.append(title);
      svg.append(path);
    }
  }

  function update() {
    const lit = highlightSet();
    for (const button of grid.querySelectorAll(".gnode")) {
      button.classList.toggle("is-selected", button.dataset.id === state.node);
      button.classList.toggle("is-lit", lit.has(button.dataset.id) && button.dataset.id !== state.node);
      button.classList.toggle("is-dim", !lit.has(button.dataset.id));
    }
    for (const tab of modeTabs.children) tab.setAttribute("aria-selected", String(tab.dataset.mode === state.mode));
    drawEdges(lit);
    renderDetail();
    const url = new URL(location.href);
    url.searchParams.set("node", state.node);
    url.searchParams.set("mode", state.mode);
    if (state.column) url.searchParams.set("column", state.column); else url.searchParams.delete("column");
    history.replaceState(null, "", url);
  }

  function nodeLink(id, suffix) {
    return h("button", { type: "button", class: "link-button node-link", onclick: () => select(id, null) }, id, suffix ? h("span", { class: "muted", text: `.${suffix}` }) : null);
  }

  function columnPicker(node) {
    if (!node.columns.length) return h("p", { class: "muted small", text: "No columns known for this asset." });
    const picker = h("select", { class: "field", "aria-label": "Column", onchange: (event) => { state.column = event.target.value; update(); } },
      node.columns.map((column) => h("option", { value: column, selected: column === state.column }, column)));
    return h("label", { class: "field-label" }, "Column", picker);
  }

  function edgeMeta(edge) {
    const [label, title] = EDGE_SOURCES[edge.source] || [edge.source, ""];
    return h("span", { class: "edge-meta" },
      tag(label, edge.source === "both" ? "ok" : edge.source === "parsed" ? "warn" : "info", title),
      tag(`${edge.confidence} confidence`, CONFIDENCE_TONE[edge.confidence] || "idle"),
      edge.last_seen ? h("span", { class: "muted small", text: `last seen ${shortDate(edge.last_seen)}` }) : h("span", { class: "muted small", text: "not seen in window" }));
  }

  function renderDetail() {
    const node = graph.nodes.get(state.node);
    const gap = graph.gaps.get(node.id);
    const head = h("div", { class: "detail-head" },
      h("p", { class: "eyebrow", text: NODE_KINDS[node.kind] || node.kind }),
      h("h2", { class: "detail-title", text: node.id }),
      h("div", { class: "detail-tags" },
        tag(EDGE_SOURCES[node.source]?.[0] || node.source, node.source === "both" ? "ok" : "info", "How this asset's identity was established"),
        gap ? tag("Not analyzed", "warn") : null),
      node.note ? h("p", { class: "muted small", text: node.note }) : null);
    let body;
    if (state.mode === "readers") {
      const readers = readersOf(graph, node.id);
      const direct = readers.filter((reader) => reader.hops === 1);
      const further = readers.filter((reader) => reader.hops > 1);
      const row = (reader) => h("li", { class: "reader" }, nodeLink(reader.id), edgeMeta(reader.edge),
        reader.hops > 1 ? h("span", { class: "muted small", text: `${reader.hops} hops, via ${reader.edge.from}` }) : null);
      body = h("div", {},
        h("h3", { text: `Direct readers (${direct.length})` }),
        direct.length ? h("ul", { class: "plain-list" }, direct.map(row)) : h("p", { class: "muted small", text: "No known readers." }),
        h("h3", { text: `Downstream (${further.length})` }),
        further.length ? h("ul", { class: "plain-list" }, further.map(row)) : h("p", { class: "muted small", text: "None beyond direct readers." }),
        graph.data.gaps.some((item) => item.kind === "unattributed_reads")
          ? h("p", { class: "callout", text: "Some ad hoc queries read tables without a destination. They are counted under Gaps, not listed here." }) : null);
    } else if (state.mode === "impact") {
      const change = h("select", { class: "field", "aria-label": "Change type", onchange: (event) => { state.change = event.target.value; update(); } },
        [["drop", "Drop column"], ["rename", "Rename column"], ["expression", "Change expression"]].map(([value, label]) =>
          h("option", { value, selected: value === state.change }, label)));
      const impact = impactFor();
      const entry = state.column ? impactCache.get(JSON.stringify([node.id, state.column, state.change])) : null;
      const effectTag = (effect) => {
        const [text, tone, title] = IMPACT_EFFECTS[effect] || [effect, "idle", ""];
        return tag(text, tone, title);
      };
      const heading = h("div", { class: "field-row" }, columnPicker(node), h("label", { class: "field-label" }, "Change", change));
      if (!impact) {
        body = h("div", {}, heading,
          h("p", { class: "muted small", text: entry?.error || (state.column ? "Assessing the change…" : "Pick a column to assess.") }));
      } else {
        const depthNote = (item) => (item.depth > 1 ? `${item.depth} hops` : "direct");
        body = h("div", {}, heading,
          h("h3", { text: `Affected (${impact.affected.length})` }),
          impact.affected.length ? h("ul", { class: "plain-list" }, impact.affected.map((item) =>
            h("li", { class: "reader" }, nodeLink(item.model),
              h("span", { class: "edge-meta" }, effectTag(item.effect),
                h("span", { class: "muted small", text: depthNote(item) }),
                item.columns?.length ? h("span", { class: "muted small", text: `uses ${item.columns.join(", ")}` }) : null))))
            : h("p", { class: "muted small", text: "No declared model uses this column." }),
          impact.unknown.length ? h("h3", { text: `Unknown (${impact.unknown.length})` }) : null,
          impact.unknown.length ? h("ul", { class: "plain-list" }, impact.unknown.map((item) =>
            h("li", { class: "reader" }, nodeLink(item.model), h("span", { class: "edge-meta" }, E.pill("unknown"),
              h("span", { class: "muted small", text: UNKNOWN_REASONS[item.reason] || item.reason }))))) : null,
          impact.observed.length ? h("h3", { text: `Seen in job history (${impact.observed.length})` }) : null,
          impact.observed.length ? h("ul", { class: "plain-list" }, impact.observed.map((item) =>
            h("li", { class: "reader" }, nodeLink(item.model),
              h("span", { class: "edge-meta" }, tag("Observed", "info", EDGE_SOURCES.observed[1]), effectTag(item.effect),
                tag(`${item.confidence} confidence`, CONFIDENCE_TONE[item.confidence] || "idle"),
                h("span", { class: "muted small", text: item.last_seen ? `last seen ${shortDate(item.last_seen)}` : "not seen in window" }),
                h("span", { class: "muted small", text: `${depthNote(item)} via ${item.via}` }))))) : null,
          impact.observed.length ? h("p", { class: "muted small", text: "Job history names tables, not columns, so these readers are listed as possibly affected." }) : null,
          impact.complete ? null : h("p", { class: "callout", text: "This result may miss readers: " + impact.incomplete_reasons.join(", ").replaceAll("_", " ") + "." }),
          h("p", { class: "callout", text: "“Safe to delete” is not offered until graph coverage is complete." }));
      }
    } else if (state.mode === "overlap") {
      const section = overlapFor();
      const entry = overlapCache.get(node.id);
      const link = (label, id) => (graph.nodes.has(id) ? nodeLink(id) : h("span", { class: "mono", text: label }));
      body = h("div", {},
        h("h3", { text: "Tables that already provide the same attributes" }),
        section ? overlapList(section, link)
          : h("p", { class: "muted small", text: entry?.error || "Comparing…" }),
        h("p", { class: "muted small", text: "Match kind comes from what each table computes, never from names. This is advisory; it never blocks a change." }));
    } else {
      const tree = state.column ? lineageOf(graph, node.id, state.column) : null;
      const renderTree = (item) => h("li", {},
        h("div", { class: `lineage-row${item.unknown ? " is-unknown" : ""}` },
          nodeLink(item.node, item.column),
          h("span", { class: "lineage-transform", text: item.transform }),
          item.unknown ? E.pill("unknown") : null),
        item.sources.length ? h("ul", { class: "lineage-tree" }, item.sources.map(renderTree)) : null);
      body = h("div", {}, columnPicker(node),
        tree ? h("ul", { class: "lineage-tree is-root" }, renderTree(tree)) : null,
        h("p", { class: "muted small", text: "Columns that cannot be traced are marked unknown instead of guessed." }));
    }
    detail.replaceChildren(head, body);
  }

  let resizeTimer;
  window.addEventListener("resize", () => { clearTimeout(resizeTimer); resizeTimer = setTimeout(update, 100); });
  requestAnimationFrame(update);
}

function stat(label, value, title) {
  return h("div", { title }, h("dt", { text: label }), h("dd", { text: value }));
}

function gapsPanel(data) {
  const kinds = {
    parse_error: "Could not parse", inaccessible: "Not accessible",
    unmatched_reference: "Unmatched reference", unattributed_reads: "No destination",
    skipped_statements: "Statements skipped", unparsed_operation: "Not analyzed", cycle: "Dependency cycle",
  };
  return panel("Gaps", { note: "Parts of the project this graph could not see", id: "gaps" },
    h("div", { class: "table-wrap" }, h("table", { class: "data-table" },
      h("thead", {}, h("tr", {}, h("th", { text: "Asset" }), h("th", { text: "Problem" }), h("th", { text: "What it means" }))),
      h("tbody", {}, data.gaps.map((gap) => h("tr", {},
        h("td", { class: "mono", text: gap.asset }),
        h("td", {}, tag(kinds[gap.kind] || gap.kind, "warn")),
        h("td", { text: gap.message })))))));
}

/* ======================================================================
   Cost (#30-#35)
   ====================================================================== */

function renderCost(data, root) {
  const c = data.currency;
  const totals = data.totals;
  root.append(h("div", { class: "tiles" },
    tile("Measured cost", money(totals.measured, c), windowLabel(data.window), basisTag("measured")),
    tile("Attributed to the graph", money(totals.attributed, c), `${percent(totals.attributed, totals.measured)} of measured cost`),
    tile("Unattributed", money(totals.unattributed, c), "Jobs that match no asset", null, "warn"),
    tile("Validated savings", money(data.validated.validated_savings, c), `From ${data.validated.accepted_changes} accepted change${data.validated.accepted_changes === 1 ? "" : "s"}`, basisTag("measured")),
    tile("Open estimates", money(data.validated.pending_estimates, c), "Not counted until validated", basisTag("estimate"))));

  const detail = h("div", { class: "reco" });
  const list = h("ol", { class: "opp-list" });
  const choose = (opportunity) => {
    for (const item of list.children) item.classList.toggle("is-selected", item.dataset.id === opportunity.id);
    detail.replaceChildren(recommendation(opportunity, c));
  };
  for (const opportunity of data.opportunities) {
    list.append(h("li", { "data-id": opportunity.id },
      h("button", { type: "button", class: "opp", onclick: () => choose(opportunity) },
        h("span", { class: "opp-rank", text: String(opportunity.rank) }),
        h("span", { class: "opp-body" },
          h("strong", { text: opportunity.title }),
          h("span", { class: "opp-meta" },
            h("span", { class: "mono", text: money(opportunity.savings.value, c) }), basisTag(opportunity.savings.basis),
            h("span", { class: "muted small", text: `${opportunity.frequency} · reaches ${opportunity.downstream_reach}` }),
            opportunity.status === "validated" ? tag("Validated", "ok") : null)))));
  }
  root.append(h("div", { class: "cost-layout" },
    panel("Opportunities", { note: "Ranked by measured cost, frequency and downstream reach" }, list),
    panel("Recommendation", {}, detail)));
  choose(data.opportunities[0]);

  const max = Math.max(...data.nodes.map((node) => node.measured), totals.unattributed);
  const bar = (label, value, extra, tone = "") => h("li", { class: "bar-row" },
    h("span", { class: "bar-label mono", text: label }),
    h("span", { class: "bar-track" }, h("span", { class: `bar-fill ${tone}`, style: { width: `${(value / max) * 100}%` } })),
    h("span", { class: "bar-value mono", text: money(value, c) }),
    h("span", { class: "muted small", text: extra }));
  root.append(h("div", { class: "cost-layout" },
    panel("Cost by asset", { note: `Measured, ${windowLabel(data.window)}` },
      h("ul", { class: "bars" },
        data.nodes.map((node) => bar(node.node, node.measured, `${node.runs} runs`)),
        bar("Unattributed", totals.unattributed, "no matching asset", "is-warn"))),
    panel("Cost rule catalog", { note: "Each rule ships alone" },
      h("div", { class: "table-wrap" }, h("table", { class: "data-table" },
        h("thead", {}, h("tr", {}, h("th", { text: "Rule" }), h("th", { text: "Safe when" }), h("th", { text: "Requires" }), h("th", { text: "Outcome" }))),
        h("tbody", {}, data.rules.map((rule) => h("tr", {},
          h("td", {}, h("strong", { text: rule.name }), h("br"), tag(rule.state === "shipped" ? "Shipped" : "Planned", rule.state === "shipped" ? "ok" : "idle")),
          h("td", { text: rule.safe_when }),
          h("td", {}, E.pill(rule.requires)),
          h("td", { class: "muted", text: rule.outcome })))))))));
}

function tile(label, value, note, badge, tone) {
  return h("div", { class: `tile${tone ? ` tile-${tone}` : ""}` },
    h("span", { class: "tile-label", text: label }),
    h("strong", { class: "tile-value", text: value }),
    h("span", { class: "tile-note" }, badge, note ? h("span", { text: note }) : null));
}

/** The first recommendation format (#33): where, who, what, and how verified. */
function recommendation(opportunity, currency) {
  const section = (number, title, ...body) => h("div", { class: "reco-step" },
    h("span", { class: "reco-num", text: number }), h("div", {}, h("h3", { text: title }), ...body));
  return h("div", {},
    h("div", { class: "reco-head" },
      h("h3", { class: "reco-title", text: opportunity.title }),
      h("p", { class: "reco-savings" },
        "Saves ", h("strong", { class: "mono", text: opportunity.savings.range
          ? `${money(opportunity.savings.range[0], currency)} to ${money(opportunity.savings.range[1], currency)}`
          : money(opportunity.savings.value, currency) }),
        " a month ", basisTag(opportunity.savings.basis),
        h("span", { class: "muted small", text: ` of ${money(opportunity.measured_cost, currency)} measured` }))),
    section("1", "Where the work repeats",
      h("ul", { class: "plain-list" }, opportunity.repeats.map((item) => h("li", {}, h("span", { class: "mono", text: item.node }), h("span", { class: "muted small", text: ` ${item.where}` }))))),
    section("2", "Who relies on it",
      h("p", { class: "chips" }, opportunity.consumers.map((id) => h("a", { class: "chip mono", href: `/graph?node=${encodeURIComponent(id)}&mode=readers`, text: id })))),
    section("3", "Proposed change", h("p", { text: opportunity.proposed_change }),
      h("p", { class: "muted small" }, "Rule: ", h("span", { class: "mono", text: opportunity.rule }))),
    section("4", "How it would be verified",
      h("p", {}, "Needs ", E.pill(opportunity.verification.required), " for every changed consumer."),
      h("ul", { class: "plain-list" }, opportunity.verification.plan.map((step) => h("li", { text: step })))));
}

/* ======================================================================
   Change reports, CI, sources and proposals (#22, #36-#42)
   ====================================================================== */

function renderChanges(data, root) {
  const report = data.report;
  const changed = report.changes.filter((change) => change.kind !== "unchanged");
  const counts = {};
  for (const change of report.changes) {
    const label = E.normalize(change.verification.label);
    counts[label] = (counts[label] || 0) + 1;
  }

  const head = h("div", { class: "report-head" },
    h("div", {},
      h("h2", { class: "report-name", text: report.title }),
      h("p", { class: "muted small" }, h("span", { class: "mono", text: `${report.base} → ${report.head}` }), ` · generated ${shortDate(report.generated_at)}`)),
    h("div", { class: "report-counts" },
      ["proven", "planner_checked", "unproven", "failed", "unchanged"].filter((key) => counts[key]).map((key) =>
        h("span", { class: "count-pill" }, h("strong", { text: String(counts[key]) }), E.pill(key)))));

  const rows = report.changes.map((change) => {
    const label = E.normalize(change.verification.label);
    const cost = change.cost.basis === "unavailable"
      ? h("span", { class: "muted", text: "–" })
      : h("span", {}, h("span", { class: "mono", text: `${money(change.cost.before)} → ${money(change.cost.after)}` }), " ", basisTag(change.cost.basis));
    const consumers = change.consumers.models;
    return h("tr", {},
      h("td", {}, h("span", { class: "mono", text: change.model }), h("br"), h("span", { class: "muted small", text: change.kind })),
      h("td", {}, E.pill(label), h("p", { class: "muted small reason", text: change.verification.reason }),
        change.verification.checks.length ? h("div", { class: "ev-checks" }, change.verification.checks.map(E.checkChip)) : null),
      h("td", {}, cost),
      h("td", {},
        consumers.length ? h("span", { class: "chips" }, consumers.map((id) => h("a", { class: "chip mono", href: `/graph?node=${encodeURIComponent(id)}&mode=readers`, text: id }))) : h("span", { class: "muted", text: "None known" }),
        change.consumers.complete ? null : h("div", {}, tag("Incomplete", "warn", "Some readers could not be analyzed"))));
  });

  const diagnostics = report.diagnostics.length
    ? h("div", { class: "callout callout-warn" },
      h("strong", { text: `${report.diagnostics.length} asset${report.diagnostics.length === 1 ? "" : "s"} could not be analyzed. ` }),
      "The rest of this report is complete.",
      h("ul", { class: "plain-list" }, report.diagnostics.map((item) => h("li", {}, h("span", { class: "mono", text: item.asset }), ` ${item.message}`))))
    : null;

  root.append(panel("Change report", { note: `${changed.length} changed model${changed.length === 1 ? "" : "s"}` },
    head,
    diagnostics,
    h("div", { class: "table-wrap" }, h("table", { class: "data-table change-table" },
      h("thead", {}, h("tr", {}, h("th", { text: "Model" }), h("th", { text: "Behavior" }), h("th", { text: "Cost per month" }), h("th", { text: "Affected consumers" }))),
      h("tbody", {}, rows))),
    h("details", { class: "ev-legend-wrap in-card" }, h("summary", { text: "What the labels mean" }), E.legend())));

  const compared = report.changes.filter((change) => change.overlaps);
  if (compared.length) {
    root.append(panel("Already done elsewhere", { note: "Advisory. Never blocks a change." },
      ...compared.map((change) => h("div", { class: "overlap-change" },
        h("h3", { class: "mono", text: change.model }),
        overlapList(change.overlaps, (label, id) => h("a", { class: "chip mono", href: `/graph?node=${encodeURIComponent(id)}&mode=overlap`, text: label }))))));
  }

  root.append(h("div", { class: "cost-layout" }, coveragePanel(data.evidence_coverage), ciPanel(data.ci, report)));
  root.append(h("div", { class: "cost-layout" }, proposalsPanel(data.proposals), sourcesPanel(data.sources)));
}

/** Share of changed outputs with useful evidence (#22); proof and planner stay separate. */
function coveragePanel(coverage) {
  const useful = coverage.useful_evidence ?? coverage.proven;
  const agreed = coverage.synthetic_agreed ?? 0;
  const keys = ["proven", "planner_checked", "unproven", "failed"];
  return panel("Evidence coverage", { note: `${coverage.changed} changed outputs, anonymized aggregate` },
    h("div", { class: "stacked", role: "img", "aria-label": keys.map((key) => `${E.LABELS[key].title} ${percent(coverage[key], coverage.changed)}`).join(", ") },
      keys.map((key) => h("span", { class: `stacked-seg ev-${E.LABELS[key].tone}`, style: { flex: String(coverage[key]) }, title: `${E.LABELS[key].title}: ${coverage[key]}` }))),
    h("dl", { class: "coverage-stats is-grid" },
      keys.map((key) => h("div", {}, h("dt", {}, E.pill(key)), h("dd", { text: percent(coverage[key], coverage.changed) })))),
    h("p", { class: "coverage-headline", "data-testid": "useful-evidence", text: `Useful evidence: ${percent(useful, coverage.changed)} of changed outputs (${useful} of ${coverage.changed}), including ${agreed} that agree on synthetic data.` }),
    h("p", { class: "muted small", text: "Proof, synthetic agreement and planner checks are counted separately. Agreement is evidence, not proof, and a planner check is never counted as either." }));
}

/** Preview of the check posted on a review request (#37). */
function ciPanel(ci, report) {
  return panel("Code review check", { note: "Posted by CI on each review request" },
    h("div", { class: "ci-card" },
      h("div", { class: "ci-row" }, h("span", { class: `ci-dot ci-${ci.conclusion}` }), h("strong", { text: ci.check_name }), tag(ci.conclusion === "neutral" ? "Needs review" : ci.conclusion, ci.conclusion === "success" ? "ok" : "warn")),
      h("p", { class: "muted small", text: ci.summary }),
      h("ul", { class: "plain-list ci-lines" }, report.changes.filter((change) => change.kind !== "unchanged").map((change) =>
        h("li", {}, E.pill(change.verification.label), " ", h("span", { class: "mono", text: change.model }),
          change.consumers.models.length ? h("span", { class: "muted small", text: ` → ${change.consumers.models.length} consumers` }) : null)))));
}

/** Graph-wide proposals (#40-#42): ready only when every consumer has a result. */
function proposalsPanel(proposals) {
  const kinds = { shared_logic: "Shared-logic extraction", upstream_filter: "Upstream filter" };
  return panel("Guided refactors", { note: "Proposals across models" },
    proposals.map((proposal) => {
      const missing = proposal.consumers.filter((item) => !["proven", "unchanged"].includes(E.normalize(item.label)));
      return h("article", { class: "proposal" },
        h("div", { class: "proposal-head" },
          h("div", {}, h("p", { class: "eyebrow", text: kinds[proposal.kind] || proposal.kind }), h("h3", { text: proposal.title })),
          proposal.ready ? tag("Ready for review", "ok") : tag("Not ready", "warn")),
        h("p", { class: "muted small", text: proposal.cost_rationale }),
        h("ul", { class: "consumer-grid" }, proposal.consumers.map((item) =>
          h("li", {}, h("span", { class: "mono", text: item.node }), E.pill(item.label)))),
        proposal.ready ? null : h("p", { class: "small warn-text", text: `${missing.length} consumer${missing.length === 1 ? " needs" : "s need"} a proof before this proposal is ready.` }));
    }));
}

/** Query sources feeding the graph (#38). */
function sourcesPanel(sources) {
  return panel("Query sources", { note: "Enabled once identities reconcile" },
    h("ul", { class: "plain-list sources" }, sources.map((source) => h("li", { class: "source-row" },
      h("div", {}, h("strong", { text: source.name }), h("br"), h("span", { class: "muted small", text: source.kind })),
      source.matched !== null ? h("span", { class: "mono small", text: `${Math.round(source.matched * 100)}% matched` }) : null,
      tag(source.state === "connected" ? "Connected" : source.state === "planned" ? "Planned" : source.state === "not_enabled" ? "Not enabled" : "Error",
        source.state === "connected" ? "ok" : source.state === "planned" || source.state === "not_enabled" ? "idle" : "bad")))));
}

start();
