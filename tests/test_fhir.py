import json
import pytest
from test_app import payload, save

TEXT = ("المريض وليد حرارته 38.5 والضغط 150/95 والنبض 110 عنده سكر نوع ثاني. "
        "اعطيت باراسيتامول 500 ملغ وريد كل 6 ساعات")


def _export(client, text=TEXT, patient_ref="100234"):
    a = payload(client, text)
    r = save(client, a, patient_ref=patient_ref, acknowledged=True)
    assert r.status_code == 201
    nid = r.get_json()["id"]
    return nid, client.get(f"/api/notes/{nid}/fhir")


def test_fhir_export_content(client):
    nid, r = _export(client)
    assert r.status_code == 200
    assert r.mimetype == "application/fhir+json"
    assert f"note-{nid}.fhir.json" in r.headers["Content-Disposition"]
    b = r.get_json()
    assert b["resourceType"] == "Bundle" and b["type"] == "collection"
    by = {}
    for e in b["entry"]:
        by.setdefault(e["resource"]["resourceType"], []).append(e["resource"])
    assert by["Patient"][0]["identifier"][0]["value"] == "100234"
    med = by["MedicationStatement"][0]
    # no product-level RxNorm code on a generic drug (the KB code belongs to one product, e.g. a 500 mg tablet)
    assert med["medicationCodeableConcept"] == {"text": "paracetamol"}
    dose = med["dosage"][0]
    assert dose["doseAndRate"][0]["doseQuantity"] == {"value": 500, "unit": "mg", "system": "http://unitsofmeasure.org", "code": "mg"}
    assert dose["route"]["coding"][0]["code"] == "47625008"
    assert dose["timing"]["repeat"] == {"frequency": 1, "period": 6, "periodUnit": "h"}
    cond = by["Condition"][0]
    assert any(c["system"] == "http://snomed.info/sct" for c in cond["code"]["coding"])
    codes = {o["code"]["coding"][0]["code"] for o in by["Observation"]}
    assert {"8310-5", "8867-4", "85354-9"} <= codes
    bp = next(o for o in by["Observation"] if o["code"]["coding"][0]["code"] == "85354-9")
    assert [c["valueQuantity"]["value"] for c in bp["component"]] == [150, 95]


def test_fhir_export_has_no_name_or_transcript(client):
    _, r = _export(client)
    assert "وليد" not in r.text and "transcript" not in r.text.lower()
    assert "name" not in r.get_json()["entry"][0]["resource"]


def test_fhir_export_available_to_care_team_but_requires_login(client, client2, app):
    nid, _ = _export(client)
    assert client2.get(f"/api/notes/{nid}/fhir").status_code == 200
    assert app.test_client().get(f"/api/notes/{nid}/fhir").status_code in (302, 401)
    assert client2.get("/api/notes/99999999/fhir").status_code == 404


def test_fhir_requires_login(app):
    assert app.test_client().get("/api/notes/1/fhir").status_code in (401, 302)


def test_fhir_validates_against_r4_models(client):
    pytest.importorskip("fhir.resources")
    from fhir.resources.R4B.bundle import Bundle
    _, r = _export(client)
    Bundle.model_validate(r.get_json())  # raises on any structural / datatype / code-shape error


def test_note_page_offers_export(client):
    nid, _ = _export(client)
    assert f"/api/notes/{nid}/fhir" in client.get(f"/notes/{nid}").text


def _save_vitals(client, vitals, ref="P-V"):
    return client.post("/api/notes", json={"transcript": "x", "patient_ref": ref, "allergies": "", "entities": vitals},
                       headers={"X-CSRFToken": client.csrf})


def _vit(kind, value, unit="", label="x"):
    return {"type": "vital", "kind": kind, "value": value, "unit": unit, "label": label}


def test_vital_value_must_fit_its_kind(client):
    # a BP needs systolic/diastolic; every other vital is a single number. Mismatches used to be saved and then crashed the export.
    for bad in ([_vit("temp", "120/80")], [_vit("bp", "120")], [_vit("glucose", "12", "kg")]):
        r = _save_vitals(client, bad)
        assert r.status_code == 400 and "علامة حيوية" in r.get_json()["error"]
    assert _save_vitals(client, [_vit("temp", "38.5"), _vit("bp", "120/80")]).status_code == 201


def test_vital_label_and_unit_come_from_the_server(client):
    r = _save_vitals(client, [_vit("temp", "38.5", unit="mg", label="anything"), _vit("glucose", "12", "mmol/L")])
    nid = r.get_json()["id"]
    b = client.get(f"/api/notes/{nid}/fhir")
    assert b.status_code == 200
    obs = {o["resource"]["code"]["coding"][0]["code"]: o["resource"] for o in b.get_json()["entry"]
           if o["resource"]["resourceType"] == "Observation"}
    assert obs["8310-5"]["valueQuantity"]["unit"] == "°C"
    assert "anything" not in b.text


def test_fhir_keeps_glucose_unit(client):
    nid = _save_vitals(client, [_vit("glucose", "12", "mmol/L"), _vit("glucose", "210", "mg/dL")]).get_json()["id"]
    q = [o["resource"]["valueQuantity"] for o in client.get(f"/api/notes/{nid}/fhir").get_json()["entry"]
         if o["resource"]["resourceType"] == "Observation"]
    assert {"value": 12, "unit": "mmol/L", "system": "http://unitsofmeasure.org", "code": "mmol/L"} in q
    assert {"value": 210, "unit": "mg/dL", "system": "http://unitsofmeasure.org", "code": "mg/dL"} in q


def test_fhir_export_survives_bad_rows_already_in_the_db(app, client):
    # notes saved before the stricter check may hold a mismatched vital: the export must skip it, not return 500
    from app.db import connect
    con = connect(app.config["DATABASE"])
    bad = json.dumps([{"type": "vital", "kind": "temp", "value": "120/80", "label": "x", "unit": "mg"},
                      {"type": "vital", "kind": "bp", "value": "abc", "label": "x", "unit": ""}])
    nid = con.execute("INSERT INTO notes(user_id, patient_ref, allergies, transcript, confirmed_json) VALUES (1,'P-BAD','','x',?)",
                      (bad,)).lastrowid
    con.commit(); con.close()
    r = client.get(f"/api/notes/{nid}/fhir")
    assert r.status_code == 200 and len(r.get_json()["entry"]) == 1      # only the Patient
