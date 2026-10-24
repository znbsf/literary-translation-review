// Shared evidence UI for provenance levels and source-bound relationships.
import { request } from "./api.js";
import { badge, empty, h, keyValues, loading, notice, num, pager, replace, select, shortHash } from "./ui.js";

const LEVEL = { verified: ["已核", "ok"], candidate: ["候选", "warn"], clue: ["检索线索", "info"] };
const RELATION = { aligned: "跨版本对应", proposed_alignment: "候选对应", unmatched: "未匹配提议",
  retrieval: "全文检索命中", speaker_attribution: "人物归属" };
const MODES = [["line", "跨版本"], ["speaker", "人物"], ["term", "词条"]];
const LIMIT = 20;
const ORIGIN = { formal_alignment: "正式库对齐记录", alignment_index: "库外对齐提议",
  formal_attribution: "库内归属记录", attribution_candidates: "归属提议",
  attribution_index: "人物建议索引", paragraph_search: "原文检索" };
const STATUS = { human_confirmed: "人工确认", model_matched: "模型匹配", candidate: "待复核", unmatched: "未匹配" };
const SPEAKER = { human_confirmed: "人工确认", model_attributed: "模型归属，未核", unresolved: "未知",
  unknown: "未知", candidate: "候选归属", not_attributed: "未做归属" };

function record(node, app) {
  const source = node.id.startsWith("candidate:") ? "candidates" : "formal";
  return h("section", { class: "evidence-record" },
    h("div", { class: "evidence-record-head" },
      h("strong", {}, node.edition_title || node.edition_id), badge(node.language || "语言未知"),
      node.kind !== "paragraph" ? h("a", { href: app.href("dialogues", { id: node.id, source, edition: node.edition_id }) }, "查看对白") : null),
    h("blockquote", {}, node.text ?? node.excerpt ?? ""),
    node.text_truncated ? h("p", { class: "muted" }, "短摘已截断，原记录共 " + num(node.text_total_chars) + " 字。") : null,
    h("p", { class: "muted" }, "说话人：" + (node.speaker || "未知") + " · " + (SPEAKER[node.speaker_status] || node.speaker_status || "未核归属")),
    node.offset_status === "endpoint_range" ? notice("跨度较长，列出首尾段落；精确字符偏移待核。", "warn") : null,
    h("details", {}, h("summary", {}, "段落定位与摘要"),
      h("p", { class: "mono" }, node.edition_id),
      (node.bindings || []).length ? node.bindings.map((b) => keyValues([
        ["段落", h("span", { class: "mono" }, b.paragraph_id)],
        ["偏移", b.start == null ? "仅段落范围，精确偏移待核" : "[" + b.start + ", " + b.end + ")"],
        ["SHA-256", h("span", { class: "mono", title: b.paragraph_sha256 }, b.paragraph_sha256)],
      ])) : notice("本条暂无法提供段落定位。", "warn")));
}

function card(item, app, nested = false) {
  const [label, tone] = LEVEL[item.level] || ["未知", "neutral"];
  return h("article", { class: "evidence-card" + (nested ? " nested" : ""), dataset: { level: item.level } },
    h("div", { class: "evidence-card-head" }, badge(label, tone),
      h("strong", {}, RELATION[item.relation] || item.relation),
      h("small", { class: "muted" }, ORIGIN[item.origin] || item.origin), item.status ? badge(STATUS[item.status] || item.status) : null),
    record(item.record, app),
    (item.claims || []).map((c) => h("p", {}, badge((LEVEL[c.level] || [c.level])[0], c.level === "verified" ? "ok" : "warn"),
      " " + (c.speaker || "未知") + " → " + (c.addressee || "对象未知") + " · " + (ORIGIN[c.origin] || c.origin))),
    (item.references || []).map((r) => record(r, app)),
    (item.review_observations || []).map((o) => h("p", { class: "muted" },
      "独立复核：" + (({ keep: "保留提议", reject: "拒绝提议", uncertain: "不确定" })[o.decision] || o.decision) + " · " + (o.basis || "依据未注明"))),
    (item.correspondences || []).map((r) => card(r, app, true)),
    item.correspondences_may_have_more ? notice("相关关系已限量显示，可进入对白继续查询。") : null);
}

function results(data, app) {
  return h("div", { class: "evidence-results" },
    data.subject ? h("section", { class: "card" }, h("h3", {}, "查询起点"), record(data.subject, app),
      data.subject.speaker ? h("a", { class: "button ghost small",
        href: app.href("evidence", { mode: "speaker", name: data.subject.speaker }) }, "该人物的其他台词") : null) : null,
    data.aliases ? h("p", { class: "muted" }, "查询别名：" + data.aliases.join("、") + "。别名扩展不代表人物归属已确认。") : null,
    data.no_correspondence_found ? notice("未找到跨版本对应；未匹配提议和检索命中不构成对应。", "warn")
      : data.mode === "line" && !data.has_verified_correspondence ? notice("尚无已核对应，以下仍是待复核提议。", "warn") : null,
    data.items.length ? data.items.map((item) => card(item, app)) : empty("没有符合条件的证据。"),
    h("p", { class: "muted mono" }, "来源 " + shortHash(data.source_sha256) + " · 正式库 " + shortHash(data.formal_sha256)));
}

export async function renderEvidenceTab(target, app, corpus, id) {
  replace(target, loading("正在核对来源并查询证据…"));
  try {
    const data = await request("/api/evidence", { params: { corpus: corpus.id, mode: "line", dialogue: id }, timeout: 90000 });
    if (!target.isConnected) return;
    replace(target, notice("已核、候选和检索线索分开显示；当前生成稿不参与查询。"),
      h("a", { class: "button ghost small", href: app.href("evidence", { mode: "line", dialogue: id }) }, "打开证据页"),
      results(data, app));
  } catch (error) {
    if (target.isConnected) replace(target, notice(error.message, "error"));
  }
}

export async function render(container, app, current) {
  const corpus = app.corpus();
  const p = app.route.params;
  const mode = MODES.some(([key]) => key === p.mode) ? p.mode : "term";
  const key = ({ line: "dialogue", speaker: "name", term: "text" })[mode];
  const value = p[key] || "";
  const modeField = select(MODES, mode, (next) => app.go("evidence", { mode: next }), "查询类型");
  const input = h("input", { name: "value", required: true, value, maxlength: mode === "line" ? 200 : 120,
    placeholder: mode === "line" ? "对白 ID" : mode === "speaker" ? "人物名称或已有别名" : "原文或固定表达",
    "aria-label": "查询内容" });
  const book = h("input", { name: "book", value: p.book || "", maxlength: 200, placeholder: "作品标识（可选）", "aria-label": "作品标识" });
  const addressee = h("input", { name: "addressee", value: p.addressee || "", maxlength: 80, placeholder: "对象（可选）", "aria-label": "说话对象" });
  const lang = h("input", { name: "lang", value: p.lang || "", maxlength: 30, placeholder: "语言，如 ja（可选）", "aria-label": "查询语言" });
  const full = h("input", { type: "checkbox", name: "with_text", checked: p.with_text === "1" });
  const form = h("form", { class: "card evidence-form", onsubmit: (event) => {
    event.preventDefault();
    app.go("evidence", { mode, [key]: input.value.trim(), book: book.value.trim(),
      addressee: mode === "speaker" ? addressee.value.trim() : "",
      lang: mode === "term" ? lang.value.trim() : "", with_text: full.checked ? "1" : "", offset: "" });
  } }, h("div", { class: "evidence-fields" }, modeField, input, book,
    mode === "speaker" ? addressee : null, mode === "term" ? lang : null),
    h("div", { class: "filter-row" }, h("label", { class: "check" }, full, "展开完整正文（仅本机）"),
      h("button", { type: "submit", class: "button primary" }, "查询证据")));
  const output = h("section", { "aria-label": "证据查询结果" });
  container.append(h("h1", {}, "证据"),
    notice("已核需要显式人工、语义与参考资格；模型建议始终是候选，全文命中始终是线索。"), form, output);
  if (!value) {
    replace(output, empty("选择查询类型并输入内容。"));
    return;
  }
  replace(output, loading("正在核对来源并查询证据…"));
  try {
    const data = await request("/api/evidence", { params: { corpus: corpus.id, mode, [key]: value,
      book: p.book || "", addressee: mode === "speaker" ? p.addressee || "" : "",
      lang: mode === "term" ? p.lang || "" : "", with_text: p.with_text === "1",
      limit: LIMIT, offset: Number(p.offset) || 0 }, timeout: 90000 });
    if (!current()) return;
    replace(output, results(data, app), pager({ offset: data.offset, limit: data.limit, hasMore: data.has_more },
      (offset) => app.go("evidence", { ...p, offset: offset || "" })));
  } catch (error) {
    if (current()) replace(output, notice(error.message, "error"));
  }
}
