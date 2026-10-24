// One record in context: text, surrounding paragraphs, every machine opinion about it, and the review form.
import { latest, request } from "./api.js";
import { renderEvidenceTab } from "./evidence.js";
import { BASIS, COMPLETION_STATUSES, CONFIDENCE, KINDS, MODEL_REVIEW, MODEL_REVIEW_TONE, SOURCES, VERDICTS,
  VERDICT_TONE, label } from "./labels.js";
import { badge, details, empty, h, keyValues, loading, notice, num, replace, shortHash, time } from "./ui.js";

const detailRequest = latest();

export async function renderDetail(pane, app, corpus, source, id, onClose) {
  const close = h("button", { type: "button", class: "button ghost small close", "aria-label": "关闭详情", onclick: onClose }, "关闭");
  replace(pane, h("div", { class: "pane-head" }, h("h2", { class: "pane-title" }, "记录详情"), close), loading());
  let data;
  try {
    data = await detailRequest(request("/api/dialogue", { params: { corpus: corpus.id, source, id } }));
  } catch (error) {
    if (error.kind !== "stale") replace(pane, h("div", { class: "pane-head" }, h("h2", { class: "pane-title" }, "记录详情"), close),
      notice(error.message, "error"));
    return;
  }
  const item = data.item;
  const header = h("div", { class: "pane-head" },
    h("div", {}, h("h2", { class: "pane-title" }, item.edition_title || item.edition_id),
      h("p", { class: "muted" }, "第 " + num(item.ordinal) + " 段 · " + SOURCES[source] + " · ", h("span", { class: "mono" }, item.id))), close);
  const body = h("div", { role: "tabpanel", "aria-label": "详情内容" },
    h("blockquote", { class: "record-quote", lang: item.language || undefined }, item.text || "（无正文）"),
    item.text_truncated ? notice("仅显示前一部分；原文共 " + num(item.text_total_chars) + " 字。", "warn") : null,
    contextBlock(data), attributionBlock(data.attribution), identityBlock(data.identity_suggestions),
    quoteCandidateBlock(data.candidate), alignmentCandidateBlock(data.alignment_candidate), formalAlignmentBlock(data.alignment),
    completionBlock(data.completion_alignment), entryBlock(data.entry), scansBlock(app, corpus, data.scan_pages),
    reviewBlock(app, corpus, source, id, data.review));
  const evidence = h("div", { role: "tabpanel", "aria-label": "对白证据", hidden: true });
  let loaded = false;
  const buttons = ["详情", "证据"].map((label, index) => h("button", { type: "button", role: "tab",
    class: "button ghost small", "aria-selected": index === 0 ? "true" : "false", onclick: () => {
      body.hidden = index !== 0;
      evidence.hidden = index !== 1;
      buttons.forEach((button, i) => button.setAttribute("aria-selected", i === index ? "true" : "false"));
      if (index === 1 && !loaded) {
        loaded = true;
        renderEvidenceTab(evidence, app, corpus, id);
      }
    } }, label));
  replace(pane, header, h("div", { class: "detail-tabs", role: "tablist", "aria-label": "对白详情页签" }, buttons), body, evidence);
}

function block(title, ...body) {
  return h("section", { class: "detail-block" }, h("h3", {}, title), body);
}

function flags(object, keys = ["requires_review", "accepted", "human_confirmed", "semantic_verified", "reference_eligible"]) {
  const names = { requires_review: "需复核", accepted: "已接受", human_confirmed: "人工确认", semantic_verified: "语义核验",
    reference_eligible: "可作参考", ocr_verified: "OCR 核验" };
  return h("div", { class: "chips" }, keys.filter((k) => k in object).map((k) =>
    badge(`${names[k]}：${object[k] ? "是" : "否"}`, k === "requires_review" ? (object[k] ? "warn" : "neutral") : (object[k] ? "ok" : "neutral"))));
}

function contextBlock(data) {
  if (!data.context || !data.context.length) return null;
  const anchor = data.context_anchor || {};
  return block("上下文", h("ol", { class: "context" }, data.context.map((p) => h("li", {
    class: p.ordinal >= anchor.start_para && p.ordinal <= anchor.end_para ? "anchor" : "",
  }, h("span", { class: "muted num" }, num(p.ordinal)), h("span", {}, p.text)))),
  anchor.mode === "endpoint_windows" ? h("p", { class: "muted" }, "跨段较长，只显示首尾附近的段落。") : null);
}

function attributionBlock(a) {
  if (!a) return null;
  return block("说话人归属",
    keyValues([["说话人", a.speaker || "未知"], ["被称呼者", a.addressee || "未知"], ["状态", a.status],
      a.basis_detector ? ["判定器", a.basis_detector] : null,
      a.base ? ["正式库原值", `${a.base.speaker || "未知"} → ${a.base.addressee || "未知"}（${a.base.attribution_status || "—"}）`] : null]),
    a.flags ? flags(a.flags) : null,
    a.provenance && a.provenance.length ? details("来源", h("ul", { class: "plain mono" }, a.provenance.map((p) =>
      h("li", {}, typeof p === "string" ? p : `${p.paragraph_id} [${p.start}, ${p.end})`)))) : null);
}

function identityBlock(s) {
  if (!s) return null;
  return block("身份建议", flags(s), s.observations && s.observations.length ? h("ol", { class: "observations" },
    s.observations.map((o) => h("li", {},
      h("div", { class: "chips" }, badge(o.speaker ? `说话人：${o.speaker}` : "说话人未知", o.speaker ? "ok" : "warn"),
        o.addressee ? badge(`对象：${o.addressee}`, "neutral") : null, badge(label(KINDS, o.kind), "neutral"),
        badge(label(BASIS, o.basis), "neutral"), h("small", { class: "muted" }, o.served_model || o.model)),
      (o.evidence || []).map((e) => h("p", { class: "evidence" }, h("span", { class: "muted" }, `第 ${num(e.ordinal)} 段：`), e.text)))))
    : empty("没有模型意见。"));
}

function observations(list) {
  if (!list || !list.length) return empty("没有独立复核意见。");
  return h("ul", { class: "plain" }, list.map((o) => h("li", {},
    badge(label(MODEL_REVIEW, o.decision), MODEL_REVIEW_TONE[o.decision]), " ",
    o.basis ? label(BASIS, o.basis) : "", " ", h("small", { class: "muted" }, o.served_model || o.model))));
}

function quoteCandidateBlock(c) {
  if (!c) return null;
  return block("引号候选", keyValues([["抽取模型", c.model], ["复核结论", badge(label(MODEL_REVIEW, c.review_status),
    MODEL_REVIEW_TONE[c.review_status])]]), observations(c.review_observations));
}

function japaneseList(list) {
  if (!list || !list.length) return empty("没有日文引用。");
  return h("ol", { class: "japanese", lang: "ja" }, list.map((j) => h("li", {},
    h("span", { class: "muted num" }, num(j.ordinal)), h("span", {}, j.text || "（无正文）"),
    j.truncated ? badge("已截断", "warn") : null)));
}

function alignmentCandidateBlock(c) {
  if (!c) return null;
  const reuse = c.target_reuse;
  return block("日中对齐候选",
    h("div", { class: "chips" }, badge(c.status === "unmatched" ? "未匹配" : "候选对齐", c.status === "unmatched" ? "warn" : "accent"),
      c.confidence ? badge(`置信 ${label(CONFIDENCE, c.confidence)}`, "neutral") : null,
      badge(label(MODEL_REVIEW, c.review_status), MODEL_REVIEW_TONE[c.review_status]), h("small", { class: "muted" }, c.model)),
    japaneseList(c.japanese), observations(c.review_observations),
    reuse ? notice(`同一日文目标被 ${num(reuse.chinese_count)} 条中文对白共享（第 ${num(reuse.ordinal_min)}–${num(reuse.ordinal_max)} 段）。`, "info") : null);
}

function formalAlignmentBlock(a) {
  if (!a) return null;
  return block("正式库对齐", keyValues([["状态", a.status], ["方法", a.method || "—"],
    ["置信", a.confidence == null ? "—" : String(label(CONFIDENCE, a.confidence))]]), japaneseList(a.japanese));
}

function completionBlock(c) {
  if (!c) return null;
  return block("待复核对齐（正式库）",
    keyValues([["旧判断", label(COMPLETION_STATUSES, c.status)], ["决定", c.decision || "—"],
      ["日文版本", c.source_edition ? `${c.source_edition.title}（${c.source_edition.origin_kind}）` : "—"],
      ["未校准分数", c.confidence_uncalibrated == null ? "—" : String(c.confidence_uncalibrated)]]),
    c.source_quotes && c.source_quotes.length ? japaneseList(c.source_quotes) : empty("没有日文引用。"),
    flags(c), h("p", { class: "muted" }, "历史摘要仅供追溯，未重放旧任务。"));
}

function entryBlock(e) {
  if (!e) return null;
  return block("参考条目", flags(e, ["reference_eligible"]),
    keyValues([["归属状态", e.attribution_status || "—"], e.alignment_status ? ["对齐状态", e.alignment_status] : null]));
}

function scansBlock(app, corpus, pages) {
  if (!pages || !pages.length) return null;
  return block("扫描页", h("ul", { class: "plain" }, pages.map((p) => h("li", {},
    h("a", { href: app.href("scans", { page: p.id }) }, `第 ${num(p.ordinal)} 页`), " ",
    badge(p.ocr_status || "OCR 状态未知", "neutral"), p.image_available ? null : badge("无图像", "warn")))));
}

function reviewBlock(app, corpus, source, id, review) {
  if (!app.overview.reviews_enabled) return block("人工复核", notice("未配置复核目录，只能浏览。", "info"));
  const message = h("p", { class: "muted", role: "status" });
  const form = h("form", { class: "review-form", onsubmit: async (event) => {
    event.preventDefault();
    const data = new FormData(form);
    const body = { corpus: corpus.id, dialogue_id: id, source, verdict: data.get("verdict") };
    for (const key of ["speaker", "addressee", "note"]) {
      const value = String(data.get(key) || "").trim();
      if (value) body[key] = value;
    }
    if (!VERDICTS[body.verdict]) {
      message.textContent = "请先选择判定。";
      return;
    }
    message.textContent = "保存中…";
    try {
      const result = await request("/api/reviews", { body });
      message.textContent = `已保存（第 ${result.review.version} 版，${time(result.review.updated_at)}）。标签不会修改语料库。`;
    } catch (error) {
      message.textContent = error.code === "stale_review" ? "数据已变化：旧标签绑定的版本与当前不同。" : error.message;
    }
  } },
  h("fieldset", { class: "verdicts" }, h("legend", {}, "判定"), Object.entries(VERDICTS).map(([key, text]) =>
    h("label", { class: "check" }, h("input", { type: "radio", name: "verdict", value: key,
      checked: review && review.verdict === key }), text))),
  h("div", { class: "filter-row" },
    h("input", { name: "speaker", placeholder: "说话人（可选）", maxlength: 60, value: (review && review.speaker) || "" }),
    h("input", { name: "addressee", placeholder: "被称呼者（可选）", maxlength: 60, value: (review && review.addressee) || "" })),
  h("textarea", { name: "note", rows: 3, maxlength: 500, placeholder: "备注（可选）" }, (review && review.note) || ""),
  h("div", { class: "form-actions" }, h("button", { type: "submit", class: "button primary" }, "保存标签"), message));
  return block("人工复核",
    review ? h("div", { class: "chips" }, badge(`已有标签：${label(VERDICTS, review.verdict)}`, VERDICT_TONE[review.verdict]),
      review.stale ? badge("数据已变化，标签过期", "warn") : null, h("small", { class: "muted" },
        `第 ${review.version} 版 · ${time(review.updated_at)} · 指纹 ${shortHash(review.corpus_fingerprint)}`)) : null,
    form);
}
