import {api, el, toast} from "./dom.js";
import {currentLayout} from "./layout.js";
import {renderAuth} from "./views/auth.js";
import {renderCalc} from "./views/calc.js";
import {renderHome} from "./views/home.js";
import {renderJob, renderJobs} from "./views/jobs.js";
import {renderMD} from "./views/md.js";
import {renderModel} from "./views/modeling.js";
import {renderArrhenius} from "./views/arrhenius.js";
import {renderCompare} from "./views/compare.js";
import {renderSeries} from "./views/series.js";
import {renderVoronoi} from "./views/voronoi.js";
import {renderCrystal} from "./views/crystal.js";
import {renderCoord} from "./views/coord.js";
import {renderSro} from "./views/sro.js";
import {renderPoly} from "./views/poly.js";
import {renderDistribution} from "./views/distribution.js";
import {renderSettings} from "./views/settings.js";
import {renderStructure, renderStructures, renderStructureTools} from "./views/structures.js";

const TABS = [
  {route: "home", label: "ホーム"},
  {route: "structures", label: "構造"},
  {route: "jobs", label: "ジョブ"},
  {route: "settings", label: "設定"},
];

export const state = {me: null};
let cleanup = null;   // 表示中の画面が使っているタイマーなどの後始末

// PC 表示では、構造とジョブを「左に一覧・右に詳細」に並べる。一覧は選び直しても描き直さない (スクロール位置と更新を保つ)
const SPLIT = {
  structures: {list: (view) => renderStructures(view, {listOnly: true}), detail: renderStructure, empty: renderStructureTools},
  jobs: {list: renderJobs, detail: renderJob, empty: "左の一覧からジョブを選んでください"},
};
let pane = null;   // {section, root, listCleanup, observer}

function closePane() {
  if (!pane) return;
  if (typeof pane.listCleanup === "function") pane.listCleanup();
  pane.observer.disconnect();
  pane = null;
}

// 一覧の中で、いま右に出している項目に印を付ける (一覧は数秒ごとに描き直されるので、そのたびに付け直す)
function markCurrent() {
  if (!pane) return;
  const current = location.hash.split("?")[0];
  for (const link of pane.root.querySelectorAll(".pane-list a[href]")) {
    const on = link.getAttribute("href") === current;
    link.classList.toggle("pane-current", on);
    if (on) link.setAttribute("aria-current", "true"); else link.removeAttribute("aria-current");
  }
}

async function openPane(section) {
  if (!pane || pane.section !== section || !pane.root.isConnected) {
    closePane();
    const list = el("div", {});
    const root = el("div", {class: "split"}, el("div", {class: "pane-list"}, list), el("div", {class: "pane-detail"}));
    document.getElementById("view").replaceChildren(root);
    const observer = new MutationObserver(markCurrent);
    pane = {section, root, listCleanup: null, observer};
    list.replaceChildren(el("p", {class: "loading", text: "読み込み中…"}));
    pane.listCleanup = await SPLIT[section].list(list);
    observer.observe(root.querySelector(".pane-list"), {childList: true, subtree: true});
  }
  const detail = el("div", {});
  pane.root.querySelector(".pane-detail").replaceChildren(detail);
  markCurrent();
  return detail;
}

// "#/jobs/3?x=1" → {parts: ["jobs", "3"], query: URLSearchParams}
function parseHash() {
  const [path, query = ""] = location.hash.replace(/^#\/?/, "").split("?");
  return {parts: path.split("/").filter(Boolean), query: new URLSearchParams(query)};
}

export function navigate(hash) {
  if (location.hash === hash) route();
  else location.hash = hash;
}

function renderTabs(active) {
  const tabs = document.getElementById("tabs");
  tabs.hidden = false;
  // 名前は PC 表示の左の帯にだけ出す
  tabs.replaceChildren(el("span", {class: "tabs-brand", text: "MatSimApp"}), ...TABS.map((tab) =>
    el("a", {class: `tab${tab.route === active ? " tab-active" : ""}`, href: `#/${tab.route}`,
             "aria-current": tab.route === active ? "page" : null, text: tab.label})));
}

// 画面の種類ごとの描き方。戻り値はその画面の後始末 (タイマーなど) をする関数
function renderSection(view, section, id, query) {
  if (section === "structures" && id) return renderStructure(view, Number(id));
  else if (section === "structures") return renderStructures(view);
  else if (section === "jobs" && id) return renderJob(view, Number(id));
  else if (section === "jobs") return renderJobs(view);
  else if (section === "md") return renderMD(view, query);
  else if (section === "calc" && id) return renderCalc(view, id, query);
  else if (section === "model" && id) return renderModel(view, id, query);
  else if (section === "arrhenius") return renderArrhenius(view, query);
  else if (section === "compare") return renderCompare(view, query);
  else if (section === "series" && id) return renderSeries(view, id);
  else if (section === "analysis" && id === "voronoi") return renderVoronoi(view, query);
  else if (section === "analysis" && id === "cna") return renderCrystal(view, query);
  else if (section === "analysis" && id === "coord") return renderCoord(view, query);
  else if (section === "analysis" && id === "sro") return renderSro(view, query);
  else if (section === "analysis" && id === "poly") return renderPoly(view, query);
  else if (section === "analysis" && id === "distribution") return renderDistribution(view, query);
  else if (section === "settings") return renderSettings(view);
  return renderHome(view);
}

async function route() {
  if (typeof cleanup === "function") cleanup();
  cleanup = null;
  // 画面ごとに新しい入れ物に描く。前の画面の読み込みが遅れて終わっても、外した入れ物に描くだけで今の画面を上書きしない
  let view = el("div", {});
  if (!state.me) {
    closePane();
    document.getElementById("view").replaceChildren(view);
    document.getElementById("tabs").hidden = true;
    await renderAuth(view, onSignedIn);
    return;
  }
  const {parts, query} = parseHash();
  const [section = "home", id] = parts;
  const split = currentLayout() === "pc" ? SPLIT[section] : null;
  if (!split) {
    closePane();
    document.getElementById("view").replaceChildren(view);
  }
  const tab = {md: "home", calc: "home", model: "structures", arrhenius: "jobs", compare: "jobs", series: "jobs",
               analysis: query.get("structure") ? "structures" : "jobs"}[section] || section;
  renderTabs(tab);
  window.scrollTo(0, 0);
  let done = null;
  try {
    if (split) {
      view = await openPane(section);
      if (!id) {
        if (typeof split.empty === "function") await split.empty(view);
        else view.replaceChildren(el("p", {class: "empty pane-empty", text: split.empty}));
        return;
      }
    }
    view.replaceChildren(el("p", {class: "loading", text: "読み込み中…"}));
    done = split ? await split.detail(view, Number(id)) : await renderSection(view, section, id, query);
  } catch (error) {
    if (error.status === 401 || !view.isConnected) return;
    view.replaceChildren(el("section", {class: "card"},
      el("p", {class: "error", text: error.message}),
      el("button", {class: "primary", type: "button", onclick: route, text: "再読み込み"})));
    return;
  }
  // 描き終わる前に別の画面へ移っていたら、この画面の後始末 (タイマーなど) だけして終える
  if (view.isConnected) cleanup = done;
  else if (typeof done === "function") done();
}

async function onSignedIn() {
  state.me = await api("/api/me");
  if (!location.hash) location.hash = "#/home";   // hashchange で route() が呼ばれる (2 回描かない)
  else await route();
}

export async function signOut() {
  try { await api("/api/auth/logout", {method: "POST"}); } catch { /* 切れていても画面はログインへ戻す */ }
  state.me = null;
  await route();
}

window.addEventListener("hashchange", route);
// 表示 (スマホ / PC) が変わったら、並べ方を作り直す
window.addEventListener("matsim:layout", () => {
  closePane();
  if (state.me) route();
});
window.addEventListener("matsim:unauthorized", () => {
  if (!state.me) return;
  state.me = null;
  toast("ログインの有効期限が切れました");
  route();
});

async function boot() {
  try {
    state.me = await api("/api/me");
  } catch {
    state.me = null;
  }
  if (state.me && !location.hash) location.hash = "#/home";
  await route();
}

boot();
