// Read-only locked wording, proposal queue and explicit decision conflicts.
import { request } from "./api.js";
import { badge, empty, h, keyValues, loading, notice, replace, select } from "./ui.js";

function entry(row) {
  return h("article", { class: "card" },
    h("h3", {}, row.source_form, " → ", row.target_form || "译法待定"),
    badge(row.status === "confirmed" ? "已确认" : "提案，待裁定", row.status === "confirmed" ? "ok" : "warn"),
    keyValues([["类型", row.kind], ["适用条件", row.conditions || "未注明"],
      ["变体", row.variants_json], ["出处与出现次数", row.sources_json], ["裁定说明", row.decision_note || "未注明"]]));
}

export async function render(container, app, current) {
  const output = h("section", { "aria-label": "词表内容" });
  container.append(h("h1", {}, "词表"),
    notice("此页只读。提案不属于确认清单；适用语境、普通同形词和范围完整性仍需人工复核。"), output);
  replace(output, loading("正在读取锁定词表…"));
  try {
    const data = await request("/api/glossary", { params: { corpus: app.corpus().id, version: app.route.params.version || "" } });
    if (!current()) return;
    if (!data.available) {
      replace(output, notice(data.message));
      return;
    }
    const versions = data.versions.map((v) => [v.version, v.version + " · " + v.entry_count + " 项"]);
    replace(output,
      versions.length ? select(versions, data.selected.version,
        (version) => app.go("glossary", { version }), "锁定版本") : empty("尚无锁定版本。"),
      data.selected ? keyValues([["锁定时间", data.selected.locked_at], ["条目数", data.selected.entry_count],
        ["SHA-256", h("span", { class: "mono" }, data.selected.sha256)]]) : null,
      h("h2", {}, "所选版本的完整确认清单"),
      data.entries.length ? data.entries.map(entry) : empty("尚无确认快照。"),
      h("h2", {}, "当前提案队列"),
      data.proposals.length ? data.proposals.map(entry) : empty("没有待裁定提案。"),
      h("h2", {}, "当前冲突与替代链"),
      data.conflicts.length ? data.conflicts.map((c) => h("article", { class: "card" },
        c.code === "multiple_confirmed" ? notice("同一原文有多个确认项：" + c.source_form, "warn")
          : h("p", { class: "mono" }, c.id + " → " + c.superseded_by),
        h("p", {}, c.decision_note || (c.ids || []).join("、")))) : empty("没有冲突或替代链。"));
  } catch (error) {
    if (current()) replace(output, notice(error.message, "error"));
  }
}
