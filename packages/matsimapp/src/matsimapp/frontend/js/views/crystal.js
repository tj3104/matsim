// 結晶構造の判定の画面 (adaptive CNA)。ジョブの最終構造 (と時間変化) か登録済みの構造の原子を、
// fcc / hcp / bcc / 正二十面体 / その他 に分け、比率をグラフにする。選んだ構造の原子を 3D 表示で色塗りする。
// 解析は「解析のジョブ」としてサーバーで動く (#/analysis/cna?task=番号)。画面を離れても続き、進み具合を表示する。
import {drawBars, drawCategoryLines, purgeChart} from "../charts.js";
import {api, busy, el, field, formatDuration, number, toast} from "../dom.js";
import {navigate} from "../main.js";
import {isActive, progressBar, progressText, statusChip} from "./jobs.js";
import {viewerCard} from "./structures.js";

const pct = (v) => `${(v * 100).toFixed(1)}%`;
const TEXT = {
  "fcc": "面心立方。となり 12 個 (Cu・Al・Ni・γ-Fe など)",
  "hcp": "六方最密。となり 12 個 (Ti・Mg・Zn・α-Zr など)。fcc の中の積層欠陥・双晶境界もここに入ります",
  "bcc": "体心立方。となり 8 + 6 個 (α-Fe・W・Mo・β-Ti など)",
  "正二十面体": "五回対称の 12 配位。液体・金属ガラスに多い詰まり方",
  "その他": "どれにも当てはまらない原子。液体・粒界・表面・転位の芯、ダイヤモンド構造など",
};

export async function renderCrystal(view, query) {
  const taskId = Number(query.get("task")) || null;
  let jobId = Number(query.get("job")) || null;
  let structureId = Number(query.get("structure")) || null;
  const settings = {frames: 40};
  let task = null;
  if (taskId) {   // 投入済みの解析: 条件を引き継ぐ
    task = await api(`/api/jobs/${taskId}/cna`);
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
  let hideOthers = false;
  const selected = new Set();
  const barChart = el("div", {class: "chart"});
  const timeChart = el("div", {class: "chart"});
  const card = viewerCard("色塗り");
  card.caption.textContent = "下の一覧で構造を選ぶと、その原子に色が付きます。ドラッグで回転、ピンチ・ホイールで拡大縮小";
  const chips = el("div", {class: "chips"});
  const legend = el("p", {class: "note"});
  const resultSlot = el("div", {class: "stack"});
  const colorOf = (type) => result.colors[type] || "#6b7280";

  function paint() {
    if (!selected.size) {
      card.viewer.setColors(null);
      legend.textContent = "色塗りなし (元素の色)";
      return;
    }
    const colors = result.view_labels.map((label) => (selected.has(label) ? colorOf(label) : null));
    card.viewer.setColors(colors, {hideOthers});
    const n = colors.filter(Boolean).length;
    legend.textContent = `表示中の ${number(result.view.shown)} 原子のうち ${number(n)} 原子に色を付けています`
      + (result.view.shown < result.view.n_atoms ? " (原子が多いので間引いて表示)" : "");
  }

  function renderChips() {
    chips.replaceChildren(...result.fractions.filter((f) => f.count > 0).map((f) => {
      const on = selected.has(f.type);
      return el("button", {class: `chip chip-2${on ? " chip-on" : ""}`, type: "button", "aria-pressed": String(on), onclick: () => {
        if (on) selected.delete(f.type); else selected.add(f.type);
        renderChips();
        paint();
      }},
      el("span", {class: "legend-swatch", style: `background:${colorOf(f.type)}`, "aria-hidden": "true"}),
      el("span", {class: "chip-text"}, el("span", {text: `${f.type} ${pct(f.fraction)}`}), el("small", {text: TEXT[f.type]})));
    }));
  }

  async function draw() {
    await drawBars(barChart, {labels: result.fractions.map((f) => f.type), values: result.fractions.map((f) => f.fraction),
                              colors: result.fractions.map((f) => colorOf(f.type)), yLabel: "比率", percent: true,
                              key: "cna-bars"});
    const evo = result.evolution;
    if (evo && evo.time_ps.length > 1) {
      const hasTime = evo.time_ps.every((t) => t !== null);
      await drawCategoryLines(timeChart, {x: hasTime ? evo.time_ps : evo.frame, xLabel: hasTime ? "時間 (ps)" : "フレーム",
        yLabel: "比率", percent: true, key: "cna-time",
        series: Object.entries(evo.fractions).map(([type, values]) => ({name: type, values, color: colorOf(type)}))});
    }
  }

  function renderResult() {
    const top = result.fractions.reduce((a, b) => (b.fraction > a.fraction ? b : a));
    const evo = result.evolution;
    const species = Object.keys(result.by_species);
    const shown = result.fractions.filter((f) => f.count > 0).map((f) => f.type);
    resultSlot.replaceChildren(...[   // replaceChildren は null を文字にしてしまうので取り除く
      el("section", {class: "card"},
        el("div", {class: "card-head"}, el("h2", {text: "結晶構造の比率"})),
        el("dl", {class: "facts"},
          el("dt", {text: "原子の数"}), el("dd", {text: `${number(result.n_atoms)} 個`}),
          el("dt", {text: "最も多い構造"}), el("dd", {class: "value", text: `${top.type} ${pct(top.fraction)}`})),
        barChart),
      card.node,
      el("section", {class: "card"}, el("div", {class: "card-head"}, el("h2", {text: "色を付ける構造"})),
        el("div", {class: "button-row"},
          el("button", {class: "secondary small", type: "button", text: "すべて選ぶ", onclick: () => {
            shown.forEach((t) => selected.add(t));
            renderChips();
            paint();
          }}),
          el("button", {class: "secondary small", type: "button", text: "選択を解除", onclick: () => {
            selected.clear();
            renderChips();
            paint();
          }})),
        el("label", {class: "check"}, el("input", {type: "checkbox", checked: hideOthers, onchange: (e) => {
          hideOthers = e.target.checked;
          paint();
        }}), "選んでいない原子を隠す"),
        chips, legend),
      evo && evo.time_ps.length > 1
        ? el("section", {class: "card"}, el("div", {class: "card-head"}, el("h2", {text: "比率の時間変化"})),
          el("p", {class: "note", text: `トラジェクトリの ${evo.time_ps.length} フレームを判定しました`}), timeChart)
        : el("section", {class: "card"}, el("p", {class: "note", text: jobId
          ? "時間変化は、トラジェクトリを保存したジョブで出せます (詳細設定のトラジェクトリの保存間隔)。"
          : "登録済みの構造は 1 枚だけなので、時間変化はありません。"})),
      species.length > 1
        ? el("section", {class: "card"}, el("div", {class: "card-head"}, el("h2", {text: "元素ごとの比率"})),
          el("div", {class: "table-scroll"}, el("table", {class: "table"},
            el("thead", {}, el("tr", {}, [el("th", {text: "元素"}), ...shown.map((t) => el("th", {text: t}))])),
            el("tbody", {}, species.map((sym) => el("tr", {}, el("th", {text: sym}),
              ...shown.map((t) => el("td", {text: pct(result.by_species[sym][t])}))))))))
        : null,
      el("section", {class: "card"},
        el("details", {class: "reading"}, el("summary", {text: "判定のしかた"}),
          el("p", {text: "各原子について、近い順に 12 個 (fcc・hcp・正二十面体) か 14 個 (bcc) のとなりを取り、"
            + "となりどうしのつながり方 (共通のとなりの数・その結合の数) を数えて、理想的な結晶と同じかどうかで判定します "
            + "(adaptive CNA)。となりまでの距離を原子ごとに決めるので、格子定数や元素によらず、温度で揺れた結晶も判定できます。"}),
          el("ul", {class: "family-list"}, ...Object.entries(TEXT).map(([type, text]) => el("li", {},
            el("span", {class: "legend-swatch", style: `background:${colorOf(type)}`, "aria-hidden": "true"}),
            el("span", {}, el("strong", {text: type}), ` ${text}`))))))].filter(Boolean));
    card.show(result.view);
    if (!selected.size) shown.filter((t) => t !== "その他").forEach((t) => selected.add(t));
    renderChips();
    paint();
    requestAnimationFrame(() => draw().catch(() => {}));
  }

  const framesInput = el("input", {type: "number", inputmode: "numeric", min: "0", max: "500", step: "1",
                                   value: settings.frames, onchange: () => { settings.frames = Number(framesInput.value); }});
  const run = el("button", {class: "primary wide", type: "button", text: taskId ? "この条件で解析し直す" : "解析を始める",
    onclick: () => busy(run, submit)});

  async function submit() {
    const {id, reused} = await api("/api/analysis/cna", {method: "POST", body: {
      job_id: jobId, structure_id: structureId, frames: settings.frames}});
    toast(reused ? "同じ条件の解析を開きます" : "解析を始めました (ジョブの一覧にも出ます)");
    navigate(`#/analysis/cna?task=${id}`);
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
    task = await api(`/api/jobs/${taskId}/cna`);
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
    el("h1", {class: "page-title", text: "結晶構造の判定"}),
    el("p", {class: "note", text: "原子ごとに、まわりの原子の並び方が fcc・hcp・bcc・正二十面体のどれと同じかを判定します。"
      + "構造相転移 (bcc → hcp など)・融解・結晶化・積層欠陥の追跡に使えます。"}),
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
    [barChart, timeChart].forEach(purgeChart);
    card.viewer.destroy();
  };
}
