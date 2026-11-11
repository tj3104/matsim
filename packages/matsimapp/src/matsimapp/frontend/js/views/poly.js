// 配位多面体の画面。中心の元素 (Si など) のまわりの配位する元素 (O など) で作る多面体を検出し、形 (四面体・八面体…)・
// 配位数・つながり方 (頂点・辺・面共有、Qⁿ)・理想形からのずれを、グラフ・表・3D 表示 (多面体を半透明に描く)・時間変化で見せる。
// 解析は「解析のジョブ」(#/analysis/poly?task=番号)。
import {CATEGORY_COLORS, drawBars, drawCategoryLines, purgeChart} from "../charts.js";
import {api, busy, el, field, formatDuration, number, toast} from "../dom.js";
import {navigate} from "../main.js";
import {isActive, progressBar, progressText, statusChip} from "./jobs.js";
import {viewerCard} from "./structures.js";
import {elementColor} from "../viewer.js";

const pct = (v) => `${(v * 100).toFixed(1)}%`;
// よく出る形は決まった色 (別の解析と見比べても同じ色)
const SHAPE_COLORS = {
  "四面体": "#2a78d6", "八面体": "#eb6834", "立方体": "#1baf7a", "平面四角形": "#8f5bd6", "平面三角形": "#d6457a",
  "三方両錐": "#16a3b8", "四角錐": "#eda100", "三角柱": "#7a9a01", "立方八面体 (fcc)": "#5467a8",
  "反立方八面体 (hcp)": "#c23b22", "正二十面体": "#b5651d", "直線": "#2e8b57", "正方逆柱": "#9c6ade",
};
const IDEAL = [
  ["直線 (2)", "180°"], ["平面三角形 (3)", "120°×3"], ["三角錐 (3)", "109.5°×3"], ["四面体 (4)", "109.5°×6"],
  ["平面四角形 (4)", "90°×4・180°×2"], ["シーソー (4)", ""], ["三方両錐 (5)", "90°・120°・180°"], ["四角錐 (5)", "90°・180°"],
  ["八面体 (6)", "90°×12・180°×3"], ["三角柱 (6)", ""], ["五方両錐 (7)", ""], ["一冠八面体 (7)", ""], ["立方体 (8)", "70.5°・109.5°・180°"],
  ["正方逆柱 (8)", ""], ["三冠三角柱 (9)", ""], ["立方八面体 (12)", "fcc の第 1 近接"], ["反立方八面体 (12)", "hcp の第 1 近接"],
  ["正二十面体 (12)", "液体・金属ガラス"],
];

export async function renderPoly(view, query) {
  const taskId = Number(query.get("task")) || null;
  let jobId = Number(query.get("job")) || null;
  let structureId = Number(query.get("structure")) || null;
  const settings = {frames: 40, center: null, ligand: null, cutoff: null};
  let task = null;
  if (taskId) {
    task = await api(`/api/jobs/${taskId}/poly`);
    jobId = task.params.job_id;
    structureId = task.params.structure_id;
    for (const key of Object.keys(settings)) if (key in task.params) settings[key] = task.params[key];
  }
  if (!jobId && !structureId) throw new Error("解析するジョブか構造を選んでください");
  if (!jobId) settings.frames = 0;
  const back = jobId ? `#/jobs/${jobId}` : `#/structures/${structureId}`;
  let stopped = false;
  let timer = null;
  let result = null;
  let hideOthers = false;
  const drawn = new Set();
  const charts = {types: el("div", {class: "chart"}), cn: el("div", {class: "chart"}), share: el("div", {class: "chart"}),
                  qn: el("div", {class: "chart"}), rms: el("div", {class: "chart"}), time: el("div", {class: "chart"})};
  const card = viewerCard("配位多面体");
  card.caption.textContent = "多面体を形ごとの色で半透明に描きます。ドラッグで回転、ピンチ・ホイールで拡大縮小";
  const chips = el("div", {class: "chips"});
  const legend = el("p", {class: "note"});
  const resultSlot = el("div", {class: "stack"});
  const colorOf = (name) => {
    if (SHAPE_COLORS[name]) return SHAPE_COLORS[name];
    if (name.includes("不規則") || name === "配位なし") return "#9ca3af";
    const k = result.types.findIndex((t) => t.name === name);
    return CATEGORY_COLORS[(k + 5) % CATEGORY_COLORS.length];
  };

  function paint() {
    card.viewer.setPolyhedra(result.polyhedra.filter((p) => drawn.has(p.shape) && p.faces.length)
      .map((p) => ({faces: p.faces, color: colorOf(p.shape)})));
    // 中心の原子は形の色、配位する原子は元素の色。それ以外は元素の色 (隠すなら隠す)。描かない形の中心は灰色
    const stride = result.view_stride || 1;
    const colors = result.view.symbols.map((sym, i) => {
      const shape = result.center_shape[String(i * stride)];
      if (shape !== undefined) return drawn.has(shape) ? colorOf(shape) : null;
      if (sym === result.ligand || result.ligand === "*") return elementColor(sym);
      return hideOthers ? null : elementColor(sym);
    });
    card.viewer.setColors(colors, {hideOthers});
    const count = result.polyhedra.filter((p) => drawn.has(p.shape)).length;
    legend.textContent = `${number(count)} 個の多面体を描いています` + (result.drawn < result.n_polyhedra
      ? ` (多いので ${number(result.n_polyhedra)} 個から ${number(result.drawn)} 個を選んで描いています)` : "");
  }

  function renderChips() {
    chips.replaceChildren(...result.types.map((t) => {
      const on = drawn.has(t.name);
      return el("button", {class: `chip chip-2${on ? " chip-on" : ""}`, type: "button", "aria-pressed": String(on), onclick: () => {
        if (on) drawn.delete(t.name); else drawn.add(t.name);
        renderChips();
        paint();
      }}, el("span", {class: "legend-swatch", style: `background:${colorOf(t.name)}`, "aria-hidden": "true"}),
      el("span", {class: "chip-text"}, el("span", {text: `${t.name} ${pct(t.fraction)}`}),
         el("small", {text: `配位数 ${t.cn}・${number(t.count)} 個`})));
    }));
  }

  async function draw() {
    await drawBars(charts.types, {labels: result.types.map((t) => t.name), values: result.types.map((t) => t.fraction),
                                  colors: result.types.map((t) => colorOf(t.name)), yLabel: "比率", percent: true, key: "poly-types"});
    await drawBars(charts.cn, {labels: result.cn_distribution.map((c) => String(c.cn)), values: result.cn_distribution.map((c) => c.fraction),
                               xLabel: "配位数", yLabel: "比率", percent: true, key: "poly-cn"});
    if (Object.keys(result.sharing).length) {
      await drawBars(charts.share, {labels: Object.keys(result.sharing), values: Object.values(result.sharing),
                                    colors: ["#2a78d6", "#eb6834", "#1baf7a"], yLabel: "組の数", key: "poly-share"});
      await drawBars(charts.qn, {labels: result.qn.map((q) => `Q${q.n}`), values: result.qn.map((q) => q.fraction),
                                 xLabel: "つながっている多面体の数", yLabel: "比率", percent: true, key: "poly-qn"});
    }
    const h = result.rms_hist;
    await drawBars(charts.rms, {labels: h.counts.map((_, k) => `${h.edges[k]}〜${h.edges[k + 1]}`), values: h.counts,
                                xLabel: "理想形からのずれ (度)", yLabel: "多面体の数", key: "poly-rms"});
    const evo = result.evolution;
    if (evo && evo.time_ps.length > 1) {
      const hasTime = evo.time_ps.every((t) => t !== null);
      await drawCategoryLines(charts.time, {x: hasTime ? evo.time_ps : evo.frame, xLabel: hasTime ? "時間 (ps)" : "フレーム",
        yLabel: "比率", percent: true, key: "poly-time",
        series: Object.entries(evo.fractions).map(([name, values]) => ({name, values, color: colorOf(name)}))});
    }
  }

  function renderResult() {
    for (const [select, value, auto] of [[centerSelect, result.center, false], [ligandSelect, result.ligand, true]]) {
      select.replaceChildren(...[...(auto ? ["*"] : []), ...result.species].map((s) => el("option",
        {value: s, selected: s === value, text: s === "*" ? "すべての元素" : s})));
    }
    settings.center = result.center;
    settings.ligand = result.ligand;
    const sharing = Object.keys(result.sharing).length > 0;
    const evo = result.evolution;
    const fmt = (v, unit, digits = 1) => (v === null || v === undefined ? "—" : `${v.toFixed(digits)}${unit}`);
    resultSlot.replaceChildren(...[
      el("section", {class: "card"},
        el("div", {class: "card-head"}, el("h2", {text: "配位多面体の形"})),
        el("dl", {class: "facts"},
          el("dt", {text: "中心 – 配位"}), el("dd", {text: `${result.center} – ${result.ligand === "*" ? "すべての元素" : result.ligand}`}),
          el("dt", {text: "配位と数える距離"}), el("dd", {text: `${result.cutoff.toFixed(2)} Å` + (result.cutoff_auto ? " (g(r) の第 1 の谷から自動)" : " (指定)")}),
          el("dt", {text: "多面体の数"}), el("dd", {text: `${number(result.n_polyhedra)} 個`}),
          el("dt", {text: "平均の配位数"}), el("dd", {class: "value", text: result.mean_cn.toFixed(2)}),
          el("dt", {text: "理想形からのずれ (平均)"}), el("dd", {text: fmt(result.mean_rms, "°")}),
          el("dt", {text: "結合の長さのゆがみ (平均)"}), el("dd", {text: fmt(result.mean_distortion * 100, "%", 2)})),
        charts.types,
        el("div", {class: "table-scroll"}, el("table", {class: "table"},
          el("thead", {}, el("tr", {}, ["形", "配位数", "数", "比率", "ずれ", "長さのゆがみ"].map((h) => el("th", {text: h})))),
          el("tbody", {}, result.types.map((t) => el("tr", {},
            el("th", {}, el("span", {class: "legend-swatch", style: `background:${colorOf(t.name)}`, "aria-hidden": "true"}), ` ${t.name}`),
            el("td", {text: String(t.cn)}), el("td", {text: number(t.count)}), el("td", {text: pct(t.fraction)}),
            el("td", {text: fmt(t.rms, "°")}), el("td", {text: fmt(t.distortion * 100, "%", 2)}))))))),
      card.node,
      el("section", {class: "card"}, el("div", {class: "card-head"}, el("h2", {text: "描く形"})),
        el("div", {class: "button-row"},
          el("button", {class: "secondary small", type: "button", text: "すべて描く", onclick: () => {
            result.types.forEach((t) => drawn.add(t.name));
            renderChips();
            paint();
          }}),
          el("button", {class: "secondary small", type: "button", text: "描かない", onclick: () => { drawn.clear(); renderChips(); paint(); }})),
        el("label", {class: "check"}, el("input", {type: "checkbox", checked: hideOthers, onchange: (e) => {
          hideOthers = e.target.checked;
          paint();
        }}), "中心と配位の元素以外を隠す"),
        chips, legend),
      el("section", {class: "card"}, el("div", {class: "card-head"}, el("h2", {text: "配位数の分布"})), charts.cn),
      sharing ? el("section", {class: "card"}, el("div", {class: "card-head"}, el("h2", {text: "多面体のつながり方"})),
        el("p", {class: "note", text: "2 つの多面体が共有する配位原子の数で、頂点共有 (1)・辺共有 (2)・面共有 (3 以上)。"
          + "例: ペロブスカイトの TiO6 は頂点共有、岩塩型の MgO6 は辺共有、ケイ酸塩の SiO4 は頂点共有。"}),
        charts.share,
        el("h3", {class: "group-title", text: "つながっている多面体の数 (Qⁿ)"}),
        el("p", {class: "note", text: "多面体ごとに、配位原子を共有している別の多面体の数。SiO4 のケイ酸塩なら Qⁿ の n (架橋酸素の数) に当たります "
          + "(Q4 = 3 次元の網目、Q0 = 孤立した四面体)。"}),
        charts.qn) : null,
      el("section", {class: "card"}, el("div", {class: "card-head"}, el("h2", {text: "理想形からのずれ"})),
        el("p", {class: "note", text: "中心から見た配位原子どうしの角度の並びが、理想形の角度の並びと平均で何度ずれているか。"
          + `${15} 度より大きいものは「不規則」としています。熱ゆらぎだけでも数度はずれます。`}),
        charts.rms),
      evo && evo.time_ps.length > 1
        ? el("section", {class: "card"}, el("div", {class: "card-head"}, el("h2", {text: "形の割合の時間変化"})),
          el("p", {class: "note", text: `トラジェクトリの ${evo.time_ps.length} フレーム (配位と数える距離は最終構造で決めたもの)`}), charts.time)
        : el("section", {class: "card"}, el("p", {class: "note", text: jobId
          ? "時間変化は、トラジェクトリを保存したジョブで出せます。" : "登録済みの構造は 1 枚だけなので、時間変化はありません。"})),
      el("section", {class: "card"},
        el("details", {class: "reading"}, el("summary", {text: "形の判定のしかたと理想形"}),
          el("p", {text: "中心の原子から見た、配位原子どうしのなす角をすべて求めて小さい順に並べ、理想形の並びと比べて最も近い形にします。"
            + "角度の並びは多面体の向きや原子の番号によらないので、回転させて重ねる必要がありません。"}),
          el("div", {class: "table-scroll"}, el("table", {class: "table"},
            el("thead", {}, el("tr", {}, el("th", {text: "理想形 (配位数)"}), el("th", {text: "中心から見た角度"}))),
            el("tbody", {}, IDEAL.map(([name, angle]) => el("tr", {}, el("td", {text: name}), el("td", {text: angle})))))))),
    ].filter(Boolean));
    card.show(result.view);
    drawn.clear();
    result.types.forEach((t) => drawn.add(t.name));
    renderChips();
    paint();
    requestAnimationFrame(() => draw().catch(() => {}));
  }

  const centerSelect = el("select", {onchange: () => { settings.center = centerSelect.value; }},
    el("option", {value: "", text: settings.center || "自動"}));
  const ligandSelect = el("select", {onchange: () => { settings.ligand = ligandSelect.value; }},
    el("option", {value: "", text: settings.ligand ? (settings.ligand === "*" ? "すべての元素" : settings.ligand) : "自動"}));
  const cutoffInput = el("input", {type: "number", inputmode: "decimal", step: "any", min: "0.5", max: "10", placeholder: "自動",
                                   value: settings.cutoff ?? "", onchange: () => {
                                     settings.cutoff = cutoffInput.value === "" ? null : Number(cutoffInput.value);
                                   }});
  const framesInput = el("input", {type: "number", inputmode: "numeric", min: "0", max: "500", step: "1", value: settings.frames,
                                   onchange: () => { settings.frames = Number(framesInput.value); }});
  const run = el("button", {class: "primary wide", type: "button", text: taskId ? "この条件で解析し直す" : "解析を始める",
    onclick: () => busy(run, submit)});
  async function submit() {
    const {id, reused} = await api("/api/analysis/poly", {method: "POST", body: {
      job_id: jobId, structure_id: structureId, frames: settings.frames, cutoff: settings.cutoff,
      center: settings.center || null, ligand: settings.ligand || null}});
    toast(reused ? "同じ条件の解析を開きます" : "解析を始めました (ジョブの一覧にも出ます)");
    navigate(`#/analysis/poly?task=${id}`);
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
    task = await api(`/api/jobs/${taskId}/poly`);
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
    el("h1", {class: "page-title", text: "配位多面体"}),
    el("p", {class: "note", text: "中心の原子 (Si・Ti など) のまわりの配位原子 (O など) がつくる多面体を見つけ、形 (四面体・八面体など)・"
      + "つながり方・ゆがみを調べます。酸化物・ガラス・イオン伝導体の構造の解析に使えます。"}),
    el("section", {class: "card"},
      el("div", {class: "card-head"}, el("h2", {text: "条件"})),
      el("div", {class: "grid-2"}, field("中心の元素", centerSelect), field("配位する元素", ligandSelect)),
      el("p", {class: "note", text: "自動: O などの陰イオンがあれば、それを配位する元素に、ほかの元素を中心にします (なければすべての元素)。"
        + "解析のあとは、構造の元素から選び直せます。"}),
      field("配位と数える距離 (Å)", cutoffInput, "空欄なら、中心と配位の組み合わせの g(r) の第 1 の谷から自動で決めます"),
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
