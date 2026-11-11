// マニュアルの目次と文字検索。
// 目次: 見出し (h2 = 部、h3 = 章) から作る。広い画面では左に常に出し、スマホでは「目次」ボタンで開く。
//       読んでいる章を目次で強調する。
// 検索: 本文の一致した所を <mark> で色付けし、件数と前後への移動ボタンを出す。一致した章の一覧から移動もできる。
//       閉じた <details> の中に一致があれば開く。Enter で次、Shift+Enter で前、「/」で検索欄へ。
(function () {
  "use strict";
  const main = document.querySelector("main");
  const toc = document.getElementById("toc");
  const tocList = document.getElementById("toc-list");
  const tocToggle = document.getElementById("toc-toggle");
  const search = document.getElementById("search");
  const counter = document.getElementById("search-count");
  const prev = document.getElementById("search-prev");
  const next = document.getElementById("search-next");
  const clear = document.getElementById("search-clear");
  const results = document.getElementById("search-results");

  // ---- 目次 -----------------------------------------------------------------------------
  const headings = [...main.querySelectorAll("h2, h3")];
  const slug = (text, i) => "s" + i;
  headings.forEach((h, i) => { if (!h.id) h.id = slug(h.textContent, i); });
  const links = new Map();
  let group = null;
  for (const h of headings) {
    const a = document.createElement("a");
    a.href = "#" + h.id;
    a.textContent = h.textContent;
    a.addEventListener("click", () => closeToc());
    links.set(h.id, a);
    const li = document.createElement("li");
    li.append(a);
    if (h.tagName === "H2") {
      li.className = "toc-part";
      group = document.createElement("ul");
      li.append(group);
      tocList.append(li);
    } else {
      (group || tocList).append(li);
    }
  }

  function openToc() { toc.classList.add("open"); tocToggle.setAttribute("aria-expanded", "true"); }
  function closeToc() { toc.classList.remove("open"); tocToggle.setAttribute("aria-expanded", "false"); }
  tocToggle.addEventListener("click", () => (toc.classList.contains("open") ? closeToc() : openToc()));
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeToc(); });

  // 読んでいる章 (画面の上の方にある最後の見出し) を強調する
  let ticking = false;
  function markCurrent() {
    ticking = false;
    let current = headings[0];
    for (const h of headings) {
      if (h.getBoundingClientRect().top < 120) current = h; else break;
    }
    links.forEach((a, id) => a.classList.toggle("current", id === current.id));
  }
  window.addEventListener("scroll", () => { if (!ticking) { ticking = true; requestAnimationFrame(markCurrent); } }, {passive: true});
  markCurrent();

  // ---- 検索 -----------------------------------------------------------------------------
  let marks = [];
  let index = -1;

  function unmark() {
    for (const m of main.querySelectorAll("mark.hit")) {
      m.replaceWith(document.createTextNode(m.textContent));
    }
    main.normalize();
    marks = [];
    index = -1;
  }

  function textNodes() {
    const walker = document.createTreeWalker(main, NodeFilter.SHOW_TEXT, {
      // 図 (SVG) の中の文字は <mark> で囲むと表示されなくなるので、色付けの対象にしない (図の説明文は対象)
      acceptNode: (node) => (node.nodeValue.trim() && !node.parentElement.closest("script, style, svg")
        ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_REJECT),
    });
    const nodes = [];
    while (walker.nextNode()) nodes.push(walker.currentNode);
    return nodes;
  }

  function sectionOf(node) {
    // その位置より前にある最後の章見出し
    let found = null;
    for (const h of headings) {
      if (h.compareDocumentPosition(node) & Node.DOCUMENT_POSITION_FOLLOWING) found = h; else break;
    }
    return found;
  }

  function run() {
    unmark();
    results.replaceChildren();
    const query = search.value.trim();
    clear.hidden = !query;
    if (!query) {
      counter.textContent = "";
      return;
    }
    const needle = query.toLowerCase();
    for (const node of textNodes()) {
      const text = node.nodeValue;
      const lower = text.toLowerCase();
      let at = lower.indexOf(needle);
      if (at < 0) continue;
      const frag = document.createDocumentFragment();
      let last = 0;
      while (at >= 0) {
        frag.append(text.slice(last, at));
        const m = document.createElement("mark");
        m.className = "hit";
        m.textContent = text.slice(at, at + query.length);
        frag.append(m);
        marks.push(m);
        last = at + query.length;
        at = lower.indexOf(needle, last);
      }
      frag.append(text.slice(last));
      node.replaceWith(frag);
    }
    counter.textContent = marks.length ? `${marks.length} 件` : "見つかりません";
    // 章ごとの一覧 (最初の一致の前後の文)
    const bySection = new Map();
    for (const [i, m] of marks.entries()) {
      const h = sectionOf(m);
      const key = h ? h.id : "";
      if (!bySection.has(key)) bySection.set(key, {heading: h, first: i, count: 0});
      bySection.get(key).count += 1;
    }
    for (const {heading, first, count} of bySection.values()) {
      const m = marks[first];
      const block = m.closest("p, li, td, pre, dd, h2, h3, h4") || m.parentElement;
      const text = block.textContent.replace(/\s+/g, " ");
      const at = text.toLowerCase().indexOf(needle);
      const snippet = (at > 30 ? "…" : "") + text.slice(Math.max(0, at - 30), at + query.length + 40) + "…";
      const button = document.createElement("button");
      button.type = "button";
      const title = document.createElement("strong");
      title.textContent = (heading ? heading.textContent : "はじめ") + ` (${count})`;
      const sub = document.createElement("span");
      sub.textContent = snippet;
      button.append(title, sub);
      button.addEventListener("click", () => go(first));
      const li = document.createElement("li");
      li.append(button);
      results.append(li);
    }
    results.hidden = !marks.length;
    if (marks.length) go(0, false);
  }

  function go(i, scroll = true) {
    if (!marks.length) return;
    if (index >= 0) marks[index].classList.remove("now");
    index = (i + marks.length) % marks.length;
    const m = marks[index];
    m.classList.add("now");
    for (let d = m.closest("details"); d; d = d.parentElement.closest("details")) d.open = true;
    counter.textContent = `${index + 1} / ${marks.length} 件`;
    if (scroll) {
      m.scrollIntoView({block: "center", behavior: "smooth"});
      results.hidden = true;
      closeToc();
    }
  }

  let timer = null;
  search.addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(run, 200); });
  search.addEventListener("focus", () => { if (marks.length) results.hidden = false; });
  search.addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      if (timer) { clearTimeout(timer); timer = null; if (!marks.length) run(); }
      go(index + (e.shiftKey ? -1 : 1));
    } else if (e.key === "Escape") {
      results.hidden = true;
    }
  });
  next.addEventListener("click", () => go(index + 1));
  prev.addEventListener("click", () => go(index - 1));
  clear.addEventListener("click", () => { search.value = ""; run(); search.focus(); });
  document.addEventListener("keydown", (e) => {
    if (e.key === "/" && document.activeElement !== search) { e.preventDefault(); search.focus(); }
  });
  document.addEventListener("click", (e) => {
    if (!e.target.closest(".searchbar")) results.hidden = true;
  });
  // URL の ?q= で開いたときは、その語で検索する
  const q = new URLSearchParams(location.search).get("q");
  if (q) { search.value = q; run(); }
})();
