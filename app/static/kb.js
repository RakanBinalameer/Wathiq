"use strict";
(() => {
  const $ = (id) => document.getElementById(id);
  const h = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text !== undefined) e.textContent = text; return e; };
  let timer = null, seq = 0;

  async function search(q) {
    const mine = ++seq;
    if (q.length < 2) { $("kb-results").replaceChildren(); $("kb-status").textContent = ""; return; }
    const res = await fetch("/api/kb/search?q=" + encodeURIComponent(q), { credentials: "same-origin" });
    if (res.status === 401) { location.href = "/login"; return; }
    if (mine !== seq) return; // a newer query is already in flight
    const { results } = await res.json();
    const box = $("kb-results"); box.replaceChildren();
    $("kb-status").textContent = results.length ? `${results.length} نتيجة` : "لا نتائج. جرّب تهجئة أخرى أو الاسم العلمي.";
    for (const r of results) {
      const item = h("div", "kb-item");
      item.append(h("span", "pill " + (r.kind === "drug" ? "drug" : "dx"), r.kind === "drug" ? "دواء" : "تشخيص"));
      const name = h("strong", "", r.label); name.dir = "ltr"; item.append(name);
      if (r.label_ar) item.append(h("span", "", r.label_ar));
      if (r.code) item.append(h("span", "code", `${r.code_system} ${r.code}`));
      if (r.kind === "drug") {
        if (r.brands) item.append(h("span", "hint", "أسماء تجارية: " + r.brands.replaceAll("|", "، ")));
        item.append(h("span", "hint", r.sfda_verified ? "مُتحقَّق من SFDA" : "غير مُتحقَّق من SFDA"));
      } else if (!r.code) item.append(h("span", "hint", "بدون ربط ICD-10"));
      box.append(item);
    }
  }

  document.addEventListener("DOMContentLoaded", () => {
    $("q").addEventListener("input", (e) => { clearTimeout(timer); timer = setTimeout(() => search(e.target.value.trim()), 180); });
  });
})();
