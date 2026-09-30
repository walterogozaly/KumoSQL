"use strict";

const $ = (id) => document.getElementById(id);

const input = $("sql-input");
const editor = $("input-editor");
const inputPane = $("input-pane");
const highlightLayer = $("input-highlight");
const gutter = $("input-gutter");
const outputPane = document.querySelector(".output-pane");
const outputLines = $("output-lines");
const outputEmpty = $("output-empty");
const resultView = $("result-view");
const diffView = $("diff-view");
const rulesList = $("rules");
const transformButton = $("transform-button");
const copyButton = $("copy-button");
const report = $("report");
const status = $("status");

const DEFAULT_ORDER = [
  "lift_subqueries",
  "inline_single_use_ctes",
  "remove_trivial_predicates",
  "remove_redundant_parentheses",
  "deduplicate_ctes",
  "remove_unused_ctes",
  "format_sql",
];
const DEFAULT_ENABLED = ["lift_subqueries"];
const MAX_FILE_BYTES = 5 * 1024 * 1024;
const HIGHLIGHT_LIMIT = 200_000;
const DIFF_CELL_LIMIT = 4_000_000;
const FOLD_CONTEXT = 3;
const STORAGE_KEY = "kumosql-ui";
const THEMES = ["system", "light", "dark"];
const THEME_META = {
  system: { icon: "#i-monitor", label: "Theme: match system" },
  light: { icon: "#i-sun", label: "Theme: light" },
  dark: { icon: "#i-moon", label: "Theme: dark" },
};

const EXAMPLES = [
  {
    title: "Nested subqueries",
    description: "Lift FROM/JOIN subqueries into CTEs and drop WHERE 1 = 1.",
    rules: ["lift_subqueries", "remove_trivial_predicates"],
    sql: `SELECT c.id, c.region, o.total
FROM (
  SELECT id, region
  FROM \`shop.sales.customers\`
  WHERE 1 = 1 AND active
) AS c
JOIN (
  SELECT customer_id, SUM(amount) AS total
  FROM \`shop.sales.orders\`
  GROUP BY customer_id
) AS o ON o.customer_id = c.id
WHERE c.region = 'EMEA'`,
  },
  {
    title: "Single-use and unused CTEs",
    description: "Inline a CTE used once and remove one nobody reads.",
    rules: ["inline_single_use_ctes", "remove_unused_ctes"],
    sql: `WITH recent AS (
  SELECT order_id, amount
  FROM \`shop.sales.orders\`
  WHERE order_date >= '2026-01-01'
),
unused AS (
  SELECT 1 AS x
)
SELECT order_id, amount
FROM recent
WHERE amount > 100`,
  },
  {
    title: "Duplicate CTEs",
    description: "Merge two CTEs with identical bodies.",
    rules: ["deduplicate_ctes", "remove_unused_ctes"],
    sql: `WITH active_customers AS (
  SELECT id, region FROM \`shop.sales.customers\`
),
customer_copy AS (
  SELECT id, region FROM \`shop.sales.customers\`
),
emea AS (
  SELECT id FROM active_customers WHERE region = 'EMEA'
),
apac AS (
  SELECT id FROM customer_copy WHERE region = 'APAC'
)
SELECT id FROM emea
UNION ALL
SELECT id FROM apac`,
  },
  {
    title: "Parentheses and trivial filters",
    description: "Strip redundant parentheses and always-true predicates.",
    rules: ["remove_redundant_parentheses", "remove_trivial_predicates"],
    sql: `SELECT ((amount * 2)) AS doubled
FROM \`shop.sales.orders\`
WHERE (((status = 'paid'))) AND TRUE`,
  },
  {
    title: "Dataform SQLX",
    description: "Config blocks and \${ref()} calls are kept intact.",
    rules: ["lift_subqueries", "remove_trivial_predicates"],
    sql: `config {
  type: "table",
  description: "Paid orders per customer"
}

SELECT customer_id, total
FROM (
  SELECT customer_id, SUM(amount) AS total
  FROM \${ref("orders")}
  WHERE TRUE AND status = 'paid'
  GROUP BY customer_id
) AS t`,
  },
];

const state = {
  rules: [],
  theme: "system",
  autoRun: true,
  output: "",
  outputKey: null,
  view: "result",
  diffOps: null,
  diffDirty: true,
  fileName: null,
  gutterLines: 0,
  currentLine: 0,
  format: null,
  scopes: [],
  sqlfluffProfiles: [],
  activeSqlfluffProfile: "",
};
let debounceTimer;
let requestVersion = 0;
let controller;
let toastTimer;

/* ---------- Preferences (saved on this computer by the local server) ---------- */

let persisted = {};
let saveTimer;

function loadLocalPrefs() {
  try {
    return JSON.parse(localStorage.getItem(STORAGE_KEY)) || {};
  } catch {
    return {};
  }
}

function currentPrefs() {
  return {
    theme: state.theme,
    autoRun: state.autoRun,
    order: state.rules.map((rule) => rule.name),
    enabled: state.rules.filter((rule) => rule.on).map((rule) => rule.name),
    sqlfluffProfiles: state.sqlfluffProfiles,
    activeSqlfluffProfile: state.activeSqlfluffProfile,
  };
}

function savePrefs() {
  const prefs = currentPrefs();
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(prefs));
  } catch {
    /* browser storage unavailable; the server copy still persists */
  }
  clearTimeout(saveTimer);
  saveTimer = setTimeout(() => {
    fetch("/api/settings/ui", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(prefs),
    }).catch(() => {});
  }, 300);
}

function profile() {
  return state.sqlfluffProfiles.find((item) => item.id === state.activeSqlfluffProfile) || state.sqlfluffProfiles[0];
}

function initSqlfluffProfiles(prefs) {
  const loaded = Array.isArray(prefs.sqlfluffProfiles) ? prefs.sqlfluffProfiles : [];
  state.sqlfluffProfiles = loaded.filter((item) => item && typeof item.name === "string" && item.format && typeof item.id === "string");
  if (!state.sqlfluffProfiles.length) state.sqlfluffProfiles = [{ id: crypto.randomUUID(), name: "Default", format: state.format }];
  state.activeSqlfluffProfile = state.sqlfluffProfiles.some((item) => item.id === prefs.activeSqlfluffProfile)
    ? prefs.activeSqlfluffProfile : state.sqlfluffProfiles[0].id;
  state.format = profile().format;
}

function renderProfiles() {
  const select = $("sqlfluff-profile-select");
  select.replaceChildren();
  for (const item of state.sqlfluffProfiles) {
    const option = document.createElement("option");
    option.value = item.id;
    option.textContent = item.name;
    select.append(option);
  }
  const active = profile();
  if (!active) return;
  select.value = active.id;
  $("sqlfluff-profile-name").value = active.name;
  $("sqlfluff-profile-delete").disabled = state.sqlfluffProfiles.length < 2;
  state.format = active.format;
  fillFormatForm();
}

async function saveActiveProfile() {
  const active = profile();
  const name = $("sqlfluff-profile-name").value.trim();
  if (!name) { toast("Give this configuration a name"); return false; }
  if (state.sqlfluffProfiles.some((item) => item.id !== active.id && item.name.toLowerCase() === name.toLowerCase())) {
    toast("Configuration names must be unique"); return false;
  }
  active.name = name;
  active.format = readFormatForm();
  state.format = active.format;
  try {
    const response = await fetch("/api/settings/format", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(active.format),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "Could not save SQLFluff settings");
    active.format = state.format = data;
  } catch (error) {
    toast(error.message);
    return false;
  }
  savePrefs();
  renderProfiles();
  toast("SQLFluff settings saved");
  return true;
}

async function activateProfile(id) {
  const selected = state.sqlfluffProfiles.find((item) => item.id === id);
  if (!selected) return;
  state.activeSqlfluffProfile = id;
  state.format = selected.format;
  renderProfiles();
  savePrefs();
  try {
    const response = await fetch("/api/settings/format", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(selected.format),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "Could not apply SQLFluff settings");
    state.format = selected.format = data;
    fillFormatForm();
    savePrefs();
    if (selectedRules().includes("format_sql")) inputsChanged();
  } catch (error) {
    toast(error.message);
  }
}

/* ---------- Small helpers ---------- */

function labelFor(name) {
  return name
    .replaceAll("_", " ")
    .replace(/\bctes\b/g, "CTEs")
    .replace(/\bcte\b/g, "CTE")
    .replace(/\bsql\b/g, "SQL")
    .replace(/\bsingle use\b/g, "single-use")
    .replace(/^./, (letter) => letter.toUpperCase());
}

function plural(count, word) {
  return `${count.toLocaleString()} ${word}${count === 1 ? "" : "s"}`;
}

function escapeHtml(text) {
  return text.replace(/[&<>"]/g, (ch) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[ch]);
}

// sqlglot error messages can carry terminal colour codes.
function stripAnsi(text) {
  return String(text).replace(/\x1b\[[0-9;]*m/g, "");
}

function lineCount(text) {
  return text ? text.split("\n").length : 0;
}

function sizeLabel(text) {
  return `${plural(lineCount(text), "line")} · ${plural(text.length, "character")}`;
}

function selectedRules() {
  return state.rules.filter((rule) => rule.on).map((rule) => rule.name);
}

function currentKey() {
  const active = profile();
  return JSON.stringify([input.value, selectedRules(), active?.id, active?.format]);
}

function toast(message) {
  const element = $("toast");
  element.textContent = message;
  element.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { element.hidden = true; }, 2200);
}

/* ---------- SQL syntax highlighting ---------- */

const KEYWORDS = new Set(`
  ALL AND ANY ARRAY AS ASC ASSERT_ROWS_MODIFIED AT BEGIN BETWEEN BY CASE CAST CLUSTER COLLATE CREATE CROSS CUBE
  CURRENT DECLARE DEFAULT DEFINE DELETE DESC DISTINCT DROP ELSE END ENUM ESCAPE EXCEPT EXCLUDE EXISTS EXTRACT FALSE
  FETCH FOLLOWING FOR FROM FULL FUNCTION GROUP GROUPING GROUPS HASH HAVING IF IGNORE IN INNER INSERT INTERSECT
  INTERVAL INTO IS JOIN LATERAL LEFT LIKE LIMIT LOOKUP MATCHED MERGE NATURAL NEW NO NOT NULL NULLS OF OFFSET ON
  OR ORDER OUTER OVER PARTITION PIVOT PRECEDING PROTO QUALIFY RANGE RECURSIVE REPLACE RESPECT RETURNS RIGHT
  ROLLUP ROWS SAFE_CAST SELECT SET SOME STRUCT TABLE TABLESAMPLE TEMP TEMPORARY THEN TO TREAT TRUE UNBOUNDED
  UNION UNNEST UNPIVOT UPDATE USING VALUES VIEW WHEN WHERE WINDOW WITH WITHIN
`.trim().split(/\s+/));

// comment | string | backtick identifier | ${...} interpolation | number | word | punctuation
const TOKEN_RE = /(--[^\n]*|#[^\n]*|\/\*[\s\S]*?(?:\*\/|$))|('''[\s\S]*?(?:'''|$)|"""[\s\S]*?(?:"""|$)|'(?:\\.|[^'\\\n])*'?|"(?:\\.|[^"\\\n])*"?)|(`[^`\n]*`?)|(\$\{[^}\n]*\}?)|(\b\d+(?:\.\d+)?(?:[eE][+-]?\d+)?\b)|([A-Za-z_][A-Za-z0-9_]*)|([(),.;*=<>!+\-/%|&[\]{}:~^]+)/g;
const FUNCTION_AFTER = /^\s*\(/;

/** Return one HTML string per source line, with spans closed at line ends. */
function highlightLines(text) {
  const lines = [""];
  const push = (chunk, cls) => {
    const parts = chunk.split("\n");
    parts.forEach((part, index) => {
      if (index > 0) lines.push("");
      if (part) lines[lines.length - 1] += cls ? `<span class="${cls}">${escapeHtml(part)}</span>` : escapeHtml(part);
    });
  };
  let last = 0;
  TOKEN_RE.lastIndex = 0;
  for (let match; (match = TOKEN_RE.exec(text));) {
    if (match.index > last) push(text.slice(last, match.index));
    const [token, comment, string, ident, template, number, word] = match;
    let cls = "p";
    if (comment) cls = "c";
    else if (string) cls = "s";
    else if (ident) cls = "q";
    else if (template) cls = "t";
    else if (number) cls = "n";
    else if (word) {
      if (KEYWORDS.has(word.toUpperCase())) cls = "k";
      else cls = FUNCTION_AFTER.test(text.slice(TOKEN_RE.lastIndex, TOKEN_RE.lastIndex + 40)) ? "f" : "";
    }
    push(token, cls);
    last = TOKEN_RE.lastIndex;
  }
  if (last < text.length) push(text.slice(last));
  return lines;
}

function plainLines(text) {
  return text.split("\n").map(escapeHtml);
}

/** Rough CTE count: `name AS (` outside strings and comments, not after TABLE/VIEW. */
function countCtes(text) {
  const code = text.replace(TOKEN_RE, (token, comment, string) => (comment || string ? " " : token));
  const matches = code.match(/(?<!\b(?:TABLE|VIEW|FUNCTION)\s+)(?:^|[\s,(])(?:[A-Za-z_]\w*|`[^`]*`)\s+AS\s*\(/gi);
  return matches ? matches.length : 0;
}

/* ---------- Input editor ---------- */

function renderInput() {
  const text = input.value;
  const plain = text.length > HIGHLIGHT_LIMIT;
  editor.classList.toggle("plain", plain);
  if (!plain) highlightLayer.innerHTML = `${highlightLines(text).join("\n")}\n`;

  const lines = Math.max(1, lineCount(text));
  if (lines !== state.gutterLines) {
    let html = "";
    for (let i = 1; i <= lines; i += 1) html += `<div>${i}</div>`;
    gutter.innerHTML = html;
    state.gutterLines = lines;
    state.currentLine = 0;
  }
  syncScroll();
  updateCursor();
}

function syncScroll() {
  highlightLayer.style.transform = `translate(${-input.scrollLeft}px, ${-input.scrollTop}px)`;
  gutter.style.transform = `translateY(${-input.scrollTop}px)`;
}

function updateCursor() {
  const before = input.value.slice(0, input.selectionStart);
  const line = before.split("\n").length;
  const column = before.length - before.lastIndexOf("\n");
  $("cursor-position").textContent = document.activeElement === input ? `Ln ${line}, Col ${column}` : "";
  const rows = gutter.children;
  rows[state.currentLine]?.classList.remove("is-current");
  state.currentLine = line - 1;
  if (document.activeElement === input) rows[state.currentLine]?.classList.add("is-current");
}

function setInput(text, { keepUndo = false } = {}) {
  input.focus();
  if (keepUndo) {
    input.select();
    if (!document.execCommand("insertText", false, text)) input.value = text;
  } else {
    input.value = text;
  }
  input.setSelectionRange(0, 0);
  input.scrollTop = 0;
  input.scrollLeft = 0;
}

/* ---------- Output views ---------- */

function renderOutput() {
  const text = state.output;
  outputEmpty.hidden = Boolean(text);
  if (!text) {
    outputLines.innerHTML = "";
    return;
  }
  const lines = text.length > HIGHLIGHT_LIMIT ? plainLines(text) : highlightLines(text);
  outputLines.innerHTML = lines
    .map((line, i) => `<div class="line"><span class="ln">${i + 1}</span><span class="lc">${line || " "}</span></div>`)
    .join("");
}

const normalizeLine = (line) => line.trim().replace(/\s+/g, " ");

/** Line diff that ignores indentation changes; returns null when too large. */
function diffLines(before, after) {
  const a = before.split("\n");
  const b = after.split("\n");
  const ka = a.map(normalizeLine);
  const kb = b.map(normalizeLine);
  let start = 0;
  while (start < a.length && start < b.length && ka[start] === kb[start]) start += 1;
  let endA = a.length;
  let endB = b.length;
  while (endA > start && endB > start && ka[endA - 1] === kb[endB - 1]) { endA -= 1; endB -= 1; }
  const n = endA - start;
  const m = endB - start;
  if ((n + 1) * (m + 1) > DIFF_CELL_LIMIT) return null;

  const width = m + 1;
  const lcs = new Uint32Array((n + 1) * width);
  for (let i = n - 1; i >= 0; i -= 1) {
    for (let j = m - 1; j >= 0; j -= 1) {
      lcs[i * width + j] = ka[start + i] === kb[start + j]
        ? lcs[(i + 1) * width + j + 1] + 1
        : Math.max(lcs[(i + 1) * width + j], lcs[i * width + j + 1]);
    }
  }

  const ops = [];
  const same = (i, j) => ops.push({ t: "same", a: i + 1, b: j + 1, text: b[j] });
  for (let k = 0; k < start; k += 1) same(k, k);
  let i = 0;
  let j = 0;
  while (i < n || j < m) {
    if (i < n && j < m && ka[start + i] === kb[start + j]) {
      same(start + i, start + j); i += 1; j += 1;
    } else if (j >= m || (i < n && lcs[(i + 1) * width + j] >= lcs[i * width + j + 1])) {
      ops.push({ t: "del", a: start + i + 1, b: null, text: a[start + i] }); i += 1;
    } else {
      ops.push({ t: "add", a: null, b: start + j + 1, text: b[start + j] }); j += 1;
    }
  }
  for (let k = 0; k < a.length - endA; k += 1) same(endA + k, endB + k);
  return ops;
}

function diffRow(op) {
  const sign = op.t === "add" ? "+" : op.t === "del" ? "−" : " ";
  const code = op.text.length > 5000 ? escapeHtml(op.text) : highlightLines(op.text)[0];
  return `<div class="dl ${op.t}"><span class="dg"><span class="ln">${op.a ?? ""}</span><span class="ln">${op.b ?? ""}</span><span class="sign">${sign}</span></span><span class="lc">${code || " "}</span></div>`;
}

function renderDiff() {
  if (!state.diffDirty) return;
  state.diffDirty = false;
  const ops = state.diffOps;
  if (!state.output) {
    diffView.innerHTML = `<div class="empty-state"><strong>Nothing to compare yet</strong><span>Transform some SQL to see what changed.</span></div>`;
    return;
  }
  if (!ops) {
    diffView.innerHTML = `<div class="empty-state"><strong>Too large to diff here</strong><span>Use the Result tab, or compare the files locally.</span></div>`;
    return;
  }
  if (!ops.some((op) => op.t !== "same")) {
    diffView.innerHTML = `<div class="empty-state"><svg class="icon"><use href="#i-equal"/></svg><strong>No differences</strong><span>The proposed SQL matches the original, ignoring indentation.</span></div>`;
    return;
  }
  let html = "";
  let index = 0;
  while (index < ops.length) {
    if (ops[index].t !== "same") { html += diffRow(ops[index]); index += 1; continue; }
    let end = index;
    while (end < ops.length && ops[end].t === "same") end += 1;
    const head = index === 0 ? 0 : FOLD_CONTEXT;
    const tail = end === ops.length ? 0 : FOLD_CONTEXT;
    if (end - index > head + tail + 2) {
      for (let k = index; k < index + head; k += 1) html += diffRow(ops[k]);
      const hidden = end - tail - (index + head);
      html += `<button class="fold" type="button" data-from="${index + head}" data-to="${end - tail}">Show ${plural(hidden, "unchanged line")}</button>`;
      for (let k = end - tail; k < end; k += 1) html += diffRow(ops[k]);
    } else {
      for (let k = index; k < end; k += 1) html += diffRow(ops[k]);
    }
    index = end;
  }
  diffView.innerHTML = `<div class="diff-lines">${html}</div>`;
}

function setView(view) {
  state.view = view;
  for (const [tab, panel, name] of [[$("tab-result"), resultView, "result"], [$("tab-diff"), diffView, "diff"]]) {
    const active = view === name;
    tab.setAttribute("aria-selected", String(active));
    tab.tabIndex = active ? 0 : -1;
    panel.hidden = !active;
  }
  if (view === "diff") renderDiff();
}

/* ---------- Verdict, stats and report ---------- */

const VERDICT_ICONS = {
  idle: "#i-file", working: "#i-spark", proven: "#i-check", unchanged: "#i-equal", unproven: "#i-alert", failed: "#i-alert",
};

function setVerdict(kind, title, detail) {
  $("verdict").className = `verdict verdict-${kind}`;
  $("verdict-icon").setAttribute("href", VERDICT_ICONS[kind] || VERDICT_ICONS.idle);
  status.textContent = title;
  $("verdict-detail").textContent = detail;
}

function complexityLabel(c) {
  return c ? `${c.score} (${c.band})` : "n/a";
}

function showStats(before, after, elapsed, complexity) {
  const ops = state.diffOps;
  const added = ops ? ops.filter((op) => op.t === "add").length : null;
  const removed = ops ? ops.filter((op) => op.t === "del").length : null;
  const arrow = (x, y) => `${x.toLocaleString()} <span class="arrow">→</span> ${y.toLocaleString()}`;
  $("stat-lines").innerHTML = arrow(lineCount(before), lineCount(after));
  $("stat-ctes").innerHTML = arrow(countCtes(before), countCtes(after));
  const complexityCell = $("stat-complexity");
  complexityCell.textContent = complexity && complexity.before && complexity.after
    ? `${complexityLabel(complexity.before)} → ${complexityLabel(complexity.after)}`
    : complexity && (complexity.before || complexity.after) ? complexityLabel(complexity.before || complexity.after) : "n/a";
  const detail = complexity && (complexity.after || complexity.before);
  complexityCell.title = detail
    ? "Structural complexity from the sqlfluff parse tree: " +
      Object.entries(detail.metrics).map(([key, value]) => `${key.replace("_", " ")}: ${value}`).join(", ")
    : "SQLX and unparseable SQL are not scored";
  $("stat-diff").innerHTML = ops ? `<span class="num-add">+${added}</span> <span class="num-del">−${removed}</span>` : "–";
  $("stat-time").textContent = `${Math.round(elapsed)} ms`;
  $("stats").hidden = false;
  const badge = $("diff-badge");
  badge.hidden = !ops || !(added + removed);
  badge.textContent = ops ? String(added + removed) : "";
}

function showReport(data) {
  report.hidden = false;
  $("details-button").hidden = false;
  $("verification-reason").textContent = data.verification.reason;
  const details = $("verification-details");
  const steps = $("steps");
  details.replaceChildren();
  steps.replaceChildren();

  const addDetail = (rule, text) => {
    const item = document.createElement("li");
    if (rule) {
      const name = document.createElement("b");
      name.textContent = `${labelFor(rule)}: `;
      item.append(name);
    }
    item.append(stripAnsi(text));
    details.append(item);
  };

  for (const detail of data.verification.details) addDetail(null, detail);
  data.steps.forEach((step, index) => {
    const outcome = step.rule_success ? step.verification : "failed";
    const item = document.createElement("li");
    item.className = `tl-step ${outcome}`;
    const dot = document.createElement("span");
    dot.className = "tl-dot";
    if (outcome === "proven") dot.innerHTML = `<svg class="icon"><use href="#i-check"/></svg>`;
    else if (outcome === "failed" || outcome === "unproven") dot.innerHTML = `<svg class="icon"><use href="#i-alert"/></svg>`;
    else dot.textContent = String(index + 1);
    const name = document.createElement("span");
    name.className = "tl-name";
    name.textContent = labelFor(step.rule);
    const meta = document.createElement("span");
    meta.className = "tl-meta";
    const pill = document.createElement("span");
    pill.className = `pill pill-${outcome}`;
    pill.textContent = outcome;
    meta.append(plural(step.changes, "change"), pill);
    item.append(dot, name, meta);
    steps.append(item);
    for (const diagnostic of step.diagnostics) addDetail(step.rule, `${diagnostic.code} — ${diagnostic.message}`);
    for (const detail of step.details) addDetail(step.rule, detail);
  });
}

/* ---------- State updates ---------- */

function updateControls() {
  const selected = selectedRules().length;
  const total = state.rules.length;
  const hasInput = Boolean(input.value.trim());
  $("input-count").textContent = sizeLabel(input.value);
  $("output-count").textContent = sizeLabel(state.output);
  $("rule-count").textContent = total ? `${selected} of ${total} on` : "Unavailable";
  $("select-all-button").disabled = !total || selected === total;
  $("clear-button").disabled = !selected;
  $("reset-button").disabled = state.rules.every((rule, i) => rule.name === defaultOrder()[i]);
  $("clear-input-button").disabled = !input.value;
  transformButton.disabled = !hasInput || !selected;
  const hasOutput = Boolean(state.output);
  copyButton.disabled = !hasOutput;
  $("download-button").disabled = !hasOutput;
  $("use-output-button").disabled = !hasOutput || state.output === input.value;
  const stale = hasOutput && state.outputKey !== currentKey();
  outputPane.classList.toggle("is-stale", stale);
  $("stale-badge").hidden = !stale;
}

function setBusy(busy) {
  if (busy) transformButton.setAttribute("aria-busy", "true");
  else transformButton.removeAttribute("aria-busy");
}

function clearOutput() {
  state.output = "";
  state.outputKey = null;
  state.diffOps = null;
  state.diffDirty = true;
  renderOutput();
  if (state.view === "diff") renderDiff();
  report.hidden = true;
  $("details-button").hidden = true;
  $("stats").hidden = true;
  $("diff-badge").hidden = true;
}

/** Called whenever the SQL or the pipeline changes. */
function inputsChanged({ immediate = false } = {}) {
  clearTimeout(debounceTimer);
  requestVersion += 1;
  controller?.abort();
  setBusy(false);

  if (!input.value.trim()) {
    clearOutput();
    setVerdict("idle", "Paste SQL to get started", "Or pick one of the examples to see each transformation in action.");
  } else if (!selectedRules().length) {
    setVerdict("idle", "Choose at least one transformation", "Turn on a rule in the pipeline to transform this SQL.");
  } else if (state.output && state.outputKey === currentKey()) {
    /* nothing changed since the last result */
  } else if (immediate) {
    updateControls();
    transform();
    return;
  } else if (state.autoRun) {
    setVerdict("working", "Waiting for you to pause…", "The result updates shortly after you stop typing.");
    debounceTimer = setTimeout(transform, 600);
  } else {
    setVerdict("idle", "Ready to transform", "Press Transform SQL or Ctrl+Enter to update the result.");
  }
  updateControls();
}

async function transform() {
  clearTimeout(debounceTimer);
  const rules = selectedRules();
  if (!input.value.trim() || !rules.length) return;
  const sql = input.value;
  const key = currentKey();
  const version = ++requestVersion;
  controller?.abort();
  controller = new AbortController();
  setVerdict("working", "Transforming…", `Running ${plural(rules.length, "rule")} and checking equivalence.`);
  setBusy(true);
  transformButton.disabled = true;
  const started = performance.now();
  try {
    const response = await fetch("/api/transform", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ sql, rules, format: state.format }),
      signal: controller.signal,
    });
    const data = await response.json();
    if (version !== requestVersion) return;
    if (!response.ok) throw new Error(data.error || "Transformation failed");

    state.output = data.sql;
    state.outputKey = key;
    state.diffOps = diffLines(sql, data.sql);
    state.diffDirty = true;
    renderOutput();
    if (state.view === "diff") renderDiff();
    showReport(data);
    showStats(sql, data.sql, performance.now() - started, data.complexity);

    const reason = data.verification.reason;
    if (data.success && data.verification.status === "unchanged") {
      setVerdict("unchanged", "No changes needed", "None of the selected rules changed this SQL.");
    } else if (data.success) {
      const changes = data.steps.reduce((sum, step) => sum + step.changes, 0);
      setVerdict("proven", "Rewrite verified", `${plural(changes, "change")}. ${reason[0].toUpperCase()}${reason.slice(1)}.`);
    } else if (data.steps.some((step) => !step.rule_success)) {
      const broken = data.steps.find((step) => !step.rule_success);
      const first = broken.diagnostics[0];
      setVerdict("failed", `${labelFor(broken.rule)} could not run cleanly`, first ? stripAnsi(first.message) : "See the verification details below before using this SQL.");
    } else {
      setVerdict("unproven", "Review required: not verified", reason);
    }
  } catch (error) {
    if (version !== requestVersion || error.name === "AbortError") return;
    clearOutput();
    setVerdict("failed", "Transformation failed", error.message || "The server could not transform this SQL.");
  } finally {
    if (version === requestVersion) {
      setBusy(false);
      updateControls();
    }
  }
}

/* ---------- Pipeline (rules sidebar) ---------- */

function defaultOrder() {
  const names = state.rules.map((rule) => rule.name);
  const rank = (name) => {
    const i = DEFAULT_ORDER.indexOf(name);
    return i < 0 ? DEFAULT_ORDER.length : i;
  };
  return [...names].sort((a, b) => rank(a) - rank(b));
}

function renderRules(focus) {
  rulesList.replaceChildren();
  state.rules.forEach((rule, index) => {
    const label = labelFor(rule.name);
    const item = document.createElement("li");
    item.className = "rule-item";
    item.draggable = true;
    item.dataset.name = rule.name;
    item.innerHTML = `
      <span class="rule-grip" aria-hidden="true"><svg class="icon"><use href="#i-grip"/></svg></span>
      <input type="checkbox" id="rule-${rule.name}">
      <label class="rule-label" for="rule-${rule.name}"><strong></strong><small></small></label>
      <div class="rule-side">
        <span class="rule-order" aria-hidden="true"></span>
        <div class="rule-move">
          <button type="button" data-move="-1"><svg class="icon"><use href="#i-up"/></svg></button>
          <button type="button" data-move="1"><svg class="icon"><use href="#i-down"/></svg></button>
        </div>
      </div>`;
    item.querySelector("strong").textContent = label;
    item.querySelector("small").textContent = rule.summary;
    const checkbox = item.querySelector("input");
    checkbox.checked = rule.on;
    const [up, down] = item.querySelectorAll("[data-move]");
    up.setAttribute("aria-label", `Move ${label} earlier`);
    down.setAttribute("aria-label", `Move ${label} later`);
    up.disabled = index === 0;
    down.disabled = index === state.rules.length - 1;
    rulesList.append(item);
  });
  updateRuleBadges();
  if (focus) rulesList.querySelector(`[data-name="${focus.name}"] [data-move="${focus.move}"]:not(:disabled)`)?.focus();
}

function updateRuleBadges() {
  let position = 0;
  for (const item of rulesList.children) {
    const rule = state.rules.find((r) => r.name === item.dataset.name);
    item.classList.toggle("is-on", rule.on);
    item.querySelector(".rule-order").textContent = rule.on ? String(++position) : "";
  }
}

function moveRule(name, toIndex) {
  const from = state.rules.findIndex((rule) => rule.name === name);
  if (from < 0) return;
  const [rule] = state.rules.splice(from, 1);
  state.rules.splice(Math.max(0, Math.min(toIndex, state.rules.length)), 0, rule);
}

function pipelineChanged(focus) {
  renderRules(focus);
  savePrefs();
  inputsChanged();
}

async function loadRules() {
  try {
    const response = await fetch("/api/rules");
    if (!response.ok) throw new Error("Could not load transformations");
    const rules = await response.json();
    const prefs = persisted;
    const saved = Array.isArray(prefs.order) ? prefs.order : [];
    const enabled = new Set(Array.isArray(prefs.enabled) ? prefs.enabled : DEFAULT_ENABLED);
    state.rules = rules.map((rule) => ({ name: rule.name, summary: rule.summary, on: enabled.has(rule.name) }));
    const order = defaultOrder();
    const rank = (name) => {
      const i = saved.indexOf(name);
      return i < 0 ? saved.length + order.indexOf(name) : i;
    };
    state.rules.sort((a, b) => rank(a.name) - rank(b.name));
    renderRules();
  } catch (error) {
    setVerdict("failed", "Could not load transformations", error.message);
  }
  updateControls();
}

rulesList.addEventListener("change", (event) => {
  const rule = state.rules.find((r) => `rule-${r.name}` === event.target.id);
  if (!rule) return;
  rule.on = event.target.checked;
  updateRuleBadges();
  savePrefs();
  inputsChanged();
});

rulesList.addEventListener("click", (event) => {
  const button = event.target.closest("[data-move]");
  if (!button) return;
  const name = button.closest(".rule-item").dataset.name;
  const index = state.rules.findIndex((rule) => rule.name === name);
  moveRule(name, index + Number(button.dataset.move));
  pipelineChanged({ name, move: button.dataset.move });
});

let draggedRule = null;
rulesList.addEventListener("dragstart", (event) => {
  const item = event.target.closest?.(".rule-item");
  if (!item) return;
  draggedRule = item.dataset.name;
  item.classList.add("dragging");
  event.dataTransfer.effectAllowed = "move";
  event.dataTransfer.setData("text/plain", draggedRule);
});
rulesList.addEventListener("dragover", (event) => {
  const item = event.target.closest(".rule-item");
  if (!draggedRule || !item) return;
  event.preventDefault();
  const after = event.clientY > item.getBoundingClientRect().top + item.offsetHeight / 2;
  for (const other of rulesList.children) other.classList.remove("drop-before", "drop-after");
  if (item.dataset.name !== draggedRule) item.classList.add(after ? "drop-after" : "drop-before");
});
rulesList.addEventListener("drop", (event) => {
  const item = event.target.closest(".rule-item");
  if (!draggedRule || !item) return;
  event.preventDefault();
  const after = item.classList.contains("drop-after");
  const name = draggedRule;
  if (item.dataset.name !== name) {
    const without = state.rules.filter((rule) => rule.name !== name);
    const target = without.findIndex((rule) => rule.name === item.dataset.name) + (after ? 1 : 0);
    moveRule(name, target);
    pipelineChanged();
  }
});
rulesList.addEventListener("dragend", () => {
  draggedRule = null;
  for (const item of rulesList.children) item.classList.remove("dragging", "drop-before", "drop-after");
});

$("select-all-button").addEventListener("click", () => {
  for (const rule of state.rules) rule.on = true;
  pipelineChanged();
});
$("clear-button").addEventListener("click", () => {
  for (const rule of state.rules) rule.on = false;
  pipelineChanged();
});
$("reset-button").addEventListener("click", () => {
  const order = defaultOrder();
  state.rules.sort((a, b) => order.indexOf(a.name) - order.indexOf(b.name));
  pipelineChanged();
});

/* ---------- Examples menu ---------- */

const examplesButton = $("examples-button");
const examplesMenu = $("examples-menu");

EXAMPLES.forEach((example, index) => {
  const item = document.createElement("button");
  item.type = "button";
  item.className = "menu-item";
  item.setAttribute("role", "menuitem");
  item.dataset.index = String(index);
  const title = document.createElement("strong");
  title.textContent = example.title;
  const description = document.createElement("span");
  description.textContent = example.description;
  item.append(title, description);
  examplesMenu.append(item);
});

function toggleMenu(open) {
  examplesMenu.hidden = !open;
  examplesButton.setAttribute("aria-expanded", String(open));
  if (open) examplesMenu.querySelector(".menu-item")?.focus();
}

examplesButton.addEventListener("click", () => toggleMenu(examplesMenu.hidden));
examplesMenu.addEventListener("click", (event) => {
  const item = event.target.closest(".menu-item");
  if (!item) return;
  const example = EXAMPLES[Number(item.dataset.index)];
  toggleMenu(false);
  const wanted = new Set(example.rules);
  for (const rule of state.rules) rule.on = wanted.has(rule.name);
  renderRules();
  savePrefs();
  state.fileName = null;
  setInput(example.sql);
  renderInput();
  inputsChanged({ immediate: true });
});
examplesMenu.addEventListener("keydown", (event) => {
  const items = [...examplesMenu.querySelectorAll(".menu-item")];
  const index = items.indexOf(document.activeElement);
  if (event.key === "ArrowDown" || event.key === "ArrowUp") {
    event.preventDefault();
    const step = event.key === "ArrowDown" ? 1 : -1;
    items[(index + step + items.length) % items.length].focus();
  } else if (event.key === "Escape") {
    toggleMenu(false);
    examplesButton.focus();
  } else if (event.key === "Tab") {
    toggleMenu(false);
  }
});
document.addEventListener("click", (event) => {
  if (!examplesMenu.hidden && !event.target.closest(".menu-wrap")) toggleMenu(false);
});

/* ---------- Theme ---------- */

function applyTheme(theme) {
  state.theme = THEMES.includes(theme) ? theme : "system";
  if (state.theme === "system") delete document.documentElement.dataset.theme;
  else document.documentElement.dataset.theme = state.theme;
  const meta = THEME_META[state.theme];
  $("theme-icon").setAttribute("href", meta.icon);
  $("theme-button").setAttribute("aria-label", meta.label);
  $("theme-button").title = meta.label;
}

$("theme-button").addEventListener("click", () => {
  applyTheme(THEMES[(THEMES.indexOf(state.theme) + 1) % THEMES.length]);
  savePrefs();
});

/* ---------- Files, copy, download ---------- */

async function openFile(file) {
  if (!file) return;
  if (file.size > MAX_FILE_BYTES) {
    toast(`${file.name} is larger than 5 MB`);
    return;
  }
  const text = await file.text();
  state.fileName = file.name;
  setInput(text.replace(/\r\n/g, "\n"));
  renderInput();
  inputsChanged({ immediate: true });
  toast(`Opened ${file.name}`);
}

$("open-button").addEventListener("click", () => $("file-input").click());
$("file-input").addEventListener("change", (event) => {
  openFile(event.target.files[0]);
  event.target.value = "";
});

let dragDepth = 0;
const hasFiles = (event) => [...(event.dataTransfer?.types || [])].includes("Files");
inputPane.addEventListener("dragenter", (event) => {
  if (!hasFiles(event)) return;
  event.preventDefault();
  dragDepth += 1;
  inputPane.classList.add("drag-over");
});
inputPane.addEventListener("dragover", (event) => {
  if (hasFiles(event)) event.preventDefault();
});
inputPane.addEventListener("dragleave", () => {
  dragDepth = Math.max(0, dragDepth - 1);
  if (!dragDepth) inputPane.classList.remove("drag-over");
});
inputPane.addEventListener("drop", (event) => {
  if (!hasFiles(event)) return;
  event.preventDefault();
  dragDepth = 0;
  inputPane.classList.remove("drag-over");
  openFile(event.dataTransfer.files[0]);
});

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    const scratch = document.createElement("textarea");
    scratch.value = text;
    scratch.className = "sr-only";
    document.body.append(scratch);
    scratch.select();
    const copied = document.execCommand("copy");
    scratch.remove();
    return copied;
  }
}

copyButton.addEventListener("click", async () => {
  toast(await copyText(state.output) ? "Proposed SQL copied" : "Copy failed; select the SQL and copy it manually");
});

$("download-button").addEventListener("click", () => {
  const sqlx = /^\s*config\s*\{/m.test(state.output);
  const base = state.fileName ? state.fileName.replace(/\.(sqlx?|txt)$/i, "") : "proposed";
  const name = `${base}${state.fileName ? ".rewritten" : ""}.${sqlx ? "sqlx" : "sql"}`;
  const link = document.createElement("a");
  link.href = URL.createObjectURL(new Blob([state.output], { type: "text/plain" }));
  link.download = name;
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(link.href), 1000);
  toast(`Saved ${name}`);
});

$("use-output-button").addEventListener("click", () => {
  setInput(state.output, { keepUndo: true });
  renderInput();
  inputsChanged({ immediate: true });
  toast("Proposed SQL moved to the editor. Ctrl+Z undoes it.");
});

$("clear-input-button").addEventListener("click", () => {
  setInput("", { keepUndo: true });
  state.fileName = null;
  renderInput();
  inputsChanged();
});

/* ---------- Tabs, keyboard, misc ---------- */

$("details-button").addEventListener("click", () => {
  report.scrollIntoView({ behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth", block: "start" });
});
$("tab-result").addEventListener("click", () => setView("result"));
$("tab-diff").addEventListener("click", () => setView("diff"));
document.querySelector(".tabs").addEventListener("keydown", (event) => {
  if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
  const next = state.view === "result" ? "diff" : "result";
  setView(next);
  $(`tab-${next}`).focus();
});
diffView.addEventListener("click", (event) => {
  const fold = event.target.closest(".fold");
  if (!fold) return;
  const rows = state.diffOps.slice(Number(fold.dataset.from), Number(fold.dataset.to)).map(diffRow).join("");
  fold.insertAdjacentHTML("afterend", rows);
  fold.remove();
});

$("auto-run").addEventListener("change", (event) => {
  state.autoRun = event.target.checked;
  savePrefs();
  inputsChanged();
});

input.addEventListener("input", () => {
  renderInput();
  inputsChanged();
});
input.addEventListener("scroll", syncScroll);
input.addEventListener("blur", updateCursor);
document.addEventListener("selectionchange", () => {
  if (document.activeElement === input) updateCursor();
});
input.addEventListener("keydown", (event) => {
  // Tab indents; Escape then Tab still moves focus out for keyboard users.
  if (event.key === "Tab" && !event.shiftKey && !input.dataset.escaped) {
    event.preventDefault();
    document.execCommand("insertText", false, "  ");
  }
  input.dataset.escaped = event.key === "Escape" ? "1" : "";
});

document.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) {
    event.preventDefault();
    if (!transformButton.disabled) inputsChanged({ immediate: true });
  }
});
transformButton.addEventListener("click", () => inputsChanged({ immediate: true }));
$("settings-button").addEventListener("click", () => {
  const settings = $("format-settings");
  settings.open = true;
  settings.scrollIntoView({ behavior: "smooth", block: "nearest" });
  $("sqlfluff-profile-select").focus();
});

/* ---------- Formatting preferences ---------- */

const formatForm = $("format-form");
let formatTimer;

function fillFormatForm() {
  const prefs = state.format;
  if (!prefs) return;
  for (const element of formatForm.elements) {
    if (!element.name) continue;
    const value = prefs[element.name];
    element.value = Array.isArray(value) ? value.join(", ") : value ?? "";
  }
}

function readFormatForm() {
  const list = (text) => text.split(",").map((item) => item.trim()).filter(Boolean);
  const data = new FormData(formatForm);
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

formatForm.addEventListener("change", () => {
  clearTimeout(formatTimer);
  formatTimer = setTimeout(async () => {
    try {
      const response = await fetch("/api/settings/format", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(readFormatForm()),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || "Could not save formatting preferences");
      state.format = data;
      const active = profile();
      if (active) active.format = data;
      savePrefs();
      if (selectedRules().includes("format_sql")) inputsChanged();
    } catch (error) {
      toast(error.message);
    }
  }, 400);
});
formatForm.addEventListener("submit", (event) => event.preventDefault());

$("sqlfluff-profile-select").addEventListener("change", (event) => activateProfile(event.target.value));
$("sqlfluff-profile-save").addEventListener("click", saveActiveProfile);
$("sqlfluff-profile-new").addEventListener("click", () => {
  const item = { id: crypto.randomUUID(), name: `Configuration ${state.sqlfluffProfiles.length + 1}`, format: state.format || readFormatForm() };
  state.sqlfluffProfiles.push(item);
  state.activeSqlfluffProfile = item.id;
  renderProfiles();
  savePrefs();
  $("sqlfluff-profile-name").focus();
  $("sqlfluff-profile-name").select();
});
$("sqlfluff-profile-delete").addEventListener("click", () => {
  if (state.sqlfluffProfiles.length < 2) return;
  state.sqlfluffProfiles = state.sqlfluffProfiles.filter((item) => item.id !== state.activeSqlfluffProfile);
  activateProfile(state.sqlfluffProfiles[0].id);
});

/* ---------- Scopes ---------- */

const scopeForm = $("scope-form");
const scopeList = $("scope-list");
let editingScope = null;

function parseFilters(text) {
  const fields = {};
  for (const line of text.split("\n")) {
    if (!line.trim()) continue;
    const at = line.indexOf("=");
    if (at < 1) throw new Error(`Write each filter as “field = value, value”: ${line.trim()}`);
    const field = line.slice(0, at).trim();
    const values = line.slice(at + 1).split(",").map((value) => value.trim()).filter(Boolean);
    if (!values.length) throw new Error(`Add at least one value for ${field}`);
    fields[field] = [...(fields[field] || []), ...values];
  }
  return fields;
}

function filtersText(scope) {
  return Object.entries(scope.fields).map(([field, values]) => `${field} = ${values.join(", ")}`).join("\n");
}

function renderScopes() {
  scopeList.replaceChildren();
  $("scope-count").textContent = state.scopes.length ? String(state.scopes.length) : "";
  for (const scope of state.scopes) {
    const item = document.createElement("li");
    item.className = "scope-item";
    item.dataset.name = scope.name;
    const text = document.createElement("div");
    const title = document.createElement("strong");
    title.textContent = scope.name;
    const summary = document.createElement("small");
    summary.textContent = Object.entries(scope.fields)
      .map(([field, values]) => `${field}: ${values.length}`).join(" · ");
    summary.title = filtersText(scope);
    text.append(title, summary);
    const edit = document.createElement("button");
    edit.type = "button";
    edit.className = "link-button";
    edit.dataset.action = "edit";
    edit.textContent = "Edit";
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "link-button";
    remove.dataset.action = "delete";
    remove.textContent = "Delete";
    item.append(text, edit, remove);
    scopeList.append(item);
  }
}

function resetScopeForm() {
  editingScope = null;
  scopeForm.reset();
  $("scope-cancel").hidden = true;
  $("scope-save").textContent = "Save scope";
}

async function saveScopes(scopes) {
  const response = await fetch("/api/settings/scopes", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(scopes),
  });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || "Could not save scopes");
  state.scopes = data;
  renderScopes();
}

scopeForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    const name = scopeForm.elements.name.value.trim();
    const scope = { name, fields: parseFilters(scopeForm.elements.filters.value) };
    const key = (editingScope ?? name).toLowerCase();
    const others = state.scopes.filter((item) => item.name.toLowerCase() !== key);
    if (others.some((item) => item.name.toLowerCase() === name.toLowerCase())) {
      throw new Error(`A scope named “${name}” already exists`);
    }
    const index = state.scopes.findIndex((item) => item.name.toLowerCase() === key);
    const next = [...state.scopes];
    if (index < 0) next.push(scope); else next[index] = scope;
    await saveScopes(next);
    resetScopeForm();
    toast(`Saved scope “${name}”`);
  } catch (error) {
    toast(error.message);
  }
});
$("scope-cancel").addEventListener("click", resetScopeForm);
scopeList.addEventListener("click", async (event) => {
  const button = event.target.closest("button[data-action]");
  const name = button?.closest(".scope-item")?.dataset.name;
  const scope = state.scopes.find((item) => item.name === name);
  if (!scope) return;
  if (button.dataset.action === "edit") {
    editingScope = scope.name;
    scopeForm.elements.name.value = scope.name;
    scopeForm.elements.filters.value = filtersText(scope);
    $("scope-cancel").hidden = false;
    $("scope-save").textContent = "Update scope";
    scopeForm.elements.name.focus();
  } else {
    try {
      await saveScopes(state.scopes.filter((item) => item !== scope));
      if (editingScope === scope.name) resetScopeForm();
      toast(`Deleted scope “${scope.name}”`);
    } catch (error) {
      toast(error.message);
    }
  }
});

/* ---------- Start ---------- */

async function start() {
  persisted = loadLocalPrefs();
  try {
    const response = await fetch("/api/settings");
    if (response.ok) {
      const settings = await response.json();
      if (settings.ui && Object.keys(settings.ui).length) persisted = settings.ui;
      state.format = settings.format;
      state.scopes = settings.scopes;
    }
  } catch {
    /* fall back to browser storage and defaults */
  }
  initSqlfluffProfiles(persisted);
  renderProfiles();
  applyTheme(persisted.theme);
  state.autoRun = persisted.autoRun !== false;
  $("auto-run").checked = state.autoRun;
  if (/Mac|iPhone|iPad/.test(navigator.platform)) {
    document.querySelector(".button-kbd").textContent = "⌘ ↵";
  }
  renderInput();
  renderOutput();
  renderScopes();
  loadRules();
}

start();
