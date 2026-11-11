// アレニウス解析の画面。拡散・イオン伝導のジョブを選び、1000/T に対する ln D・ln(σT) を直線で当てはめて
// 活性化エネルギーを出す。温度スキャン (series) で投入したジョブは、実行中でも終わったものから解析する。
import {drawArrhenius, purgeChart} from "../charts.js";
import {api, el, field, formatTime} from "../dom.js";
import {isActive, statusChip} from "./jobs.js";

const fmt = (value, digits = 3) => (value === null || value === undefined ? "—"
  : Math.abs(value) >= 1e4 || Math.abs(value) < 1e-2 ? value.toExponential(digits - 1) : value.toPrecision(digits));

function yLabel(target, sigmaMode) {
  if (target.name.startsWith("sigma:")) return sigmaMode === "sigma_t" ? "ln(σT)  (σ: mS/cm, T: K)" : "ln σ  (mS/cm)";
  return "ln D  (D: cm²/s)";
}

function prefactorText(target, sigmaMode) {
  const fit = target.fit;
  if (target.name.startsWith("sigma:")) {
    return sigmaMode === "sigma_t" ? `σ₀ = ${fmt(fit.prefactor)} mS·K/cm` : `σ₀ = ${fmt(fit.prefactor)} mS/cm`;
  }
  return `D₀ = ${fmt(fit.prefactor)} cm²/s`;
}

export async function renderArrhenius(view, query) {
  const all = (await api("/api/jobs")).filter((job) => job.kind === "diffusion");
  const series = query.get("series");
  const fromIds = (query.get("jobs") || "").split(",").map(Number).filter(Boolean);
  const selected = new Set(series ? all.filter((j) => j.series === series).map((j) => j.id) : fromIds);
  const excluded = new Set();   // 外した点のジョブ
  const included = new Set();   // 「拡散していない」と判定されたが、あえて使う点のジョブ
  const settings = {temperature: "measured", sigmaMode: "sigma_t"};
  let jobs = all;
  let charts = [];
  let lastKey = "";
  let stopped = false;

  const jobsSlot = el("div", {});
  const resultSlot = el("div", {class: "stack"});
  const csvLink = el("a", {class: "secondary", text: "CSV で保存", download: "arrhenius.csv"});

  function params() {
    // 解析に送るのは完了したジョブだけ (実行中のものは終わってから加える)
    const ready = jobs.filter((j) => selected.has(j.id) && j.status === "done").map((j) => j.id);
    const p = new URLSearchParams({jobs: ready.join(","), excluded: [...excluded].join(","), included: [...included].join(","),
                                   temperature: settings.temperature, sigma_mode: settings.sigmaMode});
    return p.toString();
  }

  function jobRow(job) {
    const done = job.status === "done";
    const box = el("input", {type: "checkbox", checked: selected.has(job.id), disabled: !done && !selected.has(job.id),
      onchange: () => {
        if (box.checked) selected.add(job.id); else selected.delete(job.id);
        analyze();
      }});
    const temperature = job.summary.temperature ? `${job.summary.temperature} K ・ ` : "";
    return el("label", {class: "check pick-row"}, box,
      el("span", {class: "pick-main"},
        el("span", {class: "list-title", text: job.name}),
        el("span", {class: "list-sub", text: `#${job.id} ・ ${temperature}${job.summary.structure || ""} ・ ${formatTime(job.created_at)}`})),
      statusChip(job));
  }

  function renderJobs() {
    // 選んだジョブ (とシリーズの仲間) を先に、残りの拡散ジョブを後に並べる
    const first = jobs.filter((j) => selected.has(j.id) || (series && j.series === series));
    const rest = jobs.filter((j) => !first.includes(j));
    const running = first.filter(isActive).length;
    // replaceChildren は null を文字として入れてしまうので、出さない部品は除いてから渡す
    jobsSlot.replaceChildren(...[
      first.length ? el("div", {class: "pick-list"}, first.map(jobRow))
        : el("p", {class: "note", text: "下の一覧から、温度の違う拡散・イオン伝導のジョブを 2 つ以上選んでください。"}),
      running ? el("p", {class: "note", role: "status", text: `${running} 件が実行中・実行待ちです。終わったものから自動で解析に加えます。`}) : null,
      rest.length ? el("details", {class: "more-jobs", open: !first.length},
        el("summary", {text: `ほかの拡散・イオン伝導のジョブ (${rest.length})`}),
        el("div", {class: "pick-list"}, rest.map(jobRow))) : null,
      !jobs.length ? el("p", {class: "empty", text: "拡散・イオン伝導のジョブがまだありません。"}) : null,
    ].filter(Boolean));
  }

  function targetCard(target, index) {
    const fit = target.fit;
    const chart = el("div", {class: "chart"});
    charts.push(chart);
    const rows = target.points.map((point) => {
      const box = el("input", {type: "checkbox", checked: point.used, disabled: point.ln === null,
        "aria-label": `${point.temperature.toFixed(1)} K の点を使う`,
        onchange: () => {
          if (box.checked) {
            excluded.delete(point.job_id);
            if (point.slow) included.add(point.job_id);
          } else {
            excluded.add(point.job_id);
            included.delete(point.job_id);
          }
          analyze();
        }});
      return el("tr", {},
        el("td", {}, box), el("td", {text: point.temperature.toFixed(1)}),
        el("td", {text: point.inv_t ? point.inv_t.toFixed(4) : "—"}),
        el("td", {text: fmt(point.value) + (point.slow ? " *" : "")}),
        el("td", {text: `#${point.job_id}`}));
    });
    const facts = fit ? [
      ["活性化エネルギー Ea", `${fit.ea_ev.toFixed(3)}${fit.ea_err !== null ? ` ± ${fit.ea_err.toFixed(3)}` : ""} eV`],
      ["前指数因子", prefactorText(target, settings.sigmaMode)],
      ["R²", fit.r2.toFixed(4)],
      [`${result.room} K の値 (外挿)`, `${fmt(fit.room)} ${target.unit}`],
      ["使った点", `${fit.n_points} 点 (${fit.t_range[0].toFixed(0)}〜${fit.t_range[1].toFixed(0)} K)`],
    ] : [];
    const node = el("section", {class: "card"},
      el("div", {class: "card-head"}, el("h2", {text: target.label})),
      fit ? el("dl", {class: "facts"}, facts.map(([k, v]) => [el("dt", {text: k}), el("dd", {class: "value", text: v})])) : null,
      chart,
      target.notes.map((text) => el("p", {class: "note caution", text})),
      el("div", {class: "table-scroll"}, el("table", {class: "table"},
        el("thead", {}, el("tr", {}, ["使う", "温度 (K)", "1000/T", `値 (${target.unit})`, "ジョブ"].map((h) => el("th", {text: h})))),
        el("tbody", {}, rows))),
      target.points.some((p) => p.slow) ? el("p", {class: "field-hint", text: "* ほとんど拡散していない点 (既定で外しています)"}) : null);
    const points = target.points.filter((p) => p.ln !== null).map((p) => ({
      x: p.inv_t, y: p.ln, temperature: p.temperature, used: p.used, label: p.name}));
    requestAnimationFrame(() => drawArrhenius(chart, {points, fit, yLabel: yLabel(target, settings.sigmaMode),
                                                      key: `${target.name}-${index}`}).catch(() => {}));
    return node;
  }

  let result = null;
  async function analyze() {
    renderJobs();
    charts.forEach(purgeChart);
    charts = [];
    const ready = jobs.filter((j) => selected.has(j.id) && j.status === "done");
    if (!ready.length) {
      resultSlot.replaceChildren();
      csvLink.hidden = true;
      return;
    }
    csvLink.hidden = false;
    csvLink.href = `/api/arrhenius/csv?${params()}`;
    try {
      result = await api(`/api/arrhenius?${params()}`);
    } catch (error) {
      resultSlot.replaceChildren(el("section", {class: "card problem"}, el("p", {class: "error", text: error.message})));
      return;
    }
    if (stopped) return;
    resultSlot.replaceChildren(
      ...result.notes.map((text) => el("p", {class: "note caution", text})),
      ...result.targets.map(targetCard));
  }

  const temperature = el("select", {onchange: () => { settings.temperature = temperature.value; analyze(); }},
    el("option", {value: "measured", text: "本計算の実測の平均温度"}), el("option", {value: "set", text: "設定した温度"}));
  const sigmaMode = el("select", {onchange: () => { settings.sigmaMode = sigmaMode.value; analyze(); }},
    el("option", {value: "sigma_t", text: "ln(σT) で当てはめる (標準)"}), el("option", {value: "sigma", text: "ln σ で当てはめる"}));

  view.replaceChildren(
    el("a", {class: "back-link", href: "#/jobs", text: "‹ ジョブ"}),
    el("h1", {class: "page-title", text: "アレニウス解析"}),
    el("p", {class: "note", text: "温度の違う拡散・イオン伝導のジョブから、1000/T に対して ln D と ln(σT) を直線で当てはめ、"
      + "傾きから活性化エネルギー Ea を求めます。表のチェックを外すと、その点を除いて当てはめ直します (融解・相転移した温度など)。"}),
    el("section", {class: "card"}, el("div", {class: "card-head"}, el("h2", {text: "使うジョブ"})), jobsSlot),
    el("details", {class: "card"}, el("summary", {text: "解析の条件"}),
      field("温度", temperature, "実測は本計算 (最後のステージ) の平均。読めないジョブは設定値を使います"),
      field("イオン伝導度", sigmaMode)),
    resultSlot,
    el("div", {class: "button-row"}, csvLink));
  await analyze();

  // 実行中のジョブがあるあいだは見に行き、終わったジョブが増えたら解析し直す
  const timer = setInterval(async () => {
    const watching = jobs.filter((j) => (selected.has(j.id) || (series && j.series === series)) && isActive(j));
    if (!watching.length) return;
    try {
      jobs = (await api("/api/jobs")).filter((job) => job.kind === "diffusion");
    } catch { return; }
    if (series) for (const j of jobs) if (j.series === series && j.status === "done") selected.add(j.id);
    const key = jobs.map((j) => `${j.id}:${j.status}`).join(",");
    if (key !== lastKey) {
      lastKey = key;
      await analyze();
    }
  }, 5000);
  return () => {
    stopped = true;
    clearInterval(timer);
    charts.forEach(purgeChart);
  };
}
