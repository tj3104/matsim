// ジョブの並べ替えと絞り込み (構成元素・計算の種類・状態・文字)。ジョブ一覧と比較の画面で使う。
// 選んだ条件はこのブラウザに覚える (storageKey ごと)。
import {el} from "./dom.js";

const SORTS = [
  {value: "new", label: "新しい順", compare: (a, b) => b.id - a.id},
  {value: "old", label: "古い順", compare: (a, b) => a.id - b.id},
  {value: "name", label: "名前", compare: (a, b) => a.name.localeCompare(b.name, "ja")},
  {value: "kind", label: "計算の種類", compare: (a, b) => kindLabel(a).localeCompare(kindLabel(b), "ja") || b.id - a.id},
  {value: "atoms", label: "原子数 (多い順)", compare: (a, b) => (b.n_atoms || 0) - (a.n_atoms || 0) || b.id - a.id},
];
const STATUSES = [
  {value: "", label: "すべての状態", test: () => true},
  {value: "done", label: "完了", test: (j) => j.status === "done"},
  {value: "active", label: "実行中・待ち", test: (j) => j.status === "running" || j.status === "queued"},
  {value: "ended", label: "失敗・中止", test: (j) => j.status === "failed" || j.status === "cancelled"},
];

export const kindLabel = (job) => job.summary?.kind_label || (job.kind === "md" ? "MD" : job.kind);
export const elementsOf = (job) => [...new Set((job.summary?.formula || "").match(/[A-Z][a-z]?/g) || [])];

function load(key) {
  try { return JSON.parse(localStorage.getItem(key)) || {}; } catch { return {}; }
}

function save(key, state) {
  try { localStorage.setItem(key, JSON.stringify(state)); } catch { /* 覚えられなくても絞り込みは効く */ }
}

// status: false にすると状態の絞り込みを出さない (比較の画面は完了したジョブだけ)
export function createJobFilter({storageKey, onChange, status = true}) {
  const state = {sort: "new", kind: "", status: "", text: "", elements: [], ...load(storageKey)};
  const sort = el("select", {"aria-label": "並べ替え"}, SORTS.map((s) => el("option", {value: s.value, text: s.label})));
  const kind = el("select", {"aria-label": "計算の種類"});
  const statusSelect = el("select", {"aria-label": "状態"}, STATUSES.map((s) => el("option", {value: s.value, text: s.label})));
  const text = el("input", {type: "search", placeholder: "名前・組成式で探す", "aria-label": "名前・組成式で探す", value: state.text});
  const elementChips = el("div", {class: "chips filter-elements"});
  const count = el("span", {class: "filter-count"});
  sort.value = state.sort;
  statusSelect.value = state.status;
  let lastKinds = "", lastElements = "";

  const changed = () => { save(storageKey, state); onChange(); };
  sort.onchange = () => { state.sort = sort.value; changed(); };
  kind.onchange = () => { state.kind = kind.value; changed(); };
  statusSelect.onchange = () => { state.status = statusSelect.value; changed(); };
  text.oninput = () => { state.text = text.value.trim(); changed(); };

  function renderElements(all) {
    elementChips.replaceChildren(...all.map((symbol) => {
      const on = state.elements.includes(symbol);
      return el("button", {type: "button", class: `chip chip-small${on ? " chip-on" : ""}`, "aria-pressed": String(on), text: symbol,
        onclick: () => {
          state.elements = on ? state.elements.filter((s) => s !== symbol) : [...state.elements, symbol];
          renderElements(all);
          changed();
        }});
    }));
  }

  // 候補 (計算の種類・元素) を今のジョブから作り直す。変わったときだけ描き直す (入力中の操作を邪魔しない)
  function update(jobs) {
    const kinds = [...new Set(jobs.map(kindLabel))].sort((a, b) => a.localeCompare(b, "ja"));
    if (kinds.join() !== lastKinds) {
      lastKinds = kinds.join();
      if (state.kind && !kinds.includes(state.kind)) state.kind = "";
      kind.replaceChildren(el("option", {value: "", text: "すべての種類"}), ...kinds.map((k) => el("option", {value: k, text: k})));
      kind.value = state.kind;
    }
    const elements = [...new Set(jobs.flatMap(elementsOf))].sort();
    if (elements.join() !== lastElements) {
      lastElements = elements.join();
      state.elements = state.elements.filter((s) => elements.includes(s));
      renderElements(elements);
    }
  }

  function apply(jobs) {
    update(jobs);
    const words = state.text.toLowerCase();
    const statusTest = STATUSES.find((s) => s.value === state.status)?.test || (() => true);
    const out = jobs.filter((job) => (!state.kind || kindLabel(job) === state.kind)
      && statusTest(job)
      && state.elements.every((symbol) => elementsOf(job).includes(symbol))
      && (!words || `${job.name} ${job.summary?.formula || ""}`.toLowerCase().includes(words)))
      .sort(SORTS.find((s) => s.value === state.sort)?.compare || SORTS[0].compare);
    count.textContent = out.length === jobs.length ? `${jobs.length} 件` : `${out.length} / ${jobs.length} 件`;
    summary.textContent = `並べ替え・絞り込み ・ ${count.textContent}`;
    return out;
  }

  // 折りたたみにする。条件を付けているときは開いておく
  const summary = el("summary", {text: "並べ替え・絞り込み"});
  const active = state.sort !== "new" || state.kind || state.status || state.text || state.elements.length;
  const node = el("details", {class: "job-filter", open: Boolean(active)}, summary,
    el("div", {class: "job-filter-body"},
      el("div", {class: "filter-row"}, sort, kind, status ? statusSelect : null, count),
      text,
      el("div", {class: "filter-elements-row"}, el("span", {class: "field-hint", text: "元素 (選んだものをすべて含む)"}), elementChips)));
  return {node, apply};
}
