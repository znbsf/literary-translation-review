// 复核: every human label saved for this corpus, whether it still matches the data, and export.
import { request } from "./api.js";
import { SOURCES, VERDICTS, VERDICT_TONE, label } from "./labels.js";
import { badge, empty, h, loading, meter, notice, num, replace, section, segmented, time } from "./ui.js";

export async function render(container, app) {
  const corpus = app.corpus();
  if (!app.overview.reviews_enabled) {
    container.append(notice("未配置复核目录（review_dir），只能浏览。", "info"));
    return;
  }
  const body = h("div", {}, loading());
  container.append(section("复核标签", body, { actions: h("span", { class: "muted" }, "标签保存在库外，不改变语料库或候选资格。") }));
  let data;
  try {
    data = await request("/api/reviews", { params: { corpus: corpus.id } });
  } catch (error) {
    replace(body, notice(error.message, "error"));
    return;
  }
  const reviews = data.reviews || [];
  if (!reviews.length) {
    replace(body, empty("还没有复核标签。在“对白”中打开一条记录即可保存。"));
    return;
  }
  const filter = { verdict: app.route.params.verdict || "", stale: app.route.params.stale === "1" };
  const counts = Object.fromEntries(Object.keys(VERDICTS).map((v) => [v, reviews.filter((r) => r.verdict === v).length]));
  const stale = reviews.filter((r) => r.stale).length;
  const list = h("div", {});
  const draw = () => {
    const shown = reviews.filter((r) => (!filter.verdict || r.verdict === filter.verdict) && (!filter.stale || r.stale));
    replace(list, shown.length ? h("div", { class: "table-wrap" }, h("table", { class: "table" },
      h("thead", {}, h("tr", {}, ["判定", "记录", "来源", "说话人 / 对象", "备注", "更新", ""].map((t) => h("th", {}, t)))),
      h("tbody", {}, shown.map((r) => h("tr", {},
        h("td", {}, badge(label(VERDICTS, r.verdict), VERDICT_TONE[r.verdict]), r.stale ? badge("已过期", "warn") : null),
        h("td", { class: "mono" }, h("a", { href: app.href("dialogues", { source: r.source, id: r.dialogue_id }) }, r.dialogue_id)),
        h("td", {}, label(SOURCES, r.source)),
        h("td", {}, [r.speaker, r.addressee].filter(Boolean).join(" → ") || "—"),
        h("td", { class: "note" }, r.note || "—"),
        h("td", { class: "muted" }, time(r.updated_at)),
        h("td", { class: "muted num" }, `v${r.version}`)))))) : empty("没有符合条件的标签。"));
  };
  replace(body,
    meter(Object.entries(counts).map(([key, value]) => ({ label: VERDICTS[key], value, tone: VERDICT_TONE[key] })),
      reviews.length, { caption: `共 ${num(reviews.length)} 条，其中 ${num(stale)} 条因数据变化而过期` }),
    h("div", { class: "filter-row" },
      segmented([["", "全部"], ...Object.entries(VERDICTS)], filter.verdict, (v) => {
        filter.verdict = v; app.remember({ verdict: v }); draw();
        for (const b of body.querySelectorAll(".segmented button")) b.setAttribute("aria-checked", String(b.textContent === (VERDICTS[v] || "全部")));
      }, "判定"),
      h("label", { class: "check" }, h("input", { type: "checkbox", checked: filter.stale, onchange: (e) => {
        filter.stale = e.target.checked; app.remember({ stale: filter.stale ? "1" : "" }); draw();
      } }), "只看过期"),
      h("button", { type: "button", class: "button ghost", onclick: () => download(corpus.id, data) }, "导出 JSON")),
    list);
  draw();
}

function download(corpusId, data) {
  const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
  const link = h("a", { href: URL.createObjectURL(blob), download: `reviews-${corpusId}.json` });
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(link.href), 1000);
}
