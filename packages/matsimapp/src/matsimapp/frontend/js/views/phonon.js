// フォノンの振動のようす。フォノン分散のジョブで、分枝 (バンド) を 1 つ選び、q をスライダーで動かすと、
// その振動 (固有ベクトル) を原子モデルの上に矢印で描き、原子を動かして見せる。分散曲線をタップしても選べる。
// サーバーが D.out (q 点ごとの動的行列) を対角化して、原子ごとの変位の振幅を返す (matsimapp/phonon_modes.py)。
import {drawBands, purgeChart} from "../charts.js";
import {api, el, field, number} from "../dom.js";
import {Viewer} from "../viewer.js";

const MAX_SHOWN = 1500;   // 表示する原子の数の上限 (動かすので軽く)
const ARROW_COLOR = "#1d3fd1";

const dot = (u, v) => u[0] * v[0] + u[1] * v[1] + u[2] * v[2];
const matmul = (m, cell) => m.map((row) => [0, 1, 2].map((x) => row[0] * cell[0][x] + row[1] * cell[1][x] + row[2] * cell[2][x]));
const det3 = (m) => m[0][0] * (m[1][1] * m[2][2] - m[1][2] * m[2][1]) - m[0][1] * (m[1][0] * m[2][2] - m[1][2] * m[2][0])
  + m[0][2] * (m[1][0] * m[2][1] - m[1][1] * m[2][0]);
function inverse3(m) {
  const d = det3(m);
  const c = (a, b, e, f) => (m[a][b] * m[e][f] - m[a][f] * m[e][b]) / d;
  return [[c(1, 1, 2, 2), -c(0, 1, 2, 2), c(0, 1, 1, 2)], [-c(1, 0, 2, 2), c(0, 0, 2, 2), -c(0, 0, 1, 2)],
          [c(1, 0, 2, 1), -c(0, 0, 2, 1), c(0, 0, 1, 1)]];
}

// 表示に使うセル: 基本単位胞の整数倍 (係数 −1〜1) で作れる、なるべく直方体に近いセル。
// fcc・bcc の基本単位胞 (ひし形) なら立方体の慣用単位胞になり、並べたときに波の様子が見やすい
function conventional(cell) {
  let best = {m: [[1, 0, 0], [0, 1, 0], [0, 0, 1]], cost: Infinity};
  const values = [-1, 0, 1];
  const rows = [];
  for (const a of values) for (const b of values) for (const c of values) if (a || b || c) rows.push([a, b, c]);
  for (const r1 of rows) for (const r2 of rows) for (const r3 of rows) {
    const m = [r1, r2, r3];
    const d = det3(m);
    if (d <= 0 || d > 4) continue;
    const v = matmul(m, cell);
    const len = v.map((x) => Math.sqrt(dot(x, x)));
    const skew = Math.abs(dot(v[0], v[1])) / (len[0] * len[1]) + Math.abs(dot(v[0], v[2])) / (len[0] * len[2])
      + Math.abs(dot(v[1], v[2])) / (len[1] * len[2]);
    // 直方体に近いほど、辺の長さがそろっているほど、セルが小さいほどよい
    const cost = skew + 0.1 * (Math.max(...len) / Math.min(...len) - 1) + 0.02 * d;
    if (cost < best.cost - 1e-9) best = {m, cost, d};
  }
  return best.cost < Infinity ? best : {m: [[1, 0, 0], [0, 1, 0], [0, 0, 1]], d: 1};
}

// 表示するセルの数の既定: セルの原子が少ないほど多く並べる (波の様子が見えるように)
function defaultRepeat(nBasis) {
  let n = 4;
  while (n > 1 && nBasis * n ** 3 > 300) n -= 1;
  return [n, n, n];
}

export async function phononCard(jobId) {
  const [info, table] = await Promise.all([api(`/api/jobs/${jobId}/phonon`), api(`/api/jobs/${jobId}/dataset?key=dispersion`)]);
  const xs = table.data.distance;
  const bands = Array.from({length: info.n_bands}, (_, i) => table.data[`nu_${i + 1}`]);
  const nBasis = info.symbols.length;
  const conv = conventional(info.cell);
  const state = {band: Math.min(info.n_bands - 1, 3 * nBasis > 3 ? 3 : 0), q: Math.round(info.ticks[1].index / 2),
                 repeat: defaultRepeat(nBasis * conv.d), motion: 0.35, arrowLength: 1.4, playing: true, arrows: true};
  let mode = null;
  let phase = 0;
  let frame = null;
  let stopped = false;
  let pending = null;

  const chart = el("div", {class: "chart"});
  const canvas = el("canvas", {class: "viewer", "aria-label": "振動のようすの 3D 表示。ドラッグで回転、ピンチで拡大縮小"});
  const viewer = new Viewer(canvas);
  const readout = el("p", {class: "phonon-readout", role: "status"});
  const kindNote = el("p", {class: "note"});
  const bandSelect = el("select", {onchange: () => { state.band = Number(bandSelect.value); update(); }},
    bands.map((_, i) => el("option", {value: i, selected: i === state.band,
                                      text: `分枝 ${i + 1}${i < 3 ? " (音響)" : " (光学)"}`})));
  const slider = el("input", {type: "range", min: "0", max: String(info.n_q - 1), step: "1", value: state.q,
                              "aria-label": "q の位置", oninput: () => { state.q = Number(slider.value); update(); }});
  const tickRow = el("div", {class: "phonon-ticks"}, info.ticks.map((tk) => el("button", {class: "secondary small", type: "button",
    text: tk.label, onclick: () => { state.q = tk.index; slider.value = tk.index; update(); }})));
  const repeatInputs = [0, 1, 2].map((k) => el("input", {type: "number", inputmode: "numeric", min: "1", max: "10", step: "1",
    value: state.repeat[k], "aria-label": `${"abc"[k]} 方向のセルの数`, onchange: () => {
      state.repeat = repeatInputs.map((input) => Math.max(1, Math.min(10, Math.round(Number(input.value) || 1))));
      render();
    }}));
  // 原子の動きと矢印は、それぞれ独立にオン・オフと大きさを決める
  const motionValue = el("span", {class: "range-value", text: `${state.motion.toFixed(2)} Å`});
  const motionInput = el("input", {type: "range", min: "0.05", max: "1", step: "0.05", value: state.motion,
                                   "aria-label": "動きの大きさ", oninput: () => {
                                     state.motion = Number(motionInput.value);
                                     motionValue.textContent = `${state.motion.toFixed(2)} Å`;
                                     render();
                                   }});
  const playCheck = el("label", {class: "check"}, el("input", {type: "checkbox", checked: true, onchange: (e) => {
    state.playing = e.target.checked;
    motionInput.disabled = !state.playing;
    if (state.playing) loop(); else { phase = 0; render(); }   // 止めたら平衡の位置に戻す
  }}), "原子を動かす");
  const arrowValue = el("span", {class: "range-value", text: `${state.arrowLength.toFixed(1)} Å`});
  const arrowInput = el("input", {type: "range", min: "0.2", max: "4", step: "0.1", value: state.arrowLength,
                                  "aria-label": "矢印の長さ", oninput: () => {
                                    state.arrowLength = Number(arrowInput.value);
                                    arrowValue.textContent = `${state.arrowLength.toFixed(1)} Å`;
                                    render();
                                  }});
  const arrowCheck = el("label", {class: "check"}, el("input", {type: "checkbox", checked: true, onchange: (e) => {
    state.arrows = e.target.checked;
    arrowInput.disabled = !state.arrows;
    render();
  }}), "矢印を描く");

  // 表示する原子 (並べたセル) の平衡位置と、q·r (位相)
  let sites = [];
  let superCell = info.cell;
  function buildSites() {
    // 表示のセル (直方体に近いセル × 並べる数) の中に入る、基本単位胞の格子点 + 原子をすべて拾う
    superCell = matmul(conv.m, info.cell).map((v, n) => v.map((x) => x * state.repeat[n]));
    const inv = inverse3(superCell);   // 直交座標 → 表示のセルの分数座標 (行ベクトル × inv)
    const frac = (p) => [0, 1, 2].map((x) => p[0] * inv[0][x] + p[1] * inv[1][x] + p[2] * inv[2][x]);
    const [a, b, c] = info.cell;
    const reach = 2 * Math.max(...state.repeat) + 2;
    sites = [];
    for (let i = -reach; i <= reach && sites.length < MAX_SHOWN; i += 1) {
      for (let j = -reach; j <= reach && sites.length < MAX_SHOWN; j += 1) {
        for (let k = -reach; k <= reach && sites.length < MAX_SHOWN; k += 1) {
          for (let s = 0; s < nBasis; s += 1) {
            const r = info.positions[s];
            const pos = [0, 1, 2].map((x) => r[x] + i * a[x] + j * b[x] + k * c[x]);
            const f = frac(pos);
            if (f.every((v) => v >= -1e-6 && v < 1 - 1e-6)) sites.push({pos, basis: s, symbol: info.symbols[s]});
          }
        }
      }
    }
  }

  // 位相 phase のときの変位の向きと大きさ: Re[u_a exp(i(q·r − phase))] (最も大きく動く成分が 1 になるようそろえてある)。
  // 原子の動きは × 動きの大きさ、矢印は × 矢印の長さ で、別々に大きさを決める
  function displacement(site, ph) {
    const re = mode.u_re[site.basis];
    const im = mode.u_im[site.basis];
    const theta = mode.q_cart[0] * site.pos[0] + mode.q_cart[1] * site.pos[1] + mode.q_cart[2] * site.pos[2] - ph;
    const c = Math.cos(theta);
    const s = Math.sin(theta);
    return [0, 1, 2].map((x) => re[x] * c - im[x] * s);
  }

  function render() {
    if (!mode) return;
    const motion = state.playing ? state.motion : 0;
    const positions = sites.map((site) => {
      const d = displacement(site, phase);
      return [0, 1, 2].map((x) => site.pos[x] + motion * d[x]);
    });
    viewer.setFrame(positions, superCell);
    if (state.arrows) {
      // 矢印は平衡位置から、変位の向き (位相 0 のとき) に。いちばん大きく動く原子の矢印が「矢印の長さ」(Å) になる。
      // 色は原子の色 (元素の色) と紛れにくい濃い青
      viewer.setArrows(sites.map((site) => {
        const d = displacement(site, 0);
        if (Math.hypot(...d) < 0.02) return null;
        return {from: site.pos, to: [0, 1, 2].map((x) => site.pos[x] + state.arrowLength * d[x]), color: ARROW_COLOR};
      }).filter(Boolean));
    } else {
      viewer.setArrows(null);
    }
  }

  function loop() {
    if (stopped || !state.playing) return;
    phase = (phase + 0.06) % (2 * Math.PI);
    render();
    frame = requestAnimationFrame(loop);
  }

  async function drawChart() {
    await drawBands(chart, {x: xs, series: bands, yLabel: "振動数 (THz)", key: `phonon-${jobId}`,
                            ticks: info.ticks.map((tk) => ({x: xs[tk.index], label: tk.label})),
                            highlight: state.band, cursor: {x: xs[state.q], y: bands[state.band][state.q]}});
  }

  async function fetchMode() {
    const key = `${state.q}:${state.band}`;
    pending = key;
    const result = await api(`/api/jobs/${jobId}/phonon/mode?q=${state.q}&band=${state.band}`);
    if (pending !== key || stopped) return;
    mode = result;
    const q = result.q_frac.map((v) => v.toFixed(3)).join(", ");
    readout.textContent = `分枝 ${state.band + 1} ・ q = (${q})${result.near ? ` = ${result.near} 点` : ""} ・ `
      + `ν = ${result.frequency.toFixed(2)} THz` + (result.frequency < -0.1 ? " (虚振動: この向きに構造が不安定)" : "");
    const atGamma = result.q_frac.every((v) => Math.abs(v) < 1e-6);
    kindNote.textContent = state.band < 3
      ? (atGamma ? "音響モード (Γ 点): すべての原子が同じ向きに動く、結晶全体の平行移動です。"
        : "音響モード: q が小さいほど、となりの原子どうしが同じ向きに動く長い波 (音波) になります。")
      : (atGamma ? "光学モード (Γ 点): どのセルも同じ動き。セルの中の原子どうしが逆向きに動きます。"
        : "光学モード: セルの中の原子どうしが逆向きに動く振動が、q の向きに波として伝わります。");
    render();
  }

  let timer = null;
  function update() {
    bandSelect.value = state.band;
    drawChart().catch(() => {});
    clearTimeout(timer);
    timer = setTimeout(() => fetchMode().catch((error) => { readout.textContent = error.message; }), 80);
  }

  const node = el("section", {class: "card"},
    el("div", {class: "card-head"}, el("h2", {text: "振動のようす"})),
    el("p", {class: "note", text: "分枝を選んで q を動かすと、その振動で原子がどう動くかを矢印と動きで示します。分散曲線をタップしても選べます。"}),
    chart,
    el("div", {class: "grid-2"}, field("分枝 (バンド)", bandSelect), el("div", {class: "field"},
      el("span", {class: "field-label", text: "高対称点へ"}), tickRow)),
    field("q の位置", slider),
    readout,
    canvas,
    kindNote,
    el("div", {class: "phonon-controls"},
      el("div", {class: "phonon-control"}, playCheck,
        field("動きの大きさ", el("div", {class: "range-row"}, motionInput, motionValue),
              "いちばん大きく動く原子の振れ幅。止めると平衡の位置に戻ります")),
      el("div", {class: "phonon-control"}, arrowCheck,
        field("矢印の長さ", el("div", {class: "range-row"}, arrowInput, arrowValue), "いちばん大きく動く原子の矢印の長さ"))),
    el("p", {class: "note", text: "動きと矢印の大きさは見やすさのためのもので、本当の振幅や周期ではありません (相対的な大きさと向きが固有ベクトル)。"}),
    el("div", {class: "field"}, el("span", {class: "field-label", text: "並べるセルの数 (a × b × c)"}),
      el("div", {class: "grid-3"}, ...repeatInputs)),
    el("p", {class: "note", text: `基本単位胞 ${nBasis} 原子。${number(MAX_SHOWN)} 原子まで表示します。`}));

  buildSites();
  viewer.setStructure({symbols: sites.map((s) => s.symbol), positions: sites.map((s) => s.pos), cell: superCell});
  repeatInputs.forEach((input) => input.addEventListener("change", () => {
    buildSites();
    viewer.setStructure({symbols: sites.map((s) => s.symbol), positions: sites.map((s) => s.pos), cell: superCell});
    render();
  }));
  // 分散曲線をタップ: その分枝と q を選ぶ (plotly のグラフは描いたあとに on でイベントを受け取れる)
  requestAnimationFrame(() => {
    drawChart().then(() => {
      chart.on?.("plotly_click", (event) => {
        const point = event.points && event.points[0];
        if (!point || point.curveNumber >= info.n_bands) return;
        state.band = point.curveNumber;
        state.q = point.pointIndex;
        slider.value = state.q;
        update();
      });
    }).catch(() => {});
  });
  await fetchMode();
  loop();
  return {
    node,
    destroy() {
      stopped = true;
      if (frame) cancelAnimationFrame(frame);
      purgeChart(chart);
      viewer.destroy();
    },
  };
}
