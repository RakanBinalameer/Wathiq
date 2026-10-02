"use strict";
// Shared SBAR review panel: four section cards + extracted vital signs.
// Used by the dictation page (as a tab) and by the standalone /sbar page. It only renders and edits;
// saving and confirmation checkboxes belong to the page that hosts it.
window.SbarPanel = (() => {
  const SECS = ["S", "B", "A", "R"];
  const TITLE = { S: "الموقف", B: "الخلفية", A: "التقييم", R: "التوصيات" };
  const TAG = { S: "S · لا يُصدَّر", B: "B · لا يُصدَّر", A: "A · يُصدَّر كـ Observation", R: "R · يُصدَّر كـ CarePlan" };

  // tiny DOM helper: all text goes through textContent / text nodes (never innerHTML)
  function h(tag, props, ...kids) {
    const el = document.createElement(tag);
    for (const [k, v] of Object.entries(props || {})) {
      if (v === null || v === undefined || v === false) continue;
      if (k === "class") el.className = v;
      else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
      else if (k === "checked" || k === "selected" || k === "value") el[k] = v;
      else el.setAttribute(k, v === true ? "" : v);
    }
    for (const kid of kids.flat()) if (kid !== null && kid !== undefined && kid !== false) el.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
    return el;
  }

  // els: { cards, vitals, vwarn } DOM elements. onChange(): called after any edit (the host re-checks its save button).
  function create(els, onChange) {
    const S = { items: [], vitals: [], nid: 0 };
    const changed = () => { if (onChange) onChange(); };

    function renderWarn() {
      const box = els.vwarn;
      const msgs = [...new Set(S.vitals.flatMap((v) => v.warnings.map((w) => `${v.label}: ${w.msg}`)))];
      box.replaceChildren();
      box.hidden = !msgs.length;
      if (msgs.length) box.append(h("strong", {}, "تنبيه: "), msgs.join(" · "));
    }

    function renderCards() {
      const box = els.cards; box.replaceChildren();
      for (const sec of SECS) {
        const rows = S.items.filter((i) => i.section === sec);
        const card = h("div", { class: `sb-card sb-${sec}`, "data-sec": sec },
          h("h3", {}, TITLE[sec], h("span", { class: "sb-tag" }, TAG[sec])));
        if (!rows.length) card.append(h("p", { class: "hint" }, "لا شيء في هذا القسم."));
        for (const it of rows) card.append(row(it));
        card.append(h("button", { class: "btn small", type: "button", onclick: () => { S.items.push({ id: ++S.nid, text: "", section: sec, review: false, flagged: false, how: "manual" }); render(); } }, "+ إضافة جملة"));
        box.append(card);
      }
    }

    function row(it) {
      const ta = h("textarea", { rows: "2", dir: "auto", maxlength: "600", "aria-label": "نص الجملة" });
      ta.value = it.text;
      ta.addEventListener("input", () => { it.text = ta.value; changed(); });
      const sel = h("select", { "aria-label": "القسم" }, SECS.map((s) => h("option", { value: s, selected: s === it.section }, `${s} — ${TITLE[s]}`)));
      sel.addEventListener("change", () => { it.section = sel.value; it.review = false; it.flagged = false; it.how = "manual"; render(); });
      const ctl = h("div", { class: "sb-ctl" }, sel);
      const el = h("div", { class: "sb-row" + (it.review ? " needs" : "") }, ctl, ta);
      if (it.flagged) {   // flagged by the splitter: stays on screen after confirming (updated in place, not re-rendered)
        const chip = h("span", { class: "sb-chip" }, "يحتاج مراجعة");
        chip.hidden = !it.review;
        const cb = h("input", { type: "checkbox", checked: !it.review });
        cb.addEventListener("change", () => { it.review = !cb.checked; chip.hidden = !it.review; el.classList.toggle("needs", it.review); changed(); });
        ctl.append(chip, h("label", { class: "sb-ack" }, cb, "صحيح"));
      }
      ctl.append(h("button", { class: "btn small", type: "button", onclick: () => { S.items = S.items.filter((x) => x !== it); render(); } }, "حذف"));
      return el;
    }

    function renderVitals() {
      const box = els.vitals; box.replaceChildren(); box.hidden = !S.vitals.length;
      if (!S.vitals.length) return;
      box.append(h("h3", {}, "العلامات الحيوية المستخرجة (تُصدَّر كـ Observation)"));
      for (const v of S.vitals) {
        const cb = h("input", { type: "checkbox", checked: v.include, onchange: (e) => { v.include = e.target.checked; changed(); } });
        box.append(h("label", {}, cb, h("span", { dir: "auto" }, `${v.label}: ${v.value} ${v.unit}`),
          h("span", { class: "hint" }, `(من ${v.section === "S" ? "الموقف" : "التقييم"})`),
          v.status === "check" ? h("span", { class: "sb-chip" }, "قيمة غير معقولة — فعّلها فقط إن كانت صحيحة") : null));
      }
    }

    function render() { renderWarn(); renderCards(); renderVitals(); changed(); }

    return {
      // data: response of /api/sbar/analyze
      load(data) {
        S.items = data.items.map((i) => ({ id: ++S.nid, text: i.text, section: i.section, review: !!i.review, flagged: !!i.review, how: i.how }));
        S.vitals = data.vitals.map((v) => ({ ...v, include: v.status === "matched" }));
        render();
      },
      clear() { S.items = []; S.vitals = []; els.cards.replaceChildren(); els.vitals.replaceChildren(); els.vitals.hidden = true; els.vwarn.hidden = true; },
      sections() {
        const out = {};
        for (const s of SECS) out[s] = S.items.filter((i) => i.section === s).map((i) => i.text.trim()).filter(Boolean);
        return out;
      },
      vitalsPayload() {
        return S.vitals.filter((v) => v.include).map((v) => ({ type: "vital", text: v.text, kind: v.kind, label: v.label, value: v.value, unit: v.unit }));
      },
      pending() { return S.items.filter((i) => i.review && i.text.trim()).length; },
      itemCount() { return S.items.length; },
      total() { const s = this.sections(); return SECS.reduce((n, k) => n + s[k].length, 0); },
      emptySections() { const s = this.sections(); return SECS.filter((k) => !s[k].length).map((k) => TITLE[k]); },
    };
  }

  return { create, SECS, TITLE };
})();
