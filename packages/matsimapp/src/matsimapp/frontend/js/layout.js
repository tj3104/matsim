// 表示の切り替え (スマホ向け / PC 向け)。選んだものはこのブラウザだけに覚える。
// 自動のときは画面の幅で決める。結果は <html data-layout="phone|pc"> に入れ、見た目は CSS で変える。

const KEY = "matsim.layout";
export const MODES = [
  {value: "auto", label: "自動"},
  {value: "phone", label: "スマホ"},
  {value: "pc", label: "PC"},
];
const WIDE = window.matchMedia("(min-width: 1100px)");

export function layoutMode() {
  try {
    const saved = localStorage.getItem(KEY);
    if (MODES.some((mode) => mode.value === saved)) return saved;
  } catch { /* 保存できないブラウザでは自動のまま */ }
  return "auto";
}

export function currentLayout() {
  const mode = layoutMode();
  return mode === "auto" ? (WIDE.matches ? "pc" : "phone") : mode;
}

export function applyLayout() {
  const next = currentLayout();
  const changed = document.documentElement.dataset.layout !== next;
  document.documentElement.dataset.layout = next;
  if (changed) window.dispatchEvent(new Event("matsim:layout"));
}

export function setLayoutMode(mode) {
  try {
    localStorage.setItem(KEY, mode);
  } catch { /* 覚えられなくても、今の画面には反映する */ }
  applyLayout();
}

WIDE.addEventListener("change", applyLayout);
applyLayout();
