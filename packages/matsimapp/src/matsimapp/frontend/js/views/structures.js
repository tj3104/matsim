import {api, busy, el, field, number, toast} from "../dom.js";
import {navigate, state} from "../main.js";
import {legendItems, Viewer} from "../viewer.js";
import {modelingCard} from "./modeling.js";
import {createSelection} from "../select.js";

export const SOURCE_LABEL = {upload: "アップロード", mp: "Materials Project", job: "計算結果", sample: "テスト構造",
                      model: "モデリング"};

function structureMain(structure) {
  const info = structure.info;
  return el("div", {class: "list-main"},
    el("span", {class: "list-title", text: structure.name}),
    el("span", {class: "list-sub",
      text: `${structure.formula} ・ ${number(structure.n_atoms)} 原子 ・ ${info.cell_shape} ・ ${SOURCE_LABEL[structure.source] || structure.source}`}));
}

// 選択モードのときは、行を押すとチェックが付く (詳細には移らない)
function structureRow(structure, selection) {
  if (selection.active) {
    return el("li", {}, el("label", {class: "list-row select-row"}, selection.box(structure), structureMain(structure)));
  }
  return el("li", {},
    el("a", {class: "list-row list-link", href: `#/structures/${structure.id}`},
      structureMain(structure),
      el("span", {class: "chevron", "aria-hidden": "true", text: "›"})));
}

// 動作確認用の構造をワンタップで追加する。構造がまだないときの MD 画面でも使う。
export async function sampleCard() {
  const samples = await api("/api/structures/samples");
  return el("section", {class: "card"},
    el("div", {class: "card-head"}, el("h2", {text: "テスト構造を追加"})),
    el("p", {class: "note", text: "動作確認用の小さな構造です。どちらも既定のポテンシャル (NEP89) で計算できます。"}),
    el("div", {class: "button-row"}, samples.map((item) => {
      const button = el("button", {class: "secondary", type: "button", text: item.label,
        onclick: () => busy(button, async () => {
          const {id} = await api("/api/structures/sample", {method: "POST", body: {key: item.key}});
          toast(`${item.label} を追加しました`);
          navigate(`#/structures/${id}`);
        })});
      return button;
    })));
}

function uploadCard() {
  const file = el("input", {type: "file", required: true, accept: ".cif,.vasp,.poscar,.xyz,.extxyz,*/*"});
  const name = el("input", {type: "text", maxlength: "60", placeholder: "空欄ならファイル名"});
  const submit = el("button", {class: "primary", type: "submit", text: "追加"});
  const form = el("form", {class: "card", onsubmit: (event) => {
    event.preventDefault();
    busy(submit, async () => {
      const body = new FormData();
      body.append("file", file.files[0]);
      body.append("name", name.value.trim());
      const {id} = await api("/api/structures/upload", {method: "POST", body});
      form.reset();
      toast("構造を追加しました");
      navigate(`#/structures/${id}`);
    });
  }},
    el("div", {class: "card-head"}, el("h2", {text: "ファイルから追加"})),
    field("構造ファイル", file, "cif / POSCAR / CONTCAR / .vasp / .xyz (セル情報つき)"),
    field("名前", name),
    el("div", {class: "button-row"}, submit));
  return form;
}

function mpCard() {
  const query = el("input", {type: "search", maxlength: "60", required: true, placeholder: "例: SiO2 / mp-149",
                             autocapitalize: "none", autocomplete: "off"});
  const submit = el("button", {class: "primary", type: "submit", text: "検索"});
  const results = el("ul", {class: "list"});

  function resultRow(item) {
    const add = el("button", {class: "secondary small", type: "button", text: "取得",
      onclick: () => busy(add, async () => {
        const {id} = await api("/api/mp/import", {method: "POST", body: {material_id: item.material_id, conventional: true}});
        toast("構造を追加しました");
        navigate(`#/structures/${id}`);
      })});
    const hull = item.energy_above_hull === null ? "" : ` ・ E_hull ${item.energy_above_hull.toFixed(3)} eV`;
    return el("li", {class: "list-row"},
      el("div", {class: "list-main"},
        el("span", {class: "list-title", text: `${item.formula} (${item.material_id})`}),
        el("span", {class: "list-sub", text: `${item.spacegroup} ・ ${item.nsites} サイト${hull}`})),
      add);
  }

  if (!state.me.mp_key_set) {
    return el("section", {class: "card"},
      el("div", {class: "card-head"}, el("h2", {text: "Materials Project から追加"})),
      el("p", {class: "note", text: "API キーが未設定です。"}),
      el("div", {class: "button-row"}, el("a", {class: "secondary", href: "#/settings", text: "設定を開く"})));
  }
  return el("form", {class: "card", onsubmit: (event) => {
    event.preventDefault();
    busy(submit, async () => {
      results.replaceChildren(el("li", {class: "empty", text: "検索中…"}));
      try {
        const found = await api(`/api/mp/search?q=${encodeURIComponent(query.value.trim())}`);
        results.replaceChildren(...(found.length ? found.map(resultRow)
          : [el("li", {class: "empty", text: "見つかりませんでした"})]));
      } catch (error) {
        results.replaceChildren();
        throw error;
      }
    });
  }},
    el("div", {class: "card-head"}, el("h2", {text: "Materials Project から追加"})),
    field("組成式または mp-id", query, "安定な構造 (E_hull の小さい順) から表示します"),
    el("div", {class: "button-row"}, submit),
    results);
}

// listOnly: PC の左右に並べる表示では、左に一覧だけを置く (追加・モデリングは renderStructureTools で右に出す)
export async function renderStructures(view, {listOnly = false} = {}) {
  let [structures, samples, listing] = listOnly
    ? [await api("/api/structures"), null, null]
    : await Promise.all([api("/api/structures"), sampleCard(), api("/api/modeling")]);
  const listCard = el("section", {class: "card"});
  const selection = createSelection({
    noun: "構造",
    onDelete: async (ids) => {
      const result = await api("/api/structures/delete", {method: "POST", body: {ids}});
      structures = await api("/api/structures");
      return `${result.deleted} 件の構造を削除しました`;
    },
    onChange: () => renderList(),
  });
  function renderList() {
    listCard.replaceChildren(...[
      el("div", {class: "card-head"}, el("h2", {text: `登録済み (${structures.length})`}),
        structures.length ? selection.toggle : null),
      selection.bar(structures),
      structures.length
        ? el("ul", {class: "list"}, structures.map((s) => structureRow(s, selection)))
        : el("p", {class: "empty", text: "まだ構造がありません。下から追加してください。"}),
    ].filter(Boolean));
  }
  renderList();
  if (listOnly) {
    view.replaceChildren(el("h1", {class: "page-title", text: "構造"}), listCard);
    return;
  }
  view.replaceChildren(
    el("h1", {class: "page-title", text: "構造"}),
    listCard,
    modelingCard(listing, {title: "モデリング (作る・加工する)"}),
    samples,
    uploadCard(),
    mpCard());
}

// 構造を追加する・作る欄 (PC の左右に並べる表示で、何も選んでいないときの右側)
export async function renderStructureTools(view) {
  const [samples, listing] = await Promise.all([sampleCard(), api("/api/modeling")]);
  view.replaceChildren(
    el("p", {class: "note", text: "左の一覧から構造を選ぶと、ここに詳細を表示します。新しい構造は次から追加できます。"}),
    modelingCard(listing, {title: "モデリング (作る・加工する)"}),
    samples,
    uploadCard(),
    mpCard());
}

// 3D 表示つきのカード。MD の設定画面でも使う。
export function viewerCard(title) {
  const canvas = el("canvas", {class: "viewer", "aria-label": "構造の 3D 表示。ドラッグで回転、ピンチで拡大縮小"});
  const legend = el("ul", {class: "legend"});
  const caption = el("p", {class: "note"});
  const viewer = new Viewer(canvas);
  return {
    viewer, caption,
    node: el("section", {class: "card"},
      el("div", {class: "card-head"}, el("h2", {text: title})), canvas, legend, caption),
    show(data) {
      viewer.setStructure(data);
      legend.replaceChildren(...legendItems(data.symbols).map((item) =>
        el("li", {class: "legend-item"},
          el("span", {class: "legend-swatch", style: `background:${item.color}`, "aria-hidden": "true"}),
          data.shown < data.n_atoms ? item.symbol : `${item.symbol} × ${number(item.count)}`)));
    },
  };
}

export async function renderStructure(view, id) {
  const [structure, listing, kinds] = await Promise.all([api(`/api/structures/${id}`), api("/api/modeling"), api("/api/kinds")]);
  const info = structure.info;
  const card = viewerCard("3D 表示");
  card.show(structure.view);
  card.caption.textContent = "ドラッグで回転、ピンチ・ホイールで拡大縮小";

  const rows = [
    ["組成", `${structure.formula} (${number(structure.n_atoms)} 原子)`],
    ["セル", `${info.a.toFixed(3)} × ${info.b.toFixed(3)} × ${info.c.toFixed(3)} Å`],
    ["角度", `${info.alpha.toFixed(1)}° / ${info.beta.toFixed(1)}° / ${info.gamma.toFixed(1)}°`],
    ["形状", info.cell_shape],
    ["密度", `${info.density_g_cm3.toFixed(3)} g/cm³`],
    ["取得元", `${SOURCE_LABEL[structure.source] || structure.source}${structure.source_detail ? ` (${structure.source_detail})` : ""}`],
  ];
  const remove = el("button", {class: "secondary danger", type: "button", text: "削除",
    onclick: () => busy(remove, async () => {
      if (!confirm(`「${structure.name}」を削除しますか？`)) return;
      await api(`/api/structures/${id}`, {method: "DELETE"});
      toast("削除しました");
      navigate("#/structures");
    })});

  view.replaceChildren(
    el("a", {class: "back-link", href: "#/structures", text: "‹ 構造の一覧"}),
    el("h1", {class: "page-title", text: structure.name}),
    card.node,
    el("section", {class: "card"},
      el("dl", {class: "facts"}, rows.map(([key, value]) => [el("dt", {text: key}), el("dd", {text: value})])),
      el("div", {class: "button-row"},
        el("a", {class: "primary", href: `#/md?structure=${id}`, text: "この構造で MD"}), remove)),
    calculationsFor(kinds, id),
    el("section", {class: "card"},
      el("div", {class: "card-head"}, el("h2", {text: "この構造を解析する"})),
      el("div", {class: "button-row"},
        el("a", {class: "secondary", href: `#/analysis/cna?structure=${id}`, text: "結晶構造の判定"}),
        el("a", {class: "secondary", href: `#/analysis/coord?structure=${id}`, text: "配位数"}),
        el("a", {class: "secondary", href: `#/analysis/poly?structure=${id}`, text: "配位多面体"}),
        el("a", {class: "secondary", href: `#/analysis/sro?structure=${id}`, text: "短範囲秩序"}),
        el("a", {class: "secondary", href: `#/analysis/voronoi?structure=${id}`, text: "ボロノイ解析"}),
        el("a", {class: "secondary", href: `#/analysis/distribution?structure=${id}`, text: "原子分布"}))),
    modelingCard(listing, {structureId: id, title: "この構造を加工する"}));
  return () => card.viewer.destroy();
}


// 構造の画面の「この構造で計算する」: 計算メニューを分類ごとに、計算の名前だけの小さなタイルで並べる。
// 押すと、その構造を選んだ状態で設定画面が開く。
function calculationsFor(listing, structureId) {
  const href = (kind) => (kind.key === "md" ? `#/md?structure=${structureId}` : `#/calc/${kind.key}?structure=${structureId}`);
  return el("section", {class: "card"},
    el("div", {class: "card-head"}, el("h2", {text: "この構造で計算する"}),
      el("a", {class: "link-button", href: "/manual/manual.html#ch-goal", target: "_blank", rel: "noopener", text: "どれを選ぶ?"})),
    listing.groups.map((group) => {
      const kinds = listing.kinds.filter((kind) => kind.group === group);
      return kinds.length ? [el("h3", {class: "group-title", text: group}),
        el("div", {class: "tiles tiles-compact"}, kinds.map((kind) => el("a", {class: "tile tile-ready", href: href(kind),
          title: kind.note}, el("span", {class: "tile-name", text: kind.label}))))] : null;
    }));
}
