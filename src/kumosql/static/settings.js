"use strict";

/* Settings page: named sqlfluff configurations used by the Format SQL rule.
   Configurations live in the shared UI state (the same object the workspace
   saves), and the active one is also written to the server's format settings. */

const STORAGE_KEY = "kumosql-ui";
const $ = (id) => document.getElementById(id);
const form = $("format-form");
let ui = {};
let profiles = [];
let activeId = "";
let toastTimer;

function toast(message) {
  const element = $("toast");
  element.textContent = message;
  element.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { element.hidden = true; }, 2200);
}

function active() {
  return profiles.find((item) => item.id === activeId) || profiles[0];
}

function fillForm(prefs) {
  if (!prefs) return;
  for (const element of form.elements) {
    if (!element.name) continue;
    const value = prefs[element.name];
    element.value = Array.isArray(value) ? value.join(", ") : value ?? "";
  }
}

function readForm() {
  const list = (text) => text.split(",").map((item) => item.trim()).filter(Boolean);
  const data = new FormData(form);
  return {
    keyword_case: data.get("keyword_case"),
    comma_position: data.get("comma_position"),
    indent_unit: data.get("indent_unit"),
    tab_space_size: Number(data.get("tab_space_size")),
    max_line_length: Number(data.get("max_line_length")),
    rules: list(data.get("rules")),
    exclude_rules: list(data.get("exclude_rules")),
  };
}

function render() {
  const select = $("sqlfluff-profile-select");
  select.replaceChildren();
  for (const item of profiles) {
    const option = document.createElement("option");
    option.value = item.id;
    option.textContent = item.name;
    select.append(option);
  }
  const current = active();
  select.value = current.id;
  $("sqlfluff-profile-name").value = current.name;
  $("sqlfluff-profile-delete").disabled = profiles.length < 2;
  fillForm(current.format);
}

async function putJson(url, payload, fallbackError) {
  const response = await fetch(url, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || fallbackError);
  return data;
}

async function saveUi() {
  ui = { ...ui, sqlfluffProfiles: profiles, activeSqlfluffProfile: activeId };
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(ui));
  } catch {
    /* browser storage unavailable; the server copy still persists */
  }
  await putJson("/api/settings/ui", ui, "Could not save settings");
}

// Validate a format with the server and make it the one the Format rule uses.
async function applyFormat(format) {
  return putJson("/api/settings/format", format, "Could not save sqlfluff settings");
}

function setStatus(text) {
  $("format-status").textContent = text;
}

async function save(event) {
  event?.preventDefault();
  const current = active();
  const name = $("sqlfluff-profile-name").value.trim();
  if (!name) { toast("Give this configuration a name"); return; }
  if (profiles.some((item) => item.id !== current.id && item.name.toLowerCase() === name.toLowerCase())) {
    toast("Configuration names must be unique");
    return;
  }
  try {
    current.format = await applyFormat(readForm());
    current.name = name;
    await saveUi();
  } catch (error) {
    toast(error.message);
    return;
  }
  render();
  setStatus("");
  toast("sqlfluff settings saved");
}

async function activate(id) {
  if (!profiles.some((item) => item.id === id)) return;
  activeId = id;
  const current = active();
  try {
    current.format = await applyFormat(current.format);
    await saveUi();
  } catch (error) {
    toast(error.message);
  }
  render();
  setStatus("");
}

form.addEventListener("submit", save);
form.addEventListener("input", () => setStatus("Unsaved changes"));
$("sqlfluff-profile-name").addEventListener("input", () => setStatus("Unsaved changes"));
$("sqlfluff-profile-select").addEventListener("change", (event) => activate(event.target.value));
$("sqlfluff-profile-new").addEventListener("click", async () => {
  const item = { id: crypto.randomUUID(), name: `Configuration ${profiles.length + 1}`, format: readForm() };
  profiles.push(item);
  await activate(item.id);
  $("sqlfluff-profile-name").focus();
  $("sqlfluff-profile-name").select();
});
$("sqlfluff-profile-delete").addEventListener("click", async () => {
  if (profiles.length < 2) return;
  const removed = active();
  if (!confirm(`Delete the configuration “${removed.name}”?`)) return;
  profiles = profiles.filter((item) => item !== removed);
  await activate(profiles[0].id);
  toast(`Deleted “${removed.name}”`);
});

async function start() {
  let format = null;
  try {
    ui = JSON.parse(localStorage.getItem(STORAGE_KEY)) || {};
  } catch {
    ui = {};
  }
  try {
    const response = await fetch("/api/settings");
    if (response.ok) {
      const settings = await response.json();
      if (settings.ui && Object.keys(settings.ui).length) ui = settings.ui;
      format = settings.format;
    }
  } catch {
    /* fall back to browser storage and defaults */
  }
  if (ui.theme === "light" || ui.theme === "dark") document.documentElement.dataset.theme = ui.theme;
  const loaded = Array.isArray(ui.sqlfluffProfiles) ? ui.sqlfluffProfiles : [];
  profiles = loaded.filter((item) => item && typeof item.name === "string" && item.format && typeof item.id === "string");
  if (!profiles.length) profiles = [{ id: crypto.randomUUID(), name: "Default", format }];
  activeId = profiles.some((item) => item.id === ui.activeSqlfluffProfile) ? ui.activeSqlfluffProfile : profiles[0].id;
  render();
}

start();
