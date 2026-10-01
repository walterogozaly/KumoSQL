"use strict";

/* Settings panel. A modal with a section sidebar that opens over whichever page
   you are on (Settings in the sidebar, or Ctrl/⌘ + ,). Changes save as
   you make them. The workspace registers a provider so its own saved state
   (rule order, auto-run) is kept and it hears about theme and format changes. */

(() => {
  const STORAGE_KEY = "kumosql-ui";
  const THEMES = [["system", "System"], ["light", "Light"], ["dark", "Dark"]];
  const FORMAT_FIELDS = [
    { name: "keyword_case", label: "Keyword case", hint: "How SQL keywords like SELECT and FROM are written.", type: "choice",
      options: [["upper", "UPPER"], ["lower", "lower"], ["capitalise", "Capitalised"], ["consistent", "Consistent"]] },
    { name: "comma_position", label: "Commas", hint: "Put commas at the end or the start of each line.", type: "choice",
      options: [["trailing", "Trailing"], ["leading", "Leading"]] },
    { name: "indent_unit", label: "Indent with", hint: "Indent nested clauses with spaces or tabs.", type: "choice",
      options: [["space", "Spaces"], ["tab", "Tabs"]] },
    { name: "tab_space_size", label: "Indent size", hint: "Spaces per indent level (1 to 8).", type: "number", min: 1, max: 8 },
    { name: "max_line_length", label: "Max line length", hint: "Lines longer than this are wrapped (20 to 500).", type: "number", min: 20, max: 500 },
    { name: "rules", label: "sqlfluff rules", hint: "Rules or groups to apply, comma separated.", type: "list", placeholder: "layout, capitalisation, LT01" },
    { name: "exclude_rules", label: "Excluded rules", hint: "Rules to skip, comma separated.", type: "list", placeholder: "e.g. LT05" },
  ];
  const SECTIONS = [
    { id: "appearance", label: "Appearance", icon: '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/>' },
    { id: "formatting", label: "SQL formatting", icon: '<path d="M4 6h16M4 12h10M4 18h13"/>' },
    { id: "repositories", label: "Repositories", icon: '<circle cx="6" cy="6" r="2"/><circle cx="6" cy="18" r="2"/><circle cx="18" cy="9" r="2"/><path d="M6 8v8M18 11c0 4-6 3-12 5"/>' },
    { id: "bigquery", label: "BigQuery projects", icon: '<ellipse cx="12" cy="6" rx="8" ry="3"/><path d="M4 6v6c0 1.7 3.6 3 8 3s8-1.3 8-3V6M4 12v6c0 1.7 3.6 3 8 3s8-1.3 8-3v-6"/>' },
    { id: "scopes", label: "Scopes", icon: '<path d="M3 5h18l-7 8v6l-4 2v-8z"/>' },
  ];

  let dialog;
  let provider = null;
  let listeners = [];
  let ui = {};
  let profiles = [];
  let activeId = "";
  let current = "appearance";
  let saveTimer;
  let formatTimer;
  // sqlfluff's rule list (from the installed sqlfluff) and the active
  // configuration's enabled rule codes while the formatting section is shown.
  let ruleCatalog = null;
  let enabledRules = null;
  const CATEGORY_HINTS = {
    layout: "Spacing, indentation, line breaks and line length.",
    capitalisation: "Upper or lower case for keywords, names, functions, literals and types.",
    aliasing: "How tables and columns are aliased.",
    convention: "One consistent choice where SQL allows several ways to write the same thing.",
    structure: "Simpler query structure, like dropping redundant CASE branches or unused CTEs.",
    ambiguous: "SQL whose meaning is unclear, like a bare JOIN or DISTINCT with GROUP BY.",
    references: "How columns and tables are referenced and quoted.",
    jinja: "Spacing inside Jinja template tags.",
  };

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
  const svg = (paths) => {
    const icon = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    icon.setAttribute("viewBox", "0 0 24 24");
    icon.setAttribute("class", "icon");
    icon.setAttribute("aria-hidden", "true");
    icon.innerHTML = paths;
    return icon;
  };

  function active() {
    return profiles.find((item) => item.id === activeId) || profiles[0];
  }

  function applyTheme(theme) {
    if (theme === "light" || theme === "dark") document.documentElement.dataset.theme = theme;
    else delete document.documentElement.dataset.theme;
  }

  function notify() {
    for (const listener of listeners) listener(structuredClone(ui));
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

  function saveUi() {
    ui = structuredClone({ ...ui, ...(provider ? provider.getUi() : {}), ...pending() });
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify(ui));
    } catch {
      /* browser storage unavailable; the server copy still persists */
    }
    notify();
    clearTimeout(saveTimer);
    saveTimer = setTimeout(() => {
      putJson("/api/settings/ui", ui, "Could not save settings").catch((error) => setStatus(error.message, true));
    }, 200);
  }

  // Values owned by this panel; they win over whatever the page last saved.
  function pending() {
    return { theme: ui.theme || "system", sqlfluffProfiles: profiles, activeSqlfluffProfile: activeId };
  }

  function setStatus(text, error = false) {
    const status = dialog?.querySelector(".sp-status");
    if (!status) return;
    status.textContent = text;
    status.classList.toggle("is-error", error);
  }

  /* ---------- Controls ---------- */

  function segmented(name, options, value, onChange) {
    const group = h("div", { class: "sp-segmented", role: "radiogroup", "aria-label": name });
    for (const [optionValue, label] of options) {
      const button = h("button", {
        type: "button", role: "radio", "aria-checked": String(optionValue === value), text: label,
        onclick: () => {
          for (const other of group.children) other.setAttribute("aria-checked", String(other === button));
          onChange(optionValue);
        },
      });
      group.append(button);
    }
    group.addEventListener("keydown", (event) => {
      if (!["ArrowLeft", "ArrowRight"].includes(event.key)) return;
      const buttons = [...group.children];
      const index = buttons.indexOf(document.activeElement);
      const next = buttons[(index + (event.key === "ArrowRight" ? 1 : buttons.length - 1)) % buttons.length];
      next.focus();
      next.click();
    });
    return group;
  }

  function row(label, hint, control, id) {
    return h("div", { class: "sp-row", "data-search": `${label} ${hint}`.toLowerCase() },
      h("div", { class: "sp-row-text" }, h("label", { class: "sp-row-label", for: id, text: label }), h("p", { class: "sp-row-hint", text: hint })),
      h("div", { class: "sp-row-control" }, control));
  }

  /* ---------- Sections ---------- */

  function renderAppearance(body) {
    body.append(
      h("h3", { class: "sp-heading", text: "Appearance" }),
      row("Theme", "Match your system, or always use light or dark.", segmented("Theme", THEMES, ui.theme || "system", (theme) => {
        ui.theme = theme;
        applyTheme(theme);
        saveUi();
      })),
    );
  }

  function readFormat(form) {
    const format = { ...(active().format || {}) };
    for (const field of FORMAT_FIELDS) {
      const element = form.querySelector(`[data-field="${field.name}"]`);
      if (field.type === "choice") format[field.name] = element.querySelector('[aria-checked="true"]')?.dataset.value ?? format[field.name];
      else if (field.type === "number") format[field.name] = Number(element.value);
      else if (element) format[field.name] = element.value.split(",").map((item) => item.trim()).filter(Boolean);
    }
    if (enabledRules) {
      format.rules = ruleCatalog.map((rule) => rule.code).filter((code) => enabledRules.has(code));
      format.exclude_rules = [];
    }
    return format;
  }

  // Saved configurations may name groups ("layout"), rule names or legacy
  // aliases; turn them into the rule codes they cover.
  function expandRules(selectors) {
    const codes = new Set();
    for (const raw of selectors || []) {
      const selector = String(raw).trim().toLowerCase();
      for (const rule of ruleCatalog) {
        if (rule.code.toLowerCase() === selector || rule.name === selector
          || rule.aliases.some((alias) => alias.toLowerCase() === selector)
          || rule.groups.includes(selector)) codes.add(rule.code);
      }
    }
    return codes;
  }

  function renderRules(form) {
    const format = active().format || {};
    const excluded = expandRules(format.exclude_rules);
    enabledRules = new Set([...expandRules(format.rules)].filter((code) => !excluded.has(code)));
    const save = () => scheduleFormatSave(form);
    const categories = [...new Set(ruleCatalog.map((rule) => rule.category))];
    const search = h("input", { type: "search", class: "sp-input sp-rule-search", placeholder: "Search rules by code, name or description", "aria-label": "Search sqlfluff rules" });
    const empty = h("p", { class: "sp-rule-empty", text: "No rules match.", hidden: true });
    const groups = [];

    for (const category of categories) {
      const rules = ruleCatalog.filter((rule) => rule.category === category);
      const count = h("span", { class: "sp-rule-count" });
      const updateCount = () => {
        const on = rules.filter((rule) => enabledRules.has(rule.code)).length;
        count.textContent = `${on} of ${rules.length} on`;
      };
      const toggles = [];
      const setAll = (value) => {
        for (const [rule, input] of toggles) {
          if (!rule.fixable) continue;
          input.checked = value;
          if (value) enabledRules.add(rule.code); else enabledRules.delete(rule.code);
        }
        updateCount();
        save();
      };
      const list = h("div", { class: "sp-rule-list" });
      for (const rule of rules) {
        const id = `sp-rule-${rule.code}`;
        const input = h("input", { id, type: "checkbox", checked: enabledRules.has(rule.code), disabled: !rule.fixable });
        input.addEventListener("change", () => {
          if (input.checked) enabledRules.add(rule.code); else enabledRules.delete(rule.code);
          updateCount();
          save();
        });
        toggles.push([rule, input]);
        list.append(h("div", { class: `sp-rule${rule.fixable ? "" : " is-lint-only"}`, "data-search": `${rule.code} ${rule.name} ${rule.description}`.toLowerCase() },
          h("label", { class: "sp-rule-text", for: id },
            h("span", { class: "sp-rule-desc", text: rule.description }),
            h("span", { class: "sp-rule-meta" },
              h("code", { text: rule.code }), h("span", { text: rule.name }),
              rule.fixable ? null : h("span", { class: "sp-rule-tag", text: "Lint only", title: "sqlfluff can report this but not fix it, so it never changes formatted SQL" }))),
          h("label", { class: "switch sp-switch", title: rule.fixable ? "" : "Lint-only rules can't change formatted SQL" },
            input, h("span", { class: "switch-track", "aria-hidden": "true" }))));
      }
      updateCount();
      const group = h("section", { class: "sp-rule-group" },
        h("div", { class: "sp-rule-head" },
          h("div", {},
            h("h4", { text: category[0].toUpperCase() + category.slice(1) }),
            h("p", { class: "sp-row-hint", text: CATEGORY_HINTS[category] || "" })),
          h("div", { class: "sp-inline" }, count,
            h("button", { type: "button", class: "link-button", text: "All on", onclick: () => setAll(true) }),
            h("button", { type: "button", class: "link-button", text: "All off", onclick: () => setAll(false) }))),
        list);
      groups.push(group);
    }

    search.addEventListener("input", () => {
      const text = search.value.trim().toLowerCase();
      let shown = 0;
      for (const group of groups) {
        let groupShown = 0;
        for (const rule of group.querySelectorAll(".sp-rule")) {
          const match = !text || rule.dataset.search.includes(text);
          rule.hidden = !match;
          if (match) groupShown += 1;
        }
        group.hidden = !groupShown;
        shown += groupShown;
      }
      empty.hidden = shown > 0;
    });

    const lintOnly = ruleCatalog.filter((rule) => !rule.fixable).length;
    return h("div", { class: "sp-rules" },
      h("h3", { class: "sp-subheading", text: "Rules" }),
      h("p", { class: "sp-lede", text: `The sqlfluff rules the Format SQL rule applies (${ruleCatalog.length} BigQuery rules from sqlfluff). ${lintOnly} are lint only: sqlfluff can report them but not fix them, so they can't change formatted SQL and stay off.` }),
      search, empty, ...groups);
  }

  function scheduleFormatSave(form) {
    clearTimeout(formatTimer);
    setStatus("Saving…");
    formatTimer = setTimeout(async () => {
      if (enabledRules && !enabledRules.size) {
        setStatus("Turn on at least one rule", true);
        return;
      }
      const profile = active();
      try {
        profile.format = await putJson("/api/settings/format", readFormat(form), "Could not save sqlfluff settings");
        saveUi();
        setStatus("Saved");
      } catch (error) {
        setStatus(error.message, true);
      }
    }, 350);
  }

  async function activate(id) {
    activeId = id;
    const profile = active();
    try {
      if (profile.format) profile.format = await putJson("/api/settings/format", profile.format, "Could not apply sqlfluff settings");
    } catch (error) {
      setStatus(error.message, true);
    }
    saveUi();
    show("formatting");
  }

  function renderFormatting(body) {
    const profile = active();
    const format = profile.format || {};
    const select = h("select", { id: "sp-profile", class: "sp-input", onchange: (event) => activate(event.target.value) });
    for (const item of profiles) select.append(h("option", { value: item.id, text: item.name, selected: item.id === activeId }));
    // Renaming waits for the Rename button (or Enter); typing alone saves nothing.
    const rename = () => {
      const next = name.value.trim();
      if (next === profile.name) return;
      if (!next || profiles.some((item) => item !== profile && item.name.toLowerCase() === next.toLowerCase())) {
        setStatus(next ? "Configuration names must be unique" : "Give this configuration a name", true);
        return;
      }
      profile.name = next;
      name.value = next;
      select.querySelector(`option[value="${profile.id}"]`).textContent = next;
      renameButton.disabled = true;
      saveUi();
      setStatus("Renamed");
    };
    const name = h("input", {
      id: "sp-profile-name", class: "sp-input", type: "text", maxlength: "60", value: profile.name,
      oninput: () => {
        const next = name.value.trim();
        renameButton.disabled = !next || next === profile.name;
        setStatus("");
      },
      onkeydown: (event) => { if (event.key === "Enter") { event.preventDefault(); rename(); } },
    });
    const renameButton = h("button", { type: "button", class: "toolbar-button", text: "Rename", disabled: true, onclick: rename });
    const actions = h("div", { class: "sp-inline" },
      renameButton,
      h("button", { type: "button", class: "toolbar-button", text: "New", onclick: () => {
        const item = { id: crypto.randomUUID(), name: `Configuration ${profiles.length + 1}`, format: { ...format } };
        profiles.push(item);
        activate(item.id).then(() => dialog.querySelector("#sp-profile-name")?.select());
      } }),
      h("button", { type: "button", class: "toolbar-button sp-danger", text: "Delete", disabled: profiles.length < 2, onclick: () => {
        if (profiles.length < 2 || !confirm(`Delete the configuration “${profile.name}”?`)) return;
        profiles = profiles.filter((item) => item !== profile);
        activate(profiles[0].id);
      } }),
    );

    const form = h("form", { class: "sp-group", autocomplete: "off", onsubmit: (event) => event.preventDefault() });
    for (const field of FORMAT_FIELDS) {
      if (field.type === "list" && ruleCatalog) continue;
      const id = `sp-${field.name}`;
      let control;
      if (field.type === "choice") {
        control = segmented(field.label, field.options, format[field.name], () => scheduleFormatSave(form));
        for (const [index, [value]] of field.options.entries()) control.children[index].dataset.value = value;
      } else if (field.type === "number") {
        control = h("input", { id, class: "sp-input sp-number", type: "number", min: field.min, max: field.max, step: "1", value: format[field.name] ?? "" });
      } else {
        control = h("input", { id, class: "sp-input sp-text", type: "text", spellcheck: "false", placeholder: field.placeholder, value: (format[field.name] || []).join(", ") });
      }
      control.dataset.field = field.name;
      if (field.type !== "choice") control.addEventListener("change", () => scheduleFormatSave(form));
      form.append(row(field.label, field.hint, control, field.type === "choice" ? null : id));
    }

    body.append(
      h("h3", { class: "sp-heading", text: "SQL formatting" }),
      h("p", { class: "sp-lede", text: "Used by the Format SQL rule in the pipeline. Keep several named sqlfluff configurations and pick which one is active." }),
      h("div", { class: "sp-group" },
        row("Active configuration", "The configuration the Format SQL rule uses.", select, "sp-profile"),
        row("Name", "Type a new name, then press Rename.", h("div", { class: "sp-inline" }, name, actions), "sp-profile-name")),
      form,
      ...(ruleCatalog ? [renderRules(form)] : []),
    );
  }

  function renderScopes(body) {
    if (!window.KumoScopes) {
      body.append(h("p", { class: "sp-lede", text: "Scopes could not be loaded on this page." }));
      return;
    }
    window.KumoScopes.renderManager(body, { onStatus: setStatus });
  }

  /* Connected Dataform repositories: saved on this computer and reloaded on start. */
  let reposChanged = false;
  async function repoCall(method, url, payload) {
    const response = await fetch(url, {
      method, headers: { "Content-Type": "application/json" }, body: payload === undefined ? undefined : JSON.stringify(payload),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "Request failed");
    if (method !== "GET") reposChanged = true;
    return data;
  }

  function ago(iso) {
    const seconds = Math.max(0, (Date.now() - Date.parse(iso)) / 1000);
    if (!Number.isFinite(seconds)) return "";
    if (seconds < 90) return "just now";
    if (seconds < 5400) return `${Math.round(seconds / 60)} min ago`;
    if (seconds < 129600) return `${Math.round(seconds / 3600)} h ago`;
    return `${Math.round(seconds / 86400)} days ago`;
  }

  async function renderRepositories(body) {
    body.append(
      h("h3", { class: "sp-heading", text: "Repositories" }),
      h("p", { class: "sp-lede", text: "Connect the Dataform repositories you want analysed. They are saved here and reloaded each time KumoSQL starts. Git runs on this computer with your own credentials, so private repositories work with an SSH remote (git@github.com:owner/repo.git), an https URL, or a local path. The active repository feeds the Query graph, Cost and Change reports pages." }));
    const list = h("ul", { class: "repo-list" });
    const url = h("input", { type: "text", class: "sp-input sp-text", placeholder: "git@github.com:owner/repository.git", "aria-label": "Repository URL", autocomplete: "off", required: "" });
    const branch = h("input", { type: "text", class: "sp-input", placeholder: "branch (default)", "aria-label": "Branch", autocomplete: "off", size: "14" });
    const form = h("form", { class: "repo-form" }, url, branch, h("button", { type: "submit", class: "toolbar-button", text: "Connect" }));
    body.append(list, form);
    let data = { repositories: [], active: null };

    const draw = () => {
      list.replaceChildren();
      if (!data.repositories.length) list.append(h("li", { class: "sp-row-hint", text: "No repositories connected yet." }));
      for (const repo of data.repositories) {
        const isActive = repo.id === data.active;
        const status = repo.error ? `Failed ${ago(repo.error_at)}: ${repo.error}`
          : repo.last_loaded ? `${repo.label || "Loaded"} · ${repo.files} files · loaded ${ago(repo.last_loaded)}${repo.stale_reason ? ` · using saved copy, could not fetch: ${repo.stale_reason}` : ""}`
          : "Not loaded yet";
        const refresh = h("button", { type: "button", class: "toolbar-button", text: "Refresh" });
        refresh.addEventListener("click", () => run(refresh, async () => { await repoCall("POST", "/api/repositories/refresh", { id: repo.id }); }, "Loaded"));
        const use = h("button", { type: "button", class: "toolbar-button", text: "Use", hidden: isActive });
        use.addEventListener("click", () => run(use, async () => { await repoCall("POST", "/api/repositories/activate", { id: repo.id }); }, "Loaded"));
        const remove = h("button", { type: "button", class: "link-button", text: "Remove" });
        remove.addEventListener("click", () => run(remove, async () => {
          const rest = data.repositories.filter((item) => item.id !== repo.id).map(({ url: u, branch: b }) => ({ url: u, branch: b }));
          await repoCall("POST", "/api/repositories", { repositories: rest });
        }, "Removed"));
        list.append(h("li", { class: `repo-item${isActive ? " is-active" : ""}` },
          h("div", { class: "repo-main" },
            h("strong", { class: "repo-url", text: repo.url }),
            h("span", { class: "repo-branch", text: repo.branch ? ` @ ${repo.branch}` : "" }),
            isActive ? h("span", { class: "repo-badge", text: "Active" }) : ""),
          h("p", { class: `sp-row-hint${repo.error ? " is-error" : ""}`, text: status }),
          h("div", { class: "repo-actions" }, use, refresh, remove)));
      }
    };
    const refreshList = async () => { data = await repoCall("GET", "/api/repositories"); draw(); };
    async function run(button, action, done) {
      button.disabled = true;
      setStatus("Working with git…");
      try { await action(); setStatus(done); } catch (error) { setStatus(error.message, true); }
      button.disabled = false;
      try { await refreshList(); } catch { /* keep the last list */ }
    }
    form.addEventListener("submit", (event) => {
      event.preventDefault();
      const button = form.querySelector("button");
      run(button, async () => {
        const current = data.repositories.map(({ url: u, branch: b }) => ({ url: u, branch: b }));
        const saved = await repoCall("POST", "/api/repositories", { repositories: [...current, { url: url.value.trim(), branch: branch.value.trim() }] });
        const added = saved.repositories[saved.repositories.length - 1];
        url.value = "";
        branch.value = "";
        await repoCall("POST", "/api/repositories/activate", { id: added.id });
      }, "Connected");
    });
    try { await refreshList(); } catch (error) { setStatus(error.message, true); }
  }

  function renderBigQuery(body) {
    body.append(h("h3", { class: "sp-heading", text: "BigQuery projects" }));
    if (!window.KumoBqProjects) {
      body.append(h("p", { class: "sp-lede", text: "The project picker could not be loaded on this page." }));
      return;
    }
    window.KumoBqProjects.render(body, { onStatus: setStatus });
    window.KumoBqProjects.renderBilling(body, { onStatus: setStatus });
  }

  const RENDERERS = { appearance: renderAppearance, formatting: renderFormatting, repositories: renderRepositories, bigquery: renderBigQuery, scopes: renderScopes };

  function show(id) {
    current = RENDERERS[id] ? id : "appearance";
    enabledRules = null;
    for (const link of dialog.querySelectorAll(".sp-nav button")) {
      link.setAttribute("aria-current", String(link.dataset.section === current));
    }
    const body = dialog.querySelector(".sp-body");
    body.replaceChildren();
    RENDERERS[current](body);
    body.scrollTop = 0;
    filter(dialog.querySelector(".sp-search input").value);
  }

  // Search narrows the sidebar to sections with a matching setting.
  const KEYWORDS = {
    appearance: "appearance theme light dark system colour color mode",
    repositories: "repositories repository dataform git connect ssh https branch refresh private remote url project",
    bigquery: "bigquery projects choose select project catalog browse tab billing project query cache hours lifetime",
    scopes: "scopes scope rule rules filter condition submitter project dataset field limit active",
    formatting: `sql formatting sqlfluff configuration profile ${FORMAT_FIELDS.map((field) => `${field.label} ${field.hint}`).join(" ")}`.toLowerCase(),
  };
  function keywordsFor(section) {
    if (section !== "formatting" || !ruleCatalog) return KEYWORDS[section];
    const rules = ruleCatalog.map((rule) => `${rule.code} ${rule.name} ${rule.description}`).join(" ");
    return `${KEYWORDS.formatting} ${rules.toLowerCase()}`;
  }

  function filter(query) {
    const text = query.trim().toLowerCase();
    let first = null;
    for (const link of dialog.querySelectorAll(".sp-nav button")) {
      const match = !text || keywordsFor(link.dataset.section).includes(text);
      link.hidden = !match;
      if (match && !first) first = link.dataset.section;
    }
    for (const item of dialog.querySelectorAll(".sp-row")) {
      item.classList.toggle("is-match", Boolean(text) && item.dataset.search.includes(text));
    }
    // A search that only matches sqlfluff rules narrows the rule list as well.
    const ruleSearch = dialog.querySelector(".sp-rule-search");
    if (ruleSearch && !KEYWORDS.formatting.includes(text)) {
      ruleSearch.value = text;
      ruleSearch.dispatchEvent(new Event("input"));
    }
    return first;
  }

  function build() {
    const search = h("input", { type: "search", placeholder: "Search", "aria-label": "Search settings" });
    search.addEventListener("input", () => {
      const first = filter(search.value);
      if (first && !keywordsFor(current).includes(search.value.trim().toLowerCase())) show(first);
    });
    const nav = h("nav", { class: "sp-nav", "aria-label": "Settings sections" });
    for (const section of SECTIONS) {
      nav.append(h("button", { type: "button", "data-section": section.id, onclick: () => show(section.id) }, svg(section.icon), h("span", { text: section.label })));
    }
    dialog = h("dialog", { class: "settings-panel", "aria-label": "Settings" },
      h("aside", { class: "sp-side" },
        h("label", { class: "sp-search" }, svg('<circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/>'), search),
        h("p", { class: "sp-side-label", text: "Settings" }),
        nav,
        h("p", { class: "sp-side-foot", text: "Saved on this computer" })),
      h("div", { class: "sp-main" },
        h("div", { class: "sp-top" },
          h("span", { class: "sp-status", role: "status", "aria-live": "polite" }),
          h("button", { type: "button", class: "icon-button sp-close", "aria-label": "Close settings", onclick: () => dialog.close() }, svg('<path d="M6 6l12 12M18 6 6 18"/>'))),
        h("div", { class: "sp-body" })),
    );
    // Pages that show the connected project reload to pick up a changed repository.
    dialog.addEventListener("close", () => {
      if (reposChanged && document.getElementById("project-form")) location.reload();
      reposChanged = false;
    });
    // Close when clicking the dimmed backdrop, not the panel.
    dialog.addEventListener("mousedown", (event) => { if (event.target === dialog) dialog.close(); });
    // A focused search box would otherwise swallow Escape.
    dialog.addEventListener("keydown", (event) => { if (event.key === "Escape") { event.preventDefault(); dialog.close(); } });
    document.body.append(dialog);
  }

  async function load() {
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
    if (provider) ui = { ...ui, ...structuredClone(provider.getUi()) };
    if (!ruleCatalog) {
      try {
        const response = await fetch("/api/sqlfluff/rules");
        if (response.ok) ruleCatalog = await response.json();
      } catch {
        /* keep the free-text rule fields */
      }
    }
    const loaded = Array.isArray(ui.sqlfluffProfiles) ? ui.sqlfluffProfiles : [];
    profiles = loaded.filter((item) => item && typeof item.name === "string" && item.format && typeof item.id === "string");
    if (!profiles.length) profiles = [{ id: crypto.randomUUID(), name: "Default", format }];
    activeId = profiles.some((item) => item.id === ui.activeSqlfluffProfile) ? ui.activeSqlfluffProfile : profiles[0].id;
  }

  async function open(section = current) {
    if (!dialog) build();
    await load();
    setStatus("");
    dialog.querySelector(".sp-search input").value = "";
    show(section);
    if (!dialog.open) dialog.showModal();
    dialog.querySelector(`.sp-nav button[data-section="${current}"]`)?.focus();
  }

  window.KumoSettings = {
    open,
    // The sidebar's theme button: apply, then save like the Appearance section does.
    async setTheme(theme) {
      applyTheme(theme);
      await load();
      ui.theme = theme;
      saveUi();
    },
    // The workspace passes getUi (its current saved state) and onChange.
    register({ getUi, onChange }) {
      provider = { getUi };
      if (onChange) listeners.push(onChange);
    },
  };

  // Apply the saved theme on every page, including ones with no page script of
  // their own: the browser copy first so it shows at once, then the server's.
  try {
    applyTheme(JSON.parse(localStorage.getItem(STORAGE_KEY))?.theme);
  } catch {
    /* browser storage unavailable */
  }
  fetch("/api/settings")
    .then((response) => (response.ok ? response.json() : null))
    .then((settings) => { if (settings?.ui?.theme) applyTheme(settings.ui.theme); })
    .catch(() => {});

  document.addEventListener("click", (event) => {
    const trigger = event.target.closest("[data-open-settings]");
    if (!trigger) return;
    event.preventDefault();
    open(trigger.dataset.openSettings || undefined);
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "," && (event.ctrlKey || event.metaKey)) {
      event.preventDefault();
      open();
    }
  });
})();
