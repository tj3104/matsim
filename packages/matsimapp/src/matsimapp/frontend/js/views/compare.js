// ジョブの結果の比較。選んだジョブの同じ種類の表を重ねて描き、要点の数値を並べる。
// API は既存のもの (/api/jobs/{id}/results・/dataset) をそのまま使う。
import {drawSeries, purgeChart} from "../charts.js";
import {api, el, field, toast} from "../dom.js";
import {createJobFilter} from "../jobfilter.js";

const MAX_JOBS = 8;
const SETTING_TEMPERATURE = "__temperature";

// ジョブの設定の温度 (温度スキャンのジョブなど)。要点の数値と並べて横軸に選べるようにする
function settingTemperature(job) {
  const p = job.params || {};
  return job.summary?.temperature ?? p.options?.temperature ?? p.stages?.[0]?.temperature ?? null;
}

// 小さい・大きい値は指数表記 (例: 拡散係数 3.543e-8 cm²/s)
function formatValue(value) {
  if (typeof value !== "number") return value ?? "—";
  const size = Math.abs(value);
  if (size !== 0 && (size < 1e-3 || size >= 1e6)) return value.toExponential(3).replace("e+", "e");
  return Number(value.toPrecision(6)).toLocaleString("ja-JP", {maximumSignificantDigits: 6});
}

// 解析のジョブ (ボロノイなど) は結果の形が違い、専用の画面で見るので比較の候補にしない
const ANALYSIS_KINDS = new Set(["voronoi", "cna", "coord", "sro", "poly"]);

// 比べるジョブを選ぶカード。選んだ状態はこのカードが持ち、変わるたびに onChange に全部を渡す
function pickerCard(jobs, initial, onChange) {
  const candidates = jobs.filter((job) => job.status === "done" && !ANALYSIS_KINDS.has(job.kind));
  const selected = new Set(initial);
  const title = el("h2", {});
  const list = el("div", {class: "compare-jobs"});
  const filter = createJobFilter({storageKey: "matsim.compare.filter", status: false, onChange: () => renderList()});
  const clear = el("button", {type: "button", class: "link-button", text: "選択を解除", onclick: () => {
    selected.clear(); renderList(); onChange([...selected]);
  }});

  function renderList() {
    title.textContent = `比べるジョブ (${selected.size} / ${MAX_JOBS})`;
    clear.hidden = !selected.size;
    const shown = filter.apply(candidates);
    list.replaceChildren(...(shown.length ? shown.map((job) => {
      const box = el("input", {type: "checkbox", checked: selected.has(job.id), onchange: () => {
        if (box.checked && selected.size >= MAX_JOBS) {
          box.checked = false;
          toast(`比べられるのは ${MAX_JOBS} 件までです`);
          return;
        }
        if (box.checked) selected.add(job.id); else selected.delete(job.id);
        title.textContent = `比べるジョブ (${selected.size} / ${MAX_JOBS})`;
        clear.hidden = !selected.size;
        onChange([...selected]);
      }});
      return el("label", {}, box, el("span", {},
        el("strong", {text: `#${job.id} ${job.name}`}),
        el("small", {class: "list-sub", text: [job.summary?.kind_label || job.kind, job.summary?.formula,
          job.n_atoms && `${job.n_atoms} 原子`, job.summary?.potential].filter(Boolean).join(" ・ ")})));
    }) : [el("p", {class: "empty", text: "条件に合うジョブはありません"})]));
  }

  renderList();
  return el("section", {class: "card"},
    el("div", {class: "card-head"}, title, clear),
    candidates.length ? [filter.node, list] : el("p", {class: "empty", text: "完了したジョブがありません"}),
    el("p", {class: "field-hint", text: "完了したジョブから選びます (解析のジョブは除く)。同じ種類の表 (熱力学量など) を重ねて描けます。"}));
}

// 同じ種類の表を重ねて描くカード
function overlayCard(entries) {
  const common = entries[0].overview.datasets.filter((d) => !d.table && d.style !== "bands" && d.style !== "colormap"
    && entries.every((e) => e.overview.datasets.some((o) => o.key === d.key)));
  const chart = el("div", {class: "chart"});
  const status = el("p", {class: "note", role: "status"});
  if (!common.length) {
    return {node: el("section", {class: "card"}, el("h2", {text: "グラフで重ねる"}),
      el("p", {class: "empty", text: "選んだジョブに共通の表がありません (計算の種類が違うときなど)"})), chart: null};
  }
  const datasetSelect = el("select", {}, common.map((d) => el("option", {value: d.key, text: d.title})));
  const xSelect = el("select", {});
  const ySelect = el("select", {});
  const markers = el("input", {type: "checkbox"});
  const cache = new Map();
  const columnsOf = (key) => {
    // 列はすべてのジョブにあるものだけ
    const lists = entries.map((e) => e.overview.datasets.find((d) => d.key === key).columns);
    return lists[0].filter((c) => lists.every((cols) => cols.some((o) => o.key === c.key)));
  };

  async function draw() {
    const key = datasetSelect.value;
    status.textContent = "読み込み中…";
    try {
      const tables = await Promise.all(entries.map(async (e) => {
        const id = `${e.job.id}:${key}`;
        if (!cache.has(id)) cache.set(id, await api(`/api/jobs/${e.job.id}/dataset?key=${key}`));
        return cache.get(id);
      }));
      const label = (k) => columnsOf(key).find((c) => c.key === k)?.label || k;
      await drawSeries(chart, {
        series: entries.map((e, i) => ({x: tables[i].data[xSelect.value], y: tables[i].data[ySelect.value],
                                        name: `#${e.job.id} ${e.job.name}`})),
        xLabel: label(xSelect.value), yLabel: label(ySelect.value), markers: markers.checked, key,
      });
      status.textContent = tables.some((t) => t.n_rows > t.data[xSelect.value].length)
        ? "行の多い表は間引いて表示しています。" : "";
    } catch (error) {
      status.textContent = error.message;
    }
  }

  function selectDataset() {
    const dataset = common.find((d) => d.key === datasetSelect.value);
    const columns = columnsOf(dataset.key);
    xSelect.replaceChildren(...columns.map((c) => el("option", {value: c.key, selected: c.key === dataset.x, text: c.label})));
    ySelect.replaceChildren(...columns.map((c) => el("option", {value: c.key, selected: c.key === dataset.y[0], text: c.label})));
    draw();
  }
  datasetSelect.onchange = selectDataset;
  xSelect.onchange = draw;
  ySelect.onchange = draw;
  markers.onchange = draw;
  setTimeout(selectDataset, 0);
  return {chart, node: el("section", {class: "card wide-card"},
    el("div", {class: "card-head"}, el("h2", {text: "グラフで重ねる"})),
    common.length > 1 ? field("データ", datasetSelect) : null,
    el("div", {class: "grid-2"}, field("横軸", xSelect), field("縦軸", ySelect)),
    el("label", {class: "check"}, markers, "点も表示する"), chart, status)};
}

// 要点の数値: ジョブごとに並べた表と、2 つを選んで描くグラフ
function valuesCards(entries) {
  const labels = [];
  for (const e of entries) for (const h of e.overview.highlights) if (!labels.includes(h.label)) labels.push(h.label);
  const valueOf = (e, label) => e.overview.highlights.find((h) => h.label === label);
  const table = el("div", {class: "table-scroll"}, el("table", {class: "table"},
    el("thead", {}, el("tr", {}, el("th", {text: "項目"}), entries.map((e) => el("th", {text: `#${e.job.id}`})))),
    el("tbody", {},
      el("tr", {}, el("th", {text: "名前"}), entries.map((e) => el("td", {text: e.job.name}))),
      el("tr", {}, el("th", {text: "設定の温度 (K)"}), entries.map((e) => el("td", {text: formatValue(settingTemperature(e.job))}))),
      labels.map((label) => el("tr", {}, el("th", {text: label}), entries.map((e) => {
        const h = valueOf(e, label);
        return el("td", {text: h ? `${formatValue(h.value)}${h.unit ? ` ${h.unit}` : ""}` : "—"});
      }))))));
  const tableCard = el("section", {class: "card wide-card"},
    el("div", {class: "card-head"}, el("h2", {text: "結果の要点"})),
    labels.length ? table : el("p", {class: "empty", text: "要点の数値がありません"}));

  const numeric = labels.filter((label) => entries.filter((e) => typeof valueOf(e, label)?.value === "number").length >= 2);
  const options = [{value: SETTING_TEMPERATURE, text: "設定の温度 (K)"}, ...numeric.map((label) => {
    const unit = entries.map((e) => valueOf(e, label)?.unit).find(Boolean);
    return {value: label, text: unit ? `${label} (${unit})` : label};
  })];
  if (options.length < 2) return {nodes: [tableCard], chart: null};
  const chart = el("div", {class: "chart"});
  const xSelect = el("select", {}, options.map((o) => el("option", {value: o.value, text: o.text})));
  const ySelect = el("select", {}, options.map((o) => el("option", {value: o.value, text: o.text})));
  ySelect.value = options[1].value;
  const logY = el("input", {type: "checkbox"});
  const note = el("p", {class: "note"});
  const read = (e, key) => (key === SETTING_TEMPERATURE ? settingTemperature(e.job) : valueOf(e, key)?.value);

  async function draw() {
    const points = entries.map((e) => ({x: read(e, xSelect.value), y: read(e, ySelect.value), name: `#${e.job.id} ${e.job.name}`}))
      .filter((p) => typeof p.x === "number" && typeof p.y === "number").sort((a, b) => a.x - b.x);
    note.textContent = points.length < entries.length ? `値のない ${entries.length - points.length} 件は除いています。` : "";
    const label = (select) => select.selectedOptions[0].textContent;
    await drawSeries(chart, {series: [{x: points.map((p) => p.x), y: points.map((p) => p.y), name: "ジョブ",
                                       text: points.map((p) => p.name)}],
                             xLabel: label(xSelect), yLabel: label(ySelect), markers: true, key: "values",
                             logY: logY.checked, legend: false});
  }
  xSelect.onchange = draw;
  ySelect.onchange = draw;
  logY.onchange = draw;
  setTimeout(draw, 0);
  const chartCard = el("section", {class: "card"},
    el("div", {class: "card-head"}, el("h2", {text: "要点の数値で比べる"})),
    el("p", {class: "note", text: "ジョブごとの値を 1 点ずつ描きます (例: 横軸を設定の温度、縦軸を拡散係数)。"}),
    el("div", {class: "grid-2"}, field("横軸", xSelect), field("縦軸", ySelect)),
    el("label", {class: "check"}, logY, "縦軸を対数にする"), chart, note);
  return {nodes: [tableCard, chartCard], chart};
}

export async function renderCompare(view, query) {
  const jobs = await api("/api/jobs");
  let selected = (query.get("jobs") || "").split(",").map(Number).filter((id) => jobs.some((j) => j.id === id && j.status === "done"));
  const body = el("div", {class: "compare-body"});
  let charts = [];

  async function update(next) {
    selected = next;
    history.replaceState(null, "", `#/compare${selected.length ? `?jobs=${selected.join(",")}` : ""}`);
    charts.forEach((c) => c && purgeChart(c));
    charts = [];
    if (selected.length < 2) {
      body.replaceChildren(el("p", {class: "empty", text: "比べるジョブを 2 件以上選んでください"}));
      return;
    }
    body.replaceChildren(el("p", {class: "loading", text: "読み込み中…"}));
    // 一覧には計算条件が入っていないので、ジョブごとに詳細も読む (設定の温度に使う)
    const entries = await Promise.all(selected.map(async (id) => {
      const [job, overview] = await Promise.all([api(`/api/jobs/${id}`), api(`/api/jobs/${id}/results`)]);
      return {job, overview};
    }));
    const overlay = overlayCard(entries);
    const values = valuesCards(entries);
    charts = [overlay.chart, values.chart];
    body.replaceChildren(overlay.node, ...values.nodes);
  }

  let pending = Promise.resolve();
  const picker = pickerCard(jobs, selected, (next) => {
    // 続けてチェックしたときに、古い読み込みが新しい選択を上書きしないよう順番に処理する
    pending = pending.then(() => update(next)).catch((e) => toast(e.message));
  });
  view.replaceChildren(
    el("a", {class: "back-link", href: "#/jobs", text: "‹ ジョブの一覧"}),
    el("h1", {class: "page-title", text: "ジョブの結果を比べる"}),
    picker, body);
  await update(selected);
  return () => charts.forEach((c) => c && purgeChart(c));
}
