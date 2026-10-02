/* Large-graph lineage view.
   Self-contained: KumoLineage.mount(container, options) draws the query graph with Cytoscape.js
   (vendored in /assets/vendor) using a layered layout computed here, so it stays fast with
   thousands of nodes. It knows nothing about the rest of the page: the page passes the graph
   payload ({nodes, edges, gaps}) and gets selections back through onSelect.

   Features: zoom and pan, automatic left-to-right layering, focus on a node's upstream and
   downstream, collapse by dataset, hover highlight, and a minimap. */
(function () {
  "use strict";

  const NODE_W = 168;
  const NODE_H = 36;
  const COL_GAP = 110;
  const ROW_GAP = 14;
  const GROUP_PREFIX = "group:";

  function el(tag, props, ...children) {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(props || {})) {
      if (key === "class") node.className = value;
      else if (key === "text") node.textContent = value;
      else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
      else if (value !== false && value != null) node.setAttribute(key, value === true ? "" : value);
    }
    for (const child of children.flat()) if (child != null) node.append(child);
    return node;
  }

  /** Longest-path layer per node. Iterative (Kahn) so deep chains cannot overflow the stack; nodes on a cycle are placed after their acyclic parents. */
  function assignLayers(ids, edges) {
    const indegree = new Map(ids.map((id) => [id, 0]));
    const children = new Map(ids.map((id) => [id, []]));
    for (const [from, to] of edges) {
      children.get(from).push(to);
      indegree.set(to, indegree.get(to) + 1);
    }
    const layer = new Map(ids.map((id) => [id, 0]));
    const queue = ids.filter((id) => indegree.get(id) === 0);
    const remaining = new Map(indegree);
    for (let i = 0; i < queue.length; i++) {
      const id = queue[i];
      for (const next of children.get(id)) {
        layer.set(next, Math.max(layer.get(next), layer.get(id) + 1));
        remaining.set(next, remaining.get(next) - 1);
        if (remaining.get(next) === 0) queue.push(next);
      }
    }
    // Anything left is on a cycle: keep its layer from the acyclic parents already placed.
    return layer;
  }

  /** Layered positions: longest-path columns, rows ordered by repeated barycenter sweeps to cut edge crossings. */
  function layeredLayout(ids, edges) {
    const layer = assignLayers(ids, edges);
    const columns = [];
    for (const id of [...ids].sort()) (columns[layer.get(id)] ||= []).push(id);
    const compact = columns.filter(Boolean);
    const up = new Map(ids.map((id) => [id, []]));
    const down = new Map(ids.map((id) => [id, []]));
    for (const [from, to] of edges) { up.get(to).push(from); down.get(from).push(to); }
    const order = new Map();
    const reindex = () => compact.forEach((column) => column.forEach((id, i) => order.set(id, i)));
    reindex();
    const sweep = (column, neighbours) => {
      const score = new Map();
      for (const id of column) {
        const list = neighbours.get(id);
        score.set(id, list.length ? list.reduce((sum, other) => sum + order.get(other), 0) / list.length : order.get(id));
      }
      column.sort((a, b) => score.get(a) - score.get(b) || order.get(a) - order.get(b));
      column.forEach((id, i) => order.set(id, i));
    };
    const sweeps = ids.length > 4000 ? 2 : 4;
    for (let pass = 0; pass < sweeps; pass++) {
      for (let c = 1; c < compact.length; c++) sweep(compact[c], up);
      for (let c = compact.length - 2; c >= 0; c--) sweep(compact[c], down);
    }
    const positions = new Map();
    const tallest = Math.max(1, ...compact.map((column) => column.length));
    compact.forEach((column, c) => {
      const offset = ((tallest - column.length) * (NODE_H + ROW_GAP)) / 2;
      column.forEach((id, i) => positions.set(id, { x: c * (NODE_W + COL_GAP), y: offset + i * (NODE_H + ROW_GAP) }));
    });
    return positions;
  }

  function reach(start, adjacency, hops) {
    const seen = new Map([[start, 0]]);
    let frontier = [start];
    for (let depth = 1; depth <= hops && frontier.length; depth++) {
      const next = [];
      for (const id of frontier) {
        for (const other of adjacency.get(id) || []) {
          if (!seen.has(other)) { seen.set(other, depth); next.push(other); }
        }
      }
      frontier = next;
    }
    return seen;
  }

  function mount(container, options) {
    const { data, onSelect } = options;
    const gaps = new Set((data.gaps || []).map((gap) => gap.asset));
    const nodesById = new Map(data.nodes.map((node) => [node.id, node]));
    const datasets = [...new Set(data.nodes.map((node) => node.dataset))].sort();
    const datasetSize = new Map();
    for (const node of data.nodes) datasetSize.set(node.dataset, (datasetSize.get(node.dataset) || 0) + 1);
    const fullUp = new Map();
    const fullDown = new Map();
    for (const edge of data.edges) {
      if (!nodesById.has(edge.from) || !nodesById.has(edge.to)) continue;
      if (!fullDown.has(edge.from)) fullDown.set(edge.from, []);
      if (!fullUp.has(edge.to)) fullUp.set(edge.to, []);
      fullDown.get(edge.from).push(edge.to);
      fullUp.get(edge.to).push(edge.from);
    }

    const state = {
      selected: null,
      lit: new Set(),
      collapsed: new Set(),
      focus: { on: false, hops: 2, direction: "both" },
    };
    let cy = null;
    let visible = new Map();   // visible node id -> { members: [node ids] }
    let owner = new Map();     // real node id -> visible node id
    let tooltipTimer = 0;
    let placed = false;

    /* ----- chrome ----- */
    const stage = el("div", { class: "lv-stage" });
    const tooltip = el("div", { class: "lv-tooltip", role: "tooltip", hidden: true });
    const minimap = el("canvas", { class: "lv-minimap", width: 200, height: 130, "aria-label": "Minimap. Click or drag to move the view." });
    const status = el("span", { class: "lv-status", role: "status" });
    const datasetPick = el("select", { class: "field lv-select", "aria-label": "Collapse or expand a dataset", onchange: () => {
      const name = datasetPick.value;
      datasetPick.value = "";
      if (!name) return;
      toggleDataset(name);
    } });
    const focusToggle = el("button", { type: "button", class: "toolbar-button lv-btn", "aria-pressed": "false", title: "Show only the selected asset with its upstream and downstream", onclick: () => setFocus({ on: !state.focus.on }) }, "Focus selection");
    const hopsPick = el("select", { class: "field lv-select", "aria-label": "How far to follow", onchange: () => setFocus({ hops: Number(hopsPick.value) }) },
      [[1, "1 hop"], [2, "2 hops"], [3, "3 hops"], [5, "5 hops"], [999, "All"]].map(([value, label]) => el("option", { value, selected: value === state.focus.hops }, label)));
    const directionPick = el("select", { class: "field lv-select", "aria-label": "Direction", onchange: () => setFocus({ direction: directionPick.value }) },
      [["both", "Up and down"], ["up", "Upstream"], ["down", "Downstream"]].map(([value, label]) => el("option", { value }, label)));
    const toolbar = el("div", { class: "lv-toolbar" },
      focusToggle, hopsPick, directionPick,
      el("span", { class: "lv-sep", "aria-hidden": "true" }),
      datasetPick,
      el("button", { type: "button", class: "toolbar-button lv-btn", onclick: () => { state.collapsed = new Set(datasets); rebuild(true); } }, "Collapse all"),
      el("button", { type: "button", class: "toolbar-button lv-btn", onclick: () => { state.collapsed.clear(); rebuild(true); } }, "Expand all"),
      el("span", { class: "lv-spacer" }), status);
    const zoomBox = el("div", { class: "lv-zoom", role: "group", "aria-label": "Zoom" },
      el("button", { type: "button", class: "lv-zbtn", "aria-label": "Zoom in", onclick: () => zoomBy(1.4) }, "+"),
      el("button", { type: "button", class: "lv-zbtn", "aria-label": "Zoom out", onclick: () => zoomBy(1 / 1.4) }, "−"),
      el("button", { type: "button", class: "lv-zbtn lv-fit", "aria-label": "Fit the whole graph", title: "Fit to view", onclick: () => fit() }, "Fit"));
    container.append(toolbar, el("div", { class: "lv-frame" }, stage, zoomBox, minimap, tooltip));

    function refreshDatasetPick() {
      datasetPick.replaceChildren(el("option", { value: "" }, "Collapse by dataset…"),
        ...datasets.map((name) => el("option", { value: name }, `${state.collapsed.has(name) ? "Expand" : "Collapse"} ${name} (${datasetSize.get(name)})`)));
    }

    /* ----- theme ----- */
    function palette() {
      const css = getComputedStyle(document.documentElement);
      const get = (name, fallback) => css.getPropertyValue(name).trim() || fallback;
      return {
        surface: get("--surface", "#fff"), surface2: get("--surface-2", "#f6f8fc"), border: get("--border-strong", "#a7b9d1"),
        text: get("--text", "#172233"), muted: get("--text-muted", "#54627a"), accent: get("--accent", "#1f4fa8"),
        accentSoft: get("--accent-soft", "#e9f0fc"), accentBorder: get("--accent-border", "#8aacde"),
        warn: get("--warn", "#c7871f"), ok: get("--ok", "#2c8a5a"), warnBg: get("--warn-bg", "#fff3dc"), bg: get("--code-bg", "#fafbfe"), edge: get("--border-strong", "#a7b9d1"),
      };
    }

    function stylesheet(large) {
      const c = palette();
      return [
        { selector: "node", style: {
          shape: "round-rectangle", width: NODE_W, height: NODE_H, "background-color": c.surface, "border-width": 1, "border-color": c.border,
          label: "data(label)", color: c.text, "font-size": 12, "font-family": "ui-monospace, SFMono-Regular, Consolas, monospace",
          "text-valign": "center", "text-halign": "center", "text-max-width": NODE_W - 14, "text-wrap": "ellipsis", "min-zoomed-font-size": 7,
        } },
        { selector: "node.kind-source", style: { "background-color": c.surface2 } },
        { selector: "node.kind-observed", style: { "border-style": "dashed" } },
        { selector: "node.not-owned", style: { opacity: 0.7 } },
        { selector: "node.is-gap", style: { "border-style": "dashed", "border-color": c.warn, "background-color": c.warnBg } },
        { selector: "node.is-scheduled", style: { "border-width": 2, "border-color": c.ok } },
        { selector: "node.is-group", style: { shape: "round-rectangle", width: NODE_W + 14, height: NODE_H + 12, "border-width": 2, "background-color": c.accentSoft, "border-color": c.accentBorder, "font-weight": 700 } },
        { selector: "node.is-lit", style: { "border-color": c.accent, "border-width": 2 } },
        { selector: "node.is-selected", style: { "border-color": c.accent, "border-width": 3, "background-color": c.accentSoft } },
        { selector: "node.is-dim", style: { opacity: 0.3 } },
        { selector: "node.is-hover", style: { "border-color": c.accent, "border-width": 2.5 } },
        { selector: "edge", style: {
          width: 1.2, "line-color": c.edge, "target-arrow-color": c.edge, "target-arrow-shape": large ? "none" : "triangle", "arrow-scale": 0.8,
          "curve-style": large ? "haystack" : "bezier", "haystack-radius": 0, opacity: large ? 0.35 : 0.55,
        } },
        { selector: "edge.edge-both", style: { width: 2 } },
        { selector: "edge.edge-observed", style: { "line-style": "dashed" } },
        { selector: "edge.edge-parsed", style: { "line-style": "dotted" } },
        { selector: "edge.is-lit, edge.is-hover", style: { "line-color": c.accent, "target-arrow-color": c.accent, opacity: 1, width: 2.4, "z-index": 5 } },
        { selector: "edge.is-dim", style: { opacity: 0.08 } },
      ];
    }

    /* ----- the visible graph: focus filter, then dataset collapse ----- */
    function focusSet() {
      if (!state.focus.on || !state.selected || !nodesById.has(state.selected)) return null;
      const set = new Set([state.selected]);
      const hops = state.focus.hops;
      if (state.focus.direction !== "down") for (const id of reach(state.selected, fullUp, hops).keys()) set.add(id);
      if (state.focus.direction !== "up") for (const id of reach(state.selected, fullDown, hops).keys()) set.add(id);
      return set;
    }

    function computeVisible() {
      const focus = focusSet();
      visible = new Map();
      owner = new Map();
      for (const node of data.nodes) {
        if (focus && !focus.has(node.id)) continue;
        // The selected asset stays individually visible even inside a collapsed dataset.
        const grouped = state.collapsed.has(node.dataset) && node.id !== state.selected;
        const id = grouped ? GROUP_PREFIX + node.dataset : node.id;
        if (!visible.has(id)) visible.set(id, { members: [] });
        visible.get(id).members.push(node.id);
        owner.set(node.id, id);
      }
      const edges = new Map();
      for (const edge of data.edges) {
        const a = owner.get(edge.from);
        const b = owner.get(edge.to);
        if (!a || !b || a === b) continue;
        const key = `${a}\u0000${b}`;
        const existing = edges.get(key);
        if (!existing) edges.set(key, { from: a, to: b, source: edge.source, count: 1 });
        else { existing.count++; if (edge.source === "both") existing.source = "both"; }
      }
      return [...edges.values()];
    }

    function rebuild(fitView) {
      if (!cy) return;
      const edges = computeVisible();
      const ids = [...visible.keys()];
      const large = edges.length > 1500;
      const positions = layeredLayout(ids, edges.map((edge) => [edge.from, edge.to]));
      const elements = [];
      for (const [id, info] of visible) {
        const isGroup = id.startsWith(GROUP_PREFIX);
        const node = nodesById.get(id);
        const classes = isGroup ? "is-group" : `kind-${node.kind}${gaps.has(id) ? " is-gap" : ""}${node.schedules ? " is-scheduled" : ""}${node.owned === false ? " not-owned" : ""}`;
        elements.push({ group: "nodes", data: { id, label: isGroup ? `${id.slice(GROUP_PREFIX.length)} · ${info.members.length}` : node.name, group: isGroup }, position: positions.get(id), classes });
      }
      edges.forEach((edge, i) => elements.push({ group: "edges", data: { id: `e${i}`, source: edge.from, target: edge.to }, classes: `edge-${edge.source}` }));
      cy.batch(() => { cy.elements().remove(); cy.style(stylesheet(large)); cy.add(elements); });
      cy.userZoomingEnabled(true);
      const tail = `${visible.size.toLocaleString()} shown`;
      const hidden = data.nodes.length - [...visible.values()].reduce((sum, item) => sum + item.members.length, 0);
      status.textContent = `${tail}${hidden ? `, ${hidden.toLocaleString()} hidden by focus` : ""} · ${edges.length.toLocaleString()} links`;
      refreshDatasetPick();
      applyHighlight();
      if (fitView) { fit(); } else drawMinimap();
    }

    /* ----- highlight ----- */
    let hoverId = null;
    function applyHighlight() {
      if (!cy) return;
      const litVisible = new Set();
      for (const id of state.lit) if (owner.has(id)) litVisible.add(owner.get(id));
      const selectedVisible = owner.get(state.selected);
      const hover = hoverId && cy.getElementById(hoverId);
      const hoverSet = hover && hover.nonempty() ? hover.closedNeighborhood() : null;
      cy.batch(() => {
        cy.elements().removeClass("is-lit is-dim is-selected is-hover");
        const hasLit = litVisible.size > 1 || hoverSet;
        cy.nodes().forEach((node) => {
          const id = node.id();
          if (id === selectedVisible) node.addClass("is-selected");
          else if (litVisible.has(id)) node.addClass("is-lit");
          if (hoverSet) { if (!hoverSet.contains(node)) node.addClass("is-dim"); else if (id === hoverId) node.addClass("is-hover"); }
          else if (hasLit && !litVisible.has(id)) node.addClass("is-dim");
        });
        cy.edges().forEach((edge) => {
          const a = edge.source().id();
          const b = edge.target().id();
          if (hoverSet) { if (hoverSet.contains(edge)) edge.addClass("is-hover"); else edge.addClass("is-dim"); }
          else if (litVisible.size > 1 && litVisible.has(a) && litVisible.has(b)) edge.addClass("is-lit");
          else if (hasLit) edge.addClass("is-dim");
        });
      });
      drawMinimap();
    }

    /* ----- viewport ----- */
    function fit(padding = 56) {  // clears the zoom buttons in the top-left corner
      if (!cy || !cy.nodes().length) return;
      cy.fit(undefined, padding);
      if (cy.zoom() > 1.2) { cy.zoom(1.2); cy.center(); }
      // A wide, shallow graph would sit in the middle of a tall, mostly empty stage; start it at the top.
      const box = cy.elements().boundingBox();
      if (box.h * cy.zoom() + 2 * padding < stage.clientHeight) cy.pan({ x: cy.pan().x, y: padding - box.y1 * cy.zoom() });
      drawMinimap();
    }
    function zoomBy(factor) {
      if (!cy) return;
      cy.zoom({ level: cy.zoom() * factor, renderedPosition: { x: stage.clientWidth / 2, y: stage.clientHeight / 2 } });
    }
    function centerOn(id, zoom) {
      if (!cy) return;
      const node = cy.getElementById(owner.get(id) || id);
      if (node.empty()) return;
      cy.animate({ center: { eles: node }, zoom: Math.max(zoom || 0, cy.zoom()) }, { duration: 220 });
    }

    /* ----- minimap ----- */
    let miniFrame = 0;
    const miniMap = { scale: 1, ox: 0, oy: 0 };
    function drawMinimap() {
      if (miniFrame) return;
      miniFrame = requestAnimationFrame(() => {
        miniFrame = 0;
        if (!cy) return;
        const ctx = minimap.getContext("2d");
        const w = minimap.width;
        const h = minimap.height;
        const c = palette();
        ctx.clearRect(0, 0, w, h);
        const box = cy.elements().boundingBox();
        if (!isFinite(box.w) || box.w === 0) return;
        const pad = 8;
        const scale = Math.min((w - pad * 2) / Math.max(box.w, 1), (h - pad * 2) / Math.max(box.h, 1));
        miniMap.scale = scale;
        miniMap.ox = pad + ((w - pad * 2) - box.w * scale) / 2 - box.x1 * scale;
        miniMap.oy = pad + ((h - pad * 2) - box.h * scale) / 2 - box.y1 * scale;
        const dot = Math.max(1.5, NODE_H * scale * 0.7);
        cy.nodes().forEach((node) => {
          const p = node.position();
          ctx.fillStyle = node.hasClass("is-selected") ? c.accent : node.hasClass("is-lit") ? c.accent : node.hasClass("is-dim") ? c.border : node.hasClass("is-group") ? c.accentBorder : c.muted;
          ctx.globalAlpha = node.hasClass("is-dim") ? 0.4 : 1;
          const width = Math.max(2, NODE_W * scale * 0.9);
          ctx.fillRect(p.x * scale + miniMap.ox - width / 2, p.y * scale + miniMap.oy - dot / 2, width, dot);
        });
        ctx.globalAlpha = 1;
        const ext = cy.extent();
        ctx.strokeStyle = c.accent;
        ctx.fillStyle = c.accent;
        ctx.lineWidth = 1.5;
        const x = ext.x1 * scale + miniMap.ox;
        const y = ext.y1 * scale + miniMap.oy;
        const rw = ext.w * scale;
        const rh = ext.h * scale;
        ctx.globalAlpha = 0.12;
        ctx.fillRect(x, y, rw, rh);
        ctx.globalAlpha = 1;
        ctx.strokeRect(x, y, rw, rh);
      });
    }
    function minimapPan(event) {
      const rect = minimap.getBoundingClientRect();
      const px = ((event.clientX - rect.left) / rect.width) * minimap.width;
      const py = ((event.clientY - rect.top) / rect.height) * minimap.height;
      const x = (px - miniMap.ox) / miniMap.scale;
      const y = (py - miniMap.oy) / miniMap.scale;
      const z = cy.zoom();
      cy.pan({ x: stage.clientWidth / 2 - x * z, y: stage.clientHeight / 2 - y * z });
    }
    let dragging = false;
    minimap.addEventListener("pointerdown", (event) => { dragging = true; minimap.setPointerCapture(event.pointerId); minimapPan(event); });
    minimap.addEventListener("pointermove", (event) => { if (dragging) minimapPan(event); });
    minimap.addEventListener("pointerup", () => { dragging = false; });

    /* ----- actions ----- */
    function toggleDataset(name) {
      if (state.collapsed.has(name)) state.collapsed.delete(name); else state.collapsed.add(name);
      rebuild(true);
    }
    function setFocus(patch) {
      Object.assign(state.focus, patch);
      focusToggle.setAttribute("aria-pressed", String(state.focus.on));
      focusToggle.classList.toggle("is-on", state.focus.on);
      rebuild(true);
    }

    function showTooltip(node) {
      const id = node.id();
      const info = visible.get(id);
      let text;
      if (id.startsWith(GROUP_PREFIX)) text = `${id.slice(GROUP_PREFIX.length)}: ${info.members.length} assets. Click to expand.`;
      else {
        const real = nodesById.get(id);
        text = `${real.id}${gaps.has(id) ? " (not analyzed)" : ""}`;
      }
      tooltip.textContent = text;
      tooltip.hidden = false;
      const p = node.renderedPosition();
      tooltip.style.left = `${Math.min(Math.max(p.x, 10), stage.clientWidth - 10)}px`;
      tooltip.style.top = `${Math.max(p.y - NODE_H * cy.zoom() / 2 - 8, 6)}px`;
    }

    /* ----- start ----- */
    function start() {
      // Cytoscape injects a <style> tag for its container; the page's CSP forbids inline styles and
      // .lv-stage already sets position: relative, so mark the tag as present to skip the injection.
      if (!document.getElementById("__________cytoscape_stylesheet")) {
        document.head.append(el("meta", { id: "__________cytoscape_stylesheet" }));
      }
      cy = window.cytoscape({
        container: stage, elements: [], style: stylesheet(false), minZoom: 0.02, maxZoom: 3, wheelSensitivity: 2.5,
        textureOnViewport: true, hideEdgesOnViewport: data.edges.length > 1500, motionBlur: false, pixelRatio: "auto", boxSelectionEnabled: false,
      });
      cy.on("tap", "node", (event) => {
        const id = event.target.id();
        if (id.startsWith(GROUP_PREFIX)) { toggleDataset(id.slice(GROUP_PREFIX.length)); return; }
        onSelect?.(id);
      });
      cy.on("mouseover", "node", (event) => {
        hoverId = event.target.id();
        applyHighlight();
        clearTimeout(tooltipTimer);
        tooltipTimer = setTimeout(() => showTooltip(event.target), 250);
      });
      cy.on("mouseout", "node", () => { hoverId = null; clearTimeout(tooltipTimer); tooltip.hidden = true; applyHighlight(); });
      cy.on("viewport", () => { tooltip.hidden = true; drawMinimap(); });
      new ResizeObserver(() => { if (cy) { cy.resize(); drawMinimap(); } }).observe(stage);
      const recolor = () => { if (cy) { cy.style(stylesheet(cy.edges().length > 1500)); drawMinimap(); } };
      matchMedia("(prefers-color-scheme: dark)").addEventListener?.("change", recolor);
      new MutationObserver(recolor).observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
      rebuild(true);
    }

    if (window.cytoscape) start();
    else status.textContent = "The graph library could not be loaded.";

    return {
      /** Called by the page whenever its selection or highlighted set changes. */
      update({ selected, lit }) {
        const changed = selected !== state.selected;
        state.selected = selected;
        state.lit = lit || new Set();
        if (!cy) return;
        // Focus and the "selected stays visible" rule depend on the selection; everything else only recolors.
        if (changed && (state.focus.on || (selected && state.collapsed.has(nodesById.get(selected)?.dataset)))) { rebuild(true); }
        else applyHighlight();
        if (!placed && selected && visible.size > 300) {
          // A graph this size is unreadable when fitted; start at the selection and let "Fit" give the overview.
          placed = true;
          const node = cy.getElementById(owner.get(selected));
          if (node.nonempty()) { cy.zoom({ level: 0.9, position: node.position() }); cy.center(node); }
        } else if (changed && selected && !state.focus.on && placed) centerOn(selected);
        // The first selection of a small graph keeps the fitted view: centering on it pushed half the graph out of sight.
        placed = true;
      },
      fit,
      resize() { if (cy) { cy.resize(); drawMinimap(); } },
      destroy() { if (cy) { cy.destroy(); cy = null; } },
      /** For tests and tooling. */
      stats() { return { shown: visible.size, edges: cy ? cy.edges().length : 0, zoom: cy ? cy.zoom() : 0 }; },
    };
  }

  window.KumoLineage = { mount };
})();
