// DOM helpers and small reusable components. Data is always inserted as text, never as HTML.

export function h(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === undefined || value === null || value === false) continue;
    if (key === "class") node.className = value;
    else if (key === "dataset") Object.assign(node.dataset, value);
    else if (key === "style") Object.assign(node.style, value); // CSSOM: allowed under the page CSP
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else if (key === "text") node.textContent = value;
    else node.setAttribute(key, value === true ? "" : value);
  }
  append(node, children);
  return node;
}

function append(node, children) {
  for (const child of children.flat(Infinity)) {
    if (child === undefined || child === null || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
}

export function replace(node, ...children) {
  node.replaceChildren();
  append(node, children);
  return node;
}

const NUMBER = new Intl.NumberFormat("zh-CN");
export const num = (value) => (typeof value === "number" && Number.isFinite(value) ? NUMBER.format(value) : "—");
export const pct = (part, whole) => (whole > 0 ? `${Math.round((part / whole) * 1000) / 10}%` : "—");

export function time(value) {
  const date = typeof value === "string" ? new Date(value) : null;
  return date && !Number.isNaN(date.getTime())
    ? date.toLocaleString("zh-CN", { dateStyle: "medium", timeStyle: "short" })
    : "—";
}

export const shortHash = (value) => (typeof value === "string" && value.length >= 12 ? value.slice(0, 12) : "—");

export function badge(text, tone = "neutral", title) {
  return h("span", { class: `badge tone-${tone}`, title }, text);
}

export function stat(label, value, hint) {
  return h("div", { class: "stat" }, h("div", { class: "stat-value" }, value), h("div", { class: "stat-label" }, label),
    hint ? h("div", { class: "stat-hint" }, hint) : null);
}

// Stacked horizontal bar. parts: [{label, value, tone}]
export function meter(parts, total, { caption } = {}) {
  const sum = total ?? parts.reduce((acc, part) => acc + (part.value || 0), 0);
  const bar = h("div", { class: "meter", role: "img",
    "aria-label": parts.map((p) => `${p.label} ${num(p.value)}`).join("，") });
  for (const part of parts) {
    if (!part.value || !sum) continue;
    bar.append(h("span", { class: `meter-part tone-${part.tone || "accent"}`, title: `${part.label} ${num(part.value)}`,
      style: { width: `${(100 * part.value) / sum}%` } }));
  }
  const legend = h("div", { class: "meter-legend" }, parts.map((p) =>
    h("span", { class: "legend-item" }, h("i", { class: `swatch tone-${p.tone || "accent"}` }), p.label, " ",
      h("b", {}, num(p.value)), sum ? h("small", {}, ` ${pct(p.value, sum)}`) : null)));
  return h("div", { class: "meter-block" }, caption ? h("div", { class: "meter-caption" }, caption) : null, bar, legend);
}

export function notice(text, tone = "info") {
  return h("p", { class: `notice tone-${tone}`, role: tone === "error" ? "alert" : "status" }, text);
}

export const loading = (text = "加载中…") => h("p", { class: "muted loading" }, text);
export const empty = (text) => h("p", { class: "muted empty" }, text);

export function section(title, body, { actions, id, open } = {}) {
  return h("section", { class: "card", id },
    h("header", { class: "card-header" }, h("h2", {}, title), actions || null), body);
}

export function details(summary, body, open = false) {
  return h("details", { class: "fold", open }, h("summary", {}, summary), body);
}

export function keyValues(rows) {
  return h("dl", { class: "kv" }, rows.filter(Boolean).map(([key, value]) => [h("dt", {}, key), h("dd", {}, value)]));
}

export function pager({ offset, limit, total, hasMore }, onPage) {
  const page = Math.floor(offset / limit) + 1;
  const pages = total != null ? Math.max(1, Math.ceil(total / limit)) : null;
  const more = total != null ? offset + limit < total : hasMore;
  return h("div", { class: "pager" },
    h("button", { class: "button ghost", type: "button", disabled: offset <= 0,
      onclick: () => onPage(Math.max(0, offset - limit)) }, "上一页"),
    h("span", { class: "muted" }, pages ? `第 ${page} / ${pages} 页` : `第 ${page} 页`),
    h("button", { class: "button ghost", type: "button", disabled: !more, onclick: () => onPage(offset + limit) }, "下一页"));
}

export function segmented(options, value, onChange, label) {
  return h("div", { class: "segmented", role: "radiogroup", "aria-label": label },
    options.map(([key, text, disabled]) => h("button", {
      type: "button", role: "radio", "aria-checked": String(key === value), disabled,
      class: key === value ? "active" : "", onclick: () => key !== value && onChange(key),
    }, text)));
}

export function select(options, value, onChange, label) {
  const node = h("select", { "aria-label": label, onchange: (event) => onChange(event.target.value) },
    options.map(([key, text]) => h("option", { value: key }, text)));
  node.value = value;
  return node;
}

export function debounce(fn, wait = 250) {
  let timer;
  return (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => fn(...args), wait);
  };
}
