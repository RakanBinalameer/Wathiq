import json
import re
from urllib.parse import urlparse

from flask import Blueprint, Response, abort, current_app, jsonify, redirect, render_template, request, session, url_for
from flask_login import UserMixin, current_user, login_required, login_user, logout_user
from werkzeug.security import check_password_hash, generate_password_hash

from . import fhir, limiter, login_manager, stt
from .db import audit, get_db
from .extract import _TO_MG, analyze, allergy_drug_ids, drug_warnings

bp = Blueprint("main", __name__)

ROUTES = {"IV", "IM", "SC", "SL", "INH", "PR", "TOP", "PO"}
FREQ_RE = re.compile(r"^(q\d{1,2}h|BID|TID|QID|QD|QHS|STAT)$")
UNITS = {"mg", "mcg", "g", "ml", "unit", "meq", "tab", "cap", "amp"}
VITAL_KINDS = {"bp", "hr", "rr", "temp", "spo2", "glucose"}
VITAL_VALUE_RE = re.compile(r"^\d{1,3}(\.\d)?(/\d{1,3})?$")
PATIENT_REF_RE = re.compile(r"^[A-Za-z0-9\-_/]{1,32}$")
_DUMMY_HASH = generate_password_hash("not-a-real-password")


class User(UserMixin):
    def __init__(self, row):
        self.id, self.username, self.display_name, self.role = row["id"], row["username"], row["display_name"], row["role"]


@login_manager.user_loader
def load_user(uid):
    row = get_db().execute("SELECT id, username, display_name, role FROM users WHERE id=?", (uid,)).fetchone()
    return User(row) if row else None


@login_manager.unauthorized_handler
def unauthorized():
    if request.path.startswith("/api/"):
        return jsonify(error="يلزم تسجيل الدخول"), 401
    return redirect(url_for("main.login", next=request.path))


def kb():
    return current_app.extensions["kb"]


def _safe_next(target):
    if not target:
        return None
    u = urlparse(target)
    return target if not u.scheme and not u.netloc and target.startswith("/") and not target.startswith("//") else None


# ---------------------------------------------------------------- pages
@bp.get("/healthz")
@limiter.exempt
def healthz():
    return jsonify(ok=True)


@bp.route("/login", methods=["GET", "POST"])
@limiter.limit("10 per minute", methods=["POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("main.index"))
    error = None
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()[:64]
        password = request.form.get("password") or ""
        row = get_db().execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        ok = check_password_hash(row["password_hash"] if row else _DUMMY_HASH, password)  # constant-ish time
        if row and ok:
            session.clear()
            login_user(User(row))
            session.permanent = True
            audit("login", "", row["id"])
            return redirect(_safe_next(request.args.get("next")) or url_for("main.index"))
        audit("login_failed", f"user={username[:32]}")
        error = "اسم المستخدم أو كلمة المرور غير صحيحة"
    return render_template("login.html", error=error), (401 if error else 200)


@bp.post("/logout")
@login_required
def logout():
    audit("logout", "", current_user.id)
    logout_user()
    session.clear()
    return redirect(url_for("main.login"))


@bp.get("/")
@login_required
def index():
    rows = get_db().execute(
        "SELECT id, patient_ref, created_at, confirmed_json FROM notes WHERE user_id=? ORDER BY id DESC LIMIT 8",
        (current_user.id,)).fetchall()
    notes = []
    for r in rows:
        ents = json.loads(r["confirmed_json"])
        notes.append({"id": r["id"], "patient_ref": r["patient_ref"], "created_at": r["created_at"],
                      "drugs": sum(e["type"] == "drug" for e in ents), "dx": sum(e["type"] == "dx" for e in ents),
                      "vitals": sum(e["type"] == "vital" for e in ents)})
    return render_template("dashboard.html", notes=notes)


@bp.get("/notes/<int:note_id>")
@login_required
def note_detail(note_id):
    r = get_db().execute("SELECT * FROM notes WHERE id=? AND user_id=?", (note_id, current_user.id)).fetchone()
    if not r:
        abort(404)  # other users' notes look non-existent
    return render_template("note.html", note=r, entities=json.loads(r["confirmed_json"]))


@bp.get("/kb")
@login_required
def kb_page():
    return render_template("kb.html", stats=kb().stats)


@bp.get("/about")
def about():
    return render_template("about.html")


# ---------------------------------------------------------------- API
def _json_body():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        abort(400)
    return data


@bp.post("/api/analyze")
@login_required
@limiter.limit("60 per minute")
def api_analyze():
    data = _json_body()
    text = data.get("text")
    allergies = data.get("allergies") or ""
    if not isinstance(text, str) or not text.strip() or not isinstance(allergies, str):
        return jsonify(error="النص مطلوب"), 400
    if len(text) > 5000 or len(allergies) > 300:
        return jsonify(error="النص طويل جداً"), 400
    result = analyze(kb(), text, allergies)
    audit("analyze", f"chars={len(text)} entities={len(result['entities'])}", current_user.id)  # never log note text
    return jsonify(result)


def _clean_entity(k, e, allergy_ids, allergy_classes):
    """Rebuild a trusted entity from KB ids; ignore any labels/codes the client sent."""
    etype = e.get("type")
    text = str(e.get("text", ""))[:200]
    if etype == "drug":
        did = e.get("match_id")
        if not isinstance(did, int) or did not in k.drugs:
            return None, "دواء غير معروف"
        dose = e.get("dose")
        if dose:
            try:
                v = float(dose["value"])
            except (KeyError, TypeError, ValueError):
                return None, "جرعة غير صالحة"
            if not (0 < v <= 100000) or dose.get("unit") not in UNITS:
                return None, "جرعة غير صالحة"
            dose = {"value": v, "unit": dose["unit"], "text": f"{v:g} {dose['unit']}",
                    "mg": v * _TO_MG[dose["unit"]] if dose["unit"] in _TO_MG else None}
        route = e.get("route") if e.get("route") in ROUTES else None
        freq = e.get("freq") if isinstance(e.get("freq"), str) and FREQ_RE.match(e["freq"]) else None
        view = k.drug_view(did)
        warns = drug_warnings(k, did, dose, allergy_ids, allergy_classes)
        return {"type": "drug", "text": text, "match_id": did, "label": view["label"], "label_ar": view["label_ar"],
                "code": view["code"], "code_system": view["code_system"],
                "dose": {kk: dose[kk] for kk in ("value", "unit", "text")} if dose else None,
                "route": route, "freq": freq, "prn": bool(e.get("prn")), "warnings": warns}, None
    if etype == "dx":
        did = e.get("match_id")
        if not isinstance(did, int) or did not in k.dx:
            return None, "تشخيص غير معروف"
        view = k.dx_view(did)
        return {"type": "dx", "text": text, "match_id": did, "label": view["label"], "label_ar": view["label_ar"],
                "code": view["code"], "code_system": view["code_system"], "warnings": []}, None
    if etype == "vital":
        kind, value = e.get("kind"), str(e.get("value", ""))
        if kind not in VITAL_KINDS or not VITAL_VALUE_RE.match(value):
            return None, "علامة حيوية غير صالحة"
        return {"type": "vital", "text": text, "kind": kind, "label": str(e.get("label", ""))[:40], "value": value,
                "unit": str(e.get("unit", ""))[:10], "warnings": []}, None
    return None, "نوع غير معروف"


@bp.post("/api/notes")
@login_required
@limiter.limit("30 per minute")
def api_save_note():
    data = _json_body()
    transcript = data.get("transcript")
    patient_ref = (data.get("patient_ref") or "").strip()
    allergies = (data.get("allergies") or "").strip()
    ents = data.get("entities")
    if not isinstance(transcript, str) or not transcript.strip() or len(transcript) > 5000:
        return jsonify(error="النص مطلوب"), 400
    if not PATIENT_REF_RE.match(patient_ref):
        return jsonify(error="رقم الملف يقبل أحرفاً وأرقاماً فقط (بدون اسم المريض)"), 400
    if not isinstance(ents, list) or len(ents) > 100 or len(allergies) > 300:
        return jsonify(error="بيانات غير صالحة"), 400
    k = kb()
    allergy_ids, allergy_classes = allergy_drug_ids(k, allergies)
    cleaned = []
    for e in ents:
        if not isinstance(e, dict):
            return jsonify(error="بيانات غير صالحة"), 400
        c, err = _clean_entity(k, e, allergy_ids, allergy_classes)
        if err:
            return jsonify(error=err), 400
        cleaned.append(c)
    danger = [w["msg"] for c in cleaned for w in c["warnings"] if w["level"] == "danger"]
    if danger and data.get("acknowledged") is not True:
        return jsonify(error="توجد تنبيهات خطرة: أكّد مراجعتها أولاً", warnings=danger), 409
    db = get_db()
    cur = db.execute(
        "INSERT INTO notes(user_id, patient_ref, allergies, transcript, confirmed_json) VALUES (?,?,?,?,?)",
        (current_user.id, patient_ref, allergies, transcript.strip(), json.dumps(cleaned, ensure_ascii=False)))
    db.commit()
    audit("note_saved", f"id={cur.lastrowid} entities={len(cleaned)} acked={bool(danger)}", current_user.id)
    return jsonify(id=cur.lastrowid, url=url_for("main.note_detail", note_id=cur.lastrowid)), 201


@bp.get("/api/notes/<int:note_id>/fhir")
@login_required
@limiter.limit("30 per minute")
def api_note_fhir(note_id):
    r = get_db().execute("SELECT id, patient_ref, created_at, confirmed_json FROM notes WHERE id=? AND user_id=?",
                         (note_id, current_user.id)).fetchone()
    if not r:
        abort(404)  # other users' notes look non-existent
    body = fhir.dumps(fhir.note_to_bundle(r, json.loads(r["confirmed_json"]), kb()))
    audit("fhir_export", f"id={note_id}", current_user.id)
    return Response(body, mimetype="application/fhir+json",
                    headers={"Content-Disposition": f'attachment; filename="note-{note_id}.fhir.json"'})


@bp.get("/api/kb/search")
@login_required
@limiter.limit("120 per minute")
def api_kb_search():
    q = (request.args.get("q") or "").strip()[:80]
    return jsonify(results=kb().search(q) if len(q) >= 2 else [])


@bp.get("/api/stt/status")
@login_required
def api_stt_status():
    return jsonify(server=stt.available())


@bp.post("/api/stt")
@login_required
@limiter.limit("20 per minute")
def api_stt():
    f = request.files.get("audio")
    if not f or not (f.mimetype or "").startswith(("audio/", "video/webm")):
        return jsonify(error="ملف صوتي مطلوب"), 400
    if not stt.available():
        return jsonify(error="التفريغ الصوتي على الخادم غير مفعّل"), 501
    text = stt.transcribe(f, kb())
    audit("stt", f"chars={len(text)}", current_user.id)
    return jsonify(text=text)
