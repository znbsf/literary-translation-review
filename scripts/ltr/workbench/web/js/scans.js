// 扫描页: OCR evidence page by page — region transcriptions, reading-order geometry, whole-page vision candidates.
import { latest, request, url } from "./api.js";
import { REGION_STATUS, SEGMENT_ROLES, label } from "./labels.js";
import { badge, empty, h, keyValues, loading, notice, num, pager, replace, segmented, select, shortHash } from "./ui.js";

const LIMIT = 20;
const SVG = "http://www.w3.org/2000/svg";
const listRequest = latest();
const viewRequest = latest();

const KINDS = {
  regions: { label: "区域转写", available: (c) => c.ocr_regions && c.ocr_regions.available },
  order: { label: "阅读顺序", available: (c) => c.ocr_reading_order_candidates && c.ocr_reading_order_candidates.available },
  vision: { label: "整页视觉", available: (c) => c.ocr_vision_candidates && c.ocr_vision_candidates.available },
};

export async function render(container, app) {
  const corpus = app.corpus();
  const p = app.route.params;
  const usable = Object.keys(KINDS).filter((k) => KINDS[k].available(corpus));
  if (!corpus.capabilities.scans) {
    container.append(notice("该语料库没有扫描页。", "info"));
    return;
  }
  const state = { kind: usable.includes(p.kind) ? p.kind : usable[0], item: p.item || "", page: p.page || "",
    offset: Number(p.offset) || 0, collection: p.collection || "" };
  const listPane = h("section", { class: "list-pane scans-list" });
  const viewer = h("section", { class: "viewer-pane" });
  container.append(h("div", { class: "scans" }, listPane, viewer));

  const choose = (item) => {
    state.item = item.key;
    state.page = "";
    app.remember({ item: item.key, page: "" });
    for (const node of listPane.querySelectorAll(".record")) node.classList.toggle("selected", node.dataset.key === item.key);
    showItem(viewer, corpus, state.kind, item);
  };
  listPane.append(
    usable.length ? segmented(usable.map((k) => [k, KINDS[k].label]), state.kind,
      (kind) => app.patch({ kind, item: "", offset: "", collection: "", page: "" }), "OCR 来源")
      : notice("已有扫描页，但未配置 OCR 候选。", "info"),
    h("div", { class: "records" }));
  if (state.kind) loadList(listPane, app, corpus, state, choose);
  if (state.page) showPage(viewer, corpus, state.page);
  else if (!state.item) replace(viewer, empty("从左侧选择一页。"));
}

// -- list ----------------------------------------------------------------------

async function loadList(pane, app, corpus, state, choose) {
  const target = pane.querySelector(".records");
  replace(target, loading());
  let rows, total, extra = null;
  try {
    if (state.kind === "regions") {
      const page = await listRequest(request("/api/ocr-regions", { params: { corpus: corpus.id, offset: state.offset, limit: LIMIT } }));
      total = page.total;
      rows = page.items.map((i) => ({ key: i.candidate_sha256, raw: i, title: i.page_id,
        note: `${num(i.region_count)} 区域`, tag: i.unreadable_count ? badge(`${num(i.unreadable_count)} 不可读`, "warn") : badge("全部转写", "ok"),
        sub: i.batch_id }));
    } else if (state.kind === "order") {
      const page = await listRequest(request("/api/ocr-reading-order-candidates", { params: {
        corpus: corpus.id, offset: state.offset, limit: LIMIT, collection_id: state.collection } }));
      total = page.total;
      rows = page.items.map((i) => ({ key: i.candidate_sha256, raw: i, title: i.page_id,
        note: `${num(i.segment_count)} 列段 · ${num(i.raw_word_count)} 词`,
        tag: i.geometry_candidate ? badge("已分列", "accent") : badge(i.error_code || i.status, "warn"), sub: i.book_key }));
      const collections = corpus.ocr_reading_order_candidates.collections || [];
      extra = select([["", "全部分册"], ...collections.map((c) => [c.collection_id, `${c.book_key}（${num(c.page_count)} 页）`])],
        state.collection, (value) => { app.patch({ collection: value, offset: "", item: "" }); }, "分册");
    } else {
      const page = await listRequest(request("/api/ocr-vision-candidates", { params: { corpus: corpus.id, offset: state.offset, limit: LIMIT } }));
      total = page.total;
      rows = (page.items || []).map((i) => ({ key: i.candidate_sha256, raw: i, title: i.metadata.page_id,
        note: `${num(i.metadata.segment_count)} 段 · ${num(i.metadata.text_characters)} 字`,
        tag: i.metadata.uncertain_region_count ? badge(`${num(i.metadata.uncertain_region_count)} 处存疑`, "warn") : null, sub: i.id }));
    }
  } catch (error) {
    if (error.kind !== "stale") replace(target, notice(error.message, "error"));
    return;
  }
  const onPage = (offset) => {
    state.offset = offset;
    app.remember({ offset: offset || "" });
    loadList(pane, app, corpus, state, choose);
  };
  replace(target, extra ? h("div", { class: "filter-row" }, extra) : null,
    h("div", { class: "list-meta muted" }, `共 ${num(total)} 页`),
    rows.length ? h("ol", { class: "record-list compact" }, rows.map((row) => h("li", {}, h("button", {
      type: "button", class: `record${row.key === state.item ? " selected" : ""}`, dataset: { key: row.key }, onclick: () => choose(row),
    }, h("div", { class: "record-head" }, h("span", { class: "mono" }, row.title), row.tag),
    h("div", { class: "muted" }, row.note, row.sub ? ` · ${row.sub}` : ""))))) : empty("没有候选。"),
    pager({ offset: state.offset, limit: LIMIT, total }, onPage));
  const selected = rows.find((r) => r.key === state.item);
  if (selected && !state.page) showItem(pane.nextElementSibling, corpus, state.kind, selected);
}

// -- viewer --------------------------------------------------------------------

function figure(src, width, height, shapes, { normalized = false } = {}) {
  const svg = document.createElementNS(SVG, "svg");
  svg.setAttribute("viewBox", normalized ? "0 0 1000 1000" : `0 0 ${width} ${height}`);
  svg.setAttribute("preserveAspectRatio", "none");
  svg.setAttribute("class", "overlay");
  for (const shape of shapes) {
    const [x0, y0, x1, y1] = shape.box;
    const rect = document.createElementNS(SVG, "rect");
    for (const [k, v] of Object.entries({ x: x0, y: y0, width: x1 - x0, height: y1 - y0 })) rect.setAttribute(k, v);
    rect.setAttribute("class", `shape tone-${shape.tone || "accent"}`);
    rect.dataset.key = shape.key;
    const title = document.createElementNS(SVG, "title");
    title.textContent = shape.title;
    rect.append(title);
    svg.append(rect);
  }
  const image = h("img", { src, alt: "扫描页", loading: "lazy", style: { aspectRatio: `${width} / ${height}` } });
  const frame = h("div", { class: "page-frame" }, image, svg);
  image.addEventListener("error", () => frame.replaceWith(notice("图像无法加载或哈希不符。", "error")));
  return frame;
}

function linkHover(frame, list) {
  const toggle = (key, on) => {
    for (const node of [...frame.querySelectorAll(`[data-key="${CSS.escape(key)}"]`), ...list.querySelectorAll(`[data-key="${CSS.escape(key)}"]`)]) {
      node.classList.toggle("hover", on);
    }
  };
  for (const node of [...frame.querySelectorAll("[data-key]"), ...list.querySelectorAll("[data-key]")]) {
    node.addEventListener("mouseenter", () => toggle(node.dataset.key, true));
    node.addEventListener("mouseleave", () => toggle(node.dataset.key, false));
  }
}

function viewerLayout(viewer, title, meta, frame, side) {
  replace(viewer, h("div", { class: "pane-head" }, h("div", {}, h("h2", { class: "pane-title mono" }, title), meta)),
    h("div", { class: "viewer" }, frame, h("div", { class: "viewer-side" }, side)));
}

async function showItem(viewer, corpus, kind, item) {
  replace(viewer, loading());
  try {
    if (kind === "regions") await showRegions(viewer, corpus, item.raw);
    else if (kind === "order") await showOrder(viewer, corpus, item.raw);
    else await showVision(viewer, corpus, item.raw);
  } catch (error) {
    if (error.kind !== "stale") replace(viewer, notice(error.message, "error"));
  }
}

async function showRegions(viewer, corpus, item) {
  const data = await viewRequest(request("/api/ocr-region", { params: { corpus: corpus.id, candidate_sha256: item.candidate_sha256 } }));
  const c = data.candidate;
  const tone = (r) => (r.status === "unreadable" ? "warn" : "accent");
  const frame = figure(url("/api/scan", { corpus: corpus.id, page: c.page_id, image_sha256: c.image_sha256 }), c.width, c.height,
    c.regions.map((r, i) => ({ key: r.region_id, box: r.box, tone: tone(r), title: `${i + 1}. ${label(REGION_STATUS, r.status)}` })));
  const list = h("ol", { class: "regions", lang: "ja" }, c.regions.map((r) => h("li", { dataset: { key: r.region_id }, class: `tone-${tone(r)}` },
    r.status === "transcribed" ? r.text : badge("不可读，待复核", "warn"))));
  viewerLayout(viewer, c.page_id, h("div", { class: "chips" },
    badge(`${num(c.transcribed_count)} 已转写`, "accent"), c.unreadable_count ? badge(`${num(c.unreadable_count)} 不可读`, "warn") : null,
    badge(data.image_bound ? "图像已验" : "图像不符", data.image_bound ? "ok" : "error"),
    badge(data.source_bound ? "源库一致" : "源库已变化", data.source_bound ? "ok" : "warn"),
    h("small", { class: "muted" }, `${c.model} · ${data.batch_id}`)), frame,
  [list, h("p", { class: "muted" }, "区域顺序来自原 OCR 几何；“全部转写”不代表整页或文字已核验。"),
    keyValues([["候选", shortHash(data.candidate_sha256)], ["几何", shortHash(c.geometry_candidate_sha256)],
      ["来源格式", (c.provenance || {}).origin_schema || "—"]])]);
  linkHover(viewer.querySelector(".page-frame"), list);
}

async function showOrder(viewer, corpus, item) {
  const params = { corpus: corpus.id, id: item.id, candidate_sha256: item.candidate_sha256 };
  const data = await viewRequest(request("/api/ocr-reading-order-candidate", { params }));
  const c = data.candidate;
  const segments = c.segments || [];
  const frame = figure(url("/api/ocr-reading-order-image", { ...params, image_sha256: c.image_sha256 }), data.width, data.height,
    segments.map((s, i) => ({ key: `s${i}`, box: s.box, tone: s.role === "ruby" ? "warn" : s.role === "main" ? "accent" : "neutral",
      title: `${i + 1}. ${label(SEGMENT_ROLES, s.role)} · 第 ${s.column} 列` })));
  const list = h("ol", { class: "regions" }, segments.map((s, i) => h("li", { dataset: { key: `s${i}` } },
    badge(label(SEGMENT_ROLES, s.role), s.role === "main" ? "accent" : "neutral"), ` 第 ${s.column} 列 · ${s.word_refs.length} 词`)));
  const unitsSlot = h("div", {}, h("button", { type: "button", class: "button ghost small", onclick: async (e) => {
    e.target.disabled = true;
    try {
      const units = await request("/api/ocr-reading-units", { params });
      replace(unitsSlot, h("ol", { class: "regions", lang: "ja" }, units.units.map((u) =>
        h("li", {}, badge(label(SEGMENT_ROLES, u.role), "neutral"), " ", u.words.map((w) => w.text).join("")))));
    } catch (error) {
      replace(unitsSlot, notice(error.message, "error"));
    }
  } }, "显示原 OCR 文字（按此顺序）"));
  viewerLayout(viewer, c.page_id, h("div", { class: "chips" }, badge(c.layout || c.status, "neutral"),
    badge(`${num(c.raw_line_count)} 行 · ${num(c.raw_word_count)} 词`, "neutral"),
    badge(data.image_bound ? "图像已验" : "图像不符", data.image_bound ? "ok" : "error")), frame,
  [list, unitsSlot, h("p", { class: "muted" }, "几何分列不代表阅读顺序或字词正确。")]);
  linkHover(viewer.querySelector(".page-frame"), list);
}

async function showVision(viewer, corpus, item) {
  const params = { corpus: corpus.id, id: item.id, candidate_sha256: item.candidate_sha256 };
  const data = await viewRequest(request("/api/ocr-vision-candidate", { params }));
  const c = data.candidate;
  const shapes = [...c.segments.map((s) => ({ key: `g${s.order}`, box: s.bbox, tone: s.kind === "ruby" ? "warn" : "accent",
    title: `${s.order}. ${label(SEGMENT_ROLES, s.kind)}` })),
  ...c.uncertain_regions.map((u, i) => ({ key: `u${i}`, box: u.bbox, tone: "error", title: `存疑：${u.reason}` }))];
  const frame = figure(url("/api/ocr-vision-image", { ...params, image_sha256: c.image_sha256 }), c.width, c.height, shapes, { normalized: true });
  const list = h("ol", { class: "regions", lang: "ja" }, c.segments.map((s) => h("li", { dataset: { key: `g${s.order}` } },
    s.kind !== "main" ? badge(label(SEGMENT_ROLES, s.kind), "neutral") : null, " ", s.text)));
  const baseline = h("div", {}, h("button", { type: "button", class: "button ghost small", onclick: async (e) => {
    e.target.disabled = true;
    try {
      const b = await request("/api/ocr-vision-baseline", { params });
      replace(baseline, b.available ? h("ol", { class: "regions", lang: "ja" }, b.rows.map((r) => h("li", {},
        h("span", { class: "muted num" }, num(r.ordinal)), " ", r.text))) : notice(b.error || "没有可对照的正式段落。", "info"));
    } catch (error) {
      replace(baseline, notice(error.message, "error"));
    }
  } }, "对照正式库已有 OCR 段落"));
  viewerLayout(viewer, c.page_id, h("div", { class: "chips" }, badge(`${num(c.segments.length)} 段`, "accent"),
    c.uncertain_regions.length ? badge(`${num(c.uncertain_regions.length)} 处存疑`, "error") : null,
    h("small", { class: "muted" }, c.model)), frame, [list, baseline]);
  linkHover(viewer.querySelector(".page-frame"), list);
}

async function showPage(viewer, corpus, pageId) {
  replace(viewer, loading());
  let regions = [];
  if (corpus.ocr_regions && corpus.ocr_regions.available) {
    try {
      regions = (await request("/api/ocr-regions", { params: { corpus: corpus.id, page_id: pageId } })).items;
    } catch { regions = []; }
  }
  if (regions.length) {
    await showRegions(viewer, corpus, regions[0]);
    return;
  }
  const image = h("img", { src: url("/api/scan", { corpus: corpus.id, page: pageId }), alt: "扫描页" });
  replace(viewer, h("div", { class: "pane-head" }, h("h2", { class: "pane-title mono" }, pageId)),
    h("div", { class: "page-frame" }, image), h("p", { class: "muted" }, "该页没有区域转写候选。"));
  image.addEventListener("error", () => image.replaceWith(notice("图像无法加载。", "error")));
}
