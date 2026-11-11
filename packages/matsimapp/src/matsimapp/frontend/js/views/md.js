import {api, busy, el, field, formatDuration, number, toast} from "../dom.js";
import {navigate, state} from "../main.js";
import {confirmCard, createPicker, showProblem} from "../picker.js";
import {sampleCard} from "./structures.js";

const ENSEMBLES = [
  {value: "nvt", label: "NVT (温度一定)"},
  {value: "npt", label: "NPT (温度・圧力一定)"},
  {value: "nve", label: "NVE (エネルギー一定)"},
];

// よく使う流れのひな形。押すとステージ一覧がこの内容に置き換わる。
const PRESETS = [
  {label: "定温 (NVT)", stages: () => [{ensemble: "nvt", temperature: 300, steps: 10000}]},
  {label: "定温定圧 (NPT)", stages: () => [{ensemble: "npt", temperature: 300, pressure: 0, steps: 10000}]},
  {label: "昇温", stages: () => [
    {ensemble: "npt", temperature: 300, pressure: 0, steps: 5000},
    {ensemble: "npt", temperature: 300, temperature_end: 1000, pressure: 0, steps: 20000},
  ]},
  {label: "アニール", stages: () => [
    {ensemble: "nvt", temperature: 300, steps: 5000},
    {ensemble: "nvt", temperature: 300, temperature_end: 1000, steps: 10000},
    {ensemble: "nvt", temperature: 1000, steps: 10000},
    {ensemble: "nvt", temperature: 1000, temperature_end: 300, steps: 20000},
  ]},
];

function numberInput(value, attrs = {}) {
  return el("input", {type: "number", inputmode: "decimal", value: value ?? "", ...attrs});
}

function readNumber(input) {
  return input.value.trim() === "" ? null : Number(input.value);
}

export async function renderMD(view, query) {
  const [structures, potentials] = await Promise.all([api("/api/structures"), api("/api/potentials")]);
  const limits = state.me.limits;

  if (!structures.length) {
    view.replaceChildren(
      el("h1", {class: "page-title", text: "MD"}),
      el("section", {class: "card"},
        el("p", {class: "note", text: "計算する構造がまだありません。先に構造を追加してください。"}),
        el("div", {class: "button-row"}, el("a", {class: "primary", href: "#/structures", text: "構造を追加"}))),
      await sampleCard());
    return null;
  }

  // 入力の初期値。再投入 (?from=ジョブ番号) のときは元のジョブの条件を引き継ぐ。
  const form = {
    name: "", structure_id: structures[0].id, potential_id: potentials[0]?.id, repeat: null,
    time_step: 1, minimize: false, thermo_interval: 100, traj_interval: 1000, stages: PRESETS[0].stages(),
  };
  if (query.get("from")) {
    try {
      const source = await api(`/api/jobs/${Number(query.get("from"))}`);
      if (source.kind === "md") Object.assign(form, source.params);
      if (!structures.some((s) => s.id === form.structure_id)) {
        form.structure_id = structures[0].id;
        form.repeat = null;
        toast("元の構造は削除されています。構造を選び直してください");
      }
    } catch (error) {
      toast(error.message);
    }
  } else if (query.get("structure")) {
    const wanted = Number(query.get("structure"));
    if (structures.some((s) => s.id === wanted)) form.structure_id = wanted;
  }

  const confirmSlot = el("div", {});
  const stageList = el("div", {class: "stages"});
  const stageTotal = el("p", {class: "note"});
  const invalidate = () => confirmSlot.replaceChildren();   // 入力を変えたら確認結果は消す
  const picker = createPicker({structures, potentials, limits, initial: form, onChange: invalidate});

  // ---- ステージ ----
  function updateTotal() {
    const steps = form.stages.reduce((sum, stage) => sum + (Number(stage.steps) || 0), 0);
    stageTotal.textContent = `合計 ${number(steps)} ステップ ＝ ${(steps * form.time_step / 1000).toFixed(2)} ps`;
    stageTotal.classList.toggle("error", steps > limits.max_total_steps);
  }

  function stageCard(stage, index) {
    const set = (key, value) => {
      stage[key] = value;
      invalidate();
      updateTotal();
    };
    const pressureField = field("圧力 (GPa)", numberInput(stage.pressure ?? 0, {step: "any",
      onchange: (event) => set("pressure", readNumber(event.target) ?? 0)}));
    const ensemble = el("select", {"aria-label": "アンサンブル", onchange: () => {
      set("ensemble", ensemble.value);
      pressureField.hidden = ensemble.value !== "npt";
    }}, ENSEMBLES.map((e) => el("option", {value: e.value, selected: e.value === stage.ensemble, text: e.label})));
    pressureField.hidden = stage.ensemble !== "npt";
    return el("fieldset", {class: "stage"},
      el("legend", {text: `ステージ ${index + 1}`}),
      el("div", {class: "stage-head"}, ensemble,
        form.stages.length > 1 && el("button", {class: "link-button danger", type: "button", text: "削除", onclick: () => {
          form.stages.splice(index, 1);
          renderStages();
        }})),
      el("div", {class: "grid-2"},
        field("温度 (K)", numberInput(stage.temperature, {min: "0", step: "any",
          onchange: (event) => set("temperature", readNumber(event.target))})),
        field("終了温度 (K)", numberInput(stage.temperature_end, {min: "0", step: "any", placeholder: "一定",
          onchange: (event) => set("temperature_end", readNumber(event.target))}), "入れると昇温・降温"),
        field("ステップ数", numberInput(stage.steps, {min: "1", step: "1", inputmode: "numeric",
          onchange: (event) => set("steps", Math.round(readNumber(event.target) || 0))})),
        pressureField));
  }

  function renderStages() {
    invalidate();
    stageList.replaceChildren(...form.stages.map(stageCard));
    updateTotal();
  }

  const addStage = el("button", {class: "secondary small", type: "button", text: "ステージを追加", onclick: () => {
    if (form.stages.length >= limits.max_stages) {
      toast(`ステージは ${limits.max_stages} 個までです`);
      return;
    }
    const last = form.stages[form.stages.length - 1];
    form.stages.push({ensemble: last.ensemble, temperature: last.temperature_end ?? last.temperature,
                      pressure: last.pressure ?? 0, steps: last.steps});
    renderStages();
  }});

  // ---- 詳細設定 ----
  const nameInput = el("input", {type: "text", maxlength: "60", value: form.name, placeholder: "空欄なら自動",
                                 onchange: () => { form.name = nameInput.value.trim(); }});
  const details = el("details", {class: "card"},
    el("summary", {text: "詳細設定"}),
    field("ジョブ名", nameInput),
    el("div", {class: "grid-2"},
      field("時間刻み (fs)", numberInput(form.time_step, {min: "0.01", max: "10", step: "any", onchange: (event) => {
        form.time_step = readNumber(event.target) || 1;
        invalidate();
        updateTotal();
      }})),
      field("熱力学量の出力間隔", numberInput(form.thermo_interval, {min: "1", step: "1", inputmode: "numeric",
        onchange: (event) => { form.thermo_interval = Math.round(readNumber(event.target) || 100); invalidate(); }}), "ステップ"),
      field("トラジェクトリの保存間隔", numberInput(form.traj_interval, {min: "0", step: "1", inputmode: "numeric",
        onchange: (event) => { form.traj_interval = Math.round(readNumber(event.target) ?? 0); invalidate(); }}), "ステップ。0 で保存しない")),
    el("label", {class: "check"},
      el("input", {type: "checkbox", checked: form.minimize, onchange: (event) => {
        form.minimize = event.target.checked;
        invalidate();
      }}),
      "MD の前に構造最適化 (FIRE) をする"));

  // ---- 確認と実行 ----
  const body = () => ({...form, ...picker.value()});
  const check = el("button", {class: "primary wide", type: "button", text: "内容を確認する", onclick: () => busy(check, async () => {
    if (!picker.check()) {
      showProblem(confirmSlot, picker.message() || "ポテンシャルを確認してください");
      return;
    }
    let result;
    try {
      result = await api("/api/md/preview", {method: "POST", body: body()});
    } catch (error) {
      showProblem(confirmSlot, error.message);
      return;
    }
    const submit = el("button", {class: "primary wide", type: "button", text: "この内容で実行する",
      onclick: () => busy(submit, async () => {
        const {id} = await api("/api/md/jobs", {method: "POST", body: body()});
        toast("ジョブを投入しました");
        navigate(`#/jobs/${id}`);
      })});
    confirmSlot.replaceChildren(confirmCard(result, submit, formatDuration));
    confirmSlot.scrollIntoView({behavior: "smooth", block: "start"});
  })});

  view.replaceChildren(
    el("a", {class: "back-link", href: "#/home", text: "‹ ホーム"}),
    el("h1", {class: "page-title", text: "MD"}),
    ...picker.nodes,
    el("section", {class: "card"},
      el("div", {class: "card-head"}, el("h2", {text: "温度・圧力の流れ"})),
      el("div", {class: "chips"}, PRESETS.map((preset) =>
        el("button", {class: "chip", type: "button", text: preset.label, onclick: () => {
          form.stages = preset.stages();
          renderStages();
        }}))),
      stageList, stageTotal,
      el("div", {class: "button-row"}, addStage)),
    details,
    check,
    confirmSlot);

  renderStages();
  await picker.load();
  return () => picker.destroy();
}
