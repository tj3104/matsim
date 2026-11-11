// 構造・スーパーセル・ポテンシャルを選ぶ部分。MD とそれ以外の計算の設定画面で共通に使う。
import {api, el, field, number} from "./dom.js";
import {viewerCard} from "./views/structures.js";
import {inputBrowser} from "./inputs.js";
import {structureChooser} from "./chooser.js";


// initial: {structure_id, repeat, potential_id, extra_potential_ids}
// committee を true にすると、2 つ目以降のポテンシャルも選べる (予測のばらつきを使う計算用)
export function createPicker({structures, potentials, limits, initial, committee = false, onChange}) {
  const state = {
    structure_id: initial.structure_id, repeat: initial.repeat || null, potential_id: initial.potential_id,
    extra_potential_ids: [...(initial.extra_potential_ids || [])],
  };
  const preview = viewerCard("構造");
  preview.caption.textContent = "ドラッグで回転、ピンチ・ホイールで拡大縮小";
  const compatibility = el("p", {class: "note", role: "status"});
  const engineNote = el("p", {class: "note caution", hidden: true, text: "SevenNet (GNN 系の汎用力場) は、GPUMD ではなく ASE で計算します。"
    + "使えるのは MD・静的計算 (構造緩和・弾性定数・状態方程式)・フォノン分散です。NEP より数十倍遅いので、"
    + "MD は数百原子・数万ステップ程度にしてください"});
  const atomCount = el("p", {class: "note", role: "status"});
  let current = null;   // 選択中の構造 (スーパーセル適用後) の情報
  let stopped = false;

  // 構造が多くなっても選びやすいよう、探す・絞る・並べ替えるができる一覧にする (chooser.js)
  const chooser = structureChooser({structures, selected: state.structure_id, onSelect: (id) => {
    state.structure_id = id;
    state.repeat = null;
    load();
  }});
  const repeatInputs = [0, 1, 2].map((axis) => el("input", {type: "number", inputmode: "numeric", value: 1, min: "1",
    max: String(limits.max_repeat), step: "1", "aria-label": `${"abc"[axis]} 方向の繰り返し`, onchange: () => {
      state.repeat = repeatInputs.map((input) =>
        Math.max(1, Math.min(limits.max_repeat, Math.round(Number(input.value) || 1))));
      load();
    }}));

  async function load() {
    onChange();
    const base = structures.find((s) => s.id === state.structure_id);
    if (!state.repeat) state.repeat = base.default_repeat;
    repeatInputs.forEach((input, i) => { input.value = state.repeat[i]; });
    const [na, nb, nc] = state.repeat;
    try {
      current = await api(`/api/structures/${state.structure_id}?na=${na}&nb=${nb}&nc=${nc}`);
    } catch (error) {
      current = null;
      atomCount.textContent = error.message;
      return;
    }
    if (stopped) return;
    const repeated = current.repeat;
    atomCount.textContent = `${number(repeated.n_atoms)} 原子 ・ ${repeated.lengths.map((v) => v.toFixed(1)).join(" × ")} Å`;
    if (repeated.too_many) atomCount.textContent += ` ― ${number(limits.max_atoms)} 原子を超える構造は扱えません`;
    else if (repeated.over_guideline) atomCount.textContent += ` ― 目安の ${number(limits.warn_atoms)} 原子を超えています (実行はできます)`;
    atomCount.classList.toggle("error", repeated.too_many);
    atomCount.classList.toggle("caution", !repeated.too_many && repeated.over_guideline);
    if (current.view) {
      preview.show(current.view);
      preview.caption.textContent = current.view.shown < current.view.n_atoms
        ? `${number(current.view.n_atoms)} 原子のうち ${number(current.view.shown)} 個を間引いて表示しています`
        : "ドラッグで回転、ピンチ・ホイールで拡大縮小";
    }
    check();
  }

  const potentialSelect = el("select", {onchange: () => {
    state.potential_id = Number(potentialSelect.value);
    state.extra_potential_ids = state.extra_potential_ids.filter((id) => id !== state.potential_id);
    renderExtras();
    onChange();
    check();
  }}, potentials.map((p) => el("option", {value: p.id, selected: p.id === state.potential_id, disabled: !p.available,
                                           text: `${p.name}${p.available ? "" : " (ファイルなし)"}`})));

  const extras = el("div", {class: "field", hidden: !committee});
  function renderExtras() {
    if (!committee) return;
    const others = potentials.filter((p) => p.id !== state.potential_id && p.available);
    extras.replaceChildren(
      el("span", {class: "field-label", text: "比べるポテンシャル (1 つ以上)"}),
      others.length
        ? others.map((p) => el("label", {class: "check"},
            el("input", {type: "checkbox", checked: state.extra_potential_ids.includes(p.id), onchange: (event) => {
              state.extra_potential_ids = event.target.checked
                ? [...state.extra_potential_ids, p.id]
                : state.extra_potential_ids.filter((id) => id !== p.id);
              onChange();
              check();
            }}), p.name))
        : el("span", {class: "field-hint", text: "ほかのポテンシャルがありません。設定画面で nep.txt を追加してください"}));
  }

  // 選んだポテンシャルがすべて、構造の元素に対応しているか
  function check() {
    const chosen = [state.potential_id, ...(committee ? state.extra_potential_ids : [])]
      .map((id) => potentials.find((p) => p.id === id)).filter(Boolean);
    if (!chosen.length || !current) {
      compatibility.textContent = chosen.length ? "" : "ポテンシャルがありません。設定画面で追加してください";
      return false;
    }
    const species = Object.keys(current.info.species);
    const missing = [...new Set(chosen.flatMap((p) => species.filter((symbol) => !p.elements.includes(symbol))))];
    compatibility.textContent = missing.length
      ? `選んだポテンシャルは ${missing.join("・")} に対応していません`
      : `この構造の元素 (${species.join("・")}) に対応しています`;
    compatibility.classList.toggle("error", missing.length > 0);
    // SevenNet (GNN 系の汎用力場) は GPUMD ではなく ASE で計算する: 使える計算と速さを知らせる
    const engine = potentials.find((p) => p.id === state.potential_id)?.engine;
    engineNote.hidden = engine !== "sevennet";
    return missing.length === 0;
  }

  renderExtras();
  const card = el("section", {class: "card"},
    el("div", {class: "card-head"}, el("h2", {text: "構造とポテンシャル"})),
    el("div", {class: "field"}, el("span", {class: "field-label", text: "構造"}), chooser.node),
    el("div", {class: "field"},
      el("span", {class: "field-label", text: "スーパーセル (a × b × c)"}),
      el("div", {class: "grid-3"}, repeatInputs),
      atomCount),
    field("ポテンシャル", potentialSelect),
    engineNote,
    extras,
    compatibility);

  return {
    nodes: [card, preview.node],
    load,
    check,
    message: () => compatibility.textContent,
    value: () => ({structure_id: state.structure_id, repeat: state.repeat, potential_id: state.potential_id,
                   extra_potential_ids: committee ? state.extra_potential_ids : []}),
    destroy() {
      stopped = true;
      preview.viewer.destroy();
    },
  };
}

// 確認でエラーになったとき、理由を画面に残して見えるところまで動かす (長い説明もそのまま読める)
export function showProblem(slot, message) {
  slot.replaceChildren(el("section", {class: "card problem", role: "alert"},
    el("div", {class: "card-head"}, el("h2", {text: "この条件では実行できません"})),
    el("p", {text: message})));
  slot.scrollIntoView({behavior: "smooth", block: "center"});
}

// 確認結果のカード (MD とそれ以外で共通)
export function confirmCard(result, submit, formatDuration) {
  return el("section", {class: "card confirm"},
    el("div", {class: "card-head"}, el("h2", {text: "実行前の確認"})),
    el("dl", {class: "facts"},
      el("dt", {text: "原子数"}), el("dd", {text: `${number(result.n_atoms)} (${result.formula})`}),
      el("dt", {text: "セル"}), el("dd", {text: `${result.cell.join(" × ")} Å`}),
      el("dt", {text: "ステップ数"}), el("dd", {text: `${number(result.total_steps)} (${result.total_time_ps} ps)`}),
      el("dt", {text: "所要時間の目安"}), el("dd", {text: `約 ${formatDuration(result.estimated_seconds)}`
        + (result.estimate_measured ? "" : " (実測がないため粗い目安)")})),
    result.notes.map((note) => el("p", {class: "warning", text: `注意: ${note}`})),
    el("details", {}, el("summary", {text: "生成される入力ファイル (run.in など)"}), previewInputs(result.run_in)),
    submit);
}

// 確認画面の入力ファイル。PLUMED の計算は run.in のあとに plumed.dat が続くので、2 つに分けて見せる
function previewInputs(text) {
  const [runIn, plumed] = text.split("\n# ---- plumed.dat ----\n");
  const files = [{name: "run.in", text: runIn}];
  if (plumed !== undefined) files.push({name: "plumed.dat", text: plumed});
  return inputBrowser(files);
}
