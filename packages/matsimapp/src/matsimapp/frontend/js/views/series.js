// 温度スキャンでまとめて投入したジョブ (シリーズ) の一覧と、結果の要点を温度に対して並べる画面。
// 融点 (固液共存法) のシリーズでは、判定から融点の推定も出す。拡散のシリーズはアレニウス解析の画面を使う。
import {drawXY, purgeChart} from "../charts.js";
import {api, el, field, number} from "../dom.js";
import {isActive, statusChip} from "./jobs.js";

function estimateCard(estimate) {
  if (!estimate) return null;
  return el("section", {class: "card"},
    el("div", {class: "card-head"}, el("h2", {text: "融点の推定"})),
    el("dl", {class: "facts"},
      el("dt", {text: "融点"}),
      el("dd", {class: "value", text: estimate.estimate === null || estimate.estimate === undefined ? "まだ決められません"
        : `${estimate.estimate.toFixed(0)} K${estimate.low !== undefined && estimate.low !== estimate.high
          ? ` (${estimate.low.toFixed(0)}〜${estimate.high.toFixed(0)} K)` : ""}`})),
    el("p", {class: "note", text: estimate.note}));
}

export async function renderSeries(view, series) {
  const chart = el("div", {class: "chart"});
  const listSlot = el("div", {});
  const tableSlot = el("div", {class: "table-scroll"});
  const estimateSlot = el("div", {});
  const pick = el("select", {onchange: () => draw()});
  let data = null;
  let lastKey = "";

  // 数値の要点 (すべての完了したジョブにあるもの) を、縦軸の候補にする
  function numericLabels() {
    const done = data.jobs.filter((j) => j.status === "done");
    const labels = [];
    for (const h of done[0]?.highlights || []) {
      if (typeof h.value === "number" && done.every((j) => j.highlights.some((x) => x.label === h.label))) labels.push(h);
    }
    return labels;
  }

  async function draw() {
    const label = pick.value;
    const done = data.jobs.filter((j) => j.status === "done" && j.temperature !== null).sort((a, b) => a.temperature - b.temperature);
    if (!label || !done.length) {
      purgeChart(chart);
      return;
    }
    const unit = done[0].highlights.find((h) => h.label === label)?.unit || "";
    await drawXY(chart, {x: done.map((j) => j.temperature), xLabel: "温度 (K)", markers: true, key: `series-${label}`,
                         series: [{values: done.map((j) => j.highlights.find((h) => h.label === label)?.value ?? null), name: label}],
                         yLabel: unit ? `${label} (${unit})` : label});
  }

  function render() {
    const sorted = [...data.jobs].sort((a, b) => (a.temperature ?? 0) - (b.temperature ?? 0));
    listSlot.replaceChildren(el("div", {class: "pick-list"}, sorted.map((job) =>
      el("a", {class: "check pick-row list-link", href: `#/jobs/${job.id}`},
        el("span", {class: "pick-main"},
          el("span", {class: "list-title", text: job.name}),
          el("span", {class: "list-sub", text: `#${job.id} ・ ${job.temperature ?? "—"} K ・ ${number(job.n_atoms)} 原子`})),
        statusChip(job)))));
    const labels = numericLabels();
    const current = pick.value;
    pick.replaceChildren(...labels.map((h) => el("option", {value: h.label, selected: h.label === current, text: h.label})));
    // 表: 温度ごとの要点 (文字の要点も含める)
    const done = sorted.filter((j) => j.status === "done");
    const columns = [];
    for (const job of done) for (const h of job.highlights) if (!columns.includes(h.label)) columns.push(h.label);
    tableSlot.replaceChildren(done.length ? el("table", {class: "table"},
      el("thead", {}, el("tr", {}, el("th", {text: "温度 (K)"}), columns.map((c) => el("th", {text: c})))),
      el("tbody", {}, done.map((job) => el("tr", {},
        el("th", {text: String(job.temperature ?? "—")}),
        columns.map((c) => {
          const h = job.highlights.find((x) => x.label === c);
          const value = h === undefined ? "—" : typeof h.value === "number" ? Number(h.value.toPrecision(5)).toString() : h.value;
          return el("td", {text: h && h.unit ? `${value} ${h.unit}` : value});
        })))))
      : el("p", {class: "note", text: "完了したジョブがまだありません。"}));
    estimateSlot.replaceChildren(...[estimateCard(data.estimate)].filter(Boolean));
    draw();
  }

  async function load() {
    data = await api(`/api/series/${encodeURIComponent(series)}`);
    const key = data.jobs.map((j) => `${j.id}:${j.status}:${j.highlights.length}`).join(",");
    if (key !== lastKey) {
      lastKey = key;
      render();
    }
  }

  // グラフは画面に置いてから描く (置く前だと幅を測れず、画面からはみ出す)
  data = await api(`/api/series/${encodeURIComponent(series)}`);
  view.replaceChildren(
    el("a", {class: "back-link", href: "#/jobs", text: "‹ ジョブ"}),
    el("h1", {class: "page-title", text: `温度スキャン: ${data.kind_label}`}),
    estimateSlot,
    el("section", {class: "card"}, el("div", {class: "card-head"}, el("h2", {text: "ジョブ"})), listSlot),
    el("section", {class: "card"}, el("div", {class: "card-head"}, el("h2", {text: "温度に対する変化"})),
      field("縦軸", pick), chart),
    el("section", {class: "card"}, el("div", {class: "card-head"}, el("h2", {text: "温度ごとの結果"})), tableSlot));
  lastKey = "";
  await load();
  const timer = setInterval(() => {
    if (data.jobs.some(isActive)) load().catch(() => {});
  }, 5000);
  return () => {
    clearInterval(timer);
    purgeChart(chart);
  };
}
