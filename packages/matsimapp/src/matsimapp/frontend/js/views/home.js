import {api, el} from "../dom.js";
import {jobCard} from "./jobs.js";

function statusRow(label, ok, detail) {
  return el("li", {class: "status-row"},
    el("span", {class: `dot ${ok ? "dot-ok" : "dot-ng"}`, "aria-hidden": "true"}),
    el("span", {class: "status-label", text: label}),
    el("span", {class: "status-detail", text: detail}));
}

function systemCard(system) {
  const {gpumd, toolkit, gpu, atomsk, plumed, sevennet, engine} = system;
  const gpuDetail = gpu.available
    ? `${gpu.name} ・ ${(gpu.memory_used_mb / 1024).toFixed(1)} / ${(gpu.memory_total_mb / 1024).toFixed(1)} GB ・ 使用率 ${gpu.utilization_percent}%`
    : "見つかりません";
  return el("section", {class: "card"},
    el("div", {class: "card-head"}, el("h2", {text: "計算環境"})),
    el("ul", {class: "status-list"},
      statusRow("GPUMD", gpumd.available, gpumd.available ? (gpumd.version || "バージョン不明") : "実行ファイルが見つかりません"),
      // NEP の MD・静的計算・フォノン分散を何で動かすか (GPUMD がなければ CPU の calorine)
      engine ? statusRow("NEP の計算", engine.md === "gpumd" || engine.calorine,
                         engine.md === "gpumd" ? "GPUMD (GPU)"
                           : `CPU (calorine の CPUNEP。MD・静的計算・フォノン分散だけ)${engine.calorine ? "" : " — calorine が入っていません"}`) : null,
      statusRow("GPUMD_toolkit", toolkit.available, toolkit.available ? `v${toolkit.version || "?"}` : "import できません"),
      statusRow("GPU", gpu.available, gpuDetail),
      statusRow("Atomsk (モデリング)", atomsk.available, atomsk.available ? (atomsk.version || "バージョン不明") : "実行ファイルが見つかりません"),
      plumed ? statusRow("PLUMED (自由エネルギー面)", plumed.available,
                         plumed.available ? (plumed.version || "バージョン不明") : "PLUMED 対応の gpumd が見つかりません") : null,
      sevennet ? statusRow("SevenNet (ASE で計算)", sevennet.available,
                           sevennet.available ? `v${sevennet.version || "?"}` : "SevenNet の入った Python が見つかりません") : null));
}

function jobsCard(jobs) {
  const active = jobs.filter((job) => job.status === "running" || job.status === "queued");
  const shown = active.length ? active : jobs.slice(0, 3);
  return el("section", {class: "card"},
    el("div", {class: "card-head"},
      el("h2", {text: active.length ? "実行中・実行待ちのジョブ" : "最近のジョブ"}),
      el("a", {class: "link-button", href: "#/jobs", text: "すべて見る"})),
    shown.length
      ? el("div", {class: "job-list"}, shown.map(jobCard))
      : el("p", {class: "empty", text: "まだジョブはありません。下の計算メニューから始められます。"}));
}

function calculationTile(kind) {
  return el("a", {class: "tile tile-ready", href: kind.key === "md" ? "#/md" : `#/calc/${kind.key}`},
    el("span", {class: "tile-name", text: kind.label}),
    el("span", {class: "tile-note", text: kind.note}));
}

function calculationsCard(listing) {
  return el("section", {class: "card"},
    el("div", {class: "card-head"}, el("h2", {text: "計算メニュー"}),
      el("a", {class: "link-button", href: "/manual/manual.html#ch-goal", target: "_blank", rel: "noopener", text: "どれを選ぶ?"})),
    listing.groups.map((group) => [
      el("h3", {class: "group-title", text: group}),
      el("div", {class: "tiles"}, listing.kinds.filter((kind) => kind.group === group).map(calculationTile)),
    ]));
}

export async function renderHome(view) {
  const jobsSlot = el("div", {});
  const systemSlot = el("div", {});
  let stopped = false;

  async function refresh() {
    const [system, jobs] = await Promise.all([api("/api/system"), api("/api/jobs")]);
    if (stopped) return;
    systemSlot.replaceChildren(systemCard(system));
    jobsSlot.replaceChildren(jobsCard(jobs));
  }

  const [listing] = await Promise.all([api("/api/kinds"), refresh()]);
  view.replaceChildren(
    el("header", {class: "app-header"},
      el("img", {class: "app-icon", src: "icons/icon.svg", alt: "", width: "44", height: "44"}),
      el("div", {}, el("h1", {text: "MatSimApp"}), el("p", {class: "subtitle", text: "GPUMD をスマホから"}))),
    jobsSlot, calculationsCard(listing), systemSlot);

  const timer = setInterval(() => { refresh().catch(() => {}); }, 5000);
  return () => {
    stopped = true;
    clearInterval(timer);
  };
}
