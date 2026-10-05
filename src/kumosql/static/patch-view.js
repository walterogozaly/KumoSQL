"use strict";

/* The patch view shared by Shared models and Reduce: verdict tags, the checks table, the assumptions and
   diagnostics lists, and the diff with Copy and Download. Styled by shared-models.css (the sm-* classes). */

window.KumoPatch = (() => {
  const LABELS = {
    proven: "Proven",
    proven_with_assumptions: "Proven under assumptions",
    unchanged: "Unchanged",
    unknown: "Unknown",
    differs: "Differs",
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

  const tag = (label) => h("span", { class: "sm-tag", "data-label": label, text: LABELS[label] || label });
  const short = (key) => key.split(".").pop();

  function diffView(text) {
    const pre = h("pre", { class: "sm-diff" });
    for (const line of text.split("\n")) {
      const kind = line.startsWith("+++") || line.startsWith("---") || line.startsWith("diff ") || line.startsWith("new file") || line.startsWith("deleted file") || line.startsWith("@@")
        ? "meta" : line.startsWith("+") ? "add" : line.startsWith("-") ? "del" : "";
      pre.append(h("span", { class: kind, text: line || " " }));
    }
    return pre;
  }

  function download(diff, filename) {
    const link = h("a", {
      href: URL.createObjectURL(new Blob([diff], { type: "text/x-diff" })),
      download: filename,
    });
    document.body.append(link);
    link.click();
    link.remove();
  }

  /* One row per check: model, what the check covers (roleText maps a role to its label), result, prover reason. */
  function checksTable(checks, roleText) {
    if (!checks.length) return null;
    const rows = checks.map((check) => h("tr", {},
      h("td", { title: check.model, text: short(check.model) }),
      h("td", { text: roleText[check.role] || check.role }),
      h("td", {}, tag(check.label)),
      h("td", { class: "sm-reason", text: check.reason })));
    return h("table", { class: "sm-checks" },
      h("thead", {}, h("tr", {}, ...["Model", "Change", "Result", "Prover"].map((text) => h("th", { text })))),
      h("tbody", {}, ...rows));
  }

  const list = (items) => (items.length ? h("ul", { class: "sm-sites" }, ...items.map((text) => h("li", { text }))) : null);

  const assumptions = (items) => (items.length ? h("details", {},
    h("summary", { text: `${items.length} assumptions` }),
    h("ul", { class: "sm-assumptions" }, ...items.map((text) => h("li", { text })))) : null);

  /* The diff card: a file count, Copy, Download and the coloured diff. `say` reports the copy result. */
  function diffCard(diff, files, filename, say) {
    const copy = h("button", { type: "button", class: "secondary-button", text: "Copy" });
    copy.addEventListener("click", () => navigator.clipboard.writeText(diff).then(() => say("Copied")).catch((error) => say(error.message, true)));
    return h("div", { class: "sm-card" },
      h("div", { class: "sm-verdict" },
        h("h2", { text: `${files} files` }),
        copy,
        h("button", { type: "button", class: "secondary-button", text: "Download", onclick: () => download(diff, filename) })),
      diffView(diff));
  }

  return { LABELS, h, call, tag, short, diffView, download, checksTable, list, assumptions, diffCard };
})();
