// 構造を選ぶ部品 (MD とそれ以外の計算の設定画面)。ふだんは選んでいる構造だけを出し、「変える」で一覧を開く。
// 一覧は、名前・組成・元素で探す、取得元で絞る、並べ替える ができる。構造が多くなっても選びやすいように。
import {el, number} from "./dom.js";
import {SOURCE_LABEL} from "./views/structures.js";

const SORTS = {
  new: {label: "新しい順", compare: (a, b) => b.id - a.id},
  name: {label: "名前順", compare: (a, b) => a.name.localeCompare(b.name, "ja")},
  small: {label: "原子の少ない順", compare: (a, b) => a.n_atoms - b.n_atoms || b.id - a.id},
};

// 探す語: 空白で区切った語がすべて当てはまるもの。元素記号 (Cu・O など、大文字で始まる 1〜2 文字) は組成の元素と比べ、
// それ以外は名前・組成・取得元に含まれるかで比べる (大文字・小文字を区別しない)
function matches(structure, words) {
  const species = Object.keys(structure.info.species || {});
  const text = `${structure.name} ${structure.formula} ${SOURCE_LABEL[structure.source] || ""} ${structure.source_detail || ""}`.toLowerCase();
  return words.every((word) => (/^[A-Z][a-z]?$/.test(word) && species.includes(word)) || text.includes(word.toLowerCase()));
}

export function structureChooser({structures, selected, onSelect}) {
  let current = selected;
  let open = false;
  let sort = "new";
  let source = "";
  const summary = el("div", {class: "chooser-summary"});
  const search = el("input", {type: "search", placeholder: "名前・組成・元素で探す (例: Cu、MgO、Ti hcp)",
                              "aria-label": "構造を探す", autocomplete: "off", oninput: () => renderList()});
  const sortSelect = el("select", {"aria-label": "並べ替え", onchange: () => { sort = sortSelect.value; renderList(); }},
    Object.entries(SORTS).map(([key, s]) => el("option", {value: key, text: s.label})));
  const sources = [...new Set(structures.map((s) => s.source))];
  const sourceRow = el("div", {class: "chips"});
  const count = el("p", {class: "note"});
  const list = el("ul", {class: "chooser-list", role: "listbox", "aria-label": "構造の一覧"});
  const panel = el("div", {class: "chooser-panel", hidden: true},
    search, el("div", {class: "chooser-tools"}, sortSelect), sources.length > 1 ? sourceRow : null, count, list);
  const node = el("div", {class: "chooser"}, summary, panel);

  function line(s) {
    return `${s.formula} ・ ${number(s.n_atoms)} 原子 ・ ${SOURCE_LABEL[s.source] || s.source}`
      + (s.created_at ? ` ・ ${s.created_at.slice(0, 10)}` : "");
  }

  function renderSummary() {
    const s = structures.find((x) => x.id === current);
    summary.replaceChildren(
      el("div", {class: "chooser-current"}, el("strong", {text: s ? s.name : "(構造がありません)"}),
        s ? el("span", {class: "list-sub", text: line(s)}) : null),
      el("button", {class: "secondary small", type: "button", "aria-expanded": String(open),
                    text: open ? "閉じる" : `変える (${number(structures.length)} 件)`, onclick: () => toggle()}));
  }

  function renderSources() {
    sourceRow.replaceChildren(...["", ...sources].map((key) => el("button", {type: "button",
      class: `chip${source === key ? " chip-on" : ""}`, "aria-pressed": String(source === key),
      text: key ? SOURCE_LABEL[key] || key : "すべて", onclick: () => { source = key; renderSources(); renderList(); }})));
  }

  function renderList() {
    const words = search.value.trim().split(/\s+/).filter(Boolean);
    const shown = structures.filter((s) => (!source || s.source === source) && matches(s, words))
      .sort(SORTS[sort].compare);
    count.textContent = `${number(shown.length)} / ${number(structures.length)} 件`;
    list.replaceChildren(...shown.slice(0, 200).map((s) => el("li", {},
      el("button", {type: "button", role: "option", "aria-selected": String(s.id === current),
                    class: `chooser-item${s.id === current ? " chooser-on" : ""}`, onclick: () => choose(s.id)},
        el("span", {class: "list-title", text: s.name}), el("span", {class: "list-sub", text: line(s)})))));
    if (!shown.length) list.append(el("li", {class: "empty", text: "当てはまる構造がありません"}));
  }

  function toggle(force) {
    open = force ?? !open;
    panel.hidden = !open;
    renderSummary();
    if (open) {
      renderSources();
      renderList();
      search.focus();
    }
  }

  function choose(id) {
    current = id;
    toggle(false);
    onSelect(id);
  }

  renderSummary();
  return {node, get value() { return current; }};
}
