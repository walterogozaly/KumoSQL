"use strict";

/* App shell: the left navigation sidebar shared by every page. Page links sit
   at the top; Settings and the theme control are pinned at the bottom. The
   sidebar collapses to icons, and the choice is remembered between visits. */

(() => {
  const KEY = "kumosql-sidebar";
  const root = document.documentElement;
  const THEMES = ["system", "light", "dark"];
  const THEME_META = {
    system: { icon: '<rect x="3" y="4" width="18" height="12" rx="2"/><path d="M8 20h8M12 16v4"/>', label: "Theme: match system" },
    light: { icon: '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/>', label: "Theme: light" },
    dark: { icon: '<path d="M20 14.5A8 8 0 0 1 9.5 4a8 8 0 1 0 10.5 10.5z"/>', label: "Theme: dark" },
  };
  const PAGES = [
    { href: "/", label: "Workspace", icon: '<path d="m16 18 6-6-6-6M8 6l-6 6 6 6"/>' },
    { href: "/graph", label: "Query graph", icon: '<circle cx="6" cy="6" r="2.5"/><circle cx="18" cy="8" r="2.5"/><circle cx="12" cy="18" r="2.5"/><path d="M8.2 7.2l7.6.4M7.2 8l3.6 8M16.8 10l-3.6 6"/>' },
    { href: "/cost", label: "Cost", icon: '<path d="M4 20V10M10 20V4M16 20v-7M22 20H2"/>' },
    { href: "/changes", label: "Change reports", icon: '<path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/><path d="M14 3v5h5M9 13h6M9 17h4"/>' },
    { href: "/refactor", label: "Refactor", icon: '<path d="M4 7h10M4 17h10M14 7l4-3v6zM14 17l4-3v6z"/><path d="M20 12h0"/>' },
    { href: "/shared-models", label: "Shared models", icon: '<rect x="9" y="3" width="6" height="5" rx="1"/><rect x="3" y="16" width="6" height="5" rx="1"/><rect x="15" y="16" width="6" height="5" rx="1"/><path d="M12 8v4M6 16v-2h12v2"/>' },
    { href: "/dead-columns", label: "Dead columns", icon: '<path d="M4 5h16M4 10h10M4 15h7M4 20h4"/><path d="m17 14 4 6m0-6-4 6"/>' },
    { href: "/reduce", label: "Reduce", icon: '<path d="M4 4h16l-6 8v6l-4 2v-8z"/>' },
    { href: "/browse", label: "BigQuery", icon: '<ellipse cx="12" cy="5" rx="8" ry="3"/><path d="M4 5v14c0 1.7 3.6 3 8 3s8-1.3 8-3V5M4 12c0 1.7 3.6 3 8 3s8-1.3 8-3"/>' },
  ];
  const SETTINGS_ICON = '<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z"/>';
  const PANEL_ICON = '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M9 4v16"/>';

  const stored = () => {
    try { return localStorage.getItem(KEY); } catch { return null; }
  };
  const remember = (value) => {
    try { localStorage.setItem(KEY, value); } catch { /* the choice just won't persist */ }
  };

  // Set before first paint so a collapsed sidebar doesn't flash open.
  if (stored() === "collapsed") root.dataset.sidebar = "collapsed";

  const icon = (paths) => `<svg class="icon" viewBox="0 0 24 24" aria-hidden="true">${paths}</svg>`;
  const currentPage = () => {
    const path = location.pathname.replace(/\/+$/, "") || "/";
    return PAGES.find((page) => page.href === path) || PAGES[0];
  };

  /** Show what the server is busy with (parsing, analysis) in the sidebar; it never blocks anything. */
  function watchActivity(rail) {
    const box = rail.querySelector("#rail-activity");
    const text = rail.querySelector("#rail-activity-text");
    let timer = 0;
    async function poll() {
      let delay = 5000;
      try {
        const response = await fetch("/api/status", { cache: "no-store" });
        const status = await response.json();
        const busy = status.busy[0];
        const background = status.analysis && status.analysis.state === "running" ? status.analysis : null;
        const item = busy || (background && { label: `Analyzing ${background.stage || "models"}`, elapsed: background.elapsed });
        if (item) {
          const step = (status.progress || [])[0];
          const count = step ? ` ${step.done.toLocaleString()}/${step.total.toLocaleString()}` : "";
          text.textContent = `${item.label}…${count} ${Math.round(item.elapsed)}s`;
          box.title = "KumoSQL is working in the background. You can keep using the app.";
          box.hidden = false;
          delay = 1500;
        } else {
          box.hidden = true;
        }
      } catch { box.hidden = true; }
      timer = setTimeout(poll, document.hidden ? 15000 : delay);
    }
    poll();
    document.addEventListener("visibilitychange", () => { if (!document.hidden) { clearTimeout(timer); poll(); } });
  }

  function build() {
    const active = currentPage();
    const links = PAGES.map((page) =>
      `<a class="rail-link" href="${page.href}" title="${page.label}"${page === active ? ' aria-current="page"' : ""}>${icon(page.icon)}<span class="rail-label">${page.label}</span></a>`
    ).join("");

    const rail = document.createElement("aside");
    rail.id = "rail";
    rail.className = "rail";
    rail.innerHTML = `
      <div class="rail-head">
        <span class="brand-mark" aria-hidden="true">{ }</span>
        <span class="rail-brand rail-label"><strong>KumoSQL</strong><span>SQL workspace</span></span>
        <button class="rail-btn" id="rail-toggle" type="button" aria-controls="rail">${icon(PANEL_ICON)}</button>
      </div>
      <nav class="rail-nav" aria-label="Main navigation">${links}</nav>
      <div class="rail-foot">
        <span class="rail-status" title="SQL is processed on this computer and never sent to BigQuery"><span class="local-dot" aria-hidden="true"></span><span class="rail-label">Running locally</span></span>
        <span class="rail-activity" id="rail-activity" role="status" aria-live="polite" hidden><span class="rail-spinner" aria-hidden="true"></span><span class="rail-label" id="rail-activity-text"></span></span>
        <span class="rail-version rail-label" id="rail-version" title="KumoSQL version"></span>
        <button class="rail-link" id="theme-button" type="button"><span id="theme-icon">${icon(THEME_META.system.icon)}</span><span class="rail-label" id="theme-label"></span></button>
        <button class="rail-link" id="settings-button" type="button" data-open-settings title="Settings (Ctrl+,)">${icon(SETTINGS_ICON)}<span class="rail-label">Settings</span></button>
      </div>`;

    document.body.prepend(rail);
    watchActivity(rail);
    document.body.classList.add("has-rail");

    const toggle = rail.querySelector("#rail-toggle");
    const collapsed = () => root.dataset.sidebar === "collapsed";

    function syncToggle() {
      const label = collapsed() ? "Expand sidebar" : "Collapse sidebar";
      toggle.setAttribute("aria-label", label);
      toggle.title = label;
      toggle.setAttribute("aria-expanded", String(!collapsed()));
    }
    toggle.addEventListener("click", () => {
      if (collapsed()) delete root.dataset.sidebar; else root.dataset.sidebar = "collapsed";
      remember(collapsed() ? "collapsed" : "expanded");
      syncToggle();
    });
    syncToggle();

    // Theme: one click cycles system, light, dark; settings.js saves it.
    const themeButton = rail.querySelector("#theme-button");
    function showTheme() {
      const theme = root.dataset.theme || "system";
      const meta = THEME_META[theme];
      rail.querySelector("#theme-icon").innerHTML = icon(meta.icon);
      rail.querySelector("#theme-label").textContent = meta.label.replace("Theme: match system", "Theme: system").replace("Theme: ", "Theme · ");
      themeButton.title = meta.label;
      themeButton.setAttribute("aria-label", meta.label);
    }
    themeButton.addEventListener("click", () => {
      const next = THEMES[(THEMES.indexOf(root.dataset.theme || "system") + 1) % THEMES.length];
      if (window.KumoSettings?.setTheme) window.KumoSettings.setTheme(next);
      else if (next === "system") delete root.dataset.theme; else root.dataset.theme = next;
      showTheme();
    });
    new MutationObserver(showTheme).observe(root, { attributes: true, attributeFilter: ["data-theme"] });
    showTheme();
    fetch("/api/version").then((response) => response.json()).then((data) => {
      const label = data.commit ? `v${data.version} · ${data.commit}` : `v${data.version}`;
      const node = rail.querySelector("#rail-version");
      node.textContent = label;
      node.title = `KumoSQL ${label}`;
    }).catch(() => { /* version is informational */ });
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", build);
  else build();
})();
