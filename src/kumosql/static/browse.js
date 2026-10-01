const status = document.getElementById("catalog-status");
const tree = document.getElementById("tree");
const tagBar = document.getElementById("tag-bar");
const tagFilter = document.getElementById("tag-filter");
const freshnessLabel = document.getElementById("catalog-freshness");
const checked = new Set();
const freshness = {};
let selectedNode = null;
const ROUTINE_ICONS = { UDF: "ƒ", TABLE_FUNCTION: "ƒ", AGGREGATE_FUNCTION: "ƒ", PROCEDURE: "⚙" };
const typeLabel = (type) => type.replaceAll("_", " ").toLowerCase();

function setStatus(message, error = false) {
  status.textContent = message;
  status.classList.toggle("error", error);
}

function queryUrl(endpoint, values) {
  return `/api/catalog/${endpoint}?${new URLSearchParams(values).toString()}`;
}

function ago(seconds) {
  if (seconds < 60) return "just now";
  if (seconds < 3600) return `${Math.floor(seconds / 60)} min ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)} h ago`;
  return `${Math.floor(seconds / 86400)} d ago`;
}

function renderFreshness() {
  const levels = Object.values(freshness);
  if (!levels.length) {
    freshnessLabel.textContent = "";
    return;
  }
  const oldest = Math.min(...levels.map((level) => level.fetchedAt));
  const stale = levels.some((level) => level.stale);
  const cached = levels.some((level) => level.cached);
  const updating = levels.some((level) => level.refreshing);
  freshnessLabel.textContent = `${stale ? "Offline, showing saved copy from" : cached ? "Saved copy from" : "Loaded"} ${ago(Date.now() / 1000 - oldest)}${updating ? ", updating…" : ""}`;
  freshnessLabel.classList.toggle("error", stale);
}

// Saved answers arrive instantly. When one has expired the server returns it at once and
// refreshes it in the background, so poll briefly and call onUpdate with the new data.
async function requestJson(url, refresh = false, onUpdate = null) {
  const response = await fetch(refresh ? `${url}&refresh=1` : url);
  const payload = await response.json();
  if (!response.ok) {
    const failure = new Error(payload.error || "BigQuery request failed");
    failure.removed = Boolean(payload.removed);
    throw failure;
  }
  freshness[url] = payload;
  renderFreshness();
  if (payload.refreshing && onUpdate) pollForUpdate(url, payload.fetchedAt, onUpdate, 8);
  return payload.data;
}

async function pollForUpdate(url, since, onUpdate, tries) {
  await new Promise((resolve) => setTimeout(resolve, 2500));
  if (freshness[url]?.fetchedAt !== since) return; // the user moved on
  try {
    const payload = await (await fetch(url)).json();
    if (payload.fetchedAt > since) {
      freshness[url] = payload;
      renderFreshness();
      onUpdate(payload.data);
    } else if (payload.refreshing && tries > 1) {
      pollForUpdate(url, since, onUpdate, tries - 1);
    } else {
      freshness[url] = { ...freshness[url], refreshing: false };
      renderFreshness();
    }
  } catch {
    /* keep showing the saved copy */
  }
}

setInterval(renderFreshness, 30000);

/* ----- The tree: project > dataset > table or routine > column ----- */

const objectKey = (project, dataset, id) => `${project}.${dataset}.${id}`;

function span(className, text) {
  const element = document.createElement("span");
  element.className = className;
  element.textContent = text;
  return element;
}

// One tree row. `load(refresh)` fills `node.children` when the node opens; leaves have no `load`.
function makeNode({ id, level, icon, label, detail, load, refreshTitle, check }) {
  const item = document.createElement("div");
  item.setAttribute("role", "treeitem");
  item.dataset.id = id;
  const row = document.createElement("div");
  row.className = "bq-row";
  row.style.setProperty("--level", String(level));
  const caret = span("bq-caret", load ? "▸" : "");
  caret.setAttribute("aria-hidden", "true");
  const symbol = span("catalog-icon", icon);
  symbol.setAttribute("aria-hidden", "true");
  row.append(caret, symbol, span("bq-label", label));
  if (detail) row.append(span("catalog-detail", detail));
  const node = { item, row, load, children: document.createElement("div"), loaded: false };
  node.children.setAttribute("role", "group");
  node.children.hidden = true;
  if (check) {
    const box = document.createElement("input");
    box.type = "checkbox";
    box.className = "catalog-check";
    box.checked = checked.has(check);
    box.setAttribute("aria-label", `Select ${label} to tag`);
    box.addEventListener("click", (event) => event.stopPropagation());
    box.addEventListener("change", () => {
      if (box.checked) checked.add(check); else checked.delete(check);
      renderBulk();
    });
    row.prepend(box);
    node.tagKey = check;
  }
  if (load) {
    item.setAttribute("aria-expanded", "false");
    const refresh = document.createElement("button");
    refresh.type = "button";
    refresh.className = "panel-refresh bq-refresh";
    refresh.title = refreshTitle;
    refresh.setAttribute("aria-label", refreshTitle);
    refresh.textContent = "↻";
    refresh.addEventListener("click", async (event) => {
      event.stopPropagation();
      await spin(refresh, () => reload(node, true));
    });
    row.append(refresh);
  }
  row.tabIndex = 0;
  row.addEventListener("click", () => {
    select(node);
    if (load) toggle(node);
  });
  row.addEventListener("keydown", (event) => {
    if (event.target !== row) return;
    if (event.key === "Enter" || event.key === " ") { event.preventDefault(); row.click(); }
    else if (event.key === "ArrowRight" && load && item.getAttribute("aria-expanded") === "false") toggle(node);
    else if (event.key === "ArrowLeft" && load && item.getAttribute("aria-expanded") === "true") toggle(node);
  });
  item.append(row, node.children);
  item._node = node;
  return node;
}

async function spin(button, task) {
  button.disabled = true;
  button.classList.add("spinning");
  try {
    await task();
  } finally {
    button.disabled = false;
    button.classList.remove("spinning");
  }
}

function select(node) {
  selectedNode?.row.setAttribute("aria-current", "false");
  selectedNode = node;
  node.row.setAttribute("aria-current", "true");
}

function setExpanded(node, open) {
  node.item.setAttribute("aria-expanded", String(open));
  node.children.hidden = !open;
  node.row.querySelector(".bq-caret").textContent = open ? "▾" : "▸";
}

async function toggle(node) {
  const open = node.item.getAttribute("aria-expanded") !== "true";
  setExpanded(node, open);
  if (open && !node.loaded) await reload(node, false);
}

async function reload(node, refresh) {
  if (node.item.getAttribute("aria-expanded") !== "true") setExpanded(node, true);
  node.loaded = true;
  if (!node.children.childElementCount) message(node, "Loading…");
  try {
    await node.load(refresh);
  } catch (error) {
    if (node.item.isConnected) {
      message(node, error.message);
      setStatus(error.message, true);
    }
  }
}

function message(node, text) {
  node.children.replaceChildren();
  const empty = document.createElement("div");
  empty.className = "empty-state";
  empty.style.setProperty("--level", String(Number(node.row.style.getPropertyValue("--level")) + 1));
  empty.textContent = text;
  node.children.append(empty);
}

// Replace a node's children with `nodes`, keeping the rows (and what is open under them) that are still listed.
function fill(parent, nodes, emptyText) {
  const existing = new Map([...parent.children.children].filter((child) => child._node).map((child) => [child.dataset.id, child._node]));
  const kept = nodes.map((node) => existing.get(node.item.dataset.id) || node);
  parent.children.replaceChildren(...kept.map((node) => node.item));
  if (!kept.length && emptyText) message(parent, emptyText);
  applyFilter();
  renderBulk();
}

function projectNode(project) {
  const node = makeNode({
    id: project, level: 0, icon: "▣", label: project, refreshTitle: `Refresh datasets in ${project}`,
    load: async (refresh) => {
      setStatus("");
      const show = (datasets) => fill(node, datasets.map((dataset) => datasetNode(project, dataset)), `No datasets found in ${project}.`);
      show(await requestJson(queryUrl("datasets", { project }), refresh, (fresh) => show(fresh)));
    },
  });
  return node;
}

function datasetNode(project, dataset) {
  const node = makeNode({
    id: dataset.id, level: 1, icon: "▤", label: dataset.id, detail: dataset.location, refreshTitle: `Refresh tables in ${dataset.id}`,
    load: async (refresh) => {
      setStatus("");
      const show = (tables) => fill(node, tables.map((table) => tableNode(project, dataset.id, table)), "No tables found.");
      const url = queryUrl("tables", { project, dataset: dataset.id });
      // Rule tags are computed from the saved catalog, which this list was just saved to, so read them again.
      const tables = await requestJson(url, refresh, (fresh) => {
        window.KumoTags?.load().then(() => { if (node.item.isConnected) show(fresh); });
      });
      await window.KumoTags?.load();
      if (node.item.isConnected) show(tables);
    },
  });
  return node;
}

function tableNode(project, dataset, table) {
  const key = objectKey(project, dataset, table.id);
  const routine = ROUTINE_ICONS[table.type];
  const icon = routine || (table.type === "VIEW" || table.type === "MATERIALIZED_VIEW" ? "◫" : "▦");
  const base = { id: table.id, level: 2, icon, label: table.id, detail: typeLabel(table.type), check: key };
  // Functions and procedures have no schema to read; they can still be tagged.
  const node = makeNode(routine ? base : {
    ...base,
    refreshTitle: `Refresh columns of ${table.id}`,
    load: async (refresh) => {
      setStatus("");
      const show = (metadata) => fill(node, fieldNodes(metadata.schema || [], 3), "No columns.");
      const url = queryUrl("table", { project, dataset, table: table.id });
      try {
        show(await requestJson(url, refresh, show));
      } catch (error) {
        if (!error.removed) throw error;
        // No access to this table: stop listing it.
        checked.delete(key);
        node.item.remove();
        renderBulk();
        setStatus(`You do not have access to ${table.id}, so it was removed from the list.`, true);
      }
    },
  });
  node.tagKey = key;
  node.drawTags = () => {
    node.row.querySelector(".tag-chips")?.remove();
    const chips = window.KumoTags?.chips(key, { max: 3 });
    if (chips?.children.length) node.row.append(chips);
  };
  node.drawTags();
  return node;
}

function fieldNodes(fields, level) {
  return fields.map((field, index) => {
    const nested = field.fields?.length;
    const node = makeNode({
      id: `${index}:${field.name}`, level, icon: "▫", label: field.name || "(unnamed)",
      detail: [field.type, field.mode && field.mode !== "NULLABLE" ? field.mode.toLowerCase() : ""].filter(Boolean).join(" · "),
      load: nested ? async () => fill(node, fieldNodes(field.fields, level + 1)) : null,
    });
    return node;
  });
}

/* ----- Tags: filter and multi-select tagging ----- */

window.KumoTags?.onError((message) => setStatus(`Tags could not be read: ${message}`, true));
const bulkEditor = window.KumoTags?.editor(() => [...checked], { onStatus: setStatus });
if (bulkEditor) document.getElementById("bulk-editor").append(bulkEditor);

function tableItems() {
  return [...tree.querySelectorAll('[role="treeitem"]')].filter((item) => item._node.tagKey);
}

function applyFilter() {
  const tags = window.KumoTags;
  if (!tags) return;
  const current = tagFilter.value;
  const names = tags.names();
  tagFilter.replaceChildren(new Option("All tags", ""), ...names.map((name) => new Option(name, name)));
  tagFilter.value = names.includes(current) ? current : "";
  tagBar.hidden = !names.length;
  for (const item of tableItems()) {
    item._node.drawTags?.();
    item.hidden = Boolean(tagFilter.value) && !tags.hasTag(item._node.tagKey, tagFilter.value);
  }
}

function renderBulk() {
  const present = new Set(tableItems().map((item) => item._node.tagKey));
  for (const key of [...checked]) if (!present.has(key)) checked.delete(key);
  document.getElementById("tag-bulk").hidden = checked.size === 0;
  document.getElementById("bulk-count").textContent = String(checked.size);
  bulkEditor?.redraw();
}

tagFilter.addEventListener("change", applyFilter);
document.getElementById("bulk-clear").addEventListener("click", () => {
  checked.clear();
  for (const box of tree.querySelectorAll(".catalog-check")) box.checked = false;
  renderBulk();
});
// A tag changed (here, in Settings, or by a rule after a reload): redraw the chips.
window.KumoTags?.onChange(applyFilter);

/* ----- Projects: the user's saved choice, read locally; BigQuery is contacted when a project is opened ----- */

function renderProjects(projects) {
  document.getElementById("refresh").disabled = !projects.length;
  const existing = new Map([...tree.children].filter((child) => child._node).map((child) => [child.dataset.id, child._node]));
  if (!projects.length) {
    tree.replaceChildren();
    const empty = document.createElement("div");
    empty.className = "empty-state";
    empty.textContent = "No projects chosen yet.";
    const choose = document.createElement("button");
    choose.type = "button";
    choose.className = "primary-button";
    choose.textContent = "Choose projects";
    choose.addEventListener("click", openProjectSettings);
    tree.append(empty, choose);
    return;
  }
  tree.replaceChildren(...projects.map((id) => (existing.get(id) || projectNode(id)).item));
  if (selectedNode && !selectedNode.item.isConnected) selectedNode = null;
  applyFilter();
  renderBulk();
}

async function loadProjects() {
  try {
    renderProjects(await window.KumoBqProjects.loadSelection());
    setStatus("");
  } catch (error) {
    tree.replaceChildren();
    setStatus(error.message, true);
  }
}

// Refresh all re-reads every open level from BigQuery, parents first, keeping what is open.
async function refreshAll() {
  const button = document.getElementById("refresh");
  await spin(button, async () => {
    await loadProjects();
    for (const item of [...tree.querySelectorAll('[aria-expanded="true"]')]) {
      if (item.isConnected) await reload(item._node, true);
    }
  });
}

function openProjectSettings() {
  window.KumoSettings.open("bigquery");
}

document.getElementById("refresh").addEventListener("click", refreshAll);
document.getElementById("choose-projects").addEventListener("click", openProjectSettings);
window.KumoTags?.load().then(applyFilter);
loadProjects();

// Choosing projects here or in Settings updates the tree, keeping the projects that are still chosen.
window.addEventListener("kumosql:bq-projects", (event) => {
  renderProjects(event.detail || []);
  setStatus("");
});
