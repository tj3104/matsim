// 原子分布の画面。選んだ元素の原子を 1 つの軸に射影し、横軸を座標、縦軸を分割幅ごとの原子数にして描く。
// ジョブならトラジェクトリの最後の何枚かで平均もできる。
import {CATEGORY_COLORS, drawCategoryLines, purgeChart} from "../charts.js";
import {api, busy, el, field, number} from "../dom.js";
import {elementColor} from "../viewer.js";

export async function renderDistribution(view, query) {
  const jobId = Number(query.get("job")) || null;
  const structureId = Number(query.get("structure")) || null;
  if (!jobId && !structureId) throw new Error("解析するジョブか構造を選んでください");
  const back = jobId ? `#/jobs/${jobId}` : `#/structures/${structureId}`;
  const settings = {axis: "z", bin_width: 1.0, frames: 0};
  const selected = new Set();       // 空ならすべての元素
  let result = null;
  let allSpecies = [];
  const chart = el("div", {class: "chart"});
  const chips = el("div", {class: "chips"});
  const summary = el("div", {});
  const status = el("p", {class: "note", role: "status"});

  // 元素の色は 3D 表示と同じ (似た色が重なるときのために、2 つ目以降は分類の色に切り替える)
  const colorOf = (symbol, i) => {
    const color = elementColor(symbol);
    return color === "#b0b0b0" ? CATEGORY_COLORS[i % CATEGORY_COLORS.length] : color;
  };

  function renderChips() {
    chips.replaceChildren(...allSpecies.map((s, i) => {
      const on = selected.has(s);
      return el("button", {class: `chip${on ? " chip-on" : ""}`, type: "button", "aria-pressed": String(on), onclick: () => {
        if (on) selected.delete(s); else selected.add(s);
        renderChips();
        run();
      }}, el("span", {class: "legend-swatch", style: `background:${colorOf(s, i)}`, "aria-hidden": "true"}), s);
    }));
  }

  async function draw() {
    const series = result.species.map((s) => ({name: s, values: result.counts[s], color: colorOf(s, allSpecies.indexOf(s))}));
    if (result.species.length > 1) series.push({name: "合計", values: result.total, color: "#6b7280", dash: "dot"});
    const unit = result.n_frames > 1 ? `原子数 (${result.n_frames} フレームの平均)` : "原子数";
    await drawCategoryLines(chart, {x: result.centers, series, xLabel: `${result.axis} 座標 (Å)`,
                                    yLabel: `${unit} / ${result.bin_width} Å`, key: "distribution", shape: "hvh",
                                    markers: false});
  }

  function csv() {
    const header = [`${result.axis}_A`, ...result.species.map((s) => `count_${s}`), "count_total"];
    const rows = result.centers.map((x, i) => [x, ...result.species.map((s) => result.counts[s][i]), result.total[i]]);
    const blob = new Blob([[header, ...rows].map((r) => r.join(",")).join("\n") + "\n"], {type: "text/csv"});
    const link = el("a", {href: URL.createObjectURL(blob), download: `distribution_${result.axis}.csv`});
    link.click();
    setTimeout(() => URL.revokeObjectURL(link.href), 1000);
  }

  async function run() {
    status.textContent = "数えています…";
    try {
      result = await api("/api/analysis/distribution", {method: "POST", body: {
        job_id: jobId, structure_id: structureId, species: [...selected], ...settings}});
    } catch (error) {
      status.textContent = error.message;
      return;
    }
    status.textContent = "";
    if (!allSpecies.length) {
      allSpecies = result.all_species;
      renderChips();
    }
    summary.replaceChildren(el("dl", {class: "facts"},
      el("dt", {text: "原子の数"}),
      el("dd", {text: result.species.map((s) => `${s} ${number(result.n_atoms[s])}`).join(" ・ ")}),
      el("dt", {text: "範囲"}), el("dd", {text: `${result.range[0].toFixed(2)} 〜 ${result.range[1].toFixed(2)} Å (${result.centers.length} 区間)`}),
      el("dt", {text: "セル"}), el("dd", {text: `${result.cell_lengths.join(" × ")} Å`}),
      el("dt", {text: "フレーム"}), el("dd", {text: result.n_frames > 1 ? `最後の ${result.n_frames} 枚の平均` : "最終構造 1 枚"})));
    requestAnimationFrame(() => draw().catch(() => {}));
  }

  const axis = el("select", {onchange: () => { settings.axis = axis.value; run(); }},
    ["x", "y", "z"].map((a) => el("option", {value: a, selected: a === settings.axis, text: `${a} 方向`})));
  const width = el("input", {type: "number", inputmode: "decimal", step: "any", min: "0.01", value: settings.bin_width,
    onchange: () => {
      settings.bin_width = Math.max(0.01, Number(width.value) || 1);
      run();
    }});
  const frames = el("input", {type: "number", inputmode: "numeric", step: "1", min: "0", max: "200", value: settings.frames,
    onchange: () => {
      settings.frames = Math.max(0, Math.min(200, Math.round(Number(frames.value) || 0)));
      run();
    }});
  const save = el("button", {class: "secondary", type: "button", text: "CSV で保存", onclick: () => result && csv()});
  const refresh = el("button", {class: "primary", type: "button", text: "表示する", onclick: () => busy(refresh, run)});

  view.replaceChildren(
    el("a", {class: "back-link", href: back, text: jobId ? "‹ ジョブに戻る" : "‹ 構造に戻る"}),
    el("h1", {class: "page-title", text: "原子分布"}),
    el("p", {class: "note", text: "選んだ元素の原子を軸に射影し、分割幅ごとの原子数を描きます (膜の厚み方向の分布、界面への偏析など)。"}),
    el("section", {class: "card"},
      el("div", {class: "card-head"}, el("h2", {text: "条件"})),
      el("div", {class: "field"}, el("span", {class: "field-label", text: "元素 (選ばなければすべて)"}), chips),
      el("div", {class: "grid-2"}, field("射影する軸", axis), field("分割幅 (Å)", width)),
      jobId ? field("平均するフレーム数", frames, "トラジェクトリの最後の何枚で平均するか。0 で最終構造だけ") : null,
      el("div", {class: "button-row"}, refresh, save), status),
    el("section", {class: "card"}, el("div", {class: "card-head"}, el("h2", {text: "分布"})), chart, summary));
  await run();
  return () => purgeChart(chart);
}
