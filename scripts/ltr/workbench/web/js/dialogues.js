// 对白: editions grouped by book, a filterable list of records from one source, and the selected record.
import { latest, request } from "./api.js";
import { renderDetail } from "./detail.js";
import { ATTRIBUTION, MODEL_REVIEW, MODEL_REVIEW_TONE, SOURCES, SOURCE_CAPABILITY, STATUS_FILTERS, label } from "./labels.js";
import { badge, debounce, empty, h, loading, notice, num, pager, replace, segmented, select } from "./ui.js";

const LIMIT = 30;
const editionsCache = new Map(); // corpus id -> Promise<items>
const listRequest = latest();
const IDENTITY = { suggested: ["有身份建议", "ok"], unresolved: ["仍未知", "warn"], conflict: ["建议冲突", "error"],
  not_dialogue: ["建议非对话", "neutral"] };

function editions(corpusId) {
  if (!editionsCache.has(corpusId)) {
    const pending = request("/api/editions", { params: { corpus: corpusId } }).then((r) => r.items || []);
    pending.catch(() => editionsCache.delete(corpusId));
    editionsCache.set(corpusId, pending);
  }
  return editionsCache.get(corpusId);
}

export function sourceAvailable(corpus, source) {
  return source === "formal" || Boolean(corpus.capabilities[SOURCE_CAPABILITY[source]]);
}

export async function render(container, app, current) {
  const corpus = app.corpus();
  const p = app.route.params;
  const source = SOURCES[p.source] && sourceAvailable(corpus, p.source) ? p.source : "formal";
  const state = { source, edition: p.edition || "", q: p.q || "", speaker: p.speaker || "", status: p.status || "",
    pending: p.pending_alignment === "1", reuse: p.target_reuse === "1", offset: Number(p.offset) || 0, id: p.id || "" };

  const sidebar = h("aside", { class: "editions-pane", "aria-label": "版本" });
  const listPane = h("section", { class: "list-pane", "aria-label": "记录列表" });
  const detailPane = h("aside", { class: "detail-pane", "aria-label": "记录详情", hidden: !state.id });
  container.append(h("div", { class: "browse" }, sidebar, listPane, detailPane));

  const refreshList = () => loadList(listPane, app, corpus, state, openDetail);
  const openDetail = (id) => {
    state.id = id;
    app.remember({ id });
    for (const row of listPane.querySelectorAll(".record")) row.classList.toggle("selected", row.dataset.id === id);
    showDetail();
  };
  const showDetail = () => {
    detailPane.hidden = !state.id;
    if (state.id) {
      renderDetail(detailPane, app, corpus, state.source, state.id, () => {
        state.id = "";
        app.remember({ id: "" });
        detailPane.hidden = true;
        for (const row of listPane.querySelectorAll(".record.selected")) row.classList.remove("selected");
      });
    }
  };

  renderSidebar(sidebar, app, corpus, state, () => {
    state.offset = 0;
    app.remember({ edition: state.edition, offset: "" });
    refreshList();
  });
  listPane.append(filters(app, corpus, state, refreshList), h("div", { class: "records" }));
  refreshList();
  showDetail();
}

function filters(app, corpus, state, refresh) {
  const sourceOptions = Object.entries(SOURCES).map(([key, text]) => [key, text, !sourceAvailable(corpus, key)]);
  const apply = () => {
    state.offset = 0;
    app.remember({ q: state.q, speaker: state.speaker, status: state.status, offset: "",
      pending_alignment: state.pending ? "1" : "", target_reuse: state.reuse ? "1" : "" });
    refresh();
  };
  const typed = debounce(apply, 350);
  const extras = [];
  if (state.source === "formal" && corpus.capabilities.completion_alignment_review) {
    extras.push(h("label", { class: "check", title: "正式库中仍待人工复核的旧对齐记录" },
      h("input", { type: "checkbox", checked: state.pending, onchange: (e) => { state.pending = e.target.checked; apply(); } }),
      "仅看待复核对齐"));
  }
  if (state.source === "alignments" && (corpus.pending_alignments || {}).target_reuse_filter_available) {
    extras.push(h("label", { class: "check", title: "多个中文对白共享完全相同的日文目标；不代表错误" },
      h("input", { type: "checkbox", checked: state.reuse, onchange: (e) => { state.reuse = e.target.checked; apply(); } }),
      "只看共享日文目标"));
  }
  return h("div", { class: "filters" },
    segmented(sourceOptions, state.source, (key) => app.patch({ source: key, status: "", offset: "", id: "",
      pending_alignment: "", target_reuse: "" }), "记录来源"),
    h("div", { class: "filter-row" },
      h("input", { type: "search", class: "grow", placeholder: "搜索正文…", value: state.q, maxlength: 120,
        "aria-label": "搜索正文", oninput: (e) => { state.q = e.target.value.trim(); typed(); } }),
      h("input", { type: "search", placeholder: "说话人", value: state.speaker, maxlength: 80, "aria-label": "说话人",
        oninput: (e) => { state.speaker = e.target.value.trim(); typed(); } }),
      select(Object.entries(STATUS_FILTERS[state.source]), state.status,
        (value) => { state.status = value; apply(); }, "状态"),
      extras));
}

async function loadList(pane, app, corpus, state, openDetail) {
  const target = pane.querySelector(".records");
  replace(target, loading());
  let page;
  try {
    page = await listRequest(request("/api/dialogues", { params: {
      corpus: corpus.id, source: state.source, edition: state.edition, q: state.q, speaker: state.speaker,
      status: state.status, offset: state.offset, limit: LIMIT,
      pending_alignment: state.source === "formal" && state.pending, target_reuse: state.source === "alignments" && state.reuse,
    } }));
  } catch (error) {
    if (error.kind !== "stale") replace(target, notice(error.message, "error"));
    return;
  }
  const items = page.items || [];
  const onPage = (offset) => {
    state.offset = offset;
    app.remember({ offset: offset || "" });
    loadList(pane, app, corpus, state, openDetail);
    pane.scrollTop = 0;
  };
  replace(target,
    h("div", { class: "list-meta muted" }, `${SOURCES[state.source]}${state.edition ? " · 已选版本" : " · 全部版本"}` +
      ` · 第 ${num(state.offset + 1)}–${num(state.offset + items.length)} 条`),
    items.length ? h("ol", { class: "record-list" }, items.map((item) => record(item, state, openDetail)))
      : empty("没有符合条件的记录。"),
    pager({ offset: state.offset, limit: LIMIT, hasMore: page.has_more }, onPage));
}

function record(item, state, openDetail) {
  return h("li", {}, h("button", {
    type: "button", class: `record${item.id === state.id ? " selected" : ""}`, dataset: { id: item.id },
    onclick: () => openDetail(item.id),
  },
  h("div", { class: "record-head" },
    h("span", { class: "record-where" }, item.edition_title || item.edition_id, ` · 第 ${num(item.ordinal)} 段`),
    item.language ? badge(item.language, "neutral") : null),
  h("p", { class: "record-text" }, item.text || "（无正文）"),
  h("div", { class: "record-tags" },
    h("span", { class: "muted" }, item.speaker ? `说话人：${item.speaker}` : "说话人未知"),
    statusBadges(item))));
}

function statusBadges(item) {
  const tags = [];
  if (item.model_review_status) {
    tags.push(badge(label(MODEL_REVIEW, item.model_review_status), MODEL_REVIEW_TONE[item.model_review_status]));
  }
  if (item.identity_status && IDENTITY[item.identity_status]) tags.push(badge(...IDENTITY[item.identity_status]));
  if (item.source === "formal" && item.attribution_status) tags.push(badge(label(ATTRIBUTION, item.attribution_status), "neutral"));
  if (item.has_alignment) tags.push(badge("有对齐", "accent"));
  if (item.reference_eligible) tags.push(badge("可作参考", "ok"));
  if (item.text_truncated) tags.push(badge("正文已截断", "warn"));
  return tags;
}

// -- editions sidebar ---------------------------------------------------------

const COUNT_KEY = { formal: "dialogues", candidates: "candidate_count", alignments: "alignment_candidate_count",
  identities: "identity_candidate_count" };

async function renderSidebar(sidebar, app, corpus, state, onSelect) {
  replace(sidebar, h("h2", { class: "pane-title" }, "版本"), loading());
  let items;
  try {
    items = await editions(corpus.id);
  } catch (error) {
    replace(sidebar, h("h2", { class: "pane-title" }, "版本"), notice(error.message, "error"));
    return;
  }
  const filter = { text: "", language: "" };
  const languages = [...new Set(items.map((e) => e.language).filter(Boolean))].sort();
  const list = h("div", { class: "edition-groups" });
  const draw = () => replace(list, groups(items, filter, state, (id) => {
    state.edition = id;
    for (const node of list.querySelectorAll("[data-edition]")) node.classList.toggle("active", node.dataset.edition === id);
    onSelect();
  }));
  replace(sidebar,
    h("div", { class: "pane-head" }, h("h2", { class: "pane-title" }, `版本 ${num(items.length)}`),
      h("button", { type: "button", class: `button ghost small${state.edition ? "" : " active"}`, dataset: { edition: "" },
        onclick: () => { state.edition = ""; draw(); onSelect(); } }, "全部")),
    h("div", { class: "filter-row" },
      h("input", { type: "search", class: "grow", placeholder: "书名…", "aria-label": "筛选版本",
        oninput: debounce((e) => { filter.text = e.target.value.trim().toLowerCase(); draw(); }, 150) }),
      select([["", "全部语言"], ...languages.map((l) => [l, l])], "", (v) => { filter.language = v; draw(); }, "语言")),
    list);
  draw();

  function groups(all, f, st, choose) {
    const byBook = new Map();
    for (const e of all) {
      if (f.language && e.language !== f.language) continue;
      if (f.text && !`${e.title} ${e.book_key}`.toLowerCase().includes(f.text)) continue;
      const key = e.book_key || e.title || e.id;
      if (!byBook.has(key)) byBook.set(key, []);
      byBook.get(key).push(e);
    }
    if (!byBook.size) return empty("没有匹配的版本。");
    const countKey = COUNT_KEY[st.source];
    return [...byBook.entries()].map(([key, variants]) => {
      const total = variants.reduce((acc, v) => acc + (v[countKey] || 0), 0);
      const open = variants.some((v) => v.id === st.edition);
      const rows = variants.map((v, i) => h("button", {
        type: "button", class: `edition${v.id === st.edition ? " active" : ""}`, dataset: { edition: v.id },
        title: v.id, onclick: () => choose(v.id),
      }, h("span", {}, variants.length > 1 ? `${v.language || "?"} #${i + 1}` : v.title),
      h("span", { class: "muted num" }, v[countKey] == null ? "—" : num(v[countKey]))));
      if (variants.length === 1) return h("div", { class: "edition-single" }, rows[0], h("small", { class: "muted" }, variants[0].language));
      return h("details", { class: "edition-group", open },
        h("summary", {}, h("span", {}, variants[0].title || key), h("span", { class: "muted num" }, `${variants.length} 版 · ${num(total)}`)),
        rows);
    });
  }
}
