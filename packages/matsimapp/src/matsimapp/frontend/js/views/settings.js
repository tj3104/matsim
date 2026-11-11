import {api, busy, el, field, toast} from "../dom.js";
import {signOut, state} from "../main.js";
import {MODES, currentLayout, layoutMode, setLayoutMode} from "../layout.js";

function layoutCard() {
  const note = el("p", {class: "note"});
  const buttons = MODES.map((mode) => el("button", {type: "button", text: mode.label, onclick: () => {
    setLayoutMode(mode.value);
    show();
  }}));

  function show() {
    const mode = layoutMode();
    MODES.forEach((item, index) => {
      buttons[index].classList.toggle("seg-on", item.value === mode);
      buttons[index].setAttribute("aria-pressed", String(item.value === mode));
    });
    const now = currentLayout() === "pc" ? "PC 向け" : "スマホ向け";
    note.textContent = mode === "auto"
      ? `画面の幅で切り替えます (幅 1100px 以上で PC 向け)。今の表示: ${now}`
      : `いつも${now}で表示します。`;
  }

  show();
  return el("section", {class: "card"},
    el("div", {class: "card-head"}, el("h2", {text: "表示"})),
    el("div", {class: "segmented", role: "group", "aria-label": "表示"}, ...buttons),
    note,
    el("p", {class: "field-hint", text: "このブラウザだけに覚えます。スマホと PC で別々に選べます"}));
}

function mpKeyCard() {
  const status = el("p", {class: "note"});
  const input = el("input", {type: "password", autocomplete: "off", maxlength: "100", placeholder: "API キー"});
  const save = el("button", {class: "primary", type: "submit", text: "保存"});
  const remove = el("button", {class: "secondary", type: "button", text: "削除", onclick: () => busy(remove, () => put(""))});

  function show() {
    status.textContent = state.me.mp_key_set
      ? "API キーは保存済みです。入れ替えるときは新しいキーを入力してください。"
      : "未設定です。Materials Project から構造を取得するには API キーが必要です。";
    remove.hidden = !state.me.mp_key_set;
  }

  async function put(key) {
    const result = await api("/api/settings/mp-key", {method: "PUT", body: {key}});
    state.me.mp_key_set = result.mp_key_set;
    input.value = "";
    show();
    toast(key ? "保存しました" : "削除しました");
  }

  show();
  return el("form", {class: "card", onsubmit: (event) => {
    event.preventDefault();
    if (!input.value.trim()) return;
    busy(save, () => put(input.value.trim()));
  }},
    el("div", {class: "card-head"}, el("h2", {text: "Materials Project"})),
    status,
    field("API キー", input, "キーはこの PC の中にだけ保存され、画面には表示されません"),
    el("div", {class: "button-row"}, save, remove));
}

function potentialRow(potential, reload) {
  const remove = !potential.builtin && el("button", {class: "link-button danger", type: "button", text: "削除",
    onclick: () => busy(remove, async () => {
      if (!confirm(`「${potential.name}」を削除しますか？`)) return;
      await api(`/api/potentials/${potential.id}`, {method: "DELETE"});
      await reload();
    })});
  const elements = potential.elements.length > 12
    ? `${potential.elements.slice(0, 12).join(" ")} … (${potential.elements.length} 元素)`
    : potential.elements.join(" ");
  return el("li", {class: "list-row"},
    el("div", {class: "list-main"},
      el("span", {class: "list-title", text: potential.name}),
      el("span", {class: "list-sub", text: potential.available ? elements : "ファイルが見つかりません"})),
    remove);
}

function potentialsCard() {
  const list = el("ul", {class: "list"});
  const name = el("input", {type: "text", maxlength: "60", required: true, placeholder: "例: PbTe 用 NEP"});
  const file = el("input", {type: "file", required: true});
  const upload = el("button", {class: "primary", type: "submit", text: "追加"});

  async function reload() {
    const potentials = await api("/api/potentials");
    list.replaceChildren(...potentials.map((p) => potentialRow(p, reload)));
  }

  const form = el("form", {class: "subform", onsubmit: (event) => {
    event.preventDefault();
    busy(upload, async () => {
      const body = new FormData();
      body.append("file", file.files[0]);
      body.append("name", name.value.trim());
      await api("/api/potentials/upload", {method: "POST", body});
      form.reset();
      toast("ポテンシャルを追加しました");
      await reload();
    });
  }},
    el("h3", {class: "group-title", text: "nep.txt を追加"}),
    field("名前", name),
    field("ファイル", file, "NEP 形式 (nep.txt)"),
    el("div", {class: "button-row"}, upload));

  return {reload, node: el("section", {class: "card"},
    el("div", {class: "card-head"}, el("h2", {text: "ポテンシャル"})), list, form)};
}

export async function renderSettings(view) {
  const potentials = potentialsCard();
  await potentials.reload();
  const logout = el("button", {class: "secondary", type: "button", text: "ログアウト", onclick: () => busy(logout, signOut)});
  view.replaceChildren(
    el("h1", {class: "page-title", text: "設定"}),
    layoutCard(),
    potentials.node,
    mpKeyCard(),
    el("section", {class: "card"},
      el("div", {class: "card-head"}, el("h2", {text: "アカウント"})),
      el("p", {class: "note", text: `${state.me.username} でログイン中`}),
      el("div", {class: "button-row"}, logout)),
    el("a", {class: "back-link", href: "/manual/manual.html", text: "使い方 (マニュアル) を開く"}));
}
