// サーバーが返す入力欄の定義 (fields) からフォームの部品を作る。計算の設定とモデリングで共通に使う。
import {el, field, toast} from "./dom.js";

export function visible(spec, values) {
  if (!spec.show_if) return true;
  return Object.entries(spec.show_if).every(([key, wanted]) =>
    (Array.isArray(wanted) ? wanted : [wanted]).includes(values[key]));
}

function readBase64(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result).split(",", 2)[1] || "");
    reader.onerror = () => reject(new Error("ファイルを読めませんでした"));
    reader.readAsDataURL(file);
  });
}

// 入力欄 1 つ。値が変わったら values を書き換えて onChange を呼ぶ。
// ctx.species: 元の構造にある元素、ctx.structures: 選べる構造の一覧 (モデリングで使う)
export function control(spec, values, onChange, ctx = {}) {
  const set = (value) => {
    values[spec.key] = value;
    onChange();
  };
  const label = spec.unit ? `${spec.label} (${spec.unit})` : spec.label;
  if (spec.type === "bool") {
    return el("label", {class: "check"},
      el("input", {type: "checkbox", checked: values[spec.key] === true, onchange: (event) => set(event.target.checked)}),
      el("span", {}, spec.label, spec.hint && el("span", {class: "field-hint block", text: spec.hint})));
  }
  if (spec.type === "select") {
    const select = el("select", {onchange: () => set(select.value)},
      spec.options.map((o) => el("option", {value: o.value, selected: o.value === values[spec.key], text: o.label})));
    return field(spec.label, select, spec.hint);
  }
  if (spec.type === "species" && spec.source === "structure" && (ctx.species || []).length) {
    const options = [...(spec.allow_all ? [{value: "*", label: "すべての元素"}] : []),
                     ...ctx.species.map((s) => ({value: s, label: s}))];
    const select = el("select", {onchange: () => set(select.value)},
      options.map((o) => el("option", {value: o.value, selected: o.value === values[spec.key], text: o.label})));
    return field(spec.label, select, spec.hint);
  }
  if (spec.type === "species") {
    const input = el("input", {type: "text", maxlength: "3", value: values[spec.key] ?? "", placeholder: "例: Ni",
                               autocapitalize: "none", autocomplete: "off", spellcheck: "false",
                               onchange: () => set(input.value.trim())});
    return field(label, input, spec.hint || "元素記号");
  }
  if (spec.type === "structure") {
    const list = ctx.structures || [];
    const select = el("select", {onchange: () => set(select.value === "" ? null : Number(select.value))},
      spec.optional && el("option", {value: "", selected: values[spec.key] === null, text: "(なし)"}),
      list.map((s) => el("option", {value: s.id, selected: s.id === values[spec.key],
                                    text: `${s.name} (${s.formula}・${s.n_atoms} 原子)`})));
    return field(spec.label, select, spec.hint || (list.length ? null : "構造がまだありません"));
  }
  if (spec.type === "file") {
    const input = el("input", {type: "file", accept: spec.accept || null, onchange: async () => {
      const file = input.files[0];
      if (!file) return set(null);
      if (file.size > spec.max_bytes) {
        toast(`ファイルが大きすぎます (最大 ${Math.floor(spec.max_bytes / 1024 / 1024)}MB)`);
        input.value = "";
        return set(null);
      }
      try {
        set({name: file.name, data: await readBase64(file)});
      } catch (error) {
        toast(error.message);
      }
    }});
    return field(spec.label, input, spec.hint);
  }
  if (spec.type === "text") {
    const input = el("input", {type: "text", maxlength: String(spec.max_length), value: values[spec.key] ?? "",
                               autocapitalize: "none", autocomplete: "off", spellcheck: "false",
                               onchange: () => set(input.value.trim())});
    return field(label, input, spec.hint);
  }
  const input = el("input", {type: "number", inputmode: spec.type === "int" ? "numeric" : "decimal", step: "any",
    value: values[spec.key] ?? "", min: spec.min ?? null, max: spec.max ?? null,
    placeholder: spec.optional ? "なし" : null,
    onchange: () => set(input.value.trim() === "" ? null : Number(input.value))});
  return field(label, input, spec.hint);
}
