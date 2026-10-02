"use strict";
(() => {
  const $ = (id) => document.getElementById(id);
  const csrf = () => document.querySelector('meta[name="csrf-token"]').content;
  const SECS = ["S", "B", "A", "R"];
  const TITLE = { S: "الموقف", B: "الخلفية", A: "التقييم", R: "التوصيات" };
  const TAG = { S: "S · لا يُصدَّر", B: "B · لا يُصدَّر", A: "A · يُصدَّر كـ Observation", R: "R · يُصدَّر كـ CarePlan" };
  const REF_RE = /^[A-Za-z0-9\-_/]{1,32}$/;

  const S = { items: [], vitals: [], analyzedText: "", serverStt: false, rec: null, recording: false, chunks: [], nid: 0 };

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

  async function api(path, body, isForm) {
    const res = await fetch(path, {
      method: "POST", credentials: "same-origin",
      headers: Object.assign({ "X-CSRFToken": csrf() }, isForm ? {} : { "Content-Type": "application/json" }),
      body: isForm ? body : JSON.stringify(body),
    });
    let data = {};
    try { data = await res.json(); } catch (_) { /* non-JSON error page */ }
    if (res.status === 401) location.href = "/login";
    return { ok: res.ok, status: res.status, data };
  }

  function setStatus(msg, err) { const el = $("status"); el.textContent = msg || ""; el.classList.toggle("err", !!err); }

  function setStep(n) {
    document.querySelectorAll("#flow li").forEach((li, i) => {
      li.classList.toggle("done", i + 1 < n); li.classList.toggle("on", i + 1 === n);
      if (i + 1 === n) li.setAttribute("aria-current", "step"); else li.removeAttribute("aria-current");
    });
  }

  // ---- analysis -----------------------------------------------------------------------------
  async function analyze() {
    const text = $("transcript").value.trim();
    if (!text) { setStatus("اكتب التسليم أو أملِه أولاً.", true); return; }
    setStatus("جارٍ التقسيم…"); $("analyze").disabled = true;
    const r = await api("/api/sbar/analyze", { text });
    $("analyze").disabled = false;
    if (!r.ok) { setStatus(r.data.error || "تعذّر التقسيم، حاول مرة أخرى.", true); return; }
    S.analyzedText = $("transcript").value;
    S.items = r.data.items.map((i) => ({ id: ++S.nid, text: i.text, section: i.section, review: !!i.review, flagged: !!i.review, how: i.how }));
    S.vitals = r.data.vitals.map((v) => ({ ...v, include: v.status === "matched" }));
    $("empty").hidden = true; $("result").hidden = false; $("stale").hidden = true;
    $("ok-review").checked = false; $("ok-names").checked = false;
    render();
    const pending = S.items.filter((i) => i.review).length;
    setStatus(r.data.mode === "headers" ? "قُسّم النص حسب العناوين التي نطقتها." : "قُسّم النص تلقائياً بالقواعد." + (pending ? ` ${pending} جملة تحتاج مراجعتك.` : ""));
  }

  // ---- rendering ----------------------------------------------------------------------------
  function render() { renderWarn(); renderCards(); renderVitals(); updateSave(); }

  function renderWarn() {
    const box = $("vwarn");
    const msgs = [...new Set(S.vitals.flatMap((v) => v.warnings.map((w) => `${v.label}: ${w.msg}`)))];
    box.replaceChildren();
    box.hidden = !msgs.length;
    if (msgs.length) box.append(h("strong", {}, "تنبيه: "), msgs.join(" · "));
  }

  function renderCards() {
    const box = $("cards"); box.replaceChildren();
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
    ta.addEventListener("input", () => { it.text = ta.value; updateSave(); });
    const sel = h("select", { "aria-label": "القسم" }, SECS.map((s) => h("option", { value: s, selected: s === it.section }, `${s} — ${TITLE[s]}`)));
    sel.addEventListener("change", () => { it.section = sel.value; it.review = false; it.flagged = false; it.how = "manual"; render(); });
    const ctl = h("div", { class: "sb-ctl" }, sel);
    const el = h("div", { class: "sb-row" + (it.review ? " needs" : "") }, ctl, ta);
    if (it.flagged) {   // flagged by the splitter: stays on screen after confirming (updated in place, not re-rendered)
      const chip = h("span", { class: "sb-chip" }, "يحتاج مراجعة");
      chip.hidden = !it.review;
      const cb = h("input", { type: "checkbox", checked: !it.review });
      cb.addEventListener("change", () => { it.review = !cb.checked; chip.hidden = !it.review; el.classList.toggle("needs", it.review); updateSave(); });
      ctl.append(chip, h("label", { class: "sb-ack" }, cb, "صحيح"));
    }
    ctl.append(h("button", { class: "btn small", type: "button", onclick: () => { S.items = S.items.filter((x) => x !== it); render(); } }, "حذف"));
    return el;
  }

  function renderVitals() {
    const box = $("vitals"); box.replaceChildren(); box.hidden = !S.vitals.length;
    if (!S.vitals.length) return;
    box.append(h("h3", {}, "العلامات الحيوية المستخرجة (تُصدَّر كـ Observation)"));
    for (const v of S.vitals) {
      const cb = h("input", { type: "checkbox", checked: v.include, onchange: (e) => { v.include = e.target.checked; updateSave(); } });
      box.append(h("label", {}, cb, h("span", { dir: "auto" }, `${v.label}: ${v.value} ${v.unit}`),
        h("span", { class: "hint" }, `(من ${v.section === "S" ? "الموقف" : "التقييم"})`),
        v.status === "check" ? h("span", { class: "sb-chip" }, "قيمة غير معقولة — فعّلها فقط إن كانت صحيحة") : null));
    }
  }

  function sections() {
    const out = {};
    for (const s of SECS) out[s] = S.items.filter((i) => i.section === s).map((i) => i.text.trim()).filter(Boolean);
    return out;
  }

  function updateSave() {
    const secs = sections();
    const total = SECS.reduce((n, s) => n + secs[s].length, 0);
    const pending = S.items.filter((i) => i.review && i.text.trim()).length;
    const stale = S.analyzedText !== $("transcript").value;
    $("stale").hidden = stale ? false : true;
    const empties = SECS.filter((s) => !secs[s].length).map((s) => TITLE[s]);
    let why = "";
    if (stale) why = "أعد التقسيم أولاً.";
    else if (!REF_RE.test($("patient-ref").value.trim())) why = "أدخل رقم الملف (أرقام وأحرف فقط).";
    else if (!total) why = "التقرير فارغ.";
    else if (pending) why = `${pending} جملة تحتاج مراجعتك.`;
    else if (!$("ok-review").checked || !$("ok-names").checked) why = "أكّد المراجعة وخلوّ النص من اسم المريض.";
    $("save").disabled = !!why;
    $("save-hint").textContent = why || (empties.length ? `تنبيه: أقسام فارغة (${empties.join("، ")}). يمكنك الحفظ مع ذلك.` : "جاهز للحفظ.");
    setStep(!S.items.length ? 1 : why ? 2 : 3);
  }

  async function save() {
    $("save").disabled = true;
    const r = await api("/api/sbar", {
      patient_ref: $("patient-ref").value.trim(), transcript: $("transcript").value, sections: sections(),
      vitals: S.vitals.filter((v) => v.include).map((v) => ({ type: "vital", text: v.text, kind: v.kind, label: v.label, value: v.value, unit: v.unit })),
      confirmed: $("ok-review").checked, no_names: $("ok-names").checked,
    });
    if (r.status === 201) { setStatus("تم حفظ التقرير."); location.href = r.data.url; return; }
    setStatus(r.data.error || "تعذّر الحفظ.", true); updateSave();
  }

  // ---- dictation (same behaviour as the note page) ------------------------------------------
  function micState(on) {
    S.recording = on; $("mic").setAttribute("aria-pressed", on ? "true" : "false");
    $("mic-label").textContent = on ? "إيقاف الإملاء" : "ابدأ الإملاء";
  }
  function startBrowserSpeech() {
    const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (!SR) { setStatus("هذا المتصفح لا يدعم الإملاء الصوتي. اكتب التسليم أو استخدم Chrome.", true); return; }
    const rec = new SR();
    rec.lang = $("lang").value; rec.continuous = true; rec.interimResults = true;
    let base = $("transcript").value;
    if (base && !/\s$/.test(base)) base += " ";
    rec.onresult = (ev) => {
      let fin = "", interim = "";
      for (let i = ev.resultIndex; i < ev.results.length; i++) (ev.results[i].isFinal ? (fin += ev.results[i][0].transcript) : (interim += ev.results[i][0].transcript));
      if (fin) base += fin + " ";
      $("transcript").value = base + interim; $("transcript").dispatchEvent(new Event("input"));
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
      const fd = new FormData(); fd.append("audio", new Blob(S.chunks, { type: mr.mimeType || "audio/webm" }), "handover.webm");
      const r = await api("/api/stt", fd, true);
      if (!r.ok) { setStatus(r.data.error || "تعذّر تفريغ الصوت.", true); return; }
      const ta = $("transcript"); ta.value = (ta.value ? ta.value.replace(/\s*$/, " ") : "") + (r.data.text || ""); setStatus(""); analyze();
    };
    S.rec = mr; mr.start(); micState(true); setStatus("أسجّل… اضغط الزر مرة أخرى عند الانتهاء.");
  }
  function toggleMic() {
    if (S.recording) { S.rec && S.rec.stop(); micState(false); return; }
    S.serverStt ? startServerRecording() : startBrowserSpeech();
  }
  async function initStt() {
    try { S.serverStt = (await (await fetch("/api/stt/status", { credentials: "same-origin" })).json()).server === true; } catch (_) { S.serverStt = false; }
    const note = $("stt-mode"); note.hidden = false;
    note.textContent = S.serverStt ? "الإملاء عبر نموذج Whisper على خادم المؤسسة." :
      "وضع العرض: الإملاء يمر عبر خدمة التعرف الصوتي في المتصفح. لا تستخدمه مع بيانات مرضى حقيقية.";
  }

  function clearAll() {
    $("transcript").value = ""; $("patient-ref").value = ""; S.items = []; S.vitals = []; S.analyzedText = "";
    $("result").hidden = true; $("empty").hidden = false; setStatus(""); setStep(1);
  }

  document.addEventListener("DOMContentLoaded", () => {
    $("mic").addEventListener("click", toggleMic);
    $("analyze").addEventListener("click", analyze);
    $("clear").addEventListener("click", clearAll);
    $("save").addEventListener("click", save);
    $("ok-review").addEventListener("change", updateSave);
    $("ok-names").addEventListener("change", updateSave);
    $("patient-ref").addEventListener("input", () => { if (!$("result").hidden) updateSave(); });
    $("transcript").addEventListener("input", () => { if (!$("result").hidden) updateSave(); });
    document.querySelectorAll("[data-sample]").forEach((b) => b.addEventListener("click", () => { $("transcript").value = b.dataset.sample; analyze(); }));
    initStt();
  });
})();
