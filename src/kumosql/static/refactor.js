"use strict";

/* Refactor: classify models as protected or editable (saved like scopes; see kumosql/refactor.py),
   then search for simpler pipelines whose protected tables are proved unchanged. */

(() => {
  const $ = (id) => document.getElementById(id);
  const status = $("rf-status");
  const list = $("rf-list");
  const filter = $("rf-filter");
  let classes = { protected: { scopes: [], models: [] }, editable: { scopes: [], models: [] } };
  let models = [];
  let scopeNames = [];
  let dirty = false;

  const h = (tag, attrs = {}, ...children) => {
    const element = document.createElement(tag);
    for (const [key, value] of Object.entries(attrs)) {
      if (key === "class") element.className = value;
      else if (key === "text") element.textContent = value;
      else if (key.startsWith("on")) element.addEventListener(key.slice(2), value);
      else if (value !== false && value != null) element.setAttribute(key, value === true ? "" : value);
    }
    element.append(...children.filter(Boolean));
    return element;
  };

  function say(message, error = false) {
    status.textContent = message;
    status.classList.toggle("error", error);
  }

  async function call(method, url, body) {
    const response = await fetch(url, {
      method,
      headers: body === undefined ? undefined : { "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || response.statusText);
    return data;
  }

  const explicitRole = (key) => (classes.protected.models.includes(key) ? "protected" : classes.editable.models.includes(key) ? "editable" : "");

  function setRole(key, role) {
    for (const name of ["protected", "editable"]) {
      classes[name].models = classes[name].models.filter((item) => item !== key);
    }
    if (role === "protected" || role === "editable") classes[role].models.push(key);
    const row = models.find((item) => item.key === key);
    if (row) row.role = role || "frozen";
    dirty = true;
    $("rf-save").disabled = false;
  }

  function renderScopes() {
    for (const name of ["protected", "editable"]) {
      const box = $(`rf-scopes-${name}`);
      box.replaceChildren();
      if (!scopeNames.length) box.append(h("span", { class: "rf-empty", text: "No saved scopes" }));
      for (const scope of scopeNames) {
        const input = h("input", { type: "checkbox" });
        input.checked = classes[name].scopes.includes(scope);
        input.addEventListener("change", () => {
          const set = new Set(classes[name].scopes);
          if (input.checked) set.add(scope); else set.delete(scope);
          classes[name].scopes = [...set];
          dirty = true;
          $("rf-save").disabled = false;
        });
        box.append(h("label", {}, input, scope));
      }
    }
  }

  const shown = () => {
    const text = filter.value.trim().toLowerCase();
    return models.filter((item) => !text || item.key.toLowerCase().includes(text));
  };

  function renderList() {
    list.replaceChildren();
    const rows = shown();
    if (!models.length) {
      list.append(h("div", { class: "rf-empty", text: "Load a project to classify its models" }));
      return;
    }
    for (const item of rows) {
      const select = h("select", { "aria-label": `Class of ${item.key}` },
        h("option", { value: "protected", text: "Protected" }),
        h("option", { value: "editable", text: "Editable" }),
        h("option", { value: "frozen", text: "Neither" }));
      select.value = item.role;
      select.addEventListener("change", () => {
        setRole(item.key, select.value);
        row.dataset.role = select.value;
      });
      const row = h("div", { class: "rf-row", "data-role": item.role },
        h("span", { class: "rf-name", title: item.key, text: item.key }),
        h("span", { class: "rf-kind", text: item.kind }),
        select);
      list.append(row);
    }
  }

  function renderResults(result) {
    const box = $("rf-results");
    box.replaceChildren();
    const head = h("tr", {}, ...["Models", "Complexity", "Moves"].map((text) => h("th", { text })));
    const rows = result.front.map((entry) => h("tr", {},
      h("td", { text: String(entry.models) }),
      h("td", { text: String(entry.complexity) }),
      h("td", {},
        h("details", {},
          h("summary", { text: entry.moves.length ? `${entry.moves.length} moves` : "No changes" }),
          ...entry.moves.map((move) => h("pre", { text: move })),
          ...Object.entries(entry.changed).map(([key, sql]) => h("pre", { text: `${key}\n${sql}` })),
          entry.assumptions.length ? h("pre", { text: `Assumes: ${entry.assumptions.join("; ")}` }) : null))));
    box.append(
      h("p", { text: `Now: ${result.baseline.models} models, complexity ${result.baseline.complexity}. Protected or exposed tables checked: ${result.observable.length}.` }),
      h("table", {}, h("thead", {}, head), h("tbody", {}, ...rows)));
    if (result.rejected_moves && result.rejected_moves.length) {
      box.append(h("details", {},
        h("summary", { text: `${result.rejected_moves.length} moves not proved` }),
        ...result.rejected_moves.map((item) => h("pre", { text: `${item.move}\n${item.why}` }))));
    }
    if (result.stopped) box.append(h("p", { class: "rf-empty", text: `Stopped at the ${result.stopped}.` }));
  }

  async function save() {
    try {
      classes = await call("PUT", "/api/settings/refactor", classes);
      dirty = false;
      $("rf-save").disabled = true;
      say("Saved");
    } catch (error) {
      say(error.message, true);
    }
  }

  let polling = 0;

  function finish(button) {
    clearTimeout(polling);
    $("rf-run").disabled = false;
    $("rf-cancel").hidden = true;
    return button;
  }

  async function poll() {
    try {
      const job = await call("GET", "/api/refactor/status");
      if (job.state === "running") {
        if (job.partial) renderResults(job.partial);
        const found = job.partial ? job.partial.front.length : 0;
        say(`Proving… ${Math.round(job.elapsed)} s, ${found} option${found === 1 ? "" : "s"} so far${job.line ? `, ${job.line}` : ""}`);
        polling = setTimeout(poll, 1000);
        return;
      }
      finish();
      if (job.state === "error") {
        say(job.error, true);
      } else if (job.result) {
        renderResults(job.result);
        const result = job.result;
        say(`${job.state === "cancelled" ? "Cancelled: " : ""}${result.front.length} option${result.front.length === 1 ? "" : "s"} on the front, ${result.tried} tried, ${result.rejected} rejected`);
      }
    } catch (error) {
      finish();
      say(error.message, true);
    }
  }

  async function run() {
    $("rf-run").disabled = true;
    $("rf-cancel").hidden = false;
    say("Proving…");
    try {
      if (dirty) await save();
      await call("POST", "/api/refactor/run", {});
      poll();
    } catch (error) {
      finish();
      say(error.message, true);
    }
  }

  async function load() {
    try {
      const data = await call("GET", "/api/refactor");
      classes = data.classes;
      models = data.models;
      scopeNames = data.scopes;
      renderScopes();
      renderList();
      $("rf-run").disabled = !models.length;
      const job = await call("GET", "/api/refactor/status");
      if (job.state === "running") {
        $("rf-run").disabled = true;
        $("rf-cancel").hidden = false;
        poll();
      }
    } catch (error) {
      say(error.message, true);
    }
  }

  filter.addEventListener("input", renderList);
  $("rf-bulk").addEventListener("change", (event) => {
    const role = event.target.value;
    if (!role) return;
    for (const item of shown()) setRole(item.key, role);
    event.target.value = "";
    renderList();
  });
  $("rf-save").addEventListener("click", save);
  $("rf-run").addEventListener("click", run);
  $("rf-cancel").addEventListener("click", () => call("POST", "/api/refactor/cancel", {}).catch((error) => say(error.message, true)));
  load();
})();
