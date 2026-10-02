"use strict";
(() => {
  const $ = (id) => document.getElementById(id);
  const csrf = () => document.querySelector('meta[name="csrf-token"]').content;
  const REF_RE = /^[A-Za-z0-9\-_/]{1,32}$/;

  const S = { analyzedText: "", serverStt: false, rec: null, recording: false, chunks: [] };
  const panel = SbarPanel.create({ cards: $("cards"), vitals: $("vitals"), vwarn: $("vwarn") }, () => updateSave());

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
    $("empty").hidden = true; $("result").hidden = false; $("stale").hidden = true;
    $("ok-review").checked = false; $("ok-names").checked = false;
    panel.load(r.data);
    const pending = panel.pending();
    setStatus(r.data.mode === "headers" ? "قُسّم النص حسب العناوين التي نطقتها." : "قُسّم النص تلقائياً بالقواعد." + (pending ? ` ${pending} جملة تحتاج مراجعتك.` : ""));
  }

  function updateSave() {
    const total = panel.total();
    const pending = panel.pending();
    const stale = S.analyzedText !== $("transcript").value;
    $("stale").hidden = stale ? false : true;
    const empties = panel.emptySections();
    let why = "";
    if (stale) why = "أعد التقسيم أولاً.";
    else if (!REF_RE.test($("patient-ref").value.trim())) why = "أدخل رقم الملف (أرقام وأحرف فقط).";
    else if (!total) why = "التقرير فارغ.";
    else if (pending) why = `${pending} جملة تحتاج مراجعتك.`;
    else if (!$("ok-review").checked || !$("ok-names").checked) why = "أكّد المراجعة وخلوّ النص من اسم المريض.";
    $("save").disabled = !!why;
    $("save-hint").textContent = why || (empties.length ? `تنبيه: أقسام فارغة (${empties.join("، ")}). يمكنك الحفظ مع ذلك.` : "جاهز للحفظ.");
    setStep(!panel.itemCount() ? 1 : why ? 2 : 3);
  }

  async function save() {
    $("save").disabled = true;
    const r = await api("/api/sbar", {
      patient_ref: $("patient-ref").value.trim(), transcript: $("transcript").value, sections: panel.sections(),
      vitals: panel.vitalsPayload(), confirmed: $("ok-review").checked, no_names: $("ok-names").checked,
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
    $("transcript").value = ""; $("patient-ref").value = ""; panel.clear(); S.analyzedText = "";
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
