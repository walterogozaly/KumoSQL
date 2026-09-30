"use strict";

/* Settings panel. A modal with a section sidebar that opens over whichever page
   you are on (Settings button in the top bar, or Ctrl/⌘ + ,). Changes save as
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
      else format[field.name] = element.value.split(",").map((item) => item.trim()).filter(Boolean);
    }
    return format;
  }

  function scheduleFormatSave(form) {
    clearTimeout(formatTimer);
    setStatus("Saving…");
    formatTimer = setTimeout(async () => {
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
    const name = h("input", {
      id: "sp-profile-name", class: "sp-input", type: "text", maxlength: "60", value: profile.name,
      onchange: (event) => {
        const next = event.target.value.trim();
        if (!next || profiles.some((item) => item !== profile && item.name.toLowerCase() === next.toLowerCase())) {
          setStatus(next ? "Configuration names must be unique" : "Give this configuration a name", true);
          event.target.value = profile.name;
          return;
        }
        profile.name = next;
        select.querySelector(`option[value="${profile.id}"]`).textContent = next;
        saveUi();
        setStatus("Saved");
      },
    });
    const actions = h("div", { class: "sp-inline" },
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
        row("Name", "Rename the active configuration.", h("div", { class: "sp-inline" }, name, actions), "sp-profile-name")),
      form,
    );
  }

  const RENDERERS = { appearance: renderAppearance, formatting: renderFormatting };

  function show(id) {
    current = RENDERERS[id] ? id : "appearance";
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
    formatting: `sql formatting sqlfluff configuration profile ${FORMAT_FIELDS.map((field) => `${field.label} ${field.hint}`).join(" ")}`.toLowerCase(),
  };
  function filter(query) {
    const text = query.trim().toLowerCase();
    let first = null;
    for (const link of dialog.querySelectorAll(".sp-nav button")) {
      const match = !text || KEYWORDS[link.dataset.section].includes(text);
      link.hidden = !match;
      if (match && !first) first = link.dataset.section;
    }
    for (const item of dialog.querySelectorAll(".sp-row")) {
      item.classList.toggle("is-match", Boolean(text) && item.dataset.search.includes(text));
    }
    return first;
  }

  function build() {
    const search = h("input", { type: "search", placeholder: "Search", "aria-label": "Search settings" });
    search.addEventListener("input", () => {
      const first = filter(search.value);
      if (first && !KEYWORDS[current].includes(search.value.trim().toLowerCase())) show(first);
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
    // The workspace passes getUi (its current saved state) and onChange.
    register({ getUi, onChange }) {
      provider = { getUi };
      if (onChange) listeners.push(onChange);
    },
  };

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
