// 配位数の画面。ジョブの最終構造 (と時間変化) か登録済みの構造について、動径分布関数 g(r) の第 1 の谷までの
// 原子を数えた配位数を、g(r) (数えた距離の縦線つき)・分布・元素の組み合わせの表・3D の色塗り・時間変化で見せる。
// 解析は「解析のジョブ」としてサーバーで動く (#/analysis/coord?task=番号)。
import {drawBars, drawCategoryLines, drawRdf, purgeChart} from "../charts.js";
import {api, busy, el, field, formatDuration, number, toast} from "../dom.js";
import {navigate} from "../main.js";
import {isActive, progressBar, progressText, statusChip} from "./jobs.js";
import {viewerCard} from "./structures.js";

const ALL = "*";
const nameOf = (value) => (value === ALL ? "すべて" : value);
const fixed = (v, digits = 2) => (v === null || v === undefined || Number.isNaN(v) ? "—" : v.toFixed(digits));

// 配位数の色: 小さいほど青、大きいほど赤 (選んだ組み合わせに出てくる範囲で)。
// 3D 表示は "#rrggbb" から陰影を付けるので、HSL を 16 進に直して返す
function cnColor(cn, lo, hi) {
  const t = hi > lo ? (cn - lo) / (hi - lo) : 0.5;
  const h = (220 - 220 * t) / 360;
  const s = 0.7;
  const l = 0.5;
  const q = l + s - l * s;
  const p = 2 * l - q;
  const channel = (k) => {
    let x = h + k;
    if (x < 0) x += 1;
    if (x > 1) x -= 1;
    const v = x < 1 / 6 ? p + (q - p) * 6 * x : x < 1 / 2 ? q : x < 2 / 3 ? p + (q - p) * (2 / 3 - x) * 6 : p;
    return Math.round(v * 255).toString(16).padStart(2, "0");
  };
  return `#${channel(1 / 3)}${channel(0)}${channel(-1 / 3)}`;
}

export async function renderCoord(view, query) {
  const taskId = Number(query.get("task")) || null;
  let jobId = Number(query.get("job")) || null;
  let structureId = Number(query.get("structure")) || null;
  const settings = {frames: 40, cutoff: null, per_frame: true};
  let task = null;
  if (taskId) {   // 投入済みの解析: 条件を引き継ぐ
    task = await api(`/api/jobs/${taskId}/coord`);
    jobId = task.params.job_id;
    structureId = task.params.structure_id;
    settings.frames = task.params.frames;
    settings.cutoff = task.params.cutoff;
    settings.per_frame = task.params.per_frame ?? true;
  }
  if (!jobId && !structureId) throw new Error("解析するジョブか構造を選んでください");
  if (!jobId) settings.frames = 0;
  const back = jobId ? `#/jobs/${jobId}` : `#/structures/${structureId}`;
  let stopped = false;
  let timer = null;
  let result = null;
  let center = ALL;
  let neighbor = ALL;
  let hideOthers = false;
  const selected = new Set();
  const rdfChart = el("div", {class: "chart"});
  const distChart = el("div", {class: "chart"});
  const timeChart = el("div", {class: "chart"});
  const cutoffChart = el("div", {class: "chart"});
  const card = viewerCard("配位数で色塗り");
  card.caption.textContent = "下の一覧で配位数を選ぶと、その原子に色が付きます (少ないほど青、多いほど赤)。ドラッグで回転";
  const chips = el("div", {class: "chips"});
  const legend = el("p", {class: "note"});
  const meanText = el("dd", {class: "value"});
  const matrixSlot = el("div", {class: "table-scroll"});
  const centerSelect = el("select", {onchange: () => { center = centerSelect.value; resetSelection(); update(); }});
  const neighborSelect = el("select", {onchange: () => { neighbor = neighborSelect.value; resetSelection(); update(); }});
  const resultSlot = el("div", {class: "stack"});

  const dist = () => result.distribution[center][neighbor];
  const range = () => {
    const values = dist().map((d) => d.cn);
    return [Math.min(...values), Math.max(...values)];
  };
  // 3D 表示の原子ごとの配位数 (中心の元素でない原子は null)
  const viewCn = () => {
    const k = result.species.indexOf(neighbor);
    return result.view_atoms.map((row, i) => {
      if (center !== ALL && result.view.symbols[i] !== center) return null;
      return k < 0 ? row.reduce((a, b) => a + b, 0) : row[k];
    });
  };
  function resetSelection() {
    selected.clear();
    dist().forEach((d) => selected.add(d.cn));
  }

  function paint() {
    const [lo, hi] = range();
    const colors = viewCn().map((cn) => (cn !== null && selected.has(cn) ? cnColor(cn, lo, hi) : null));
    card.viewer.setColors(selected.size ? colors : null, {hideOthers});
    const n = colors.filter(Boolean).length;
    legend.textContent = selected.size
      ? `表示中の ${number(result.view.shown)} 原子のうち ${number(n)} 原子に色を付けています`
        + (result.view.shown < result.view.n_atoms ? " (原子が多いので間引いて表示)" : "")
      : "色塗りなし (元素の色)";
  }

  function renderChips() {
    const [lo, hi] = range();
    chips.replaceChildren(...dist().map((d) => {
      const on = selected.has(d.cn);
      return el("button", {class: `chip${on ? " chip-on" : ""}`, type: "button", "aria-pressed": String(on), onclick: () => {
        if (on) selected.delete(d.cn); else selected.add(d.cn);
        renderChips();
        paint();
      }}, el("span", {class: "legend-swatch", style: `background:${cnColor(d.cn, lo, hi)}`, "aria-hidden": "true"}),
      `${d.cn} 個 ${(d.fraction * 100).toFixed(1)}%`);
    }));
  }

  function renderMatrix() {
    const rows = [ALL, ...result.species];
    const cols = [ALL, ...result.species];
    matrixSlot.replaceChildren(el("table", {class: "table"},
      el("thead", {}, el("tr", {}, el("th", {text: "中心 ＼ となり"}), ...cols.map((c) => el("th", {text: nameOf(c)})))),
      el("tbody", {}, rows.map((r) => el("tr", {}, el("th", {text: nameOf(r)}),
        ...cols.map((c) => el("td", {class: r === center && c === neighbor ? "cell-on" : "", text: fixed(result.matrix[r][c])})))))));
  }

  async function draw() {
    const pairs = Object.entries(result.cutoffs);
    const series = [{name: "全体", values: result.rdf.total, width: result.species.length > 1 ? 1.5 : 2,
                     dash: result.species.length > 1 ? "dot" : "solid"}];
    for (const [pair, values] of Object.entries(result.rdf.partial)) series.push({name: pair.replace("-", "–"), values});
    await drawRdf(rdfChart, {r: result.rdf.r, series, key: "coord-rdf",
                             cutoffs: pairs.map(([pair, x]) => ({x, label: `${pair.replace("-", "–")} ${x.toFixed(2)} Å`}))});
    const [lo, hi] = range();
    await drawBars(distChart, {labels: dist().map((d) => String(d.cn)), values: dist().map((d) => d.fraction),
                               colors: dist().map((d) => cnColor(d.cn, lo, hi)), xLabel: "配位数", yLabel: "比率", percent: true,
                               key: "coord-dist"});
    const evo = result.evolution;
    if (evo && evo.time_ps.length > 1) {
      const hasTime = evo.time_ps.every((t) => t !== null);
      await drawCategoryLines(timeChart, {x: hasTime ? evo.time_ps : evo.frame, xLabel: hasTime ? "時間 (ps)" : "フレーム",
        yLabel: "平均の配位数", key: "coord-time",
        series: Object.entries(evo.mean).map(([name, values], i) => ({name, values, color: undefined, dash: i ? "solid" : "dot"}))});
      if (evo.per_frame) {
        await drawCategoryLines(cutoffChart, {x: hasTime ? evo.time_ps : evo.frame, xLabel: hasTime ? "時間 (ps)" : "フレーム",
          yLabel: "数えた距離 (Å)", key: "coord-cutoff",
          series: Object.entries(evo.cutoff).map(([pair, values]) => ({name: pair.replace("-", "–"), values, color: undefined}))});
      }
    }
  }

  function update() {
    meanText.textContent = `${fixed(result.matrix[center][neighbor])} 個`;
    renderMatrix();
    renderChips();
    paint();
    requestAnimationFrame(() => draw().catch(() => {}));
  }

  function renderResult() {
    const species = result.species;
    if (species.length > 1) {   // 化合物・合金: 既定は「1 つ目の元素のまわりの 2 つ目の元素」(Mg のまわりの O など)
      center = species[0];
      neighbor = species[1];
    }
    for (const [select, value] of [[centerSelect, center], [neighborSelect, neighbor]]) {
      select.replaceChildren(...[ALL, ...species].map((s) => el("option", {value: s, selected: s === value, text: nameOf(s)})));
    }
    const cutoffText = Object.entries(result.cutoffs).map(([pair, x]) => `${pair.replace("-", "–")} ${x.toFixed(2)} Å`).join("、");
    const evo = result.evolution;
    resultSlot.replaceChildren(...[   // replaceChildren は null を文字にしてしまうので取り除く
      el("section", {class: "card"},
        el("div", {class: "card-head"}, el("h2", {text: "配位数"})),
        el("dl", {class: "facts"},
          el("dt", {text: "原子の数"}), el("dd", {text: `${number(result.n_atoms)} 個`}),
          el("dt", {text: "数えた距離"}), el("dd", {text: cutoffText + (result.cutoff_auto ? " (g(r) の第 1 の谷から自動)" : " (指定)")}),
          el("dt", {text: "平均の配位数"}), meanText),
        species.length > 1 ? el("div", {class: "grid-2"},
          field("中心の元素", centerSelect), field("数えるとなりの元素", neighborSelect)) : null,
        species.length > 1 ? el("p", {class: "note", text: "「すべて」は、中心ならすべての原子、となりならすべての元素を数えます。"
          + "イオン結晶では、陽イオンのまわりの陰イオン (例: Mg のまわりの O) を見るのが一般的です。"}) : null,
        distChart),
      el("section", {class: "card"}, el("div", {class: "card-head"}, el("h2", {text: "動径分布関数 g(r)"})),
        el("p", {class: "note", text: "原子のまわりの距離 r に、平均に比べて何倍の原子があるか。点線の縦線の内側を「となり」として数えています。"
          + "山が第 1 配位殻、その外側の谷までが配位数です。"}), rdfChart),
      species.length > 1 ? el("section", {class: "card"}, el("div", {class: "card-head"}, el("h2", {text: "元素の組み合わせごとの平均"})),
        el("p", {class: "note", text: "行の元素 (中心) のまわりにある、列の元素 (となり) の平均の数。色の付いたマスが、上で選んだ組み合わせです。"}),
        matrixSlot) : null,
      card.node,
      el("section", {class: "card"}, el("div", {class: "card-head"}, el("h2", {text: "色を付ける配位数"})),
        el("div", {class: "button-row"},
          el("button", {class: "secondary small", type: "button", text: "すべて選ぶ", onclick: () => { resetSelection(); renderChips(); paint(); }}),
          el("button", {class: "secondary small", type: "button", text: "選択を解除", onclick: () => { selected.clear(); renderChips(); paint(); }})),
        el("label", {class: "check"}, el("input", {type: "checkbox", checked: hideOthers, onchange: (e) => {
          hideOthers = e.target.checked;
          paint();
        }}), "選んでいない原子を隠す"),
        chips, legend),
      evo && evo.time_ps.length > 1
        ? el("section", {class: "card"}, el("div", {class: "card-head"}, el("h2", {text: "平均の配位数の時間変化"})),
          el("p", {class: "note", text: `トラジェクトリの ${evo.time_ps.length} フレーム。` + (evo.per_frame
            ? "数える距離は、フレームごとにその時点の g(r) の谷から決め直しています (下のグラフ)。構造が変わると距離も変わります"
            : "数える距離は、最終構造で決めたもの (または指定した距離) をすべてのフレームに使っています")}),
          timeChart,
          evo.per_frame ? el("h3", {class: "group-title", text: "数えた距離の変化"}) : null,
          evo.per_frame ? cutoffChart : null)
        : el("section", {class: "card"}, el("p", {class: "note", text: jobId
          ? "時間変化は、トラジェクトリを保存したジョブで出せます (詳細設定のトラジェクトリの保存間隔)。"
          : "登録済みの構造は 1 枚だけなので、時間変化はありません。"})),
      el("section", {class: "card"},
        el("details", {class: "reading"}, el("summary", {text: "配位数の求め方"}),
          el("p", {text: "各原子のまわりで、ある距離 (カットオフ) の内側にある原子を数えます。カットオフは、元素の組み合わせごとの"
            + "動径分布関数 g(r) の「第 1 の谷」(最初の山のあとの最も低い所) にします。結晶では第 1 近接と第 2 近接のあいだ、"
            + "液体では第 1 配位殻の外側です。条件の「数える距離」に入れると、その距離ですべての組み合わせを数えます。"}),
          el("p", {text: "ボロノイ解析の配位数 (多面体の面の数) とは数え方が違います。fcc・hcp はどちらも 12 ですが、bcc は"
            + "ボロノイでは 14、距離では完全な結晶で 8、熱で第 1・第 2 近接の山がつながると 14 になります。"}),
          el("p", {text: "目安: fcc・hcp 12、bcc 8 (または 14)、ダイヤモンド 4、岩塩型の陽イオンのまわりの陰イオン 6、液体の金属 12 前後に広がる。"})))
    ].filter(Boolean));
    card.show(result.view);
    resetSelection();
    update();
  }

  const cutoffInput = el("input", {type: "number", inputmode: "decimal", step: "any", min: "0.5", max: "10", placeholder: "自動",
                                   value: settings.cutoff ?? "", onchange: () => {
                                     settings.cutoff = cutoffInput.value === "" ? null : Number(cutoffInput.value);
                                   }});
  const framesInput = el("input", {type: "number", inputmode: "numeric", min: "0", max: "500", step: "1",
                                   value: settings.frames, onchange: () => { settings.frames = Number(framesInput.value); }});
  const run = el("button", {class: "primary wide", type: "button", text: taskId ? "この条件で解析し直す" : "解析を始める",
    onclick: () => busy(run, submit)});

  async function submit() {
    const {id, reused} = await api("/api/analysis/coord", {method: "POST", body: {
      job_id: jobId, structure_id: structureId, frames: settings.frames, cutoff: settings.cutoff, per_frame: settings.per_frame}});
    toast(reused ? "同じ条件の解析を開きます" : "解析を始めました (ジョブの一覧にも出ます)");
    navigate(`#/analysis/coord?task=${id}`);
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
    task = await api(`/api/jobs/${taskId}/coord`);
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
    el("h1", {class: "page-title", text: "配位数"}),
    el("p", {class: "note", text: "各原子のまわりの、となりの原子の数を数えます。動径分布関数 g(r) と一緒に見て、"
      + "どこまでを「となり」と数えたかを確かめられます。融解・アモルファス化・相転移での配位の変化の追跡に使えます。"}),
    el("section", {class: "card"},
      el("div", {class: "card-head"}, el("h2", {text: "条件"})),
      field("数える距離 (Å)", cutoffInput, "空欄なら、元素の組み合わせごとに g(r) の第 1 の谷から自動で決めます"),
      jobId ? field("時間変化に使うフレーム数", framesInput, "トラジェクトリから等間隔に選びます。0 で最終構造だけ") : null,
      jobId ? el("label", {class: "check"}, el("input", {type: "checkbox", checked: settings.per_frame, onchange: (e) => {
        settings.per_frame = e.target.checked;
      }}), "時間変化の数える距離をフレームごとに決め直す (距離が自動のとき)") : null,
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
    [rdfChart, distChart, timeChart, cutoffChart].forEach(purgeChart);
    card.viewer.destroy();
  };
}
