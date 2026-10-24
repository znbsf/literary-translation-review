// 概览: what each corpus holds and how far every completion pipeline has got.
import { ARCHIVE_KINDS, COMPLETION_STATUSES, MODEL_REVIEW, MODEL_REVIEW_TONE, label } from "./labels.js";
import { badge, details, empty, h, keyValues, meter, notice, num, section, shortHash, stat, time } from "./ui.js";

const IDENTITY_TONES = { suggested: "ok", unresolved: "warn", conflict: "error", not_dialogue: "neutral" };
const IDENTITY_LABELS = { suggested: "有身份建议", unresolved: "仍未知", conflict: "建议冲突", not_dialogue: "建议非对话" };

export function render(container, app) {
  const corpus = app.corpus();
  container.append(corpusStrip(app, corpus));
  if (!corpus || !corpus.available) {
    container.append(notice("该语料库的数据库不可用；以下不是零值统计。", "error"));
    return;
  }
  container.append(
    h("p", { class: "muted meta-line" }, `统计时间 ${time(app.overview.generated_at)} · 证据来源：`,
      evidenceLabel(corpus.evidence_source), ` · 已保存复核 ${num(corpus.review_count)} 条`),
    scale(corpus),
    progress(corpus, app),
    runSnapshot(corpus.run_snapshot),
    references(corpus),
  );
}

function corpusStrip(app, current) {
  return h("div", { class: "corpus-strip" }, app.overview.corpora.map((c) => h("a", {
    class: `corpus-tile${current && c.id === current.id ? " active" : ""}`, href: app.href("overview", { corpus: c.id }),
  }, h("strong", {}, c.name),
  c.available ? h("span", { class: "muted" }, `${num(c.counts.editions)} 个版本 · ${num(c.counts.dialogues)} 条对白`)
    : badge("不可用", "error"))));
}

function evidenceLabel(source) {
  if (!source || !source.available) return "不可用";
  return source.binding === "source_snapshot" ? `源库快照 ${shortHash(source.source_sha256)}`
    : `当前库 ${shortHash(source.source_sha256)}`;
}

function scale(c) {
  const languages = (c.languages || []).map((l) => badge(`${l.language || "未标注"} ${num(l.count)}`, "neutral"));
  return section("数据规模", h("div", {},
    h("div", { class: "stats" },
      stat("版本", num(c.counts.editions)), stat("段落", num(c.counts.paragraphs)),
      stat("对白", num(c.counts.dialogues)), stat("中文条目", num(c.counts.entries)),
      stat("正式对齐", num(c.counts.alignments)), stat("扫描页", num(c.ocr.total),
        c.ocr.total ? `${num(c.ocr.unverified)} 页 OCR 未核验` : null)),
    h("div", { class: "chips" }, languages)));
}

function pipeline(title, body, link) {
  return h("article", { class: "pipeline" },
    h("header", {}, h("h3", {}, title), link ? h("a", { class: "button ghost small", href: link[1] }, link[0]) : null),
    body);
}

function unavailable(entry, what) {
  if (entry && entry.error) return notice(`${what}不可用：${entry.error}`, "warn");
  return empty(`未配置${what}。`);
}

function progress(c, app) {
  const missing = [];
  const items = pipelines(c, app, missing).filter(Boolean);
  return section("补全进度", h("div", {}, h("div", { class: "pipelines" }, items),
    missing.length ? h("p", { class: "muted" }, `未配置：${missing.join("、")}`) : null));
}

// Builds one card per pipeline; pipelines without any configured source are named in `missing` instead.
function pipelines(c, app, missing) {
  const skip = (entry, name) => {
    if (entry && entry.available) return false;
    if (entry && entry.error) return false;
    missing.push(name);
    return true;
  };
  const items = [];
  const alignments = c.pending_alignments || {};
  const coverage = alignments.source_coverage || {};
  if (!skip(alignments, "日中对齐候选")) items.push(pipeline("日中对齐候选", alignments.available && coverage.available ? h("div", {},
    meter([
      { label: "有日文引用", value: coverage.matched_dialogues, tone: "accent" },
      { label: "仅未匹配", value: coverage.unmatched_dialogues, tone: "warn" },
      { label: "尚无候选", value: coverage.unprocessed_dialogues, tone: "empty" },
    ], coverage.source_dialogues, { caption: `中文对白 ${num(coverage.source_dialogues)} 条的提议覆盖` }),
    reviewChips(alignments.review_status),
    alignments.target_reuse ? h("p", { class: "muted" },
      `共享同一日文目标的组 ${num(alignments.target_reuse.groups_with_multiple_chinese)} 个，涉及 ` +
      `${num(alignments.target_reuse.affected_chinese_records)} 条中文对白（不代表错误）。`) : null,
  ) : unavailable(alignments, "对齐候选索引"),
  alignments.available ? ["浏览", app.href("dialogues", { source: "alignments" })] : null));

  const quotes = c.pending_candidates || {};
  if (!skip(quotes, "引号候选")) items.push(pipeline("引号候选（无引号版本）", quotes.available ? h("div", {},
    meter(Object.entries(quotes.review_status || {}).map(([key, value]) =>
      ({ label: label(MODEL_REVIEW, key), value, tone: MODEL_REVIEW_TONE[key] })), quotes.total,
    { caption: `候选 ${num(quotes.total)} 条，模型已复核 ${num(quotes.reviewed)} 条` }),
  ) : unavailable(quotes, "引号候选索引"), quotes.available ? ["浏览", app.href("dialogues", { source: "candidates" })] : null));

  const identities = c.pending_identities || {};
  if (!skip(identities, "身份建议")) items.push(pipeline("身份建议", identities.available ? h("div", {},
    meter(Object.entries(identities.statuses || {}).map(([key, value]) =>
      ({ label: IDENTITY_LABELS[key] || key, value, tone: IDENTITY_TONES[key] })), identities.total,
    { caption: `建议 ${num(identities.total)} 条，其中有说话人 ${num(identities.with_speaker)} 条` }),
  ) : unavailable(identities, "身份建议索引"), identities.available ? ["浏览", app.href("dialogues", { source: "identities" })] : null));

  const a = c.attribution || {};
  items.push(pipeline("正式库说话人归属", h("div", {},
    meter([{ label: "有说话人", value: a.speaker_known, tone: "accent" },
      { label: "未知", value: Math.max(0, (a.total || 0) - (a.speaker_known || 0)), tone: "empty" }], a.total,
    { caption: `归属记录 ${num(a.total)} 条` }),
    h("p", { class: "muted" }, `人工确认 ${num(a.human_confirmed)} · 语义核验 ${num(a.semantic_verified)} · 可作参考 ${num(a.reference_eligible)}`)),
  ["浏览", app.href("dialogues", { source: "formal" })]));

  const completion = c.completion_alignment_review || {};
  if (completion.available) {
    items.push(pipeline("正式库待复核对齐", h("div", {},
      meter(Object.entries(completion.statuses || {}).map(([key, value]) =>
        ({ label: label(COMPLETION_STATUSES, key), value, tone: key === "other" ? "neutral" : "warn" })),
      completion.pending_rows, { caption: `待复核 ${num(completion.pending_rows)} 行，可定位 ${num(completion.mapped_pending_rows)} 行` })),
    ["浏览", app.href("dialogues", { source: "formal", pending_alignment: "1" })]));
  }

  if (c.ocr.total) items.push(pipeline("扫描页 OCR", ocrSummary(c), ["查看扫描页", app.href("scans")]));
  else missing.push("扫描页 OCR");

  const archive = c.machine_archive || {};
  items.push(pipeline("正式库内的机器候选", archive.available ? h("div", {},
    meter(Object.entries(archive.by_kind || {}).map(([key, value]) => ({ label: label(ARCHIVE_KINDS, key), value, tone: "accent" })),
      archive.total, { caption: `已追加 ${num(archive.total)} 条，全部${archive.requires_review_all ? "仍需复核" : "（复核标记异常）"}` }),
  ) : unavailable(archive, "归档摘要")));
  return items;
}

function reviewChips(status = {}) {
  return h("div", { class: "chips" }, Object.entries(status).map(([key, value]) =>
    badge(`${label(MODEL_REVIEW, key)} ${num(value)}`, MODEL_REVIEW_TONE[key] || "neutral")));
}

function ocrSummary(c) {
  const rows = [];
  const order = c.ocr_reading_order_candidates || {};
  if (order.available) {
    rows.push(meter([{ label: "已分列", value: order.classified_count, tone: "accent" },
      { label: "未分类", value: order.unclassified_count, tone: "warn" },
      { label: "拒绝", value: order.rejected_count, tone: "error" }], order.total,
    { caption: `阅读顺序几何 ${num(order.total)} 页（${order.collections ? order.collections.length : 0} 册）` }));
  }
  const regions = c.ocr_regions || {};
  if (regions.available) {
    rows.push(meter([{ label: "全部区域已转写", value: regions.fully_transcribed, tone: "accent" },
      { label: "含不可读区域", value: regions.partial, tone: "warn" }], regions.candidates,
    { caption: `区域转写候选 ${num(regions.candidates)} 页，不可读区域 ${num(regions.unreadable_regions)} 个` }));
  }
  const vision = c.ocr_vision_candidates || {};
  if (vision.available) rows.push(h("p", {}, `整页视觉 OCR 候选 ${num(vision.candidate_count)} 页`));
  if (!rows.length) return empty(c.ocr.total ? "已有扫描页，但未配置 OCR 候选。" : "该语料库没有扫描页。");
  return h("div", {}, c.ocr.total ? h("p", { class: "muted" }, `扫描页 ${num(c.ocr.total)}，正式 OCR 未核验 ${num(c.ocr.unverified)}`) : null, rows);
}

function runSnapshot(snapshot) {
  if (!snapshot || !snapshot.available) {
    return snapshot && snapshot.error ? section("运行快照", notice(`快照不可用：${snapshot.error}`, "warn")) : null;
  }
  const head = ["任务", "类型", "状态", "计划", "有效", "失败", "结果未知", "未尝试", "调用"];
  return section("运行快照", h("div", {},
    h("p", { class: "muted" }, `生成于 ${time(snapshot.generated_at)}；各任务可能重叠，不能相加得出完成率。`),
    h("div", { class: "table-wrap" }, h("table", { class: "table" },
      h("thead", {}, h("tr", {}, head.map((t) => h("th", {}, t)))),
      h("tbody", {}, snapshot.workers.map((w) => h("tr", {},
        h("td", { class: "mono" }, w.worker_id), h("td", {}, w.kind),
        h("td", {}, w.terminal ? badge(w.stop_reason, "neutral") : badge("运行中/未知", "warn")),
        [w.planned_rows, w.valid_rows, w.failed_rows, w.outcome_unknown_rows, w.unattempted_rows, w.attempted_calls]
          .map((v) => h("td", { class: "num" }, num(v))))))))));
}

function references(c) {
  const bib = c.publisher_bibliography || {};
  const ids = c.publisher_identifiers || {};
  const body = h("div", {});
  if (bib.available) {
    body.append(details(`出版社书目 ${num(bib.total)} 条（核对于 ${time(bib.verified_at_utc)}）`,
      h("div", { class: "table-wrap" }, h("table", { class: "table" },
        h("thead", {}, h("tr", {}, ["标题", "分类", "版本", "发行日期", "ISBN-13", "链接"].map((t) => h("th", {}, t)))),
        h("tbody", {}, (bib.entries || []).map((e) => h("tr", {},
          h("td", {}, e.title_ja), h("td", {}, e.category || "—"), h("td", {}, e.edition_variant || "—"),
          h("td", {}, e.release_date || "—"), h("td", { class: "mono" }, e.isbn13 || "—"),
          h("td", {}, safeLink(e.publisher_url)))))))));
  } else if (bib.error) {
    body.append(notice(`书目不可用：${bib.error}`, "warn"));
  }
  if (ids.available) {
    body.append(keyValues([["检索版本", num(ids.searched_editions)], ["检索段落", num(ids.searched_paragraphs)],
      ["含 ISBN 候选的版本", num(ids.editions_with_candidates)], ["候选行", num(ids.candidate_rows)],
      ["对应出版记录", num(ids.distinct_publisher_records)]]));
  }
  if (!body.childNodes.length) return null;
  body.append(h("p", { class: "muted" }, "书目与本地版本的对应关系未经核实，仅供参考。"));
  return section("书目与出版标识", body);
}

function safeLink(value) {
  try {
    const parsed = new URL(value);
    if (parsed.protocol !== "https:") return "—";
    return h("a", { href: parsed.href, target: "_blank", rel: "noopener noreferrer" }, parsed.hostname);
  } catch {
    return "—";
  }
}

