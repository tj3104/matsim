// 化学的短範囲秩序 (ウォーレン・カウリーの秩序度) の画面。殻ごとの α_ij を、色付きの表・殻ごとの変化・g(r) と殻の境目・
// 原子ごとの局所的な秩序度の 3D の色塗り・時間変化で見せる。解析は「解析のジョブ」(#/analysis/sro?task=番号)。
import {drawBars, drawCategoryLines, drawRdf, purgeChart} from "../charts.js";
import {api, busy, el, field, formatDuration, number, toast} from "../dom.js";
import {navigate} from "../main.js";
import {isActive, progressBar, progressText, statusChip} from "./jobs.js";
import {viewerCard} from "./structures.js";

const fixed = (v, digits = 3) => (v === null || v === undefined ? "—" : (v > 0 ? "+" : "") + v.toFixed(digits));

// α の色: 負は青、正は赤、0 は白 (|α| = 1 で最も濃い)。3D 表示用に "#rrggbb" で返す
function alphaColor(a) {
  const t = Math.max(-1, Math.min(1, a ?? 0));
  const mix = (from, to) => Math.round(from + (to - from) * Math.abs(t));
  const [r, g, b] = t < 0 ? [mix(240, 37), mix(240, 99), mix(240, 235)] : [mix(240, 220), mix(240, 38), mix(240, 38)];
  return `#${[r, g, b].map((v) => v.toString(16).padStart(2, "0")).join("")}`;
}

export async function renderSro(view, query) {
  const taskId = Number(query.get("task")) || null;
  let jobId = Number(query.get("job")) || null;
  let structureId = Number(query.get("structure")) || null;
  const settings = {frames: 40};
  let task = null;
  if (taskId) {
    task = await api(`/api/jobs/${taskId}/sro`);
    jobId = task.params.job_id;
    structureId = task.params.structure_id;
    settings.frames = task.params.frames;
  }
  if (!jobId && !structureId) throw new Error("解析するジョブか構造を選んでください");
  if (!jobId) settings.frames = 0;
  const back = jobId ? `#/jobs/${jobId}` : `#/structures/${structureId}`;
  let stopped = false;
  let timer = null;
  let result = null;
  let shell = 0;
  let only = "*";
  const charts = {bars: el("div", {class: "chart"}), shells: el("div", {class: "chart"}), rdf: el("div", {class: "chart"}),
                  time: el("div", {class: "chart"})};
  const card = viewerCard("局所的な秩序度で色塗り");
  card.caption.textContent = "原子ごとの第 1 殻の秩序度: 青 = 違う元素に囲まれている、赤 = 同じ元素に囲まれている、白 = ランダム程度";
  const tableSlot = el("div", {class: "table-scroll"});
  const shellTabs = el("div", {class: "segmented", role: "group", "aria-label": "殻"});
  const onlySelect = el("select", {onchange: () => { only = onlySelect.value; paint(); }});
  const legend = el("p", {class: "note"});
  const resultSlot = el("div", {class: "stack"});
  const unlike = () => result.species.flatMap((a, i) => result.species.slice(i + 1).map((b) => [a, b]));

  function paint() {
    const colors = result.view_local.map((v, i) => (only === "*" || result.view.symbols[i] === only ? alphaColor(v) : null));
    card.viewer.setColors(colors, {hideOthers: only !== "*"});
    const shown = colors.filter(Boolean).length;
    legend.textContent = `表示中の ${number(shown)} 原子に色を付けています` + (result.view.shown < result.view.n_atoms ? " (原子が多いので間引いて表示)" : "");
  }

  function renderTable() {
    const sh = result.shells[shell];
    const sp = result.species;
    tableSlot.replaceChildren(el("table", {class: "table sro-table"},
      el("thead", {}, el("tr", {}, el("th", {text: "中心 ＼ となり"}), ...sp.map((b) => el("th", {text: b})))),
      el("tbody", {}, sp.map((a) => el("tr", {}, el("th", {text: a}), ...sp.map((b) => {
        const v = sh.alpha[a][b];
        const sigma = sh.sigma[a][b];
        const weak = v === null || Math.abs(v) < 2 * (sigma || 0);
        return el("td", {class: weak ? "sro-cell weak" : "sro-cell", style: `background:${alphaColor(v)}`},
          el("span", {class: "sro-value", text: fixed(v)}), el("small", {text: `±${(sigma ?? 0).toFixed(3)}`}));
      }))))));
  }

  function renderTabs() {
    shellTabs.replaceChildren(...result.shells.map((sh, k) => el("button", {type: "button", class: k === shell ? "seg-on" : "",
      "aria-pressed": String(k === shell), text: `第 ${sh.shell} 殻`, onclick: () => { shell = k; renderTabs(); renderTable(); draw(); }})));
  }

  async function draw() {
    const sh = result.shells[shell];
    const pairs = unlike();
    await drawBars(charts.bars, {labels: pairs.map(([a, b]) => `${a}–${b}`), values: pairs.map(([a, b]) => sh.alpha[a][b]),
                                 colors: pairs.map(([a, b]) => alphaColor(sh.alpha[a][b])), yLabel: `α (第 ${sh.shell} 殻)`,
                                 key: "sro-bars"});
    await drawCategoryLines(charts.shells, {x: result.shells.map((s) => s.shell), xLabel: "殻 (第 k 近接)", yLabel: "α",
      key: "sro-shells", series: pairs.map(([a, b]) => ({name: `${a}–${b}`, values: result.shells.map((s) => s.alpha[a][b])}))});
    await drawRdf(charts.rdf, {r: result.rdf.r, series: [{name: "全体", values: result.rdf.total}], key: "sro-rdf",
                               cutoffs: result.shells.map((s) => ({x: s.outer, label: `第 ${s.shell} 殻まで ${s.outer.toFixed(2)} Å`}))});
    const evo = result.evolution;
    if (evo && evo.time_ps.length > 1) {
      const hasTime = evo.time_ps.every((t) => t !== null);
      await drawCategoryLines(charts.time, {x: hasTime ? evo.time_ps : evo.frame, xLabel: hasTime ? "時間 (ps)" : "フレーム",
        yLabel: "α (第 1 殻)", key: "sro-time",
        series: pairs.map(([a, b]) => ({name: `${a}–${b}`, values: evo.alpha[`${a}–${b}`]}))});
    }
  }

  function renderResult() {
    onlySelect.replaceChildren(...["*", ...result.species].map((s) => el("option", {value: s, text: s === "*" ? "すべての元素" : `${s} だけ`})));
    const evo = result.evolution;
    const conc = result.species.map((s) => `${s} ${(result.concentration[s] * 100).toFixed(1)}%`).join("、");
    resultSlot.replaceChildren(...[
      el("section", {class: "card"},
        el("div", {class: "card-head"}, el("h2", {text: "ウォーレン・カウリーの秩序度"})),
        el("dl", {class: "facts"},
          el("dt", {text: "原子の数"}), el("dd", {text: `${number(result.n_atoms)} 個`}),
          el("dt", {text: "組成"}), el("dd", {text: conc}),
          el("dt", {text: "殻"}), el("dd", {text: result.shells.map((s) => `第 ${s.shell} 殻 〜${s.outer.toFixed(2)} Å (${s.coordination.toFixed(1)} 個)`).join("、")})),
        el("p", {class: "note", text: "違う元素の組 (例: Cu–Ni) で読みます。負 (青) = 違う元素どうしが好まれる (規則化)、"
          + "0 = ランダム、正 (赤) = 同じ元素どうしが集まる (クラスター化・相分離)。同じ元素の組 (対角) は符号の意味が逆です。"}),
        shellTabs, tableSlot,
        el("p", {class: "note", text: "± は、ランダムな並びでも原子数が有限なために出るゆらぎの目安 (σ)。|α| が 2σ より小さいマスは薄くしています"
          + " (ランダムと区別できない)。"}),
        charts.bars),
      el("section", {class: "card"}, el("div", {class: "card-head"}, el("h2", {text: "殻ごとの変化"})),
        el("p", {class: "note", text: "規則合金では殻ごとに符号が入れ替わり (例: B2 の第 1 殻は負、第 2 殻は正)、相分離では遠くの殻まで正が続きます。"}),
        charts.shells),
      el("section", {class: "card"}, el("div", {class: "card-head"}, el("h2", {text: "動径分布関数と殻の境目"})),
        el("p", {class: "note", text: "殻の境目は g(r) の谷です。熱で山がつながると、殻は合わさります (bcc の第 1・第 2 近接など)。"}),
        charts.rdf),
      card.node,
      el("section", {class: "card"}, el("div", {class: "card-head"}, el("h2", {text: "色を付ける元素"})),
        field("元素", onlySelect, "1 つの元素にすると、その元素の原子だけを表示します"), legend),
      evo && evo.time_ps.length > 1
        ? el("section", {class: "card"}, el("div", {class: "card-head"}, el("h2", {text: "第 1 殻の α の時間変化"})),
          el("p", {class: "note", text: `トラジェクトリの ${evo.time_ps.length} フレーム。殻の境目はフレームごとに g(r) から決め直しています`}),
          charts.time)
        : el("section", {class: "card"}, el("p", {class: "note", text: jobId
          ? "時間変化は、トラジェクトリを保存したジョブで出せます。" : "登録済みの構造は 1 枚だけなので、時間変化はありません。"})),
    ].filter(Boolean));
    card.show(result.view);
    renderTabs();
    renderTable();
    paint();
    requestAnimationFrame(() => draw().catch(() => {}));
  }

  const framesInput = el("input", {type: "number", inputmode: "numeric", min: "0", max: "500", step: "1", value: settings.frames,
                                   onchange: () => { settings.frames = Number(framesInput.value); }});
  const run = el("button", {class: "primary wide", type: "button", text: taskId ? "この条件で解析し直す" : "解析を始める",
    onclick: () => busy(run, submit)});
  async function submit() {
    const {id, reused} = await api("/api/analysis/sro", {method: "POST", body: {job_id: jobId, structure_id: structureId,
                                                                              frames: settings.frames}});
    toast(reused ? "同じ条件の解析を開きます" : "解析を始めました (ジョブの一覧にも出ます)");
    navigate(`#/analysis/sro?task=${id}`);
  }

  const progressSlot = el("div", {});
  function renderProgress() {
    const job = task.job;
    const cancel = el("button", {class: "secondary danger small", type: "button", disabled: job.cancel_requested,
      text: job.status === "queued" ? "取り消す" : "中止する", onclick: () => busy(cancel, async () => {
        await api(`/api/jobs/${job.id}/cancel`, {method: "POST"});
        await poll();
      })});
    const remaining = job.status === "running" && job.steps_done > 0 && job.elapsed_seconds
      ? ` ・ 残り約 ${formatDuration(job.elapsed_seconds * (job.total_steps - job.steps_done) / job.steps_done)}` : "";
    progressSlot.replaceChildren(el("section", {class: "card"},
      el("div", {class: "card-head"}, el("h2", {text: "解析の進み具合"}), statusChip(job)),
      job.status === "running" ? progressBar(job) : null,
      el("p", {class: "note", role: "status", text: job.status === "queued" ? "順番を待っています" : `${progressText(job)}${remaining}`}),
      job.message ? el("p", {class: job.status === "failed" ? "error" : "note", text: job.message}) : null,
      isActive(job) ? el("div", {class: "button-row"}, cancel) : null));
  }
  async function poll() {
    task = await api(`/api/jobs/${taskId}/sro`);
    if (stopped) return;
    renderProgress();
    if (task.result && !result) {
      result = task.result;
      renderResult();
    }
    if (!isActive(task.job)) {
      clearInterval(timer);
      timer = null;
      if (task.job.status === "done") progressSlot.replaceChildren();
    }
  }

  view.replaceChildren(
    el("a", {class: "back-link", href: back, text: jobId ? "‹ ジョブに戻る" : "‹ 構造に戻る"}),
    el("h1", {class: "page-title", text: "短範囲秩序 (ウォーレン・カウリー)"}),
    el("p", {class: "note", text: "合金・化合物で、ある元素のまわりに別の元素がランダムより多いか少ないかを、近接の殻ごとに調べます。"
      + "規則化・クラスター化・相分離の始まりの判定に使います (元素が 2 種類以上の構造)。"}),
    el("section", {class: "card"},
      el("div", {class: "card-head"}, el("h2", {text: "条件"})),
      jobId ? field("時間変化に使うフレーム数", framesInput, "トラジェクトリから等間隔に選びます。0 で最終構造だけ") : null,
      run),
    progressSlot,
    resultSlot);
  if (taskId) {
    await poll();
    if (isActive(task.job)) timer = setInterval(() => poll().catch(() => {}), 1500);
  }
  return () => {
    stopped = true;
    if (timer) clearInterval(timer);
    Object.values(charts).forEach(purgeChart);
    card.viewer.destroy();
  };
}
