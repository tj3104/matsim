// 一覧の「選択」モード。チェックを付けた行をまとめて削除する (構造の一覧・ジョブの一覧で使う)。
import {busy, el, toast} from "./dom.js";

// items: 現在の行、canSelect(item): 選べるか、onDelete(ids): 削除して結果の文を返す、onChange: 作り直し
export function createSelection({noun, canSelect = () => true, onDelete, onChange}) {
  const state = {active: false, ids: new Set()};
  const toggle = el("button", {class: "link-button", type: "button", text: "選択", onclick: () => {
    state.active = !state.active;
    state.ids.clear();
    toggle.textContent = state.active ? "やめる" : "選択";
    onChange();
  }});

  function bar(items) {
    if (!state.active) return null;
    const selectable = items.filter(canSelect);
    // 一覧から消えた行の選択は外す
    for (const id of [...state.ids]) if (!selectable.some((item) => item.id === id)) state.ids.delete(id);
    const all = selectable.length > 0 && selectable.every((item) => state.ids.has(item.id));
    const remove = el("button", {class: "secondary danger small", type: "button", disabled: !state.ids.size,
      text: `選択した ${state.ids.size} 件を削除`, onclick: () => busy(remove, async () => {
        if (!confirm(`選択した ${state.ids.size} 件の${noun}を削除しますか？元に戻せません。`)) return;
        const message = await onDelete([...state.ids]);
        toast(message);
        state.active = false;
        state.ids.clear();
        toggle.textContent = "選択";
        onChange();
      })});
    return el("div", {class: "select-bar"},
      el("button", {class: "secondary small", type: "button", disabled: !selectable.length,
        text: all ? "選択を解除" : "すべて選択", onclick: () => {
          if (all) state.ids.clear(); else selectable.forEach((item) => state.ids.add(item.id));
          onChange();
        }}),
      remove);
  }

  // 行の左に置くチェックボックス
  function box(item) {
    return el("input", {type: "checkbox", class: "select-box", checked: state.ids.has(item.id), disabled: !canSelect(item),
      "aria-label": "選択", onchange: (event) => {
        if (event.target.checked) state.ids.add(item.id); else state.ids.delete(item.id);
        onChange();
      }});
  }

  return {toggle, bar, box, get active() { return state.active; }};
}
