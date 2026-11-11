// DOM を組み立てる小さなヘルパーと API 呼び出し。CSP でスクリプトの inline を禁止しているので innerHTML は使わない。

export function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === false || value === null || value === undefined) continue;
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else if (key === "value") node.value = value;
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else node.setAttribute(key, value === true ? "" : value);
  }
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

export class ApiError extends Error {
  constructor(message, status) {
    super(message);
    this.status = status;
  }
}

function errorMessage(detail, status) {
  if (typeof detail === "string") return detail;
  // 入力チェック (422) は項目ごとの配列で返ってくる
  if (Array.isArray(detail) && detail.length) {
    const first = detail[0];
    const where = (first.loc || []).filter((part) => part !== "body").join(" › ");
    return `入力を確認してください${where ? ` (${where})` : ""}`;
  }
  return `エラー (${status})`;
}

// body にオブジェクトを渡すと JSON、FormData を渡すとファイル送信になる
export async function api(path, {method = "GET", body} = {}) {
  const options = {method, credentials: "same-origin", headers: {"X-Requested-With": "matsimapp"}};
  if (body instanceof FormData) {
    options.body = body;
  } else if (body !== undefined) {
    options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(body);
  }
  let response;
  try {
    response = await fetch(path, options);
  } catch {
    throw new ApiError("サーバーに接続できません", 0);
  }
  if (!response.ok) {
    let detail = null;
    try { detail = (await response.json()).detail; } catch { /* JSON でない応答 */ }
    if (response.status === 401 && !path.startsWith("/api/auth/")) {
      window.dispatchEvent(new Event("matsim:unauthorized"));
    }
    throw new ApiError(errorMessage(detail, response.status), response.status);
  }
  return response.json();
}

let toastTimer = null;
export function toast(message) {
  const node = document.getElementById("toast");
  node.textContent = message;
  node.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { node.hidden = true; }, 3500);
}

// ボタンを押している間は二重送信を防ぎ、失敗したらトーストで知らせる
export async function busy(button, action) {
  if (button.disabled) return;
  button.disabled = true;
  try {
    return await action();
  } catch (error) {
    toast(error.message);
  } finally {
    button.disabled = false;
  }
}

export function field(label, control, hint) {
  return el("label", {class: "field"},
    el("span", {class: "field-label", text: label}),
    control,
    hint && el("span", {class: "field-hint", text: hint}));
}

export function formatDuration(seconds) {
  if (seconds === null || seconds === undefined) return "—";
  const s = Math.round(seconds);
  if (s < 60) return `${s}秒`;
  if (s < 3600) return `${Math.floor(s / 60)}分${String(s % 60).padStart(2, "0")}秒`;
  return `${Math.floor(s / 3600)}時間${String(Math.floor((s % 3600) / 60)).padStart(2, "0")}分`;
}

export function formatBytes(bytes) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 ** 2) return `${(bytes / 1024).toFixed(1)} KB`;
  if (bytes < 1024 ** 3) return `${(bytes / 1024 ** 2).toFixed(1)} MB`;
  return `${(bytes / 1024 ** 3).toFixed(2)} GB`;
}

// サーバーの時刻 (UTC) を端末の時刻で「10/14 08:15」のように表示する
export function formatTime(iso) {
  if (!iso) return "—";
  const date = new Date(iso.includes("T") ? iso : `${iso.replace(" ", "T")}Z`);
  if (Number.isNaN(date.getTime())) return "—";
  const pad = (n) => String(n).padStart(2, "0");
  return `${date.getMonth() + 1}/${date.getDate()} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

export const number = (value) => Number(value).toLocaleString("ja-JP");
