// ボロノイ多面体の解析画面。ジョブの最終構造 (と時間変化) か登録済みの構造を、ボロノイ指数 <n3,n4,n5,n6> で分類し、
// 種類ごとの比率をグラフにする。種類を好きなだけ選ぶと、その原子を 3D 表示で色塗りする。
// 解析は「解析のジョブ」としてサーバーで動く (#/analysis/voronoi?task=番号)。画面を離れても続き、進み具合を表示する。
import {CATEGORY_COLORS, drawBars, drawCategoryLines, purgeChart} from "../charts.js";
import {api, busy, el, field, formatDuration, number, toast} from "../dom.js";
import {navigate} from "../main.js";
import {isActive, progressBar, progressText, statusChip} from "./jobs.js";
import {viewerCard} from "./structures.js";

const pct = (v) => `${(v * 100).toFixed(1)}%`;

// 指数のまとまり (matsimapp/voronoi.py の FAMILIES と同じ順・同じ名前)。色は固定
const FAMILY_COLORS = {"正二十面体": "#dc2626", "二十面体に近い": "#f97316", "Frank–Kasper型": "#a855f7",
                       "fcc・hcp": "#2563eb", "bcc": "#16a34a", "結晶に近い": "#06b6d4", "乱れた多面体": "#9ca3af"};
const FAMILY_TEXT = {
  "正二十面体": "<0,0,12,0>。五角形 12 枚。液体・金属ガラスに多く、結晶にはない詰まり方",
  "二十面体に近い": "五角形が 8 枚以上で 6 割以上。正二十面体が少し崩れたもの",
  "Frank–Kasper型": "<0,0,12,n6>。五角形 12 枚＋六角形。大きな原子のまわり (配位数 13〜16)",
  "fcc・hcp": "<0,12,0,0>。四角形 12 枚。fcc・hcp の結晶 (配位数 12)",
  "bcc": "<0,6,0,8>。四角形 6 枚＋六角形 8 枚。bcc の結晶 (配位数 14)",
  "結晶に近い": "四角形が五角形より多い。結晶が熱でゆがんだもの・積層欠陥など",
  "乱れた多面体": "どれにも当てはまらない。液体・粒界・表面などの乱れた場所",
};

export async function renderVoronoi(view, query) {
  const taskId = Number(query.get("task")) || null;
  let jobId = Number(query.get("job")) || null;
  let structureId = Number(query.get("structure")) || null;
  const settings = {center: "*", area_threshold: 0.01, edge_fraction: 0.18, frames: 20, top: 10};
  let task = null;
  if (taskId) {   // 投入済みの解析: 条件を引き継ぐ
    task = await api(`/api/jobs/${taskId}/voronoi`);
    jobId = task.params.job_id;
    structureId = task.params.structure_id;
    for (const key of Object.keys(settings)) if (key in task.params) settings[key] = task.params[key];
  }
  if (!jobId && !structureId) throw new Error("解析するジョブか構造を選んでください");
  if (!jobId) settings.frames = 0;
  const back = jobId ? `#/jobs/${jobId}` : `#/structures/${structureId}`;
  let stopped = false;
  let timer = null;
  // 色塗り・グラフの単位。family = まとまり (正二十面体・fcc など)、index = ボロノイ指数そのもの
  let mode = "family";
  const selectedOf = {family: new Set(), index: new Set()};
  const selected = () => selectedOf[mode];
  let result = null;
  let hideOthers = false;
  const charts = [];

  const card = viewerCard("色塗り");
  card.caption.textContent = "下の一覧で種類を選ぶと、その原子に色が付きます。ドラッグで回転、ピンチ・ホイールで拡大縮小";
  const chips = el("div", {class: "chips"});
  const legend = el("p", {class: "note"});
  const barChart = el("div", {class: "chart"});
  const cnChart = el("div", {class: "chart"});
  const timeChart = el("div", {class: "chart"});
  charts.push(barChart, cnChart, timeChart);
  const resultSlot = el("div", {class: "stack"});
  const modeButtons = {};
  const quickButton = el("button", {class: "secondary small", type: "button", onclick: () => {
    selected().clear();
    items().slice(0, mode === "family" ? Infinity : 5).forEach((x) => selected().add(x.key));
    update();
  }});

  // 種類ごとの色。まとまりは決まった色、指数は比率の大きい順に CATEGORY_COLORS を割り当てる (いつも同じ色になるように)
  const colorOf = (key) => {
    if (mode === "family") return FAMILY_COLORS[key] || "#6b7280";
    const rank = result.ranking.findIndex((r) => r.index === key);
    return rank >= 0 && rank < CATEGORY_COLORS.length ? CATEGORY_COLORS[rank] : "#6b7280";
  };
  // 一覧に出す種類 (key・比率・説明)
  const items = () => mode === "family"
    ? result.families.map((f) => ({key: f.family, fraction: f.fraction, count: f.count, text: f.family,
                                    detail: FAMILY_TEXT[f.family]}))
    : result.ranking.slice(0, 30).map((r) => ({key: r.index, fraction: r.fraction, count: r.count,
                                                text: r.index, detail: `${r.detail} (${r.family})`}));
  const labelOf = (index) => (mode === "family" ? result.family_of[index] : index);

  function paint() {
    if (!result) return;
    if (!selected().size) {
      card.viewer.setColors(null);
      legend.textContent = "色塗りなし (元素の色)";
      return;
    }
    const colors = result.view_labels.map((label, i) => {
      const okCenter = result.center === "*" || result.view.symbols[i] === result.center;
      const key = labelOf(label);
      return okCenter && selected().has(key) ? colorOf(key) : null;
    });
    card.viewer.setColors(colors, {hideOthers});
    const n = colors.filter(Boolean).length;
    legend.textContent = `表示中の ${number(result.view.shown)} 原子のうち ${number(n)} 原子に色を付けています`
      + (result.view.shown < result.view.n_atoms ? " (原子が多いので間引いて表示)" : "");
  }

  function renderChips() {
    chips.replaceChildren(...items().map((x) => {
      const on = selected().has(x.key);
      return el("button", {class: `chip chip-2${on ? " chip-on" : ""}`, type: "button", "aria-pressed": String(on), onclick: () => {
        if (on) selected().delete(x.key); else selected().add(x.key);
        renderChips();
        paint();
      }},
      el("span", {class: "legend-swatch", style: `background:${colorOf(x.key)}`, "aria-hidden": "true"}),
      el("span", {class: "chip-text"}, el("span", {text: `${x.text} ${pct(x.fraction)}`}),
         el("small", {text: x.detail})));
    }));
  }

  async function draw() {
    if (mode === "family") {
      await drawBars(barChart, {labels: result.families.map((f) => f.family), values: result.families.map((f) => f.fraction),
                                colors: result.families.map((f) => colorOf(f.family)), yLabel: "比率", percent: true,
                                key: "voronoi-bars"});
    } else {
      const top = result.ranking.slice(0, settings.top);
      const other = 1 - top.reduce((sum, r) => sum + r.fraction, 0);
      await drawBars(barChart, {labels: [...top.map((r) => r.index), ...(other > 1e-9 ? ["その他"] : [])],
                                values: [...top.map((r) => r.fraction), ...(other > 1e-9 ? [other] : [])],
                                colors: [...top.map((r) => colorOf(r.index)), "#9ca3af"], yLabel: "比率", percent: true,
                                key: "voronoi-bars"});
    }
    await drawBars(cnChart, {labels: result.coordination.map((c) => String(c.cn)),
                             values: result.coordination.map((c) => c.fraction), xLabel: "配位数 (数えた面の数)",
                             yLabel: "比率", percent: true, key: "voronoi-cn"});
    const evo = result.evolution;
    if (evo && evo.time_ps.length > 1) {
      const hasTime = evo.time_ps.every((t) => t !== null);
      const series = mode === "family" && evo.families
        ? Object.entries(evo.families).map(([name, values]) => ({name, values, color: colorOf(name)}))
        : [...Object.entries(evo.fractions).map(([index, values]) => ({name: index, values, color: colorOf(index)})),
           {name: "その他", values: evo.other, color: "#9ca3af", dash: "dot"}];
      await drawCategoryLines(timeChart, {x: hasTime ? evo.time_ps : evo.frame, xLabel: hasTime ? "時間 (ps)" : "フレーム",
        yLabel: "比率", percent: true, key: "voronoi-time", series});
    }
  }

  // 分け方を切り替えたときなど、一覧・色・グラフをまとめて描き直す
  function update() {
    for (const [key, button] of Object.entries(modeButtons)) {
      button.classList.toggle("seg-on", key === mode);
      button.setAttribute("aria-pressed", String(key === mode));
    }
    quickButton.textContent = mode === "family" ? "すべて選ぶ" : "上位 5 種類を選ぶ";
    renderChips();
    paint();
    requestAnimationFrame(() => draw().catch(() => {}));
  }

  function renderResult() {
    const top = result.ranking[0];
    const topFamily = result.families.reduce((a, b) => (b.fraction > a.fraction ? b : a));
    const centerText = result.center === "*" ? "すべての原子" : `${result.center} を中心とする多面体`;
    const evo = result.evolution;
    const timeCard = evo && evo.time_ps.length > 1
      ? el("section", {class: "card"}, el("div", {class: "card-head"}, el("h2", {text: "比率の時間変化"})),
        el("p", {class: "note", text: `トラジェクトリの ${evo.time_ps.length} フレームを解析しました`}),
        timeChart)
      : el("section", {class: "card"}, el("p", {class: "note", text: jobId
        ? "時間変化は、トラジェクトリを保存したジョブで出せます (詳細設定のトラジェクトリの保存間隔)。"
        : "登録済みの構造は 1 枚だけなので、時間変化はありません。"}));
    const seg = el("div", {class: "segmented", role: "group", "aria-label": "グラフと色塗りの分け方"},
      ...[["family", "まとまりで見る"], ["index", "指数で見る"]].map(([key, text]) => {
        modeButtons[key] = el("button", {type: "button", text, onclick: () => { mode = key; update(); }});
        return modeButtons[key];
      }));
    resultSlot.replaceChildren(
      el("section", {class: "card"},
        el("div", {class: "card-head"}, el("h2", {text: "ボロノイ多面体の比率"})),
        el("dl", {class: "facts"},
          el("dt", {text: "対象"}), el("dd", {text: `${centerText} (${number(result.n_center)} 個)`}),
          el("dt", {text: "最も多いまとまり"}), el("dd", {class: "value", text: `${topFamily.family} ${pct(topFamily.fraction)}`}),
          el("dt", {text: "最も多い指数"}), el("dd", {text: `${top.index} ${pct(top.fraction)} (${top.detail})`}),
          el("dt", {text: "指数の種類"}), el("dd", {text: `${number(result.ranking.length)} 種類`})),
        seg,
        el("p", {class: "note", text: "「まとまり」は指数を構造の似たもの同士でまとめたもの。グラフ・色塗り・時間変化の分け方が切り替わります"}),
        barChart),
      card.node,
      el("section", {class: "card"}, el("div", {class: "card-head"}, el("h2", {text: "色を付ける種類"})),
        el("div", {class: "button-row"}, quickButton,
          el("button", {class: "secondary small", type: "button", text: "選択を解除", onclick: () => {
            selected().clear();
            renderChips();
            paint();
          }})),
        el("label", {class: "check"}, el("input", {type: "checkbox", checked: hideOthers, onchange: (e) => {
          hideOthers = e.target.checked;
          paint();
        }}), "選んでいない原子を隠す"),
        chips, legend),
      timeCard,
      el("section", {class: "card"}, el("div", {class: "card-head"}, el("h2", {text: "配位数の分布"})),
        el("p", {class: "note", text: "配位数 = 多面体の面の数 = となりの原子の数"}), cnChart),
      el("section", {class: "card"}, el("div", {class: "card-head"}, el("h2", {text: "すべての指数"})),
        el("div", {class: "table-scroll"}, el("table", {class: "table"},
          el("thead", {}, el("tr", {}, ["ボロノイ指数", "面の内訳", "まとまり", "比率", "個数"].map((h) => el("th", {text: h})))),
          el("tbody", {}, result.ranking.slice(0, 100).map((r) => el("tr", {},
            el("th", {text: r.index}), el("td", {text: r.name ? `${r.detail} (${r.name})` : r.detail}),
            el("td", {}, el("span", {class: "legend-swatch", style: `background:${FAMILY_COLORS[r.family]}`, "aria-hidden": "true"}),
               ` ${r.family}`),
            el("td", {text: pct(r.fraction)}), el("td", {text: number(r.count)}))))))));
    card.show(result.view);
    if (!selectedOf.family.size) result.families.slice().sort((a, b) => b.fraction - a.fraction).slice(0, 3)
      .forEach((f) => selectedOf.family.add(f.family));
    if (!selectedOf.index.size) result.ranking.slice(0, 3).forEach((r) => selectedOf.index.add(r.index));
    for (const index of [...selectedOf.index]) if (!result.ranking.some((r) => r.index === index)) selectedOf.index.delete(index);
    update();
  }

  const centerSelect = el("select", {onchange: () => { settings.center = centerSelect.value; }},
    el("option", {value: "*", text: "すべての原子"}),
    settings.center !== "*" ? el("option", {value: settings.center, selected: true, text: `${settings.center} を中心とする多面体`}) : null);
  const numberInput = (key, attrs, scale = 1) => {
    const input = el("input", {type: "number", inputmode: "decimal", step: "any", value: settings[key] * scale, ...attrs,
                               onchange: () => { settings[key] = Number(input.value) / scale; }});
    return input;
  };
  const run = el("button", {class: "primary wide", type: "button", text: taskId ? "この条件で解析し直す" : "解析を始める",
    onclick: () => busy(run, submit)});

  // 解析のジョブを投入する。同じ条件の解析が済んでいれば、それを開く
  async function submit() {
    const {id, reused} = await api("/api/analysis/voronoi", {method: "POST", body: {
      job_id: jobId, structure_id: structureId, ...settings}});
    toast(reused ? "同じ条件の解析を開きます" : "解析を始めました (ジョブの一覧にも出ます)");
    navigate(`#/analysis/voronoi?task=${id}`);
  }

  // 進み具合のカード (実行待ち・実行中のあいだ)
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
    progressSlot.replaceChildren(...[
      el("section", {class: "card"},
        el("div", {class: "card-head"}, el("h2", {text: "解析の進み具合"}), statusChip(job)),
        job.status === "running" ? progressBar(job) : null,
        el("p", {class: "note", role: "status", text: job.status === "queued" ? "順番を待っています"
          : `${progressText(job)}${remaining}`}),
        job.message ? el("p", {class: job.status === "failed" ? "error" : "note", text: job.message}) : null,
        isActive(job) ? el("p", {class: "note", text: "この画面を閉じても解析は続きます。「ジョブ」タブからも開けます。"}) : null,
        isActive(job) ? el("div", {class: "button-row"}, cancel) : null),
    ].filter(Boolean));
  }

  async function poll() {
    task = await api(`/api/jobs/${taskId}/voronoi`);
    if (stopped) return;
    renderProgress();
    if (task.result && !result) {
      result = task.result;
      progressSlot.replaceChildren();
      const have = new Set([...centerSelect.options].map((o) => o.value));
      for (const s of result.species) {
        if (!have.has(s)) centerSelect.append(el("option", {value: s, text: `${s} を中心とする多面体`}));
      }
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
    el("h1", {class: "page-title", text: "ボロノイ多面体の解析"}),
    el("p", {class: "note", text: "各原子のまわりの「となりの原子の並び方」を、ボロノイ多面体の形で分類します。"}),
    readingCard(),
    el("section", {class: "card"},
      el("div", {class: "card-head"}, el("h2", {text: "条件"})),
      field("数える多面体", centerSelect, "元素を選ぶと、その元素を中心とする多面体だけを数えます"),
      jobId ? field("時間変化に使うフレーム数", numberInput("frames", {min: "0", max: "200", step: "1", inputmode: "numeric"}),
                    "トラジェクトリから等間隔に選びます。0 で最終構造だけ") : null,
      el("details", {}, el("summary", {text: "詳細設定"}),
        field("小さな面を数えない基準 (%)", numberInput("area_threshold", {min: "0", max: "20"}, 100),
              "多面体の全表面積に対する割合がこれ未満の面は数えない (既定 1%)"),
        field("短い辺を数えない基準 (原子間隔に対する倍率)", numberInput("edge_fraction", {min: "0", max: "0.5"}),
              "熱ゆらぎでできる短い辺を数えない (既定 0.18。0 にすると完全な結晶以外はばらつきます)"),
        field("グラフに出す種類の数", numberInput("top", {min: "1", max: "30", step: "1", inputmode: "numeric"}))),
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
    charts.forEach(purgeChart);
    card.viewer.destroy();
  };
}

// 「ボロノイ指数の読み方」の説明 (開閉できる)
function readingCard() {
  return el("section", {class: "card"},
    el("details", {class: "reading"},
      el("summary", {text: "ボロノイ指数の読み方"}),
      el("p", {text: "原子のまわりを、となりの原子との垂直二等分面で囲むと多面体ができます (ボロノイ多面体)。"
        + "面 1 枚がとなりの原子 1 個に当たるので、面の数 = 配位数です。"}),
      el("p", {text: "ボロノイ指数 <n3,n4,n5,n6> は、その多面体の三角形・四角形・五角形・六角形の面の枚数です。"}),
      el("p", {class: "reading-example"}, el("strong", {text: "<0,0,12,0>"}),
         " = 三角形 0・四角形 0・五角形 12・六角形 0 → 配位数 12 の正二十面体"),
      el("p", {class: "reading-example"}, el("strong", {text: "<1,5,5,1>"}),
         " = 三角形 1・四角形 5・五角形 5・六角形 1 → 配位数 12 の乱れた多面体"),
      el("p", {text: "目安: 五角形が多いほど二十面体 (液体・ガラス) に近く、四角形・六角形が多いほど結晶 (fcc・bcc) に近い形です。"
        + "指数は種類が多くなるので、似たものを次の「まとまり」に分けて見られます。"}),
      el("ul", {class: "family-list"}, ...Object.entries(FAMILY_TEXT).map(([name, text]) => el("li", {},
        el("span", {class: "legend-swatch", style: `background:${FAMILY_COLORS[name]}`, "aria-hidden": "true"}),
        el("span", {}, el("strong", {text: name}), ` ${text}`))))));
}
