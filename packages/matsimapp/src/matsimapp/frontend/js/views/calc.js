// MD 以外の計算の設定画面。入力欄はサーバーが返す定義 (fields) から作るので、
// 計算の種類が増えてもこのファイルは変えなくてよい。
import {api, busy, el, field, formatDuration, toast} from "../dom.js";
import {navigate, state} from "../main.js";
import {control, visible} from "../form.js";
import {confirmCard, createPicker, showProblem} from "../picker.js";
import {sampleCard} from "./structures.js";

export async function renderCalc(view, kindKey, query) {
  const [listing, structures, potentials] = await Promise.all(
    [api("/api/kinds"), api("/api/structures"), api("/api/potentials")]);
  const kind = listing.kinds.find((k) => k.key === kindKey && k.fields);
  if (!kind) throw new Error("計算の種類が見つかりません");
  const limits = state.me.limits;
  // マニュアルの、その計算の章 (詳しい説明があるもの)。ほかは「どの計算を選ぶか」の章
  const chapter = {plumed: "ch-plumed", transition: "ch-transition"}[kind.key] || "ch-goal";
  const header = [el("a", {class: "back-link", href: "#/home", text: "‹ ホーム"}),
                  el("h1", {class: "page-title", text: kind.label}), el("p", {class: "note", text: kind.note}),
                  el("p", {class: "note"}, el("a", {href: `/manual/manual.html#${chapter}`, target: "_blank", rel: "noopener",
                    text: kind.key === "plumed" ? "初めての人へ: CV とパラメータの決め方 (マニュアル)" : "マニュアルで詳しく見る"}))];

  if (!structures.length) {
    view.replaceChildren(...header,
      el("section", {class: "card"},
        el("p", {class: "note", text: "計算する構造がまだありません。先に構造を追加してください。"}),
        el("div", {class: "button-row"}, el("a", {class: "primary", href: "#/structures", text: "構造を追加"}))),
      await sampleCard());
    return null;
  }

  // 入力の初期値。作り直し (?from=ジョブ番号) のときは元のジョブの条件を引き継ぐ。
  const common = {name: "", structure_id: structures[0].id, potential_id: potentials[0]?.id, repeat: null,
                  extra_potential_ids: [], time_step: 1, thermo_interval: 100, traj_interval: 1000};
  const values = Object.fromEntries(kind.fields.map((spec) => [spec.key, spec.default]));
  if (query.get("from")) {
    try {
      const source = await api(`/api/jobs/${Number(query.get("from"))}`);
      if (source.kind === kindKey) {
        const {options, kind: _kind, ...rest} = source.params;
        Object.assign(common, rest);
        for (const spec of kind.fields) if (spec.key in (options || {})) values[spec.key] = options[spec.key];
        if (!structures.some((s) => s.id === common.structure_id)) {
          common.structure_id = structures[0].id;
          common.repeat = null;
          toast("元の構造は削除されています。構造を選び直してください");
        }
      }
    } catch (error) {
      toast(error.message);
    }
  } else if (query.get("structure")) {
    const wanted = Number(query.get("structure"));
    if (structures.some((s) => s.id === wanted)) common.structure_id = wanted;
  }

  const confirmSlot = el("div", {});
  const invalidate = () => confirmSlot.replaceChildren();   // 入力を変えたら確認結果は消す
  const picker = createPicker({structures, potentials, limits, initial: common, committee: kind.committee,
                               onChange: invalidate});

  // 条件によって出たり消えたりする欄があるので、値が変わるたびに欄を作り直す
  const mainSlot = el("div", {class: "form-fields"});
  const advancedSlot = el("div", {class: "form-fields"});
  function renderFields() {
    invalidate();
    const shown = kind.fields.filter((spec) => visible(spec, values));
    mainSlot.replaceChildren(...shown.filter((spec) => !spec.advanced).map((spec) => control(spec, values, renderFields)));
    advancedSlot.replaceChildren(...shown.filter((spec) => spec.advanced).map((spec) => control(spec, values, renderFields)));
  }

  const commonNumber = (key, label, hint, attrs) => {
    const input = el("input", {type: "number", inputmode: "decimal", step: "any", value: common[key], ...attrs,
      onchange: () => {
        common[key] = Number(input.value);
        invalidate();
      }});
    return field(label, input, hint);
  };
  const nameInput = el("input", {type: "text", maxlength: "60", value: common.name, placeholder: "空欄なら自動",
                                 onchange: () => { common.name = nameInput.value.trim(); }});
  const details = el("details", {class: "card"},
    el("summary", {text: "詳細設定"}),
    field("ジョブ名", nameInput),
    advancedSlot,
    el("div", {class: "grid-2"},
      commonNumber("time_step", "時間刻み (fs)", null, {min: "0.01", max: "10"}),
      commonNumber("thermo_interval", "熱力学量の出力間隔", "ステップ", {min: "1", step: "1", inputmode: "numeric"}),
      commonNumber("traj_interval", "トラジェクトリの保存間隔", "ステップ。0 で保存しない", {min: "0", step: "1", inputmode: "numeric"})));

  const body = () => ({kind: kindKey, ...common, ...picker.value(), options: values});
  const check = el("button", {class: "primary wide", type: "button", text: "内容を確認する", onclick: () => busy(check, async () => {
    if (!picker.check()) {
      showProblem(confirmSlot, picker.message() || "ポテンシャルを確認してください");
      return;
    }
    let result;
    try {
      result = await api("/api/calc/preview", {method: "POST", body: body()});
    } catch (error) {
      showProblem(confirmSlot, error.message);
      return;
    }
    const submit = el("button", {class: "primary wide", type: "button", text: "この内容で実行する",
      onclick: () => busy(submit, async () => {
        const {id, ids, series} = await api("/api/calc/jobs", {method: "POST", body: body()});
        if (series) {
          // 温度スキャン: 温度ごとのジョブをまとめて見る画面へ (拡散はアレニウス解析)
          toast(`${ids.length} 個のジョブを投入しました`);
          navigate(kindKey === "diffusion" ? `#/arrhenius?series=${series}` : `#/series/${series}`);
          return;
        }
        toast("ジョブを投入しました");
        navigate(`#/jobs/${id}`);
      })});
    confirmSlot.replaceChildren(confirmCard(result, submit, formatDuration));
    confirmSlot.scrollIntoView({behavior: "smooth", block: "start"});
  })});

  view.replaceChildren(...header,
    ...picker.nodes,
    el("section", {class: "card"},
      el("div", {class: "card-head"}, el("h2", {text: "計算の条件"})), mainSlot),
    details, check, confirmSlot);
  renderFields();
  await picker.load();
  return () => picker.destroy();
}
