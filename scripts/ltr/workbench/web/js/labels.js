// Display labels for codes returned by the API. Unknown codes are shown as-is.

export const label = (table, key) => (key in table ? table[key] : key || "—");

export const SOURCES = {
  formal: "已入库对白",
  candidates: "引号候选",
  alignments: "日中对齐候选",
  identities: "身份建议",
};
export const SOURCE_CAPABILITY = { candidates: "candidates", alignments: "alignment_candidates", identities: "identities" };

export const STATUS_FILTERS = {
  formal: { "": "全部归属状态", candidate: "机器候选", unresolved: "尚未判定", model_attributed: "模型已判断",
    not_dialogue: "非对话", quoted_excluded: "排除的引用", human_confirmed: "人工确认", semantic_verified: "语义已核验" },
  candidates: { "": "全部模型意见", keep: "建议保留", reject: "建议排除", uncertain: "待定", conflict: "意见冲突",
    unreviewed: "未复核" },
  alignments: { "": "全部模型意见", keep: "建议保留", reject: "建议排除", uncertain: "待定", conflict: "意见冲突",
    unreviewed: "未复核", not_applicable: "未匹配" },
  identities: { "": "全部身份状态", suggested: "有身份建议", unresolved: "仍未知", conflict: "建议冲突",
    not_dialogue: "建议非对话" },
};

export const MODEL_REVIEW = { keep: "建议保留", reject: "建议排除", uncertain: "待定", conflict: "意见冲突",
  unreviewed: "未复核", not_applicable: "未匹配" };
export const MODEL_REVIEW_TONE = { keep: "ok", reject: "warn", uncertain: "warn", conflict: "error",
  unreviewed: "neutral", not_applicable: "neutral" };

export const ATTRIBUTION = { candidate: "机器候选", unresolved: "尚未判定", model_attributed: "模型已判断",
  not_dialogue: "非对话", quoted_excluded: "排除的引用", human_confirmed: "人工确认", semantic_verified: "语义已核验",
  suggested: "有身份建议", conflict: "建议冲突" };

export const KINDS = { spoken: "口语", thought: "心理活动", written: "书面", uncertain: "类型未定", not_dialogue: "非对话" };
export const BASIS = { explicit_name: "明确姓名", direct_address: "直接称呼", self_identification: "自我表明身份",
  context_inference: "上下文推断", insufficient_context: "上下文不足", source_not_dialogue: "来源非对话",
  direct_speech: "直接对话", narration: "叙述", written_quote: "书面引用", identity_ambiguous: "人物不明",
  semantic_match: "语义相符", semantic_mismatch: "语义不符", ocr_unclear: "OCR 不清", partial_match: "部分相符" };
export const CONFIDENCE = { high: "高", medium: "中", low: "低" };

export const VERDICTS = { correct: "正确", incorrect: "错误", uncertain: "不确定" };
export const VERDICT_TONE = { correct: "ok", incorrect: "error", uncertain: "warn" };

export const ARCHIVE_KINDS = { alignment: "对齐", identity: "身份", ocr_region: "OCR 区域", publisher_identifier: "出版标识",
  unquoted: "引号候选" };
export const COMPLETION_STATUSES = { unresolved_insufficient_evidence: "证据不足", unresolved_low_support_match: "支持度低",
  unresolved_not_corresponding: "不对应", unresolved_partial_candidate: "部分对应", other: "其他" };

export const REGION_STATUS = { transcribed: "已转写", unreadable: "不可读" };
export const SEGMENT_ROLES = { main: "正文", ruby: "注音", header: "页眉", footer: "页脚", other: "其他", unknown: "未知" };
