// 構造のモデリング画面。入力欄はサーバーが返す定義 (fields) から作るので、
// 操作の種類が増えてもこのファイルは変えなくてよい。
import {api, busy, el, field, number, toast} from "../dom.js";
import {control, visible} from "../form.js";
import {navigate} from "../main.js";
import {viewerCard} from "./structures.js";

const speciesOf = (structure) => Object.keys(structure?.info?.species || {});

function opTile(op, structureId) {
  const query = structureId && op.needs_structure ? `?structure=${structureId}` : "";
  return el("a", {class: "tile tile-ready", href: `#/model/${op.key}${query}`},
    el("span", {class: "tile-name", text: op.label}),
    el("span", {class: "tile-note", text: op.note}));
}

// モデリングのメニュー。structureId を渡すと、その構造を加工する操作だけを並べる。
export function modelingCard(listing, {structureId = null, title = "モデリング"} = {}) {
  const ops = listing.ops.filter((op) => !structureId || op.needs_structure);
  const groups = listing.groups.filter((group) => ops.some((op) => op.group === group));
  return el("section", {class: "card"},
    el("div", {class: "card-head"}, el("h2", {text: title})),
    listing.atomsk ? null : el("p", {class: "note caution", text: "Atomsk が見つからないため、Atomsk を使う操作は実行できません。"}),
    // 操作が多いので、分類ごとに折りたたむ (最初の分類だけ開いておく)
    groups.map((group, i) => {
      const items = ops.filter((op) => op.group === group);
      return el("details", {class: "op-group", open: i === 0},
        el("summary", {}, group, el("span", {class: "op-count", text: ` ${items.length}`})),
        el("div", {class: "tiles"}, items.map((op) => opTile(op, structureId))));
    }));
}

function resultCard(result, onSave) {
  const info = result.info;
  const name = el("input", {type: "text", maxlength: "60", value: result.default_name});
  const save = el("button", {class: "primary wide", type: "button", text: "構造として保存する",
                             onclick: () => busy(save, () => onSave(name.value.trim()))});
  const rows = [
    ["組成", `${result.formula} (${number(result.n_atoms)} 原子)`],
    ["セル", `${info.a.toFixed(3)} × ${info.b.toFixed(3)} × ${info.c.toFixed(3)} Å`],
    ["角度", `${info.alpha.toFixed(1)}° / ${info.beta.toFixed(1)}° / ${info.gamma.toFixed(1)}°`],
    ["密度", `${info.density_g_cm3.toFixed(3)} g/cm³`],
  ];
  const notes = [...result.warnings];
  if (result.over_guideline) notes.push("原子数が多いので、計算に時間がかかります");
  return el("section", {class: "card"},
    el("div", {class: "card-head"}, el("h2", {text: "結果"})),
    el("dl", {class: "facts"}, rows.map(([key, value]) => [el("dt", {text: key}), el("dd", {text: value})])),
    notes.length ? el("div", {class: "stack"}, notes.map((text) => el("p", {class: "note caution", text}))) : null,
    field("保存する名前", name),
    save);
}

export async function renderModel(view, key, query) {
  const [listing, structureList] = await Promise.all([api("/api/modeling"), api("/api/structures")]);
  const op = listing.ops.find((o) => o.key === key);
  if (!op) throw new Error("モデリングの種類が見つかりません");
  const fromId = Number(query.get("structure")) || null;
  const back = fromId ? `#/structures/${fromId}` : "#/structures";
  const header = [el("a", {class: "back-link", href: back, text: fromId ? "‹ 構造に戻る" : "‹ 構造の一覧"}),
                  el("h1", {class: "page-title", text: op.label}), el("p", {class: "note", text: op.note})];
  if (op.uses_atomsk && !listing.atomsk) {
    view.replaceChildren(...header, el("section", {class: "card"},
      el("p", {class: "note error", text: "Atomsk が見つかりません。サーバーの設定 (MATSIM_ATOMSK_BIN) を確認してください。"})));
    return null;
  }
  if (op.needs_structure && !structureList.length) {
    view.replaceChildren(...header, el("section", {class: "card"},
      el("p", {class: "note", text: "加工する構造がまだありません。先に構造を追加してください。"}),
      el("div", {class: "button-row"}, el("a", {class: "primary", href: "#/structures", text: "構造を追加"}))));
    return null;
  }

  let structureId = op.needs_structure
    ? (structureList.some((s) => s.id === fromId) ? fromId : structureList[0].id) : null;
  const values = Object.fromEntries(op.fields.map((spec) => [spec.key, spec.default]));
  for (const spec of op.fields) {
    if (spec.type === "structure" && !spec.optional && values[spec.key] === null && structureList.length) {
      values[spec.key] = structureList[0].id;
    }
  }
  const current = () => structureList.find((s) => s.id === structureId);
  // 元の構造にある元素から選ぶ欄は、構造を変えたら選び直す
  function fitSpecies() {
    const present = speciesOf(current());
    for (const spec of op.fields) {
      if (spec.type !== "species" || spec.source !== "structure") continue;
      const value = values[spec.key];
      if (spec.allow_all && (value === "*" || value === null)) values[spec.key] = "*";
      else if (!present.includes(value)) values[spec.key] = present[0] ?? null;
    }
  }
  fitSpecies();

  const resultSlot = el("div", {});
  const card = viewerCard("プレビュー");
  card.caption.textContent = "ドラッグで回転、ピンチ・ホイールで拡大縮小";
  card.node.hidden = true;
  const invalidate = () => {
    resultSlot.replaceChildren();
    card.node.hidden = true;
  };

  const mainSlot = el("div", {class: "form-fields"});
  const advancedSlot = el("div", {class: "form-fields"});
  const advanced = el("details", {class: "card"}, el("summary", {text: "詳細設定"}), advancedSlot);
  function renderFields() {
    invalidate();
    const ctx = {species: speciesOf(current()), structures: structureList};
    const shown = op.fields.filter((spec) => visible(spec, values));
    mainSlot.replaceChildren(...shown.filter((spec) => !spec.advanced).map((spec) => control(spec, values, renderFields, ctx)));
    const extra = shown.filter((spec) => spec.advanced);
    advancedSlot.replaceChildren(...extra.map((spec) => control(spec, values, renderFields, ctx)));
    advanced.hidden = !extra.length;
    if (!op.fields.length) mainSlot.replaceChildren(el("p", {class: "note", text: "設定する項目はありません。"}));
  }

  const source = op.needs_structure ? el("select", {onchange: () => {
    structureId = Number(source.value);
    fitSpecies();
    renderFields();
  }}, structureList.map((s) => el("option", {value: s.id, selected: s.id === structureId,
                                               text: `${s.name} (${s.formula}・${number(s.n_atoms)} 原子)`}))) : null;

  const body = () => ({op: op.key, structure_id: structureId, options: values});
  const preview = el("button", {class: "primary wide", type: "button", text: "プレビュー", onclick: () => busy(preview, async () => {
    resultSlot.replaceChildren(el("p", {class: "loading", text: "組み立て中…"}));
    let result;
    try {
      result = await api("/api/modeling/preview", {method: "POST", body: body()});
    } catch (error) {
      resultSlot.replaceChildren(el("section", {class: "card problem"}, el("p", {class: "error", text: error.message})));
      return;
    }
    card.node.hidden = false;
    card.show(result.view);
    if (result.view.shown < result.view.n_atoms) {
      card.caption.textContent = `原子が多いので ${number(result.view.shown)} 個に間引いて表示しています`;
    }
    resultSlot.replaceChildren(resultCard(result, async (name) => {
      const {id} = await api("/api/modeling/save", {method: "POST", body: {...body(), name}});
      toast("構造を保存しました");
      navigate(`#/structures/${id}`);
    }));
    card.node.scrollIntoView({behavior: "smooth", block: "start"});
  })});

  // replaceChildren は null を文字として入れてしまうので、出さない部品は除いてから渡す
  view.replaceChildren(...[...header,
    source && el("section", {class: "card"},
      el("div", {class: "card-head"}, el("h2", {text: "元の構造"})), field("構造", source)),
    el("section", {class: "card"},
      el("div", {class: "card-head"}, el("h2", {text: "条件"})), mainSlot),
    advanced, preview, card.node, resultSlot].filter(Boolean));
  renderFields();
  return () => card.viewer.destroy();
}
