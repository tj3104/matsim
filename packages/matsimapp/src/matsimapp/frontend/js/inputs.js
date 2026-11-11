// 入力ファイル (GPUMD の run.in・PLUMED の plumed.dat) を、行ごとの日本語の説明つきで見せる部品。
// 説明は行の書き方から作る (キーワードと引数を読む)。知らないキーワードは説明なしでそのまま出す。
import {el} from "./dom.js";

const ENSEMBLES = {
  nve: "エネルギー一定 (NVE)。温度も圧力も制御しない",
  nvt_ber: "温度一定 (NVT、Berendsen 熱浴。速く平衡に近づけるが、ゆらぎは正しくない)",
  nvt_nhc: "温度一定 (NVT、Nosé–Hoover 鎖。正しいカノニカル分布)",
  nvt_bdp: "温度一定 (NVT、Bussi–Donadio–Parrinello。正しいカノニカル分布で安定)",
  nvt_lan: "温度一定 (NVT、Langevin 熱浴)",
  nvt_bao: "温度一定 (NVT、Langevin の BAOAB 積分)",
  nvt_mttk: "温度一定 (NVT、MTTK)",
  npt_ber: "温度・圧力一定 (NPT、Berendsen。速く平衡に近づけるが、ゆらぎは正しくない)",
  npt_scr: "温度・圧力一定 (NPT、確率的セル再スケーリング。正しい NPT 分布)",
  npt_mttk: "温度・圧力一定 (NPT、MTTK。圧力を変えながらの計算もできる)",
  heat_lan: "局所熱浴 (Langevin)。熱源と熱浴のグループを違う温度に保つ (NEMD)",
  heat_nhc: "局所熱浴 (Nosé–Hoover)。熱源と熱浴のグループを違う温度に保つ",
  heat_bdp: "局所熱浴 (BDP)。熱源と熱浴のグループを違う温度に保つ",
  msst: "衝撃波 (MSST 法)。一定速度の衝撃圧縮",
  nphug: "衝撃波 (Hugoniot 上の状態)",
  pimd: "経路積分 MD (原子核の量子効果)",
  rpmd: "リングポリマー MD (量子効果を入れた動的性質)",
  trpmd: "熱浴つきリングポリマー MD",
  ti_spring: "熱力学積分 (Frenkel–Ladd、固体の絶対自由エネルギー)",
  ti_liquid: "熱力学積分 (Uhlenbeck–Ford、液体の絶対自由エネルギー)",
  ti_rs: "熱力学積分 (温度方向の reversible scaling)",
  ti_as: "熱力学積分 (圧力方向の adiabatic switching)",
  ttm: "二温度モデル (電子と格子)",
  wall_piston: "ピストンの壁 (衝撃)",
  wall_mirror: "鏡の壁 (衝撃)",
  wall_harmonic: "調和ポテンシャルの壁",
};

const RUN_IN = {
  potential: (a) => `ポテンシャル (原子間の力の模型) のファイル: ${a[0]?.split("/").pop() || ""}`,
  velocity: (a) => `最初の速度を ${a[0]} K の温度に合うランダムな値にする`,
  time_step: (a) => `時間刻み ${a[0]} fs (1 ステップで進む時間)`,
  ensemble: (a) => {
    const name = a[0];
    const base = ENSEMBLES[name] || `アンサンブル ${name}`;
    if (/^(nvt|npt)_/.test(name) && a.length >= 3) {
      const temps = a[1] === a[2] ? `${a[1]} K` : `${a[1]} → ${a[2]} K に変えながら`;
      return `${base}。温度 ${temps}、熱浴の時定数 ${a[3]} ステップ`
        + (name.startsWith("npt") ? "。続く数字は目標の圧力 (GPa)・弾性率 (GPa)・圧力の時定数 (ステップ)" : "");
    }
    return base;
  },
  run: (a) => `ここまでの設定で ${Number(a[0]).toLocaleString()} ステップ実行する (この下は次のステージ)`,
  minimize: (a) => `MD の前に構造最適化 (${a[0]} 法、力の許容値 ${a[1]} eV/Å、最大 ${a[2]} ステップ)`,
  dump_thermo: (a) => `${a[0]} ステップごとに温度・エネルギー・圧力・セルを thermo.out に書く`,
  dump_xyz: (a) => `${a[0]} ステップごとに原子の座標を ${a[1] || "dump.xyz"} に書く (トラジェクトリ)`,
  dump_exyz: (a) => `${a[0]} ステップごとに原子の座標を書く (トラジェクトリ)`,
  dump_position: (a) => `${a[0]} ステップごとに座標を書く`,
  dump_restart: (a) => `${a[0]} ステップごとに、続きから計算できる restart.xyz を書く`,
  dump_netcdf: (a) => `${a[0]} ステップごとに座標を NetCDF 形式で書く`,
  replicate: (a) => `構造を ${a.join(" × ")} に繰り返したスーパーセルにする`,
  compute: () => "グループごとの温度などの量を記録する (compute.out)",
  compute_msd: (a) => `平均二乗変位 (MSD、拡散係数のもと) を計算する (${a[0]} ステップごとに記録)`,
  compute_sdc: () => "速度自己相関 (自己拡散係数・振動状態密度のもと) を計算する",
  compute_rdf: () => "動径分布関数 (RDF、原子間距離の分布) を計算する",
  compute_hac: () => "熱流の自己相関 (Green–Kubo の熱伝導率のもと) を計算する",
  compute_hnemd: () => "熱伝導率を HNEMD 法 (小さな駆動力をかける) で計算する",
  compute_shc: () => "熱伝導率のスペクトル分解 (振動数ごとの寄与) を計算する",
  compute_dos: () => "振動状態密度 (VDOS) を計算する",
  compute_elastic: () => "弾性定数を計算する (セルを少しずつ変形させる)",
  compute_phonon: () => "フォノン (格子振動) を有限変位法で計算する",
  compute_cohesive: () => "状態方程式 (セルを伸縮させたエネルギー) を計算する",
  compute_extrapolation: () => "ポテンシャルの外挿度 (学習範囲から外れていないか) を記録する",
  compute_orientorder: () => "結合の向きの秩序 (Steinhardt の Q_l) を計算する",
  compute_viscosity: () => "粘性を Green–Kubo で計算する",
  deform: () => "セルを一定の速さで変形させる (引張試験など)",
  fix: (a) => `グループ ${a[0]} の原子を動かさない (固定する)`,
  move: () => "グループの原子を一定の速度で動かす",
  add_force: () => "グループの原子に外力をかける",
  add_efield: () => "電場をかける",
  add_spring: () => "ばねでつなぐ",
  deposit: () => "原子を一定間隔で上から入れる (成膜)",
  mc: () => "Monte Carlo で原子の種類を入れ替える",
  plumed: (a) => `PLUMED を ${a[1]} ステップごとに呼ぶ (入力 ${a[0]})。バイアスをかけるときは毎ステップ (1)`,
  electron_stop: () => "電子阻止能 (速い原子のエネルギー損失) を入れる",
  dftd3: () => "DFT-D3 の分散力 (ファンデルワールス力) を足す",
  correct_velocity: () => "全体の並進運動を取り除く",
  change_box: () => "セルの大きさを変える",
  active: () => "複数のポテンシャルの予測のばらつきを見て、怪しい構造を保存する (Active Learning)",
};

const PLUMED = {
  UNITS: () => "PLUMED の入出力の単位 (長さ Å・時間 fs・エネルギー eV)",
  GROUP: () => "原子の番号のまとまり (グループ) に名前を付ける",
  COORDINATIONNUMBER: () => "配位数 (となりの原子の数) を原子ごとに数え、MEAN で平均する",
  Q6: () => "Steinhardt の Q6 (まわりの原子の並びの規則正しさ。固体で大きく、液体で小さい) の平均",
  Q4: () => "Steinhardt の Q4 (fcc・hcp・bcc で値が違う) の平均",
  ENVIRONMENTSIMILARITY: (k) => `まわりの原子の並びが ${String(k.CRYSTAL_STRUCTURE || "").toLowerCase()} (格子定数 ${k.LATTICE_CONSTANTS} Å) と`
    + "どれだけ同じか (0〜1) の平均",
  VOLUME: () => "セルの体積",
  CELL: () => "セルの 3 辺のベクトル (成分を .ax などで取り出す)",
  CUSTOM: (k) => `式で新しい値を作る: ${k.FUNC || ""}`,
  DISTANCE: (k) => `原子 ${k.ATOMS} のあいだの距離`,
  METAD: (k) => `メタダイナミクス: ${k.ARG} に沿って、${k.PACE} ステップごとに高さ ${k.HEIGHT} eV・幅 ${k.SIGMA} の`
    + `ガウス関数を積む。バイアス係数 γ=${k.BIASFACTOR} (well-tempered、温度 ${k.TEMP} K)。積んだ山は ${k.FILE} に書く`,
  RESTRAINT: (k) => `${k.ARG} を ${k.AT} に引き寄せるばね (強さ ${k.KAPPA})`,
  UPPER_WALLS: (k) => `${k.ARG} が ${k.AT} を超えないようにする壁`,
  LOWER_WALLS: (k) => `${k.ARG} が ${k.AT} を下回らないようにする壁`,
  PRINT: (k) => `${k.ARG} を ${k.STRIDE} ステップごとに ${k.FILE} に書く`,
  FLUSH: (k) => `${k.STRIDE} ステップごとにファイルへの書き込みを確定する (途中経過を見られるように)`,
};

function explainRunIn(line) {
  const tokens = line.trim().split(/\s+/);
  const describe = RUN_IN[tokens[0]];
  return describe ? describe(tokens.slice(1)) : "";
}

function explainPlumed(line) {
  let body = line.trim();
  let label = "";
  const named = body.match(/^([A-Za-z0-9_]+):\s+(.*)$/);
  if (named) [, label, body] = named;
  const [action, ...rest] = body.split(/\s+/);
  const keys = {};
  for (const item of rest) {
    const at = item.indexOf("=");
    if (at > 0) keys[item.slice(0, at)] = item.slice(at + 1);
  }
  if (keys.LABEL) label = keys.LABEL;
  const describe = PLUMED[action];
  if (!describe) return "";
  return (label ? `「${label}」= ` : "") + describe(keys);
}

// 入力ファイルの種類 (名前から)。説明を付けられないものは "raw"
export function inputKind(name) {
  if (/(^|\/)run\.in$/.test(name)) return "runin";
  if (/(^|\/)plumed\.dat$/.test(name)) return "plumed";
  return "raw";
}

// テキストを表示する。kind が runin / plumed で annotated なら行ごとの説明つき
export function inputView(text, kind, annotated = true) {
  if (kind === "raw" || !annotated) return el("pre", {class: "code", text});
  const explain = kind === "runin" ? explainRunIn : explainPlumed;
  return el("div", {class: "annotated"}, ...text.split("\n").map((line) => {
    if (!line.trim()) return el("div", {class: "annot-gap"});
    if (line.trim().startsWith("#")) return el("div", {class: "annot-comment", text: line});
    const note = explain(line);
    return el("div", {class: "annot-row"}, el("code", {class: "annot-code", text: line}),
      note ? el("div", {class: "annot-note", text: note}) : null);
  }));
}

// 「説明つき / そのまま」を切り替えられる表示。files: [{name, text}]
export function inputBrowser(files, {note = ""} = {}) {
  let current = 0;
  let annotated = true;
  const body = el("div", {});
  const tabs = el("div", {class: "segmented", role: "group", "aria-label": "ファイル"});
  const mode = el("label", {class: "check"}, el("input", {type: "checkbox", checked: true, onchange: (e) => {
    annotated = e.target.checked;
    render();
  }}), "行ごとの説明を付ける");

  function render() {
    tabs.replaceChildren(...files.map((file, i) => el("button", {type: "button", text: file.name,
      class: i === current ? "seg-on" : "", "aria-pressed": String(i === current), onclick: () => { current = i; render(); }})));
    const file = files[current];
    const kind = inputKind(file.name);
    mode.hidden = kind === "raw";
    body.replaceChildren(...[   // replaceChildren は null を文字にしてしまうので取り除く
      inputView(file.text, kind, annotated),
      file.truncated ? el("p", {class: "note", text: "大きいファイルなので先頭だけ表示しています (全体はダウンロードしてください)"}) : null,
    ].filter(Boolean));
  }
  render();
  return el("div", {class: "stack"}, files.length > 1 ? tabs : null, mode,
            note ? el("p", {class: "note", text: note}) : null, body);
}
