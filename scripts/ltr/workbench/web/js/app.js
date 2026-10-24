// Shell: hash routes (#/view?key=value), corpus switcher, theme, and the shared overview.
import { request } from "./api.js";
import { h, loading, notice, replace } from "./ui.js";
import * as overview from "./overview.js";
import * as dialogues from "./dialogues.js";
import * as scans from "./scans.js";
import * as reviews from "./reviews.js";

import * as evidence from "./evidence.js";
import * as glossary from "./glossary.js";

const VIEWS = { overview, dialogues, scans, reviews, evidence, glossary };
const THEMES = ["auto", "light", "dark"];
const THEME_LABELS = { auto: "主题：跟随系统", light: "主题：浅色", dark: "主题：深色" };

const main = document.getElementById("main");
const corpusSelect = document.getElementById("corpus-select");
const themeToggle = document.getElementById("theme-toggle");

const app = {
  overview: null,
  route: { view: "overview", params: {} },

  corpus() {
    const corpora = this.overview ? this.overview.corpora : [];
    return corpora.find((c) => c.id === this.route.params.corpus) || corpora.find((c) => c.available) || corpora[0];
  },

  href(view, params = {}) {
    const corpus = params.corpus ?? this.route.params.corpus ?? (this.corpus() || {}).id;
    const query = new URLSearchParams(Object.entries({ ...params, corpus })
      .filter(([, v]) => v !== undefined && v !== null && v !== ""));
    return `#/${view}${query.toString() ? "?" + query : ""}`;
  },

  go(view, params, { replaceHistory = false } = {}) {
    const target = this.href(view, params);
    if (target === window.location.hash) return;
    if (replaceHistory) history.replaceState(null, "", target);
    else window.location.hash = target;
    if (replaceHistory) render();
  },

  // Update query parameters of the current view without adding a history entry.
  patch(params) {
    this.go(this.route.view, { ...this.route.params, ...params }, { replaceHistory: true });
  },

  // Record view state in the URL (for reload/deep links) without re-rendering.
  remember(params) {
    this.route.params = { ...this.route.params, ...params };
    history.replaceState(null, "", this.href(this.route.view, this.route.params));
  },
};

function parseRoute() {
  const [path, query = ""] = window.location.hash.replace(/^#\/?/, "").split("?");
  const view = VIEWS[path] ? path : "overview";
  return { view, params: Object.fromEntries(new URLSearchParams(query)) };
}

let renderTicket = 0;
async function render() {
  const ticket = ++renderTicket;
  app.route = parseRoute();
  for (const link of document.querySelectorAll(".tabs a")) {
    link.setAttribute("aria-current", link.dataset.view === app.route.view ? "page" : "false");
    link.href = app.href(link.dataset.view, {});
  }
  if (!app.overview) return;
  const corpus = app.corpus();
  if (corpus && app.route.params.corpus !== corpus.id) {
    app.route.params.corpus = corpus.id;
    history.replaceState(null, "", app.href(app.route.view, app.route.params));
  }
  corpusSelect.value = corpus ? corpus.id : "";
  const container = h("div", { class: `view view-${app.route.view}` });
  replace(main, container);
  try {
    await VIEWS[app.route.view].render(container, app, () => ticket === renderTicket);
  } catch (error) {
    if (ticket === renderTicket && error.kind !== "stale") replace(container, notice(error.message, "error"));
  }
}

async function loadOverview() {
  replace(main, loading("正在读取语料概览（首次加载需校验数据库，可能需要半分钟）…"));
  try {
    app.overview = await request("/api/overview", { timeout: 90000 });
  } catch (error) {
    replace(main, notice(error.message, "error"), h("button", { class: "button", onclick: loadOverview }, "重试"));
    return;
  }
  replace(corpusSelect, app.overview.corpora.map((c) =>
    h("option", { value: c.id, disabled: !c.available }, c.available ? c.name : `${c.name}（不可用）`)));
  render();
}

function applyTheme(theme) {
  if (theme === "auto") delete document.documentElement.dataset.theme;
  else document.documentElement.dataset.theme = theme;
  themeToggle.textContent = THEME_LABELS[theme];
  try { localStorage.setItem("corpus-theme", theme); } catch { /* storage unavailable */ }
}

let theme = "auto";
try { theme = THEMES.includes(localStorage.getItem("corpus-theme")) ? localStorage.getItem("corpus-theme") : "auto"; }
catch { /* storage unavailable */ }
applyTheme(theme);
themeToggle.addEventListener("click", () => {
  theme = THEMES[(THEMES.indexOf(theme) + 1) % THEMES.length];
  applyTheme(theme);
});
corpusSelect.addEventListener("change", () => app.go(app.route.view, { corpus: corpusSelect.value }));
window.addEventListener("hashchange", render);
loadOverview();
