const input = document.getElementById("sql-input");
const output = document.getElementById("sql-output");
const rulesContainer = document.getElementById("rules");
const transformButton = document.getElementById("transform-button");
const copyButton = document.getElementById("copy-button");
const report = document.getElementById("report");
const status = document.getElementById("status");
const preferredOrder = [
  "lift_subqueries",
  "inline_single_use_ctes",
  "remove_trivial_predicates",
  "remove_redundant_parentheses",
  "deduplicate_ctes",
  "remove_unused_ctes",
];

let debounceTimer;
let requestVersion = 0;
let controller;

function labelFor(name) {
  return name.replaceAll("_", " ").replace(/^./, (letter) => letter.toUpperCase());
}

function selectedRules() {
  return [...rulesContainer.querySelectorAll("input:checked")].map((checkbox) => checkbox.value);
}

function setStatus(message, kind = "idle") {
  status.textContent = message;
  status.className = `status status-${kind}`;
}

function updateControls() {
  document.getElementById("input-count").textContent = `${input.value.length.toLocaleString()} characters`;
  document.getElementById("output-count").textContent = `${output.value.length.toLocaleString()} characters`;
  document.getElementById("rule-count").textContent = `${selectedRules().length} selected`;
  transformButton.disabled = !input.value.trim() || !selectedRules().length;
  copyButton.disabled = !output.value;
}

function markPending() {
  clearTimeout(debounceTimer);
  requestVersion += 1;
  controller?.abort();
  output.value = "";
  report.hidden = true;
  setStatus(input.value.trim() ? "Ready to transform" : "Ready when you are");
  updateControls();
}

function showReport(data) {
  report.hidden = false;
  document.getElementById("verification-reason").textContent = data.verification.reason;
  const details = document.getElementById("verification-details");
  const steps = document.getElementById("steps");
  details.replaceChildren();
  steps.replaceChildren();

  for (const detail of data.verification.details) {
    const item = document.createElement("li");
    item.textContent = detail;
    details.append(item);
  }
  for (const step of data.steps) {
    const item = document.createElement("div");
    item.className = "step";
    const title = document.createElement("strong");
    title.textContent = labelFor(step.rule);
    item.append(title, ` · ${step.changes} changes · ${step.verification}`);
    steps.append(item);
    for (const diagnostic of step.diagnostics) {
      const line = document.createElement("li");
      line.textContent = `${labelFor(step.rule)}: ${diagnostic.code} — ${diagnostic.message}`;
      details.append(line);
    }
    for (const detail of step.details) {
      const line = document.createElement("li");
      line.textContent = `${labelFor(step.rule)}: ${detail}`;
      details.append(line);
    }
  }
}

async function transform() {
  clearTimeout(debounceTimer);
  if (!input.value.trim() || !selectedRules().length) return;
  const version = ++requestVersion;
  controller?.abort();
  controller = new AbortController();
  setStatus("Transforming…");
  transformButton.disabled = true;
  try {
    const response = await fetch("/api/transform", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ sql: input.value, rules: selectedRules() }),
      signal: controller.signal,
    });
    const data = await response.json();
    if (version !== requestVersion) return;
    if (!response.ok) throw new Error(data.error || "Transformation failed");
    output.value = data.sql;
    showReport(data);
    const kind = data.success
      ? data.verification.status
      : (data.steps.some((step) => !step.rule_success) ? "failed" : "unproven");
    const message = data.success
      ? (kind === "unchanged" ? "No changes needed" : "Rewrite verified")
      : "Review required — result not verified";
    setStatus(message, kind);
  } catch (error) {
    if (version !== requestVersion || error.name === "AbortError") return;
    setStatus(error.message || "Transformation failed", "failed");
    report.hidden = true;
  } finally {
    if (version === requestVersion) updateControls();
  }
}

function scheduleTransform() {
  markPending();
  if (input.value.trim() && selectedRules().length) {
    debounceTimer = setTimeout(transform, 650);
  }
}

async function loadRules() {
  try {
    const response = await fetch("/api/rules");
    if (!response.ok) throw new Error("Could not load transformations");
    const rules = await response.json();
    rules.sort((a, b) => {
      const ai = preferredOrder.indexOf(a.name);
      const bi = preferredOrder.indexOf(b.name);
      return (ai < 0 ? preferredOrder.length : ai) - (bi < 0 ? preferredOrder.length : bi);
    });
    for (const rule of rules) {
      const label = document.createElement("label");
      label.className = "rule";
      const checkbox = document.createElement("input");
      checkbox.type = "checkbox";
      checkbox.value = rule.name;
      checkbox.checked = rule.name === "lift_subqueries";
      const text = document.createElement("span");
      const title = document.createElement("strong");
      title.textContent = labelFor(rule.name);
      const summary = document.createElement("small");
      summary.textContent = rule.summary;
      text.append(title, summary);
      label.append(checkbox, text);
      rulesContainer.append(label);
    }
    updateControls();
  } catch (error) {
    setStatus(error.message, "failed");
    document.getElementById("rule-count").textContent = "Unavailable";
  }
}

input.addEventListener("input", scheduleTransform);
input.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) {
    event.preventDefault();
    transform();
  }
});
rulesContainer.addEventListener("change", scheduleTransform);
transformButton.addEventListener("click", transform);
document.getElementById("example-button").addEventListener("click", () => {
  input.value = "SELECT c.id\nFROM (SELECT id FROM `project.dataset.customers` WHERE 1 = 1) AS c\nWHERE c.id > 0";
  scheduleTransform();
  input.focus();
});
copyButton.addEventListener("click", async () => {
  try {
    await navigator.clipboard.writeText(output.value);
    const previous = copyButton.textContent;
    copyButton.textContent = "Copied";
    setTimeout(() => { copyButton.textContent = previous; }, 1400);
  } catch {
    setStatus("Copy failed; select the output and copy it manually", "failed");
  }
});

loadRules();
