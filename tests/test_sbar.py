import json
import shutil
import sqlite3
import pytest
from app import create_app
from app.sbar import split_sbar

MOCK = ("المريضة تعاني من ضيق تنفس حاد، SpO2 88%، وارتفاع حرارة 38.5. تاريخ مرضي بـ COPD، أدخلت أمس للعناية المركزة. "
        "الحالة حرجة. تم إعطاء أكسجين عبر القناع، وجرعة باراسيتامول. أوصي بمتابعة دقيقة لـ SpO2 وضغط الدم، "
        "واستدعاء الطبيب المناوب في حال استمرار ضيق التنفس.")
HEADERS = ("الموقف: المريض يشتكي من ألم في الصدر. الخلفية: مصاب بالسكري منذ 10 سنوات. "
           "التقييم: الحالة غير مستقرة، الضغط 90/60 والنبض 120. التوصية: نحتاج تخطيط قلب فوري.")
ENGLISH = ("Situation: pt c/o shortness of breath, SpO2 88%. Background: known case of COPD, admitted yesterday. "
           "Assessment: patient looks unstable, gave oxygen via mask. Recommendation: please monitor SpO2 closely and call the doctor.")


def sec(res):
    return {s: " | ".join(res["sections"][s]) for s in "SBAR"}


# ---- splitter ----------------------------------------------------------------------------
def test_spoken_headers_cut_the_text():
    r = split_sbar(HEADERS)
    assert r["mode"] == "headers"
    s = sec(r)
    assert "ألم في الصدر" in s["S"] and "السكري" in s["B"] and "غير مستقرة" in s["A"] and "تخطيط قلب" in s["R"]
    assert all(not i["review"] for i in r["items"]) and "الموقف" not in s["S"]      # header words are not kept


def test_english_headers():
    s = sec(split_sbar(ENGLISH))
    assert "shortness of breath" in s["S"] and "COPD" in s["B"] and "unstable" in s["A"] and "monitor" in s["R"]


def test_rules_mode_without_headers():
    r = split_sbar(MOCK)
    assert r["mode"] == "rules"
    s = sec(r)
    assert "ضيق تنفس حاد" in s["S"] and "COPD" in s["B"] and "أكسجين" in s["A"] and "استدعاء" in s["R"]
    assert [i["section"] for i in r["items"]] == ["S", "B", "A", "A", "R"]


def test_unpunctuated_dictation_is_cut_at_strong_starters():
    r = split_sbar("المريضة تعاني من ضيق تنفس حاد والاكسجين 88 تاريخ مرضي بالربو ادخلت امس للعناية المركزة الحالة حرجة "
                   "تم اعطاء اكسجين عبر القناع اوصي بمتابعة دقيقة واستدعاء الطبيب")
    assert [i["section"] for i in r["items"]][0] == "S" and [i["section"] for i in r["items"]][-1] == "R"
    assert {i["section"] for i in r["items"]} == set("SBAR")


def test_unrecognised_text_is_flagged_not_trusted():
    r = split_sbar("مرحبا كيف الحال")
    assert r["items"][0]["review"] is True and r["items"][0]["how"] == "carry"


def test_empty_text():
    assert split_sbar("")["items"] == []


# ---- API -----------------------------------------------------------------------------------
def analyze(c, text=MOCK):
    return c.post("/api/sbar/analyze", json={"text": text}, headers={"X-CSRFToken": c.csrf})


def body(**kw):
    b = {"patient_ref": "100234", "transcript": MOCK, "confirmed": True, "no_names": True,
         "sections": {"S": ["المريضة تعاني من ضيق تنفس حاد"], "B": ["تاريخ مرضي بـ COPD"],
                      "A": ["الحالة حرجة", "تم إعطاء أكسجين عبر القناع"], "R": ["متابعة دقيقة لـ SpO2", "استدعاء الطبيب المناوب"]},
         "vitals": [{"type": "vital", "text": "SpO2 88%", "kind": "spo2", "label": "SpO2", "value": "88", "unit": "%"}]}
    b.update(kw)
    return b


def post(c, **kw):
    return c.post("/api/sbar", json=body(**kw), headers={"X-CSRFToken": c.csrf})


def test_pages_and_api_require_login(app):
    c = app.test_client()
    assert c.post("/api/sbar/analyze", json={"text": "x"}).status_code in (400, 401)
    assert c.get("/api/sbar/1/fhir").status_code in (401, 302)
    assert c.get("/sbar/1").status_code == 302


def test_analyze_returns_items_and_vitals_with_warning(client):
    r = analyze(client)
    assert r.status_code == 200
    d = r.get_json()
    assert d["mode"] == "rules" and {i["section"] for i in d["items"]} == set("SBAR")
    v = {x["kind"]: x for x in d["vitals"]}
    assert v["spo2"]["value"] == "88" and v["spo2"]["section"] == "S" and v["spo2"]["warnings"]
    assert "المريضة" not in json.dumps(d["vitals"], ensure_ascii=False)


@pytest.mark.parametrize("bad", [{}, {"text": ""}, {"text": 5}, {"text": "x" * 5001}])
def test_analyze_rejects_bad_input(client, bad):
    assert client.post("/api/sbar/analyze", json=bad, headers={"X-CSRFToken": client.csrf}).status_code == 400


def test_save_requires_both_confirmations(client):
    assert post(client, confirmed=False).status_code == 409
    assert post(client, no_names=False).status_code == 409
    assert post(client, confirmed="yes").status_code == 409


@pytest.mark.parametrize("kw", [
    {"patient_ref": "أحمد علي"}, {"patient_ref": ""}, {"sections": {"S": [], "B": [], "A": [], "R": []}},
    {"sections": {"S": "text"}}, {"sections": {"S": ["x" * 601]}}, {"sections": {"S": ["a"] * 31}},
    {"vitals": [{"type": "vital", "kind": "spo2", "value": "abc", "label": "x", "unit": "%"}]},
    {"vitals": [{"type": "drug", "match_id": 1}]}, {"vitals": "x"}, {"transcript": ""},
])
def test_save_validation(client, kw):
    assert post(client, **kw).status_code == 400


def test_save_detail_patient_page_and_audit(client, client2, db_path):
    r = post(client)
    assert r.status_code == 201
    rid = r.get_json()["id"]
    page = client.get(f"/sbar/{rid}").text
    assert "الحالة حرجة" in page and "استدعاء الطبيب المناوب" in page and f"/api/sbar/{rid}/fhir" in page
    assert client2.get(f"/sbar/{rid}").status_code == 200                       # any registered nurse can open it
    assert f"/sbar/{rid}" in client2.get("/patients/100234").text              # patient-centred listing
    assert client.get("/sbar/99999999").status_code == 404
    acts = [row[0] for row in sqlite3.connect(db_path).execute("SELECT action FROM audit")]
    assert "sbar_saved" in acts and "sbar_view_other" in acts
    detail = " ".join(row[0] or "" for row in sqlite3.connect(db_path).execute("SELECT detail FROM audit WHERE action LIKE 'sbar%'"))
    assert "حرجة" not in detail and "COPD" not in detail                        # note text never goes to the audit log


# ---- FHIR export ---------------------------------------------------------------------------
def export(c, **kw):
    rid = post(c, **kw).get_json()["id"]
    return rid, c.get(f"/api/sbar/{rid}/fhir")


def by_type(bundle):
    out = {}
    for e in bundle["entry"]:
        out.setdefault(e["resource"]["resourceType"], []).append(e["resource"])
    return out


def test_fhir_maps_assessment_to_observation_and_recommendation_to_careplan(client):
    rid, r = export(client)
    assert r.status_code == 200 and r.mimetype == "application/fhir+json"
    assert f"sbar-{rid}.fhir.json" in r.headers["Content-Disposition"]
    b = r.get_json()
    t = by_type(b)
    assert t["Patient"][0]["identifier"][0]["value"] == "100234"
    assess = [o for o in t["Observation"] if o["code"]["coding"][0]["code"] == "51848-0"]
    assert [o["valueString"] for o in assess] == ["الحالة حرجة", "تم إعطاء أكسجين عبر القناع"]
    assert all(o["status"] == "final" and o["category"][0]["coding"][0]["code"] == "exam" for o in assess)
    vit = [o for o in t["Observation"] if o["code"]["coding"][0]["code"] == "59408-5"]
    assert vit and vit[0]["valueQuantity"]["value"] == 88
    assert len(t["CarePlan"]) == 1
    cp = t["CarePlan"][0]
    assert cp["intent"] == "proposal" and cp["status"] == "draft" and cp["category"][0]["coding"][0]["code"] == "18776-5"
    assert [a["detail"]["description"] for a in cp["activity"]] == ["متابعة دقيقة لـ SpO2", "استدعاء الطبيب المناوب"]
    assert all(a["detail"]["status"] == "not-started" for a in cp["activity"])


def test_fhir_leaves_out_situation_background_and_transcript(client):
    _, r = export(client)
    for secret in ("ضيق تنفس حاد", "تاريخ مرضي", MOCK[:20], "transcript"):
        assert secret not in r.text
    assert "name" not in r.get_json()["entry"][0]["resource"]


def test_fhir_without_recommendations_has_no_careplan(client):
    _, r = export(client, sections={"S": ["x"], "B": [], "A": ["مستقرة"], "R": []}, vitals=[])
    t = by_type(r.get_json())
    assert "CarePlan" not in t and len(t["Observation"]) == 1


def test_fhir_validates_against_r4_models(client):
    pytest.importorskip("fhir.resources")
    from fhir.resources.R4B.bundle import Bundle
    _, r = export(client)
    Bundle.model_validate(r.get_json())


def test_fhir_requires_login_and_care_team_access(client, client2, app):
    rid, _ = export(client)
    assert client2.get(f"/api/sbar/{rid}/fhir").status_code == 200
    assert app.test_client().get(f"/api/sbar/{rid}/fhir").status_code in (302, 401)
    assert client2.get("/api/sbar/99999999/fhir").status_code == 404


# ---- existing databases get the new table ----------------------------------------------------
def test_existing_database_is_upgraded_on_startup(db_path, tmp_path):
    old = tmp_path / "old.db"
    shutil.copy(db_path, old)
    con = sqlite3.connect(old); con.execute("DROP TABLE sbar_reports"); con.commit(); con.close()
    create_app({"DATABASE": str(old), "TESTING": True, "RATELIMIT_ENABLED": False})
    tables = {r[0] for r in sqlite3.connect(old).execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "sbar_reports" in tables
