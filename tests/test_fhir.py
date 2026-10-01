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
    coding = med["medicationCodeableConcept"]["coding"][0]
    assert coding["system"].endswith("rxnorm") and coding["code"]
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


def test_fhir_export_is_private_to_owner(client, client2):
    nid, _ = _export(client)
    assert client2.get(f"/api/notes/{nid}/fhir").status_code == 404


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
