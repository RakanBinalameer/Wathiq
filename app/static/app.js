"use strict";
(() => {
  const $ = (id) => document.getElementById(id);
  const csrf = () => document.querySelector('meta[name="csrf-token"]').content;
  const TYPE_AR = { drug: "دواء", dx: "تشخيص", vital: "علامة حيوية" };
  const GROUP_AR = { drug: "الأدوية", dx: "التشخيصات", vital: "العلامات الحيوية" };
  const ROUTES = ["IV", "IM", "PO", "SC", "SL", "INH", "PR", "TOP"];
  const FREQS = ["QD", "BID", "TID", "QID", "QHS", "STAT", "q4h", "q6h", "q8h", "q12h"];
  const UNITS = ["mg", "mcg", "g", "ml", "unit", "meq", "tab", "cap", "amp"];

  const S = { items: [], analyzedText: "", serverStt: false, rec: null, recording: false, chunks: [], needAck: false };

  // ---- tiny DOM helper (never innerHTML: all text goes through textContent) -------------------
  function h(tag, props, ...kids) {
    const el = document.createElement(tag);
    for (const [k, v] of Object.entries(props || {})) {
      if (v === null || v === undefined || v === false) continue;
      if (k === "class") el.className = v;
      else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
      else if (k === "checked" || k === "selected") el[k] = !!v;
      else el.setAttribute(k, v === true ? "" : v);
    }
    for (const kid of kids.flat()) if (kid !== null && kid !== undefined && kid !== false) el.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
    return el;
  }

  async function api(path, body, isForm) {
    const res = await fetch(path, {
      method: "POST", credentials: "same-origin",
      headers: Object.assign({ "X-CSRFToken": csrf() }, isForm ? {} : { "Content-Type": "application/json" }),
      body: isForm ? body : JSON.stringify(body),
    });
    let data = {};
    try { data = await res.json(); } catch (_) { /* non-JSON error page */ }
    if (res.status === 401) { location.href = "/login"; }
    return { ok: res.ok, status: res.status, data };
  }

  function setStatus(msg, err) {
    const el = $("status");
    el.textContent = msg || "";
    el.classList.toggle("err", !!err);
  }

  // ---- analysis -----------------------------------------------------------------------------
  async function analyze() {
    const text = $("transcript").value.trim();
    if (!text) { setStatus("اكتب الملاحظة أو أملِها أولاً.", true); return; }
    setStatus("جارٍ التحليل…");
    $("analyze").disabled = true;
    const r = await api("/api/analyze", { text, allergies: $("allergies").value });
    $("analyze").disabled = false;
    if (!r.ok) { setStatus(r.data.error || "تعذّر التحليل، حاول مرة أخرى.", true); return; }
    const res = r.data;
    S.analyzedText = $("transcript").value;
    S.needAck = false;
    S.items = res.entities.map((e) => ({
      id: e.id, type: e.type, text: e.text, start: e.start, end: e.end, status: e.status, score: e.score,
      match: e.match, alternatives: e.alternatives || [], warnings: e.warnings || [],
      chosenId: e.match ? e.match.id : null,
      dose: e.attrs && e.attrs.dose ? { value: e.attrs.dose.value, unit: e.attrs.dose.unit } : null,
      route: e.attrs ? e.attrs.route : null, freq: e.attrs ? e.attrs.freq : null, prn: e.attrs ? e.attrs.prn : false,
      kind: e.kind, label: e.label, value: e.value, unit: e.unit,
      confirmed: e.status === "matched", removed: false,
    }));
    $("empty").hidden = true; $("result").hidden = false; $("stale").hidden = true;
    render(res.text);
    const s = res.summary;
    setStatus(s.drugs + s.dx + s.vitals === 0 ? "لم يُعثر على مصطلحات طبية معروفة في هذا النص." :
      `تم التعرف على ${s.drugs} أدوية، ${s.dx} تشخيصات، ${s.vitals} علامات حيوية.` + (s.review ? ` ${s.review} تحتاج مراجعتك.` : ""));
  }

  // ---- rendering ----------------------------------------------------------------------------
  function render(text) {
    renderAnnotated(text || S.analyzedText);
    renderEntities();
    updateSave();
  }

  // flow stepper: 1 = dictate/write, 2 = review and confirm, 3 = ready to save
  function setStep(n) {
    document.querySelectorAll("#flow li").forEach((li, i) => {
      li.classList.toggle("done", i + 1 < n);
      li.classList.toggle("on", i + 1 === n);
      if (i + 1 === n) li.setAttribute("aria-current", "step"); else li.removeAttribute("aria-current");
    });
  }

  function renderSummary() {
    const box = $("summary");
    box.replaceChildren();
    const kept = S.items.filter((i) => !i.removed);
    const count = (t) => kept.filter((i) => i.type === t).length;
    const pending = kept.filter((i) => !i.confirmed).length;
    const dangers = kept.filter((i) => i.warnings.some((w) => w.level === "danger")).length;
    const card = (num, label, cls) => h("div", { class: "sum-card" + (cls ? " " + cls : "") }, h("strong", {}, num), h("span", {}, label));
    box.append(
      card(count("drug"), "أدوية"), card(count("dx"), "تشخيصات"), card(count("vital"), "علامات حيوية"),
      card(pending, "بانتظار تأكيدك", pending ? "review" : "ok"),
      card(dangers, "تنبيهات خطرة", dangers ? "danger" : ""),
      h("div", { class: "sum-progress" },
        h("span", {}, `تم تأكيد ${kept.length - pending} من ${kept.length}`),
        h("progress", { max: String(kept.length), value: String(kept.length - pending), "aria-label": "تقدّم تأكيد العناصر" })));
  }

  // row state: "ok" marks a confirmed row; has-warn / has-danger follow the warnings.
  // CSS lets has-danger win, so a confirmed row with a danger warning stays red, never green.
  function stateClass(it) {
    if (it.removed) return " off";
    const ws = currentWarnings(it);
    return (it.confirmed ? " ok" : "") +
      (ws.some((w) => w.level === "warn") ? " has-warn" : "") +
      (ws.some((w) => w.level === "danger") ? " has-danger" : "");
  }

  function renderAnnotated(text) {
    const box = $("annotated");
    box.replaceChildren();
    let pos = 0;
    for (const it of [...S.items].sort((a, b) => a.start - b.start)) {
      if (it.start < pos) continue;
      if (it.start > pos) box.append(text.slice(pos, it.start));
      const needs = !it.removed && !it.confirmed;
      const danger = !it.removed && currentWarnings(it).some((w) => w.level === "danger");
      const cls = `${it.type}${needs ? " review" : ""}${danger ? " danger" : ""}`;
      const tip = `${TYPE_AR[it.type]}${needs ? " — يحتاج مراجعة" : ""}`;
      box.append(h("mark", { class: cls, "data-id": it.id, title: tip, tabindex: "0", onclick: () => focusRow(it.id), onkeydown: (ev) => { if (ev.key === "Enter") focusRow(it.id); } }, text.slice(it.start, it.end)));
      pos = it.end;
    }
    if (pos < text.length) box.append(text.slice(pos));
  }

  function currentWarnings(it) { return it.removed ? [] : it.warnings; }

  function focusRow(id) {
    const row = document.querySelector(`.ent[data-id="${id}"]`);
    if (!row) return;
    row.scrollIntoView({ block: "center", behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth" });
    row.classList.add("hl"); setTimeout(() => row.classList.remove("hl"), 1400);
  }

  function select(options, current, onchange, label, blank) {
    const opts = options.includes(current) || !current ? options : [current, ...options];
    const sel = h("select", { "aria-label": label, onchange: (e) => onchange(e.target.value || null) },
      h("option", { value: "" }, blank || "—"),
      opts.map((o) => h("option", { value: o, selected: o === current }, o)));
    return sel;
  }

  function renderEntities() {
    const root = $("entities");
    root.replaceChildren();
    renderSummary();
    for (const type of ["drug", "dx", "vital"]) {
      const group = S.items.filter((i) => i.type === type);
      if (!group.length) continue;
      root.append(h("h3", { class: "group-title" }, `${GROUP_AR[type]} (${group.length})`));
      if (type === "vital") root.append(h("div", { class: "vital-grid" }, group.map(vitalCard)));
      else for (const it of group) root.append(row(it));
    }
    if (!S.items.length) root.append(h("p", { class: "hint" }, "لا توجد عناصر للمراجعة. يمكنك تعديل النص وإعادة التحليل."));
    renderAlerts();
  }

  function row(it) {
    const needs = !it.confirmed && !it.removed;
    const el = h("div", { class: `ent ${it.type}${needs ? " needs" : ""}${stateClass(it)}`, "data-id": it.id });
    const head = h("div", { class: "ent-head" }, h("span", { class: `pill ${it.type}` }, TYPE_AR[it.type]));
    head.append(h("span", { class: "found", dir: "auto" }, `«${it.text}»`));
    const m = currentMatch(it);
    head.append("←", h("span", { class: "name", dir: "ltr" }, m.label));
    if (m.label_ar) head.append(h("span", { class: "ar" }, m.label_ar));
    if (m.code) head.append(h("span", { class: "code" }, `${m.code_system} ${m.code}`));
    if (it.score < 100) head.append(h("span", { class: "hint" }, `تطابق ${it.score}%`));
    el.append(head);

    if (it.alternatives.length > 1) {
      el.append(h("div", { class: "alt" }, h("label", { for: `alt-${it.id}` }, "اختر الاسم الصحيح"),
        h("select", { id: `alt-${it.id}`, onchange: (e) => { it.chosenId = Number(e.target.value); it.confirmed = false; refresh(); } },
          it.alternatives.map((a) => h("option", { value: a.id, selected: a.id === it.chosenId }, `${a.label}${a.label_ar ? " — " + a.label_ar : ""} (${a.score}%)`)))));
    }

    if (it.type === "drug") {
      const dose = it.dose || { value: "", unit: "mg" };
      el.append(h("div", { class: "attrs" },
        h("div", {}, h("label", { for: `dv-${it.id}` }, "الجرعة"),
          h("input", { id: `dv-${it.id}`, type: "number", min: "0", step: "any", inputmode: "decimal", value: dose.value, dir: "ltr",
            onchange: (e) => { const v = parseFloat(e.target.value); it.dose = v > 0 ? { value: v, unit: (it.dose || dose).unit } : null; it.confirmed = false; refresh(); } })),
        h("div", {}, h("label", {}, "الوحدة"), select(UNITS, dose.unit, (v) => { if (it.dose) { it.dose.unit = v || "mg"; it.confirmed = false; refresh(); } }, "وحدة الجرعة", "mg")),
        h("div", {}, h("label", {}, "الطريقة"), select(ROUTES, it.route, (v) => { it.route = v; }, "طريقة الإعطاء")),
        h("div", {}, h("label", {}, "التكرار"), select(FREQS, it.freq, (v) => { it.freq = v; }, "التكرار"))));
    }
    appendFlags(el, it);
    el.append(entFoot(it));
    return el;
  }

  // a vital sign is a compact card inside .vital-grid: label, big value field, unit
  function vitalCard(it) {
    const needs = !it.confirmed && !it.removed;
    const el = h("div", { class: `ent vital${needs ? " needs" : ""}${stateClass(it)}`, "data-id": it.id });
    el.append(
      h("span", { class: "vname" }, it.label),
      h("span", { class: "vfound", dir: "auto" }, `«${it.text}»`),
      h("div", { class: "vval" },
        h("input", { id: `vv-${it.id}`, "aria-label": `${it.label} (${it.unit})`, value: it.value, dir: "ltr", inputmode: "decimal", maxlength: "7",
          onchange: (e) => { it.value = e.target.value.trim(); it.confirmed = false; refresh(); } }),
        h("span", { class: "unit" }, it.unit)));
    appendFlags(el, it);
    el.append(entFoot(it));
    return el;
  }

  function appendFlags(el, it) {
    for (const w of currentWarnings(it)) el.append(h("span", { class: `flag ${w.level}`, role: w.level === "danger" ? "alert" : null }, w.msg));
  }

  function entFoot(it) {
    const foot = h("div", { class: "ent-foot" });
    if (!it.removed) {
      foot.append(h("label", {}, h("input", { type: "checkbox", checked: it.confirmed, onchange: (e) => { it.confirmed = e.target.checked; refresh(true); } }),
        it.status === "matched" && it.score >= 100 ? "صحيح" : "أؤكد أن هذا صحيح"));
    }
    foot.append(h("button", { class: "btn small", type: "button", onclick: () => { it.removed = !it.removed; refresh(); } }, it.removed ? "استعادة" : "إزالة"));
    return foot;
  }

  function currentMatch(it) {
    if (it.alternatives.length > 1) return it.alternatives.find((a) => a.id === it.chosenId) || it.match;
    return it.match;
  }

  function refresh(keepFocus) {
    const active = keepFocus ? document.activeElement : null;
    const id = active && active.closest ? (active.closest(".ent") || {}).dataset : null;
    render();
    if (id && id.id) { const b = document.querySelector(`.ent[data-id="${id.id}"] input[type=checkbox]`); if (b) b.focus(); }
  }

  function renderAlerts() {
    const box = $("alerts");
    const dangers = S.items.flatMap((i) => currentWarnings(i).filter((w) => w.level === "danger").map((w) => w.msg));
    box.replaceChildren();
    const show = dangers.length > 0 || S.needAck;
    box.hidden = !show; $("ack-wrap").hidden = !show;
    if (show) {
      box.append(h("strong", {}, "تنبيهات خطرة تحتاج قرارك"));
      const ul = h("ul"); [...new Set(dangers)].forEach((m) => ul.append(h("li", {}, m))); box.append(ul);
      if (S.needAck && !dangers.length) ul.append(h("li", {}, "أعاد الخادم فحص القيم بعد تعديلك ووجد تنبيهات، راجعها ثم أكّد."));
    } else { $("ack").checked = false; }
  }

  function updateSave() {
    const kept = S.items.filter((i) => !i.removed);
    const pending = kept.filter((i) => !i.confirmed).length;
    const stale = $("transcript").value !== S.analyzedText;
    const refOk = /^[A-Za-z0-9\-_/]{1,32}$/.test($("patient-ref").value.trim());
    const dangers = document.querySelectorAll("#alerts li").length > 0 && !$("alerts").hidden;
    const ackOk = !dangers || $("ack").checked;
    $("stale").hidden = !stale;
    $("save").disabled = stale || pending > 0 || !refOk || !ackOk;
    $("save-hint").textContent = stale ? "أعد التحليل بعد تعديل النص." :
      pending > 0 ? `أكّد ${pending} عنصراً قبل الحفظ.` :
      !refOk ? "أدخل رقم الملف (أحرف وأرقام فقط) لتفعيل الحفظ." :
      !ackOk ? "أكّد مراجعة التنبيهات الخطرة." : "جاهز للحفظ.";
    setStep($("save").disabled ? 2 : 3);
  }

  async function save() {
    $("save").disabled = true;
    const entities = S.items.filter((i) => !i.removed).map((i) => {
      if (i.type === "drug") return { type: "drug", text: i.text, match_id: i.chosenId, dose: i.dose, route: i.route, freq: i.freq, prn: i.prn };
      if (i.type === "dx") return { type: "dx", text: i.text, match_id: i.chosenId };
      return { type: "vital", text: i.text, kind: i.kind, label: i.label, value: i.value, unit: i.unit };
    });
    const r = await api("/api/notes", {
      patient_ref: $("patient-ref").value.trim(), allergies: $("allergies").value, transcript: $("transcript").value,
      entities, acknowledged: $("ack").checked,
    });
    if (r.status === 201) { setStatus("تم حفظ الملاحظة."); location.href = r.data.url; return; }
    if (r.status === 409) {
      S.needAck = true;
      S.items.forEach((i) => { i.warnings = i.warnings.filter((w) => w.level !== "danger"); });
      const first = S.items.find((i) => !i.removed && i.type === "drug");
      (r.data.warnings || []).forEach((m) => first && first.warnings.push({ level: "danger", msg: m }));
      renderEntities(); updateSave();
      setStatus(r.data.error, true);
      return;
    }
    setStatus(r.data.error || "تعذّر الحفظ.", true);
    updateSave();
  }

  // ---- dictation ----------------------------------------------------------------------------
  function micState(on) {
    S.recording = on;
    $("mic").setAttribute("aria-pressed", on ? "true" : "false");
    $("mic-label").textContent = on ? "إيقاف الإملاء" : "ابدأ الإملاء";
  }

  function appendText(t) {
    const ta = $("transcript");
    ta.value = (ta.value ? ta.value.replace(/\s*$/, " ") : "") + t;
    ta.dispatchEvent(new Event("input"));
  }

  function startBrowserSpeech() {
    const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (!SR) { setStatus("هذا المتصفح لا يدعم الإملاء الصوتي. اكتب الملاحظة أو استخدم Chrome.", true); return; }
    const rec = new SR();
    rec.lang = $("lang").value; rec.continuous = true; rec.interimResults = true;
    let base = $("transcript").value;
    if (base && !/\s$/.test(base)) base += " ";
    rec.onresult = (ev) => {
      let fin = "", interim = "";
      for (let i = ev.resultIndex; i < ev.results.length; i++) (ev.results[i].isFinal ? (fin += ev.results[i][0].transcript) : (interim += ev.results[i][0].transcript));
      if (fin) base += fin + " ";
      $("transcript").value = base + interim;
      $("transcript").dispatchEvent(new Event("input"));
    };
    rec.onerror = (ev) => { setStatus(ev.error === "not-allowed" ? "لم يُسمح باستخدام الميكروفون. فعّله من إعدادات المتصفح." : "تعذّر الإملاء الصوتي: " + ev.error, true); micState(false); };
    rec.onend = () => { if (S.recording) { micState(false); if ($("transcript").value.trim()) analyze(); } };
    S.rec = rec; rec.start(); micState(true); setStatus("أتكلّم… اضغط الزر مرة أخرى عند الانتهاء.");
  }

  async function startServerRecording() {
    let stream;
    try { stream = await navigator.mediaDevices.getUserMedia({ audio: true }); }
    catch (_) { setStatus("لم يُسمح باستخدام الميكروفون. فعّله من إعدادات المتصفح.", true); return; }
    S.chunks = [];
    const mr = new MediaRecorder(stream);
    mr.ondataavailable = (e) => e.data.size && S.chunks.push(e.data);
    mr.onstop = async () => {
      stream.getTracks().forEach((t) => t.stop());
      setStatus("جارٍ تفريغ الصوت على الخادم…");
      const fd = new FormData(); fd.append("audio", new Blob(S.chunks, { type: mr.mimeType || "audio/webm" }), "note.webm");
      const r = await api("/api/stt", fd, true);
      if (!r.ok) { setStatus(r.data.error || "تعذّر تفريغ الصوت.", true); return; }
      appendText(r.data.text || ""); setStatus(""); analyze();
    };
    S.rec = mr; mr.start(); micState(true); setStatus("أسجّل… اضغط الزر مرة أخرى عند الانتهاء.");
  }

  function toggleMic() {
    if (S.recording) { S.rec && S.rec.stop(); micState(false); return; }
    S.serverStt ? startServerRecording() : startBrowserSpeech();
  }

  async function initStt() {
    try {
      const res = await fetch("/api/stt/status", { credentials: "same-origin" });
      S.serverStt = (await res.json()).server === true;
    } catch (_) { S.serverStt = false; }
    const note = $("stt-mode");
    note.hidden = false;
    note.textContent = S.serverStt ? "الإملاء عبر نموذج Whisper على خادم المؤسسة." :
      "وضع العرض: الإملاء يمر عبر خدمة التعرف الصوتي في المتصفح. لا تستخدمه مع بيانات مرضى حقيقية.";
  }

  // ---- wiring -------------------------------------------------------------------------------
  function clearAll() {
    $("transcript").value = ""; $("allergies").value = ""; $("patient-ref").value = "";
    S.items = []; S.analyzedText = ""; S.needAck = false;
    $("result").hidden = true; $("empty").hidden = false; setStatus("");
    setStep(1);
  }

  document.addEventListener("DOMContentLoaded", () => {
    $("mic").addEventListener("click", toggleMic);
    $("analyze").addEventListener("click", analyze);
    $("clear").addEventListener("click", clearAll);
    $("save").addEventListener("click", save);
    $("ack").addEventListener("change", updateSave);
    $("patient-ref").addEventListener("input", () => { if (!$("result").hidden) updateSave(); });
    $("transcript").addEventListener("input", () => { if (!$("result").hidden) updateSave(); });
    document.querySelectorAll("[data-sample]").forEach((b) => b.addEventListener("click", () => {
      $("transcript").value = b.dataset.sample; $("allergies").value = b.dataset.allergies || ""; analyze();
    }));
    $("entities").addEventListener("mouseover", (e) => {
      const r = e.target.closest(".ent"); document.querySelectorAll("mark.hl").forEach((m) => m.classList.remove("hl"));
      if (r) { const m = document.querySelector(`mark[data-id="${r.dataset.id}"]`); if (m) m.classList.add("hl"); }
    });
    initStt();
  });
})();
