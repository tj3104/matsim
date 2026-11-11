import {CHARTS, drawBands, drawChart, drawColormap, drawXY, purgeChart} from "../charts.js";
import {api, busy, el, field, formatBytes, formatDuration, formatTime, number, toast} from "../dom.js";
import {navigate} from "../main.js";
import {viewerCard} from "./structures.js";
import {createSelection} from "../select.js";
import {createJobFilter} from "../jobfilter.js";
import {inputBrowser, inputKind, inputView} from "../inputs.js";
import {phononCard} from "./phonon.js";

// 状態は色だけに頼らず、記号と文字でも示す
const STATUS = {
  queued: {label: "実行待ち", icon: "…", tone: "wait"},
  running: {label: "実行中", icon: "▶", tone: "run"},
  done: {label: "完了", icon: "✓", tone: "ok"},
  failed: {label: "失敗", icon: "!", tone: "ng"},
  cancelled: {label: "中止", icon: "×", tone: "off"},
};
export const isActive = (job) => job.status === "running" || job.status === "queued";

export function statusChip(job) {
  const status = STATUS[job.status] || {label: job.status, icon: "?", tone: "off"};
  const label = job.status === "running" && job.cancel_requested ? "中止しています" : status.label;
  return el("span", {class: `status status-${status.tone}`},
    el("span", {"aria-hidden": "true", text: status.icon}), label);
}

export function progressBar(job) {
  const percent = Math.round(job.progress * 100);
  return el("div", {class: "progress", role: "progressbar", "aria-valuemin": "0", "aria-valuemax": "100",
                    "aria-valuenow": String(percent), "aria-label": "進捗"},
    el("div", {class: "progress-fill", style: `width:${percent}%`}));
}

export function progressText(job) {
  const steps = `${number(job.steps_done)} / ${number(job.total_steps)} ${job.unit || "ステップ"}`;
  if (job.status === "running") {
    const rest = `残り${job.remaining_seconds === null ? "を計算中" : `約 ${formatDuration(job.remaining_seconds)}`}`;
    // ファインチューニングは、いまの工程 (MD・SevenNet の計算・学習) を添える
    return job.phase ? `${job.phase} ・ ${rest}` : `${steps} ・ ${rest}`;
  }
  if (job.status === "queued") return "順番を待っています";
  return `${steps} ・ ${formatDuration(job.elapsed_seconds)}`;
}

function jobCardBody(job) {
  const summary = job.summary;
  return [
    el("div", {class: "job-head"}, el("span", {class: "job-name", text: job.name}), statusChip(job)),
    el("span", {class: "list-sub", text: `${summary.kind_label || "MD"} ・ ${summary.formula || ""} ・ ${number(job.n_atoms)} 原子 ・ ${job.unit === "フレーム" ? "" : `${summary.time_ps} ps ・ `}${formatTime(job.created_at)}`}),
    isActive(job) && progressBar(job),
    isActive(job) && el("span", {class: "list-sub", text: progressText(job)}),
    job.status === "failed" && job.message && el("span", {class: "list-sub error", text: job.message}),
  ];
}

// 解析のジョブ (ボロノイ解析など) は、結果を見る画面へ直接つなぐ
// 解析のジョブ (ボロノイ・結晶構造の判定) は専用の画面で見る
const ANALYSIS_PAGES = {voronoi: "voronoi", cna: "cna", coord: "coord", sro: "sro", poly: "poly"};
export const jobHref = (job) => (ANALYSIS_PAGES[job.kind] ? `#/analysis/${ANALYSIS_PAGES[job.kind]}?task=${job.id}`
                                                          : `#/jobs/${job.id}`);

export function jobCard(job) {
  return el("a", {class: "job-card", href: jobHref(job)}, jobCardBody(job));
}

export async function renderJobs(view) {
  const card = el("section", {class: "card"});
  let stopped = false;
  let jobs = [];
  // 実行中・実行待ちのジョブは選べない (先に中止する)
  const selection = createSelection({
    noun: "ジョブと結果のファイル",
    canSelect: (job) => !isActive(job) && job.status !== "preparing",
    onDelete: async (ids) => {
      const result = await api("/api/jobs/delete", {method: "POST", body: {ids}});
      jobs = await api("/api/jobs");
      return `${result.deleted} 件のジョブを削除しました` + (result.skipped.length ? ` (実行中の ${result.skipped.length} 件は残しました)` : "");
    },
    onChange: () => render(),
  });

  function row(job) {
    if (!selection.active) return jobCard(job);
    const off = isActive(job) || job.status === "preparing";
    return el("label", {class: `job-card select-row${off ? " select-off" : ""}`},
      selection.box(job), el("div", {class: "select-main"}, jobCardBody(job)));
  }

  // 並べ替え・絞り込みは一覧の外に置く (一覧は数秒ごとに描き直すので、入力中の文字を消さないため)
  const filter = createJobFilter({storageKey: "matsim.jobs.filter", onChange: () => render()});

  function render() {
    const shown = filter.apply(jobs);
    card.replaceChildren(...[
      jobs.length ? el("div", {class: "card-head"}, el("h2", {text: `ジョブ (${jobs.length})`}), selection.toggle) : null,
      selection.bar(shown),
      el("div", {class: "job-list"}, shown.length ? shown.map(row)
        : el("p", {class: "empty", text: jobs.length ? "条件に合うジョブはありません。" : "まだジョブはありません。"})),
    ].filter(Boolean));
  }

  async function refresh() {
    const latest = await api("/api/jobs");
    if (stopped) return;
    jobs = latest;
    render();
  }

  await refresh();
  view.replaceChildren(
    el("h1", {class: "page-title", text: "ジョブ"}),
    jobs.length ? filter.node : null,
    card,
    el("a", {class: "secondary wide", href: "#/compare", text: "ジョブの結果を比べる"}),
    el("a", {class: "secondary wide", href: "#/arrhenius", text: "アレニウス解析 (拡散のジョブを選ぶ)"}),
    el("a", {class: "primary wide", href: "#/home", text: "新しい計算を始める"}));
  const timer = setInterval(() => { refresh().catch(() => {}); }, 3000);
  return () => {
    stopped = true;
    clearInterval(timer);
  };
}

// ---- ジョブの詳細 --------------------------------------------------------------------

const SUMMARY_COLUMNS = [
  {key: "temperature", label: "温度 (K)", digits: 1},
  {key: "pressure", label: "圧力 (GPa)", digits: 3},
  {key: "potential_energy_per_atom", label: "ポテンシャル (eV/atom)", digits: 4},
  {key: "volume", label: "体積 (Å³)", digits: 1},
  {key: "density", label: "密度 (g/cm³)", digits: 3},
];

function summaryTable(rows) {
  const cell = (row, column) => {
    const mean = row[`${column.key}_mean`];
    const std = row[`${column.key}_std`];
    return mean === null || mean === undefined ? "—" : `${mean.toFixed(column.digits)} ± ${(std ?? 0).toFixed(column.digits)}`;
  };
  return el("div", {class: "table-scroll"},
    el("table", {class: "table"},
      el("thead", {}, el("tr", {}, el("th", {text: "ステージ"}), SUMMARY_COLUMNS.map((c) => el("th", {text: c.label})))),
      el("tbody", {}, rows.map((row) => el("tr", {},
        el("th", {scope: "row", text: `${row.stage + 1}. ${row.ensemble}`}),
        SUMMARY_COLUMNS.map((c) => el("td", {text: cell(row, c)})))))));
}

function trajectoryCard(jobId) {
  const card = viewerCard("トラジェクトリ");
  const slider = el("input", {type: "range", min: "0", max: "0", value: "0", step: "1", "aria-label": "フレーム"});
  const play = el("button", {class: "secondary small", type: "button", text: "再生"});
  const controls = el("div", {class: "player", hidden: true}, play, slider);
  card.node.append(controls);
  let frames = [];
  let timer = null;

  function show(index) {
    const frame = frames[index];
    if (!frame) return;
    slider.value = String(index);
    card.viewer.setFrame(frame.positions, frame.cell);
    card.caption.textContent = `フレーム ${index + 1} / ${frames.length}`;
  }

  function stop() {
    clearInterval(timer);
    timer = null;
    play.textContent = "再生";
  }

  slider.addEventListener("input", () => {
    stop();
    show(Number(slider.value));
  });
  play.addEventListener("click", () => {
    if (timer) {
      stop();
      return;
    }
    play.textContent = "停止";
    if (Number(slider.value) >= frames.length - 1) show(0);
    timer = setInterval(() => {
      const next = Number(slider.value) + 1;
      if (next >= frames.length) stop();
      else show(next);
    }, 120);
  });

  return {
    node: card.node,
    async load() {
      const data = await api(`/api/jobs/${jobId}/trajectory`);
      if (!data.frames.length) {
        card.node.hidden = true;
        return;
      }
      card.node.hidden = false;
      const keepIndex = frames.length ? Number(slider.value) : null;
      frames = data.frames;
      if (keepIndex === null) card.show({symbols: data.symbols, positions: frames[0].positions, cell: frames[0].cell});
      slider.max = String(frames.length - 1);
      controls.hidden = frames.length < 2;
      show(keepIndex === null ? 0 : Math.min(keepIndex, frames.length - 1));
      if (data.n_frames_total > frames.length) {
        card.caption.textContent += ` (全 ${number(data.n_frames_total)} フレームから間引いて表示)`;
      }
    },
    destroy() {
      stop();
      card.viewer.destroy();
    },
  };
}

const MAX_SERIES = 4;   // 1 枚のグラフに重ねる系列の上限

// 要点の数値 (拡散係数・弾性率など) と注意書き
function highlightsCard(overview) {
  if (!overview.highlights.length && !overview.notes.length) return null;
  const format = (value) => (typeof value === "number" ? Number(value.toPrecision(7)).toLocaleString("ja-JP", {maximumSignificantDigits: 7}) : value);
  return el("section", {class: "card"},
    el("div", {class: "card-head"}, el("h2", {text: "結果の要点"})),
    overview.highlights.length > 0 && el("dl", {class: "facts"}, overview.highlights.map((item) => [
      el("dt", {text: item.label}),
      el("dd", {class: "value", text: `${format(item.value)}${item.unit ? ` ${item.unit}` : ""}`})])),
    overview.notes.map((note) => el("p", {class: "note", text: note})));
}

// 横軸・縦軸を選んで描くカード。表 (データの種類) ごとに選べる列が変わる。
function explorerCard(jobId, overview) {
  const datasets = overview.datasets.filter((d) => !d.table);
  const cache = new Map();
  const chart = el("div", {class: "chart"});
  const status = el("p", {class: "note", role: "status"});
  const datasetSelect = el("select", {}, datasets.map((d) => el("option", {value: d.key, text: d.title})));
  const xSelect = el("select", {});
  const ySlot = el("div", {class: "chips"});
  const markers = el("input", {type: "checkbox"});
  let picked = {x: null, y: []};

  const current = () => datasets.find((d) => d.key === datasetSelect.value);
  const labelOf = (dataset, key) => dataset.columns.find((c) => c.key === key)?.label || key;

  async function draw() {
    const dataset = current();
    if (!picked.y.length) {
      status.textContent = "縦軸を 1 つ以上選んでください";
      return;
    }
    status.textContent = "";
    try {
      if (!cache.has(dataset.key)) cache.set(dataset.key, await api(`/api/jobs/${jobId}/dataset?key=${dataset.key}`));
      const table = cache.get(dataset.key);
      await drawXY(chart, {
        x: table.data[picked.x], xLabel: labelOf(dataset, picked.x),
        series: picked.y.map((key) => ({values: table.data[key], name: labelOf(dataset, key)})),
        yLabel: picked.y.length === 1 ? labelOf(dataset, picked.y[0]) : "", markers: markers.checked,
        key: `${dataset.key}:${picked.x}`,
      });
      if (table.n_rows > table.data[picked.x].length) {
        status.textContent = `${number(table.n_rows)} 行から間引いて表示しています。全部の値は csv にあります。`;
      }
    } catch (error) {
      status.textContent = error.message;
    }
  }

  function renderY() {
    const dataset = current();
    ySlot.replaceChildren(...dataset.columns.filter((c) => c.key !== picked.x).map((column) => {
      const on = picked.y.includes(column.key);
      return el("button", {class: `chip${on ? " chip-on" : ""}`, type: "button", "aria-pressed": String(on), text: column.label,
        onclick: () => {
          if (on) picked.y = picked.y.filter((key) => key !== column.key);
          else if (picked.y.length >= MAX_SERIES) {
            toast(`重ねられるのは ${MAX_SERIES} 本までです`);
            return;
          } else picked.y = [...picked.y, column.key];
          renderY();
          draw();
        }});
    }));
  }

  function selectDataset() {
    const dataset = current();
    picked = {x: dataset.x, y: dataset.y.slice(0, MAX_SERIES)};
    xSelect.replaceChildren(...dataset.columns.map((c) => el("option", {value: c.key, selected: c.key === picked.x, text: c.label})));
    renderY();
    draw();
  }

  datasetSelect.addEventListener("change", selectDataset);
  xSelect.addEventListener("change", () => {
    picked.x = xSelect.value;
    picked.y = picked.y.filter((key) => key !== picked.x);
    renderY();
    draw();
  });
  markers.addEventListener("change", draw);

  const node = el("section", {class: "card"},
    el("div", {class: "card-head"}, el("h2", {text: "軸を選んでグラフにする"})),
    el("p", {class: "note", text: "時間以外の関係も見られます (例: 横軸を温度、縦軸を体積)。"}),
    datasets.length > 1 ? field("データ", datasetSelect) : null,
    field("横軸", xSelect),
    el("div", {class: "field"}, el("span", {class: "field-label", text: `縦軸 (${MAX_SERIES} 本まで)`}), ySlot),
    el("label", {class: "check"}, markers, "点で表示する (線で結ばない)"),
    chart, status);
  return {
    node, chart,
    start: selectDataset,
    // 実行中は熱力学量が増えていくので、読み直して描き直す
    refresh() {
      cache.delete("thermo");
      if (current()?.key === "thermo") draw();
    },
  };
}

export async function renderJob(view, id) {
  let job = await api(`/api/jobs/${id}`);
  if (ANALYSIS_PAGES[job.kind]) {   // 解析のジョブは専用の画面で見る
    location.replace(jobHref(job));
    return null;
  }
  let stopped = false;
  let timer = null;
  let tick = 0;

  const head = el("div", {});
  const actions = el("div", {class: "button-row"});
  const chartNodes = CHARTS.map((chart) => ({chart, node: el("div", {class: "chart"}),
    block: el("div", {class: "chart-block"}, el("h3", {class: "group-title", text: `${chart.title} (${chart.unit})`}))}));
  chartNodes.forEach((item) => item.block.append(item.node));
  const chartCard = el("section", {class: "card wide-card", hidden: true},
    el("div", {class: "card-head"}, el("h2", {text: "熱力学量の時間推移"})),
    el("div", {class: "chart-grid"}, chartNodes.map((item) => item.block)));
  const highlightsSlot = el("div", {});
  const resultCharts = el("div", {class: "stack result-grid"});   // 計算の種類ごとの結果グラフ
  const phononSlot = el("div", {});   // フォノン分散のジョブ: 振動のようす (分枝と q を選んで矢印で示す)
  let phonon = null;
  async function ensurePhonon() {
    if (phonon || job.kind !== "phonon" || job.status !== "done") return;
    phonon = {loading: true};
    try {
      phonon = await phononCard(id);
      if (stopped) phonon.destroy(); else phononSlot.replaceChildren(phonon.node);
    } catch (error) {
      phononSlot.replaceChildren(el("section", {class: "card"}, el("p", {class: "note", text: `振動のようすを表示できません: ${error.message}`})));
    }
  }
  const explorerSlot = el("div", {});
  let explorer = null;
  let resultNodes = [];
  const summaryCard = el("section", {class: "card", hidden: true});
  const trajectory = trajectoryCard(id);
  trajectory.node.hidden = true;
  const filesCard = el("section", {class: "card"});
  const logCard = el("details", {class: "card"});
  // 入力ファイル (run.in・plumed.dat・model.xyz)。開いたときに読み込む
  const inputCard = el("details", {class: "card"});
  let inputLoaded = false;
  inputCard.addEventListener("toggle", () => {
    if (inputCard.open && !inputLoaded) {
      inputLoaded = true;
      loadInputs().catch((error) => inputCard.append(el("p", {class: "error", text: error.message})));
    }
  });
  async function loadInputs() {
    const names = ["run.in", "plumed.dat", "model.xyz"].filter((name) => job.files.some((f) => f.name === name));
    const files = await Promise.all(names.map((name) => api(`/api/jobs/${id}/text?name=${encodeURIComponent(name)}`)));
    inputCard.append(files.length
      ? inputBrowser(files, {note: "GPUMD (run.in) と PLUMED (plumed.dat) の入力です。行ごとの説明は、書き方から自動で付けています。"
                              + "読み方はマニュアルの「入力ファイルの読み方」にもあります。"})
      : el("p", {class: "empty", text: "入力ファイルがありません。"}));
  }
  // ファイル一覧の「見る」で開いた中身
  const fileViewer = el("div", {});
  const TEXT_FILE = /\.(in|dat|xyz|out|err|csv|json|txt|log)$|(^|\/)(CONTCAR|HILLS|COLVAR)$/;

  function renderHead() {
    const summary = job.summary;
    // replaceChildren は false や null を文字として入れてしまうので、出さない部品は除いてから渡す
    head.replaceChildren(...[
      el("div", {class: "job-head"}, el("h1", {class: "page-title", text: job.name}), statusChip(job)),
      el("p", {class: "note", text: `${summary.kind_label || "MD"} ・ ${summary.structure} ・ ${number(job.n_atoms)} 原子 ・ ${summary.potential} ・ ${summary.time_ps} ps`}),
      job.status !== "queued" && progressBar(job),
      el("p", {class: "note", role: "status", text: job.status === "queued"
        ? `順番を待っています (前に ${job.queue_position} 件)` : progressText(job)}),
      job.message && el("p", {class: job.status === "failed" ? "error" : "note", text: job.message}),
    ].filter(Boolean));
  }

  function renderActions() {
    const buttons = [];
    if (isActive(job)) {
      const cancel = el("button", {class: "secondary danger", type: "button", disabled: job.cancel_requested,
        text: job.status === "queued" ? "取り消す" : "中止する",
        onclick: () => busy(cancel, async () => {
          if (!confirm(job.status === "queued" ? "このジョブを取り消しますか？" : "実行中の計算を中止しますか？")) return;
          await api(`/api/jobs/${id}/cancel`, {method: "POST"});
          await poll(true);
        })});
      buttons.push(cancel);
    }
    buttons.push(el("a", {class: "secondary", href: job.kind === "md" ? `#/md?from=${id}` : `#/calc/${job.kind}?from=${id}`,
                          text: "同じ条件で作り直す"}));
    if (job.kind === "diffusion") {
      buttons.push(el("a", {class: "secondary", text: "アレニウスプロット",
                            href: job.series ? `#/arrhenius?series=${job.series}` : `#/arrhenius?jobs=${id}`}));
    } else if (job.series) {
      buttons.push(el("a", {class: "secondary", text: "温度スキャンのまとめ", href: `#/series/${job.series}`}));
    }
    if (job.status === "done" && job.kind === "finetune") {   // できたポテンシャルはポテンシャルの一覧に入っている
      buttons.push(el("a", {class: "secondary", href: "#/settings", text: "ポテンシャルの一覧"}));
    } else if (job.status === "done") {
      buttons.push(el("a", {class: "secondary", href: `#/compare?jobs=${id}`, text: "ほかのジョブと比較"}));
      buttons.push(el("a", {class: "secondary", href: `#/analysis/voronoi?job=${id}`, text: "ボロノイ解析"}));
      buttons.push(el("a", {class: "secondary", href: `#/analysis/cna?job=${id}`, text: "結晶構造の判定"}));
      buttons.push(el("a", {class: "secondary", href: `#/analysis/coord?job=${id}`, text: "配位数"}));
      buttons.push(el("a", {class: "secondary", href: `#/analysis/poly?job=${id}`, text: "配位多面体"}));
      buttons.push(el("a", {class: "secondary", href: `#/analysis/sro?job=${id}`, text: "短範囲秩序"}));
      buttons.push(el("a", {class: "secondary", href: `#/analysis/distribution?job=${id}`, text: "原子分布"}));
      const save = el("button", {class: "secondary", type: "button", text: "最終構造を登録",
        onclick: () => busy(save, async () => {
          const {id: structureId} = await api(`/api/jobs/${id}/save-final`, {method: "POST", body: {}});
          toast("最終構造を構造の一覧に追加しました");
          navigate(`#/structures/${structureId}`);
        })});
      buttons.push(save);
    }
    if (!isActive(job)) {
      const remove = el("button", {class: "secondary danger", type: "button", text: "削除",
        onclick: () => busy(remove, async () => {
          if (!confirm("このジョブと結果のファイルを削除しますか？")) return;
          await api(`/api/jobs/${id}`, {method: "DELETE"});
          toast("削除しました");
          navigate("#/jobs");
        })});
      buttons.push(remove);
    }
    actions.replaceChildren(...buttons);
  }

  function renderFiles() {
    filesCard.replaceChildren(
      el("div", {class: "card-head"}, el("h2", {text: "ファイル"}),
        !isActive(job) && job.files.length > 0 && el("a", {class: "link-button", href: `/api/jobs/${id}/zip`, download: "", text: "まとめて zip"})),
      job.files.length
        ? el("ul", {class: "list"}, job.files.map((file) => el("li", {class: "file-row"},
            el("a", {class: "list-row list-link", href: `/api/jobs/${id}/file?name=${encodeURIComponent(file.name)}`, download: ""},
              el("span", {class: "list-title mono", text: file.name}),
              el("span", {class: "list-sub", text: formatBytes(file.size)})),
            TEXT_FILE.test(file.name) ? el("button", {class: "secondary small", type: "button", text: "見る",
              "aria-label": `${file.name} の中身を見る`, onclick: (e) => busy(e.currentTarget, () => showFile(file.name))}) : null)))
        : el("p", {class: "empty", text: "まだファイルはありません。"}),
      fileViewer);
  }

  async function showFile(name) {
    const file = await api(`/api/jobs/${id}/text?name=${encodeURIComponent(name)}`);
    fileViewer.replaceChildren(...[el("div", {class: "card-head"}, el("h3", {text: name}),
      el("button", {class: "link-button", type: "button", text: "閉じる", onclick: () => fileViewer.replaceChildren()})),
      inputView(file.text, inputKind(name)),
      file.truncated ? el("p", {class: "note", text: "大きいファイルなので先頭だけ表示しています (全体はダウンロードしてください)"}) : null,
    ].filter(Boolean));
    fileViewer.scrollIntoView({behavior: "smooth", block: "start"});
  }

  function renderInputHead() {
    if (!inputCard.querySelector("summary")) inputCard.prepend(el("summary", {text: "入力ファイル (run.in・plumed.dat など)"}));
  }

  function renderLog() {
    const open = logCard.open;
    logCard.replaceChildren(...[
      el("summary", {text: "GPUMD のログ"}),
      el("pre", {class: "code", text: job.log || "(まだ出力がありません)"}),
      job.error_log && el("pre", {class: "code error", text: job.error_log}),
    ].filter(Boolean));
    logCard.open = open;
  }

  // 要点・種類ごとのグラフ・軸を選べるグラフ。表の一覧が変わったとき (計算が終わったとき) だけ作り直す。
  let overviewKey = "";
  async function refreshOverview() {
    const overview = await api(`/api/jobs/${id}/results`);
    if (stopped) return;
    const key = JSON.stringify([overview.highlights.length, overview.datasets.map((d) => d.key)]);
    if (key === overviewKey) {
      if (explorer) explorer.refresh();
      return;
    }
    overviewKey = key;
    highlightsSlot.replaceChildren(...[highlightsCard(overview)].filter(Boolean));
    resultNodes.forEach(purgeChart);
    resultNodes = [];
    const cards = [];
    for (const dataset of overview.datasets.filter((d) => d.key !== "thermo")) {
      if (dataset.table) {   // 行列などはグラフにせず、そのまま表で見せる
        const slot = el("div", {class: "table-scroll"});
        cards.push(el("section", {class: "card"}, el("div", {class: "card-head"}, el("h2", {text: dataset.title})), slot));
        api(`/api/jobs/${id}/dataset?key=${dataset.key}`).then((table) => {
          const rows = table.data[dataset.columns[0].key].map((_, i) => dataset.columns.map((c) => table.data[c.key][i]));
          slot.replaceChildren(el("table", {class: "table"},
            el("thead", {}, el("tr", {}, dataset.columns.map((c) => el("th", {text: c.label})))),
            el("tbody", {}, rows.map((row) => el("tr", {}, row.map((value, j) =>
              el(j === 0 ? "th" : "td", {text: value === null ? "—"
                : (j === 0 || Number.isInteger(value) ? String(value) : value.toFixed(1))})))))));
        }).catch(() => {});
        continue;
      }
      const node = el("div", {class: "chart"});
      resultNodes.push(node);
      cards.push(el("section", {class: "card"}, el("div", {class: "card-head"}, el("h2", {text: dataset.title})), node));
      const labelOf = (column) => dataset.columns.find((c) => c.key === column)?.label || column;
      api(`/api/jobs/${id}/dataset?key=${dataset.key}`).then((table) => {
        if (dataset.style === "bands") {   // 分散曲線: すべての分枝を同じ色で
          return drawBands(node, {x: table.data[dataset.x], series: dataset.y.map((column) => table.data[column]),
                                  xLabel: labelOf(dataset.x), yLabel: "振動数 (THz)", ticks: dataset.ticks || [],
                                  key: dataset.key});
        }
        if (dataset.style === "colormap") {   // 地図: 点を color の列で色分け
          return drawColormap(node, {x: table.data[dataset.x], y: table.data[dataset.y[0]], c: table.data[dataset.color],
                                     xLabel: labelOf(dataset.x), yLabel: labelOf(dataset.y[0]),
                                     cLabel: labelOf(dataset.color), key: dataset.key,
                                     equalAxes: dataset.equal_axes !== false});
        }
        if (dataset.style === "parity") {   // 2 つの値の比較: 点と y = x の線 (ファインチューニングの力の比較)
          return drawXY(node, {x: table.data[dataset.x], xLabel: labelOf(dataset.x), yLabel: "NEP の力 (eV/Å)",
                               series: dataset.y.map((column) => ({values: table.data[column], name: labelOf(column)})),
                               markers: true, diagonal: true, key: dataset.key});
        }
        return drawXY(node, {
          x: table.data[dataset.x], xLabel: labelOf(dataset.x),
          series: dataset.y.slice(0, MAX_SERIES).map((column) => ({values: table.data[column], name: labelOf(column)})),
          yLabel: dataset.y.length === 1 ? labelOf(dataset.y[0]) : "", markers: table.n_rows < 12, key: dataset.key,
        });
      }).catch(() => {});
    }
    resultCharts.replaceChildren(...cards);
    if (explorer) purgeChart(explorer.chart);
    explorer = overview.datasets.some((d) => !d.table) ? explorerCard(id, overview) : null;
    explorerSlot.replaceChildren(...(explorer ? [explorer.node] : []));
    if (explorer) explorer.start();
  }

  async function refreshResults() {
    await refreshOverview().catch(() => {});
    const thermo = await api(`/api/jobs/${id}/thermo`);
    if (stopped || thermo.n_rows < 2) return;   // 1 点だけ (静的計算など) は時間推移にならない
    chartCard.hidden = false;
    for (const {chart, node} of chartNodes) await drawChart(node, chart, thermo);
    if (thermo.summary.length) {
      summaryCard.hidden = false;
      summaryCard.replaceChildren(
        el("div", {class: "card-head"}, el("h2", {text: "ステージごとの平均"})),
        summaryTable(thermo.summary),
        el("p", {class: "note", text: "平均 ± 標準偏差。各ステージの最初の 30% は平衡化として除いています。"}));
    }
  }

  function renderAll() {
    renderHead();
    renderActions();
    renderFiles();
    renderInputHead();
    renderLog();
    ensurePhonon().catch(() => {});
  }

  // 実行中は 2 秒ごとに状態を、6 秒ごとにグラフを更新する。終わったら最後に全部読み直して止める。
  async function poll(force = false) {
    const wasActive = isActive(job);
    job = await api(`/api/jobs/${id}`);
    if (stopped) return;
    renderAll();
    tick += 1;
    const finished = wasActive && !isActive(job);
    if (force || finished || (job.status === "running" && tick % 3 === 0)) {
      await refreshResults().catch(() => {});
      if (finished || tick % 9 === 0) await trajectory.load().catch(() => {});
    }
    if (!isActive(job) && timer) {
      clearInterval(timer);
      timer = null;
    }
  }

  renderAll();
  // PC 表示では job-body を 2 列に並べる (広いグラフは wide-card で 1 行を使う)
  view.replaceChildren(
    el("a", {class: "back-link", href: "#/jobs", text: "‹ ジョブの一覧"}),
    head, actions,
    el("div", {class: "job-body"}, highlightsSlot, resultCharts, phononSlot, chartCard, summaryCard, explorerSlot,
       trajectory.node, inputCard, filesCard, logCard));
  if (job.status !== "queued") {
    await refreshResults().catch((error) => toast(error.message));
    await trajectory.load().catch(() => {});
  }
  if (isActive(job)) timer = setInterval(() => { poll().catch(() => {}); }, 2000);

  return () => {
    stopped = true;
    clearInterval(timer);
    chartNodes.forEach(({node}) => purgeChart(node));
    resultNodes.forEach(purgeChart);
    if (explorer) purgeChart(explorer.chart);
    trajectory.destroy();
    if (phonon && phonon.destroy) phonon.destroy();
  };
}
