// 熱力学量の時間推移グラフ (plotly.js)。量ごとに 1 枚ずつ描き、y 軸は 1 つだけにする。

let plotlyPromise = null;

// plotly.js は 5MB 近くあるので、グラフを初めて出すときに読み込む
export function loadPlotly() {
  if (!plotlyPromise) {
    plotlyPromise = new Promise((resolve, reject) => {
      const script = document.createElement("script");
      script.src = "vendor/plotly.min.js";
      script.onload = () => resolve(window.Plotly);
      script.onerror = () => {
        plotlyPromise = null;
        reject(new Error("グラフの部品を読み込めませんでした"));
      };
      document.head.append(script);
    });
  }
  return plotlyPromise;
}

// 系列の色。ライト・ダークそれぞれの面の色に対して、色覚特性での見分けやすさを検証済みの並び。
// ライトの 3・4 色目は面とのコントラストが低めなので、凡例の文字と csv の表で補う。
const SERIES = {light: ["#2a78d6", "#eb6834", "#1baf7a", "#eda100"], dark: ["#3987e5", "#d95926", "#199e70", "#c98500"]};

export const CHARTS = [
  {key: "temperature", title: "温度", unit: "K", series: [
    {column: "temperature", name: "温度"},
    {column: "target_temperature", name: "目標温度", dash: "dash"},
  ]},
  {key: "pressure", title: "圧力", unit: "GPa", series: [{column: "pressure", name: "圧力"}]},
  {key: "energy", title: "エネルギー", unit: "eV/atom", series: [
    {column: "potential_energy_per_atom", name: "ポテンシャル"},
    {column: "total_energy_per_atom", name: "全エネルギー"},
  ]},
  {key: "volume", title: "体積", unit: "Å³", series: [{column: "volume", name: "体積"}]},
];

function theme(node) {
  const style = getComputedStyle(node);
  const token = (name) => style.getPropertyValue(name).trim();
  const dark = window.matchMedia("(prefers-color-scheme: dark)").matches;
  return {text: token("--text"), muted: token("--muted"), line: token("--line"), surface: token("--card"),
          series: dark ? SERIES.dark : SERIES.light};
}

export async function drawChart(node, chart, data) {
  const Plotly = await loadPlotly();
  const colors = theme(node);
  const time = data.series.time_ps;
  const traces = chart.series
    .filter((s) => data.series[s.column] && data.series[s.column].some((v) => v !== null))
    .map((s, i) => ({
      x: time, y: data.series[s.column], name: s.name, type: "scatter", mode: "lines",
      line: {color: colors.series[i], width: 2, dash: s.dash || "solid"},
      hovertemplate: `%{y:.4~g} ${chart.unit}<extra>${s.name}</extra>`,
    }));
  // ステージの境目に細い縦線を入れる
  const shapes = data.stages.slice(1).map((stage) => ({
    type: "line", xref: "x", yref: "paper", x0: stage.start_ps, x1: stage.start_ps, y0: 0, y1: 1,
    line: {color: colors.line, width: 1, dash: "dot"},
  }));
  const axis = {gridcolor: colors.line, linecolor: colors.line, zeroline: false, tickfont: {color: colors.muted, size: 11},
                title: {font: {color: colors.muted, size: 11}}};
  const layout = {
    height: 230, margin: {l: 58, r: 12, t: 8, b: 40},
    paper_bgcolor: "rgba(0,0,0,0)", plot_bgcolor: "rgba(0,0,0,0)",
    font: {family: "inherit", color: colors.text, size: 12},
    xaxis: {...axis, title: {...axis.title, text: "時間 (ps)"}},
    yaxis: {...axis, title: {...axis.title, text: chart.unit}, tickformat: ".5~g"},
    showlegend: traces.length > 1,
    legend: {orientation: "h", x: 0, y: 1.18, font: {color: colors.text, size: 11}},
    hovermode: "x unified",
    hoverlabel: {bgcolor: colors.surface, bordercolor: colors.line, font: {color: colors.text, size: 12}},
    shapes,
    uirevision: chart.key,   // 実行中に更新しても、拡大した範囲を保つ
  };
  if (traces.length > 1) layout.margin.t = 30;
  await Plotly.react(node, traces, layout, {responsive: true, displayModeBar: false});
}

// 表の列を自由に選んで描く。series は [{values, name}] で、横軸が時間とは限らないので
// 点の並びは表の行の順のまま結ぶ (温度と体積のような関係も、たどった順に線になる)。
// diagonal を true にすると y = x の線を引き、縦横の目盛りをそろえる (2 つの値の比較)
export async function drawXY(node, {x, xLabel, series, yLabel, markers = false, key = "xy", diagonal = false}) {
  const Plotly = await loadPlotly();
  const colors = theme(node);
  const palette = colors.series;
  const traces = series.map((s, i) => ({
    x, y: s.values, name: s.name, type: "scatter", mode: markers ? "markers" : "lines",
    line: {color: palette[i], width: 2},
    marker: diagonal ? {color: palette[i], size: 5, opacity: 0.6}
                     : {color: palette[i], size: 8, line: {color: colors.surface, width: 2}},
    hovertemplate: `%{y:.5~g}<extra>${s.name}</extra>`,
  }));
  if (diagonal) {
    const values = [...x, ...series.flatMap((s) => s.values)].filter((v) => v !== null && Number.isFinite(v));
    const lo = Math.min(...values), hi = Math.max(...values);
    traces.unshift({x: [lo, hi], y: [lo, hi], type: "scatter", mode: "lines", name: "y = x", hoverinfo: "skip",
                    line: {color: colors.muted, width: 1, dash: "dot"}});
  }
  const axis = {gridcolor: colors.line, linecolor: colors.line, zeroline: false, tickfont: {color: colors.muted, size: 11},
                title: {font: {color: colors.muted, size: 11}}, tickformat: ".5~g"};
  const layout = {
    height: 260, margin: {l: 62, r: 12, t: traces.length > 1 ? 30 : 8, b: 44},
    paper_bgcolor: "rgba(0,0,0,0)", plot_bgcolor: "rgba(0,0,0,0)",
    font: {family: "inherit", color: colors.text, size: 12},
    xaxis: {...axis, title: {...axis.title, text: xLabel}},
    yaxis: {...axis, title: {...axis.title, text: yLabel}},
    showlegend: traces.length > 1,
    legend: {orientation: "h", x: 0, y: 1.16, font: {color: colors.text, size: 11}},
    hovermode: markers ? "closest" : "x unified",
    hoverlabel: {bgcolor: colors.surface, bordercolor: colors.line, font: {color: colors.text, size: 12}},
    uirevision: key,
  };
  if (diagonal) {
    layout.height = 340;
    layout.yaxis.scaleanchor = "x";
  }
  await Plotly.react(node, traces, layout, {responsive: true, displayModeBar: false});
}

// 横軸がそれぞれ違う系列を重ねる (別のジョブの結果の比較)。series: [{x, y, name}]
export async function drawSeries(node, {series, xLabel, yLabel, markers = false, key = "series", logY = false, legend = true}) {
  const Plotly = await loadPlotly();
  const colors = theme(node);
  const traces = series.map((s, i) => ({
    x: s.x, y: s.y, name: s.name, type: "scatter", mode: markers ? "lines+markers" : "lines",
    line: {color: colors.series[i % colors.series.length], width: 2},
    marker: {color: colors.series[i % colors.series.length], size: 8, line: {color: colors.surface, width: 2}},
    text: s.text, hovertemplate: s.text ? "%{text}<br>%{x:.5~g}, %{y:.5~g}<extra></extra>" : `%{x:.5~g}, %{y:.5~g}<extra>${s.name}</extra>`,
  }));
  const axis = {gridcolor: colors.line, linecolor: colors.line, zeroline: false, tickfont: {color: colors.muted, size: 11},
                title: {font: {color: colors.muted, size: 11}}, tickformat: ".5~g"};
  await Plotly.react(node, traces, {
    height: 320, margin: {l: 62, r: 12, t: 30, b: 44}, paper_bgcolor: "rgba(0,0,0,0)", plot_bgcolor: "rgba(0,0,0,0)",
    font: {family: "inherit", color: colors.text, size: 12},
    xaxis: {...axis, title: {...axis.title, text: xLabel}},
    yaxis: {...axis, type: logY ? "log" : "linear", title: {...axis.title, text: yLabel}},
    showlegend: legend, legend: {orientation: "h", x: 0, y: 1.16, font: {color: colors.text, size: 11}},
    hovermode: "closest", hoverlabel: {bgcolor: colors.surface, bordercolor: colors.line, font: {color: colors.text, size: 12}},
    uirevision: key,
  }, {responsive: true, displayModeBar: false});
}

// 分散曲線のように、同じ意味の線をたくさん重ねる (色は 1 色、凡例なし)。ticks で横軸の目盛りに名前を付ける。
// highlight: 目立たせる分枝の番号 (ほかは薄く)、cursor: {x, y} の位置に印 (フォノンの振動のようすで選んでいる q)
export async function drawBands(node, {x, series, xLabel, yLabel, ticks = [], key = "bands", highlight = null, cursor = null}) {
  const Plotly = await loadPlotly();
  const colors = theme(node);
  const traces = series.map((values, i) => ({x, y: values, type: "scatter", mode: "lines", showlegend: false,
                                          line: {color: highlight === null || i === highlight ? colors.series[0] : colors.line,
                                                 width: i === highlight ? 3 : 1.5},
                                          hovertemplate: "%{y:.4~g}<extra></extra>"}));
  if (cursor) {
    traces.push({x: [cursor.x], y: [cursor.y], type: "scatter", mode: "markers", showlegend: false, hoverinfo: "skip",
                 marker: {size: 13, color: colors.series[1], line: {color: colors.surface, width: 2}}});
  }
  const axis = {gridcolor: colors.line, linecolor: colors.line, zeroline: false, tickfont: {color: colors.muted, size: 11},
                title: {font: {color: colors.muted, size: 11}}};
  const xaxis = {...axis, title: {...axis.title, text: ticks.length ? "" : xLabel}};
  if (ticks.length) Object.assign(xaxis, {tickvals: ticks.map((t) => t.x), ticktext: ticks.map((t) => t.label),
                                          range: [ticks[0].x, ticks[ticks.length - 1].x]});
  const layout = {
    height: 300, margin: {l: 58, r: 12, t: 8, b: 40},
    paper_bgcolor: "rgba(0,0,0,0)", plot_bgcolor: "rgba(0,0,0,0)",
    font: {family: "inherit", color: colors.text, size: 12},
    xaxis, yaxis: {...axis, title: {...axis.title, text: yLabel}, tickformat: ".4~g", zeroline: true, zerolinecolor: colors.line},
    shapes: ticks.map((t) => ({type: "line", xref: "x", yref: "paper", x0: t.x, x1: t.x, y0: 0, y1: 1,
                               line: {color: colors.line, width: 1}})),
    hovermode: "closest",
    hoverlabel: {bgcolor: colors.surface, bordercolor: colors.line, font: {color: colors.text, size: 12}},
    uirevision: key,
  };
  await Plotly.react(node, traces, layout, {responsive: true, displayModeBar: false});
}

// マス目ごとの値を色で描く (場所ごとの平均エネルギーの地図など)。x, y, c は同じ長さの列で、値のないマス目は空白になる。
// WebGL を使わない heatmap にしてある (WebGL が使えない端末でも表示できるように)。
export async function drawColormap(node, {x, y, c, xLabel, yLabel, cLabel, key = "colormap", equalAxes = true}) {
  const Plotly = await loadPlotly();
  const colors = theme(node);
  const trace = {x, y, z: c, type: "heatmap", colorscale: "Viridis", hoverongaps: false, showlegend: false,
                 colorbar: {title: {text: cLabel, side: "right", font: {color: colors.muted, size: 11}},
                            tickfont: {color: colors.muted, size: 10}, thickness: 12},
                 hovertemplate: `%{x:.2f}, %{y:.2f}<br>${cLabel}: %{z:.4~g}<extra></extra>`};
  const axis = {gridcolor: colors.line, linecolor: colors.line, zeroline: false, tickfont: {color: colors.muted, size: 11},
                title: {font: {color: colors.muted, size: 11}}};
  const layout = {
    height: 340, margin: {l: 52, r: 8, t: 8, b: 40},
    paper_bgcolor: "rgba(0,0,0,0)", plot_bgcolor: "rgba(0,0,0,0)",
    font: {family: "inherit", color: colors.text, size: 12},
    xaxis: {...axis, title: {...axis.title, text: xLabel}},
    // 座標の地図は縦横の目盛りをそろえる。単位の違う 2 つの量 (自由エネルギー面の CV など) はそろえない
    yaxis: {...axis, title: {...axis.title, text: yLabel}, ...(equalAxes ? {scaleanchor: "x"} : {})},
    hovermode: "closest",
    hoverlabel: {bgcolor: colors.surface, bordercolor: colors.line, font: {color: colors.text, size: 12}},
    uirevision: key,
  };
  await Plotly.react(node, [trace], layout, {responsive: true, displayModeBar: false});
}

// 解析の分類 (ボロノイ指数など) に付ける色。色塗りの 3D 表示と、グラフの色をそろえる。
export const CATEGORY_COLORS = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#8f5bd6", "#d6457a", "#16a3b8",
                                "#7a9a01", "#b5651d", "#5467a8", "#c23b22", "#2e8b57"];

// 棒グラフ。labels と values は同じ長さ、colors を渡すと棒ごとの色にする。
export async function drawBars(node, {labels, values, colors, xLabel = "", yLabel = "", key = "bars", percent = false}) {
  const Plotly = await loadPlotly();
  const theme_ = theme(node);
  const trace = {x: labels, y: values, type: "bar", marker: {color: colors || theme_.series[0]},
                 hovertemplate: percent ? "%{x}: %{y:.1%}<extra></extra>" : "%{x}: %{y}<extra></extra>"};
  const axis = {gridcolor: theme_.line, linecolor: theme_.line, zeroline: false, tickfont: {color: theme_.muted, size: 11},
                title: {font: {color: theme_.muted, size: 11}}};
  const layout = {
    height: 260, margin: {l: 52, r: 8, t: 8, b: 70},
    paper_bgcolor: "rgba(0,0,0,0)", plot_bgcolor: "rgba(0,0,0,0)",
    font: {family: "inherit", color: theme_.text, size: 12},
    xaxis: {...axis, title: {...axis.title, text: xLabel}, type: "category", tickangle: -45},
    yaxis: {...axis, title: {...axis.title, text: yLabel}, tickformat: percent ? ".0%" : ""},
    hoverlabel: {bgcolor: theme_.surface, bordercolor: theme_.line, font: {color: theme_.text, size: 12}},
    uirevision: key,
  };
  await Plotly.react(node, [trace], layout, {responsive: true, displayModeBar: false});
}

// 分類ごとに決まった色で線を描く (本数の上限なし)。series: [{name, values, color, dash}]
// shape="hvh" で階段状 (ヒストグラムの見た目)、markers=false で点を描かない。
export async function drawCategoryLines(node, {x, series, xLabel, yLabel, key = "lines", percent = false,
                                               shape = "linear", markers = true}) {
  const Plotly = await loadPlotly();
  const theme_ = theme(node);
  const traces = series.map((s) => ({x, y: s.values, name: s.name, type: "scatter", mode: markers ? "lines+markers" : "lines",
                                     line: {color: s.color, width: 2, dash: s.dash || "solid", shape}, marker: {size: 5},
                                     hovertemplate: percent ? `%{y:.1%}<extra>${s.name}</extra>` : `%{y:.4~g}<extra>${s.name}</extra>`}));
  const axis = {gridcolor: theme_.line, linecolor: theme_.line, zeroline: false, tickfont: {color: theme_.muted, size: 11},
                title: {font: {color: theme_.muted, size: 11}}};
  const layout = {
    height: 300, margin: {l: 52, r: 8, t: 8, b: 44},
    paper_bgcolor: "rgba(0,0,0,0)", plot_bgcolor: "rgba(0,0,0,0)",
    font: {family: "inherit", color: theme_.text, size: 12},
    xaxis: {...axis, title: {...axis.title, text: xLabel}},
    yaxis: {...axis, title: {...axis.title, text: yLabel}, tickformat: percent ? ".0%" : ".4~g", rangemode: "tozero"},
    showlegend: true, legend: {orientation: "h", x: 0, y: -0.25, font: {color: theme_.text, size: 11}},
    hovermode: "x unified",
    hoverlabel: {bgcolor: theme_.surface, bordercolor: theme_.line, font: {color: theme_.text, size: 12}},
    uirevision: key,
  };
  layout.height += Math.ceil(series.length / 3) * 18;
  layout.margin.b += Math.ceil(series.length / 3) * 18;
  await Plotly.react(node, traces, layout, {responsive: true, displayModeBar: false});
}

// アレニウスプロット。使った点は塗りつぶし、外した点は白抜きで描き、当てはめた直線を重ねる。
// points: [{x, y, temperature, used, label}]、fit: {slope, intercept} または null
export async function drawArrhenius(node, {points, fit, yLabel, key}) {
  const Plotly = await loadPlotly();
  const colors = theme(node);
  const color = colors.series[0];
  const make = (rows, used) => ({
    x: rows.map((p) => p.x), y: rows.map((p) => p.y), customdata: rows.map((p) => [p.temperature, p.label]),
    type: "scatter", mode: "markers", name: used ? "使った点" : "外した点",
    marker: {size: 10, color: used ? color : colors.surface, line: {color, width: 2}},
    hovertemplate: "%{customdata[1]}<br>T = %{customdata[0]:.1f} K<br>1000/T = %{x:.4f}<br>%{y:.4f}<extra></extra>",
  });
  const used = points.filter((p) => p.used);
  const unused = points.filter((p) => !p.used);
  const traces = [make(used, true)];
  if (unused.length) traces.push(make(unused, false));
  if (fit) {
    const xs = points.map((p) => p.x);
    const pad = (Math.max(...xs) - Math.min(...xs)) * 0.05 || 0.05;
    const x0 = Math.min(...xs) - pad;
    const x1 = Math.max(...xs) + pad;
    traces.push({x: [x0, x1], y: [fit.intercept + fit.slope * x0, fit.intercept + fit.slope * x1], type: "scatter",
                 mode: "lines", name: "当てはめ", line: {color: colors.series[1], width: 2, dash: "dash"},
                 hoverinfo: "skip"});
  }
  const axis = {gridcolor: colors.line, linecolor: colors.line, zeroline: false, tickfont: {color: colors.muted, size: 11},
                title: {font: {color: colors.muted, size: 11}}, tickformat: ".4~g"};
  const layout = {
    height: 280, margin: {l: 62, r: 12, t: 30, b: 44},
    paper_bgcolor: "rgba(0,0,0,0)", plot_bgcolor: "rgba(0,0,0,0)",
    font: {family: "inherit", color: colors.text, size: 12},
    xaxis: {...axis, title: {...axis.title, text: "1000 / T (1/K)"}},
    yaxis: {...axis, title: {...axis.title, text: yLabel}},
    showlegend: true, legend: {orientation: "h", x: 0, y: 1.16, font: {color: colors.text, size: 11}},
    hovermode: "closest",
    hoverlabel: {bgcolor: colors.surface, bordercolor: colors.line, font: {color: colors.text, size: 12}},
    uirevision: key,
  };
  await Plotly.react(node, traces, layout, {responsive: true, displayModeBar: false});
}

export function purgeChart(node) {
  if (window.Plotly) window.Plotly.purge(node);
}

// 動径分布関数 g(r)。cutoffs: [{x, label}] の位置に点線の縦線と名前を描く (配位数を数えた距離)。
export async function drawRdf(node, {r, series, cutoffs = [], key = "rdf"}) {
  const Plotly = await loadPlotly();
  const colors = theme(node);
  const traces = series.map((s, i) => ({
    x: r, y: s.values, name: s.name, type: "scatter", mode: "lines",
    line: {color: colors.series[i % colors.series.length], width: s.width || 2, dash: s.dash || "solid"},
    hovertemplate: `r = %{x:.2f} Å: %{y:.3~g}<extra>${s.name}</extra>`,
  }));
  const shapes = cutoffs.map((c, i) => ({
    type: "line", xref: "x", yref: "paper", x0: c.x, x1: c.x, y0: 0, y1: 1,
    line: {color: colors.series[(i + 1) % colors.series.length], width: 1.5, dash: "dash"},
  }));
  const annotations = cutoffs.map((c, i) => ({
    x: c.x, y: 1 - i * 0.09, xref: "x", yref: "paper", text: c.label, showarrow: false, xanchor: "left", xshift: 4,
    font: {size: 11, color: colors.series[(i + 1) % colors.series.length]}, bgcolor: colors.surface,
  }));
  const axis = {gridcolor: colors.line, linecolor: colors.line, zeroline: false, tickfont: {color: colors.muted, size: 11},
                title: {font: {color: colors.muted, size: 11}}};
  const layout = {
    height: 280, margin: {l: 52, r: 12, t: traces.length > 1 ? 30 : 8, b: 44},
    paper_bgcolor: "rgba(0,0,0,0)", plot_bgcolor: "rgba(0,0,0,0)",
    font: {family: "inherit", color: colors.text, size: 12},
    xaxis: {...axis, title: {...axis.title, text: "原子間の距離 r (Å)"}},
    yaxis: {...axis, title: {...axis.title, text: "g(r)"}, rangemode: "tozero"},
    showlegend: traces.length > 1,
    legend: {orientation: "h", x: 0, y: 1.16, font: {color: colors.text, size: 11}},
    hovermode: "x unified",
    hoverlabel: {bgcolor: colors.surface, bordercolor: colors.line, font: {color: colors.text, size: 12}},
    shapes, annotations, uirevision: key,
  };
  await Plotly.react(node, traces, layout, {responsive: true, displayModeBar: false});
}
