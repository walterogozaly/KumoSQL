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

async function requestJson(url) {
  const response = await fetch(url);
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.error || "BigQuery request failed");
  return payload;
}

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

async function loadProjects() {
  const version = ++projectRequest;
  chosenProject = chosenDataset = chosenTable = "";
  clearPane(projectsPane, "Loading projects…");
  clearPane(datasetsPane, "Choose a project.");
  clearPane(tablesPane, "Choose a dataset.");
  clearPane(schemaPane, "Choose a table to inspect its schema.");
  document.getElementById("project-count").textContent = "";
  document.getElementById("dataset-count").textContent = "";
  document.getElementById("table-count").textContent = "";
  setStatus("Loading projects…");
  try {
    const projects = await requestJson("/api/catalog/projects");
    if (version !== projectRequest) return;
    clearPane(projectsPane);
    for (const project of projects) {
      const button = itemButton(project.name, "▣", project.id !== project.name ? project.id : "", false, () => selectProject(project.id));
      button.dataset.value = project.id;
      projectsPane.append(button);
    }
    document.getElementById("project-count").textContent = String(projects.length);
    setStatus(projects.length ? "Choose a project to see its datasets." : "No accessible projects were found.");
  } catch (error) {
    if (version !== projectRequest) return;
    clearPane(projectsPane, "Could not load projects.");
    setStatus(error.message, true);
  }
}

async function selectProject(project) {
  chosenProject = project;
  chosenDataset = chosenTable = "";
  renderProjectsSelection();
  clearPane(datasetsPane, "Loading datasets…");
  clearPane(tablesPane, "Choose a dataset.");
  clearPane(schemaPane, "Choose a table to inspect its schema.");
  document.getElementById("table-count").textContent = "";
  const version = ++datasetRequest;
  setStatus(`Loading datasets in ${project}…`);
  try {
    const datasets = await requestJson(queryUrl("datasets", { project }));
    if (version !== datasetRequest || chosenProject !== project) return;
    clearPane(datasetsPane);
    for (const dataset of datasets) {
      const button = itemButton(dataset.id, "▤", dataset.location, false, () => selectDataset(dataset.id));
      button.dataset.value = dataset.id;
      datasetsPane.append(button);
    }
    document.getElementById("dataset-count").textContent = String(datasets.length);
    setStatus(datasets.length ? `Choose a dataset in ${project}.` : `No datasets found in ${project}.`);
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

async function selectDataset(dataset) {
  chosenDataset = dataset;
  chosenTable = "";
  for (const button of datasetsPane.querySelectorAll("button")) {
    button.setAttribute("aria-current", button.dataset.value === dataset ? "true" : "false");
  }
  clearPane(tablesPane, "Loading tables…");
  clearPane(schemaPane, "Choose a table to inspect its schema.");
  const project = chosenProject;
  const version = ++tableRequest;
  setStatus(`Loading tables in ${project}.${dataset}…`);
  try {
    const tables = await requestJson(queryUrl("tables", { project, dataset }));
    if (version !== tableRequest || chosenDataset !== dataset) return;
    clearPane(tablesPane);
    for (const table of tables) {
      const button = itemButton(table.id, table.type === "VIEW" ? "◫" : "▦", table.type, false, () => selectTable(table.id));
      button.dataset.value = table.id;
      tablesPane.append(button);
    }
    document.getElementById("table-count").textContent = String(tables.length);
    setStatus(tables.length ? `Choose a table in ${project}.${dataset}.` : `No tables found in ${project}.${dataset}.`);
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

async function selectTable(table) {
  chosenTable = table;
  for (const button of tablesPane.querySelectorAll("button")) {
    button.setAttribute("aria-current", button.dataset.value === table ? "true" : "false");
  }
  clearPane(schemaPane, "Loading schema…");
  const project = chosenProject;
  const dataset = chosenDataset;
  const version = ++schemaRequest;
  setStatus(`Loading schema for ${project}.${dataset}.${table}…`);
  try {
    const metadata = await requestJson(queryUrl("table", { project, dataset, table }));
    if (version !== schemaRequest || chosenTable !== table) return;
    clearPane(schemaPane);
    const title = document.createElement("p");
    title.className = "schema-title";
    title.textContent = `${project}.${dataset}.${metadata.id} · ${metadata.type}${metadata.numRows ? ` · ${Number(metadata.numRows).toLocaleString()} rows` : ""}`;
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
    setStatus(`Showing ${metadata.schema?.length || 0} top-level fields for ${project}.${dataset}.${table}.`);
  } catch (error) {
    if (version !== schemaRequest) return;
    clearPane(schemaPane, "Could not load schema.");
    setStatus(error.message, true);
  }
}

document.getElementById("refresh").addEventListener("click", loadProjects);
loadProjects();
