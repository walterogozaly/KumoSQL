const status = document.getElementById("catalog-status");
const projectsPane = document.getElementById("projects");
const datasetsPane = document.getElementById("datasets");
const tablesPane = document.getElementById("tables");
const schemaPane = document.getElementById("schema");
let chosenProject = "";
let chosenDataset = "";
let chosenTable = "";
let projectRequest = 0;
let datasetRequest = 0;
let tableRequest = 0;
let schemaRequest = 0;
let shownTables = [];
const checked = new Set();
const tagBar = document.getElementById("tag-bar");
const tagFilter = document.getElementById("tag-filter");
const ROUTINE_ICONS = { UDF: "ƒ", TABLE_FUNCTION: "ƒ", AGGREGATE_FUNCTION: "ƒ", PROCEDURE: "⚙" };
const objectKey = (id) => `${chosenProject}.${chosenDataset}.${id}`;

function setStatus(message, error = false) {
  status.textContent = message;
  status.classList.toggle("error", error);
}

function clearPane(pane, emptyText) {
  pane.replaceChildren();
  if (emptyText) {
    const empty = document.createElement("p");
    empty.className = "empty-state";
    empty.textContent = emptyText;
    pane.append(empty);
  }
}

function queryUrl(endpoint, values) {
  const params = new URLSearchParams(values);
  return `/api/catalog/${endpoint}?${params.toString()}`;
}

const freshness = {};
const freshnessLabel = document.getElementById("catalog-freshness");

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
async function requestJson(url, level, refresh = false, onUpdate = null) {
  const response = await fetch(refresh ? `${url}&refresh=1` : url);
  const payload = await response.json();
  if (!response.ok) {
    const failure = new Error(payload.error || "BigQuery request failed");
    failure.removed = Boolean(payload.removed);
    throw failure;
  }
  freshness[level] = payload;
  renderFreshness();
  if (payload.refreshing && onUpdate) pollForUpdate(url, level, payload.fetchedAt, onUpdate, 8);
  return payload.data;
}

async function pollForUpdate(url, level, since, onUpdate, tries) {
  await new Promise((resolve) => setTimeout(resolve, 2500));
  if (freshness[level]?.fetchedAt !== since) return; // the user moved on
  try {
    const payload = await (await fetch(url)).json();
    if (payload.fetchedAt > since) {
      freshness[level] = payload;
      renderFreshness();
      onUpdate(payload.data);
    } else if (payload.refreshing && tries > 1) {
      pollForUpdate(url, level, since, onUpdate, tries - 1);
    } else {
      freshness[level] = { ...freshness[level], refreshing: false };
      renderFreshness();
    }
  } catch {
    /* keep showing the saved copy */
  }
}

setInterval(renderFreshness, 30000);

function itemButton(label, icon, detail, selected, onClick) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "catalog-item";
  button.setAttribute("aria-current", selected ? "true" : "false");
  const symbol = document.createElement("span");
  symbol.className = "catalog-icon";
  symbol.setAttribute("aria-hidden", "true");
  symbol.textContent = icon;
  const name = document.createElement("span");
  name.textContent = label;
  button.append(symbol, name);
  if (detail) {
    const hint = document.createElement("span");
    hint.className = "catalog-detail";
    hint.textContent = detail;
    button.append(hint);
  }
  button.addEventListener("click", onClick);
  return button;
}

function renderProjects(projects) {
  clearPane(projectsPane);
  for (const id of projects) {
    const button = itemButton(id, "▣", "", id === chosenProject, () => selectProject(id));
    button.dataset.value = id;
    projectsPane.append(button);
  }
  document.getElementById("project-count").textContent = projects.length ? String(projects.length) : "";
  document.getElementById("refresh").disabled = !projects.length;
  if (!projects.length) {
    clearPane(projectsPane, "No projects chosen yet.");
    const choose = document.createElement("button");
    choose.type = "button";
    choose.className = "primary-button";
    choose.textContent = "Choose projects";
    choose.addEventListener("click", openProjectSettings);
    projectsPane.append(choose);
    setStatus("Choose at least one project to start. Nothing is loaded from BigQuery until you do.");
  }
}

// The project list is the user's saved choice, read locally; BigQuery is only
// contacted when a project is opened.
async function loadProjects() {
  const version = ++projectRequest;
  chosenProject = chosenDataset = chosenTable = "";
  for (const key of Object.keys(freshness)) delete freshness[key];
  clearPane(datasetsPane);
  clearPane(tablesPane);
  clearPane(schemaPane);
  for (const id of ["dataset-count", "table-count"]) document.getElementById(id).textContent = "";
  try {
    const projects = await window.KumoBqProjects.loadSelection();
    if (version !== projectRequest) return;
    renderProjects(projects);
    if (projects.length) setStatus("");
  } catch (error) {
    if (version !== projectRequest) return;
    clearPane(projectsPane, "Could not load your project choice.");
    setStatus(error.message, true);
  }
}

function fillDatasets(project, datasets) {
  clearPane(datasetsPane);
  for (const dataset of datasets) {
    const button = itemButton(dataset.id, "▤", dataset.location, dataset.id === chosenDataset, () => selectDataset(dataset.id));
    button.dataset.value = dataset.id;
    datasetsPane.append(button);
  }
  document.getElementById("dataset-count").textContent = String(datasets.length);
}

async function selectProject(project, refresh = false) {
  chosenProject = project;
  chosenDataset = chosenTable = "";
  for (const key of ["datasets", "tables", "table"]) delete freshness[key];
  renderProjectsSelection();
  clearPane(datasetsPane, "Loading datasets…");
  clearPane(tablesPane);
  clearPane(schemaPane);
  document.getElementById("table-count").textContent = "";
  const version = ++datasetRequest;
  setStatus(`Loading datasets in ${project}…`);
  try {
    const datasets = await requestJson(queryUrl("datasets", { project }), "datasets", refresh, (fresh) => {
      if (chosenProject === project) fillDatasets(project, fresh);
    });
    if (version !== datasetRequest || chosenProject !== project) return;
    fillDatasets(project, datasets);
    setStatus(datasets.length ? "" : `No datasets found in ${project}.`);
  } catch (error) {
    if (version !== datasetRequest) return;
    clearPane(datasetsPane, "Could not load datasets.");
    setStatus(error.message, true);
  }
}

function renderProjectsSelection() {
  for (const button of projectsPane.querySelectorAll("button")) {
    button.setAttribute("aria-current", button.dataset.value === chosenProject ? "true" : "false");
  }
}

const typeLabel = (type) => type.replaceAll("_", " ").toLowerCase();

function fillTables(tables) {
  shownTables = tables;
  clearPane(tablesPane);
  const wanted = tagFilter.value;
  const visible = wanted ? tables.filter((table) => window.KumoTags?.hasTag(objectKey(table.id), wanted)) : tables;
  for (const table of visible) {
    const icon = ROUTINE_ICONS[table.type] || (table.type === "VIEW" || table.type === "MATERIALIZED_VIEW" ? "◫" : "▦");
    const button = itemButton(table.id, icon, typeLabel(table.type), table.id === chosenTable, () => selectTable(table.id));
    button.dataset.value = table.id;
    const tagged = window.KumoTags?.chips(objectKey(table.id), { max: 3 });
    if (tagged?.children.length) button.append(tagged);
    const box = document.createElement("input");
    box.type = "checkbox";
    box.className = "catalog-check";
    box.checked = checked.has(table.id);
    box.setAttribute("aria-label", `Select ${table.id} to tag`);
    box.addEventListener("change", () => {
      if (box.checked) checked.add(table.id); else checked.delete(table.id);
      renderTagBar();
    });
    const row = document.createElement("div");
    row.className = "catalog-row";
    row.append(box, button);
    tablesPane.append(row);
  }
  if (wanted && !visible.length) clearPane(tablesPane, `Nothing in this dataset is tagged “${wanted}”.`);
  document.getElementById("table-count").textContent = wanted ? `${visible.length} of ${tables.length}` : String(tables.length);
  renderTagBar();
}

/* ----- Tags: filter, multi-select tagging, and the editor for the open object ----- */

window.KumoTags?.onError((message) => setStatus(`Tags could not be read: ${message}`, true));
const bulkEditor = window.KumoTags?.editor(() => [...checked].map(objectKey), { onStatus: setStatus });
if (bulkEditor) document.getElementById("bulk-editor").append(bulkEditor);

function renderTagBar() {
  const tags = window.KumoTags;
  tagBar.hidden = !tags || !shownTables.length;
  if (tagBar.hidden) return;
  const current = tagFilter.value;
  tagFilter.replaceChildren(new Option("All tags", ""), ...tags.names().map((name) => new Option(name, name)));
  tagFilter.value = tags.names().includes(current) ? current : "";
  const ids = new Set(shownTables.map((table) => table.id));
  for (const id of [...checked]) if (!ids.has(id)) checked.delete(id);
  const box = document.getElementById("check-all");
  box.checked = checked.size > 0 && tablesPane.querySelectorAll(".catalog-check").length === checked.size;
  box.indeterminate = checked.size > 0 && !box.checked;
  document.getElementById("tag-bulk").hidden = checked.size === 0;
  document.getElementById("bulk-count").textContent = String(checked.size);
  bulkEditor?.redraw();
}

tagFilter.addEventListener("change", () => fillTables(shownTables));
document.getElementById("check-all").addEventListener("change", (event) => {
  checked.clear();
  if (event.target.checked) for (const box of tablesPane.querySelectorAll(".catalog-row")) checked.add(box.querySelector("button").dataset.value);
  fillTables(shownTables);
});
document.getElementById("bulk-clear").addEventListener("click", () => {
  checked.clear();
  fillTables(shownTables);
});
// A tag changed (here, in Settings, or by a rule after a reload): redraw the chips and keep the open schema.
window.KumoTags?.onChange(() => {
  if (shownTables.length) fillTables(shownTables);
});

async function selectDataset(dataset, refresh = false) {
  chosenDataset = dataset;
  chosenTable = "";
  checked.clear();
  shownTables = [];
  tagFilter.value = "";
  tagBar.hidden = true;
  for (const key of ["tables", "table"]) delete freshness[key];
  for (const button of datasetsPane.querySelectorAll("button")) {
    button.setAttribute("aria-current", button.dataset.value === dataset ? "true" : "false");
  }
  clearPane(tablesPane, "Loading tables…");
  clearPane(schemaPane);
  const project = chosenProject;
  const version = ++tableRequest;
  setStatus(`Loading tables in ${project}.${dataset}…`);
  try {
    const tables = await requestJson(queryUrl("tables", { project, dataset }), "tables", refresh, (fresh) => {
      // Rule tags are computed from the saved catalog, so read them again now that it has changed.
      window.KumoTags?.load().then(() => { if (chosenProject === project && chosenDataset === dataset) fillTables(fresh); });
    });
    if (version !== tableRequest || chosenDataset !== dataset) return;
    // The list was just saved to the catalog; rules tag its objects from that copy, so tags read earlier are stale.
    await window.KumoTags?.load();
    if (version !== tableRequest || chosenDataset !== dataset) return;
    fillTables(tables);
    setStatus(tables.length ? "" : `No tables found in ${project}.${dataset}.`);
  } catch (error) {
    if (version !== tableRequest) return;
    clearPane(tablesPane, "Could not load tables.");
    setStatus(error.message, true);
  }
}

function addFields(rows, fields, depth = 0) {
  for (const field of fields) {
    const row = document.createElement("tr");
    const name = document.createElement("td");
    name.className = "schema-field";
    name.style.setProperty("--depth", String(depth));
    name.textContent = field.name || "(unnamed)";
    const type = document.createElement("td");
    type.className = "schema-type";
    type.textContent = field.type || "";
    const mode = document.createElement("td");
    mode.textContent = field.mode || "NULLABLE";
    row.append(name, type, mode);
    rows.append(row);
    if (field.fields?.length) addFields(rows, field.fields, depth + 1);
  }
}

function objectTags(key) {
  if (!window.KumoTags) return null;
  const box = document.createElement("div");
  box.className = "object-tags";
  const label = document.createElement("p");
  label.className = "schema-title";
  label.textContent = "Tags";
  box.append(label, window.KumoTags.editor(() => [key], { onStatus: setStatus }));
  return box;
}

function fillSchema(project, dataset, metadata) {
  clearPane(schemaPane);
  const title = document.createElement("p");
  title.className = "schema-title";
  title.textContent = `${project}.${dataset}.${metadata.id} · ${metadata.type}${metadata.numRows ? ` · ${Number(metadata.numRows).toLocaleString()} rows` : ""}`;
  const tags = objectTags(`${project}.${dataset}.${metadata.id}`);
  if (tags) schemaPane.append(tags);
  const tableElement = document.createElement("table");
  tableElement.className = "schema-table";
  const head = document.createElement("thead");
  const headingRow = document.createElement("tr");
  for (const label of ["Field", "Type", "Mode"]) {
    const th = document.createElement("th");
    th.textContent = label;
    headingRow.append(th);
  }
  head.append(headingRow);
  const body = document.createElement("tbody");
  addFields(body, metadata.schema || []);
  tableElement.append(head, body);
  schemaPane.append(title, tableElement);
}

async function selectTable(table, refresh = false) {
  chosenTable = table;
  for (const button of tablesPane.querySelectorAll("button")) {
    button.setAttribute("aria-current", button.dataset.value === table ? "true" : "false");
  }
  const routine = shownTables.find((item) => item.id === table && ROUTINE_ICONS[item.type]);
  if (routine) {
    // Functions and procedures have no schema to read; they can still be tagged.
    schemaRequest++;
    clearPane(schemaPane);
    const title = document.createElement("p");
    title.className = "schema-title";
    title.textContent = `${chosenProject}.${chosenDataset}.${table} · ${typeLabel(routine.type)}`;
    schemaPane.append(title, objectTags(objectKey(table)) || "");
    setStatus(`${table} is a ${typeLabel(routine.type)}.`);
    return;
  }
  clearPane(schemaPane, "Loading schema…");
  const project = chosenProject;
  const dataset = chosenDataset;
  const version = ++schemaRequest;
  setStatus(`Loading schema for ${project}.${dataset}.${table}…`);
  try {
    const metadata = await requestJson(queryUrl("table", { project, dataset, table }), "table", refresh, (fresh) => {
      if (chosenTable === table && chosenDataset === dataset && chosenProject === project) fillSchema(project, dataset, fresh);
    });
    if (version !== schemaRequest || chosenTable !== table) return;
    fillSchema(project, dataset, metadata);
    setStatus(`Showing ${metadata.schema?.length || 0} top-level fields for ${project}.${dataset}.${table}.`);
  } catch (error) {
    if (version !== schemaRequest) return;
    if (error.removed) {
      // No access to this table: stop listing it.
      tablesPane.querySelector(`button[data-value="${CSS.escape(table)}"]`)?.closest(".catalog-row")?.remove();
      shownTables = shownTables.filter((item) => item.id !== table);
      checked.delete(table);
      document.getElementById("table-count").textContent = String(tablesPane.querySelectorAll("button").length);
      chosenTable = "";
      clearPane(schemaPane);
      setStatus(`You do not have access to ${table}, so it was removed from the list.`, true);
      return;
    }
    clearPane(schemaPane, "Could not load schema.");
    // The object can still be tagged without its schema.
    const tags = objectTags(`${project}.${dataset}.${table}`);
    if (tags) schemaPane.append(tags);
    setStatus(error.message, true);
  }
}

// Refresh re-reads every level currently on screen from BigQuery, keeping the selection.
async function refreshAll() {
  const [project, dataset, table] = [chosenProject, chosenDataset, chosenTable];
  const button = document.getElementById("refresh");
  button.disabled = true;
  try {
    await loadProjects();
    if (!project) return;
    await selectProject(project, true);
    if (!dataset) return;
    await selectDataset(dataset, true);
    if (table) await selectTable(table, true);
  } finally {
    button.disabled = false;
  }
}

// Each column's refresh icon re-reads just that level from BigQuery, for example right
// after creating a table. The columns to its right are restored from the saved copy.
async function refreshLevel(level) {
  const [project, dataset, table] = [chosenProject, chosenDataset, chosenTable];
  if (level === "projects") {
    await loadProjects();
    if (project) await selectProject(project);
    return;
  }
  if (level === "datasets" && project) {
    await selectProject(project, true);
    if (dataset) await selectDataset(dataset);
    if (dataset && table) await selectTable(table);
  } else if (level === "tables" && dataset) {
    await selectDataset(dataset, true);
    if (table) await selectTable(table);
  } else if (level === "schema" && table) {
    await selectTable(table, true);
  }
}

for (const button of document.querySelectorAll(".panel-refresh")) {
  button.addEventListener("click", async () => {
    button.disabled = true;
    button.classList.add("spinning");
    try {
      await refreshLevel(button.dataset.level);
    } finally {
      button.disabled = false;
      button.classList.remove("spinning");
    }
  });
}

document.getElementById("refresh").addEventListener("click", refreshAll);
window.KumoTags?.load().then(() => { if (shownTables.length) fillTables(shownTables); });
loadProjects();

// Choosing projects here or in Settings updates the list, keeping the open project if it is still chosen.
window.addEventListener("kumosql:bq-projects", (event) => {
  const projects = event.detail || [];
  renderProjects(projects);
  if (projects.length && !chosenProject) setStatus("");
  if (chosenProject && !projects.includes(chosenProject)) {
    chosenProject = chosenDataset = chosenTable = "";
    clearPane(datasetsPane);
    clearPane(tablesPane);
    clearPane(schemaPane);
    setStatus(projects.length ? "" : status.textContent);
  }
});

function openProjectSettings() {
  window.KumoSettings.open("bigquery");
}

document.getElementById("choose-projects").addEventListener("click", openProjectSettings);
