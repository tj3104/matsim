import {api, busy, el, field} from "../dom.js";

// ログイン画面。アカウントが 1 つもないときは、最初のアカウントを作る画面になる。
export async function renderAuth(view, onSignedIn) {
  const status = await api("/api/status");
  const setup = status.needs_setup;
  const username = el("input", {type: "text", name: "username", autocomplete: "username", autocapitalize: "none",
                                required: true, minlength: "3", maxlength: "32"});
  const password = el("input", {type: "password", name: "password", required: true, maxlength: "200",
                                minlength: setup ? "10" : null,
                                autocomplete: setup ? "new-password" : "current-password"});
  const message = el("p", {class: "error", role: "alert", hidden: true});
  const submit = el("button", {class: "primary wide", type: "submit", text: setup ? "アカウントを作成" : "ログイン"});

  const form = el("form", {class: "card auth-card", onsubmit: (event) => {
    event.preventDefault();
    message.hidden = true;
    busy(submit, async () => {
      try {
        await api(setup ? "/api/auth/setup" : "/api/auth/login",
                  {method: "POST", body: {username: username.value.trim(), password: password.value}});
      } catch (error) {
        message.textContent = error.message;
        message.hidden = false;
        return;
      }
      await onSignedIn();
    });
  }},
    el("h2", {text: setup ? "最初のアカウントを作成" : "ログイン"}),
    setup && el("p", {class: "note", text: "このアプリから GPU の計算を動かせるのは、ここで作るアカウントだけです。"}),
    field("ユーザー名", username, setup ? "半角英数字と . _ - で 3〜32 文字" : null),
    field("パスワード", password, setup ? "10 文字以上" : null),
    message, submit);

  view.replaceChildren(
    el("header", {class: "app-header"},
      el("img", {class: "app-icon", src: "icons/icon.svg", alt: "", width: "44", height: "44"}),
      el("div", {}, el("h1", {text: "MatSimApp"}),
        el("p", {class: "subtitle", text: `GPUMD をスマホから ・ v${status.app_version}`}))),
    form);
  username.focus();
}
