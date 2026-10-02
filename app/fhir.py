"""Export a confirmed note as an HL7 FHIR R4 Bundle (type "collection").

Only nurse-confirmed, KB-backed data is exported: no free-text transcript and no patient name
(the transcript may contain one). The patient is identified by the file number only.
This is an export, not a live integration with a hospital system.
"""
import json
import uuid
from datetime import datetime, timezone

_NS = uuid.UUID("6f1a8f0e-5d0c-4c53-9a43-6e7a1c0b9f11")
SYS_RXNORM = "http://www.nlm.nih.gov/research/umls/rxnorm"
SYS_ICD10 = "http://hl7.org/fhir/sid/icd-10"
SYS_SNOMED = "http://snomed.info/sct"
SYS_LOINC = "http://loinc.org"
SYS_UCUM = "http://unitsofmeasure.org"
SYS_GTS = "http://terminology.hl7.org/CodeSystem/v3-GTSAbbreviation"
SYS_OBS_CAT = "http://terminology.hl7.org/CodeSystem/observation-category"
SYS_PATIENT_REF = "urn:nursevoice:patient-ref"
SYS_NOTE = "urn:nursevoice:note"

ROUTE_SNOMED = {
    "PO": ("26643006", "Oral route"), "IV": ("47625008", "Intravenous route"),
    "IM": ("78421000", "Intramuscular route"), "SC": ("34206005", "Subcutaneous route"),
    "SL": ("37839007", "Sublingual route"), "PR": ("37161004", "Rectal route"),
    "TOP": ("6064005", "Topical route"), "INH": ("447694001", "Respiratory tract route"),
}
DOSE_UCUM = {"mg": ("mg", "mg"), "mcg": ("mcg", "ug"), "g": ("g", "g"), "ml": ("mL", "mL"), "meq": ("mEq", "meq")}
GTS_FREQ = {"BID": "BID", "TID": "TID", "QID": "QID", "QD": "QD"}
VITAL_LOINC = {
    "temp": ("8310-5", "Body temperature", "Cel", "°C"),
    "hr": ("8867-4", "Heart rate", "/min", "/min"),
    "rr": ("9279-1", "Respiratory rate", "/min", "/min"),
    "spo2": ("59408-5", "Oxygen saturation in Arterial blood by Pulse oximetry", "%", "%"),
    "glucose": ("2339-0", "Glucose [Mass/volume] in Blood", "mg/dL", "mg/dL"),
}
BP_PANEL = ("85354-9", "Blood pressure panel with all children optional")
BP_SYS = ("8480-6", "Systolic blood pressure")
BP_DIA = ("8462-4", "Diastolic blood pressure")


def _uid(note_id, kind, i=0):
    return str(uuid.uuid5(_NS, f"{note_id}:{kind}:{i}"))


def _iso(created_at):
    """SQLite CURRENT_TIMESTAMP is UTC 'YYYY-MM-DD HH:MM:SS'."""
    try:
        dt = datetime.strptime(created_at, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        dt = datetime.now(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _num(v):
    f = float(v)
    return int(f) if f.is_integer() else f


def _medication(note_id, i, e, patient_url, when):
    code = {"text": e["label"]}
    if e.get("code") and e.get("code_system") == "RxNorm":
        code["coding"] = [{"system": SYS_RXNORM, "code": str(e["code"]), "display": e["label"]}]
    dosage = {}
    parts = []
    dose = e.get("dose")
    if dose:
        q = {"value": _num(dose["value"]), "unit": DOSE_UCUM.get(dose["unit"], (dose["unit"],))[0]}
        if dose["unit"] in DOSE_UCUM:
            q.update(system=SYS_UCUM, code=DOSE_UCUM[dose["unit"]][1])
        dosage["doseAndRate"] = [{"doseQuantity": q}]
        parts.append(dose["text"])
    route = e.get("route")
    if route in ROUTE_SNOMED:
        c, d = ROUTE_SNOMED[route]
        dosage["route"] = {"coding": [{"system": SYS_SNOMED, "code": c, "display": d}], "text": route}
        parts.append(route)
    freq = e.get("freq")
    if freq:
        parts.append(freq)
        if freq.startswith("q") and freq.endswith("h") and freq[1:-1].isdigit():
            dosage["timing"] = {"repeat": {"frequency": 1, "period": int(freq[1:-1]), "periodUnit": "h"}, "code": {"text": freq}}
        elif freq in GTS_FREQ:
            dosage["timing"] = {"code": {"coding": [{"system": SYS_GTS, "code": GTS_FREQ[freq]}], "text": freq}}
        elif freq == "QHS":
            dosage["timing"] = {"repeat": {"when": ["HS"]}, "code": {"text": freq}}
        else:  # STAT
            dosage["timing"] = {"code": {"text": freq}}
    if e.get("prn"):
        dosage["asNeededBoolean"] = True
        parts.append("PRN")
    res = {
        "resourceType": "MedicationStatement", "id": _uid(note_id, "med", i),
        # "unknown": the note records a mention by the nurse, not whether it was given or ordered.
        "status": "unknown", "medicationCodeableConcept": code,
        "subject": {"reference": patient_url}, "dateAsserted": when,
    }
    if dosage:
        dosage["text"] = " ".join(parts)
        res["dosage"] = [dosage]
    return res


def _condition(note_id, i, e, kb, patient_url, when):
    codings = []
    row = kb.dx.get(e["match_id"]) if kb is not None else None
    if row and row["icd10"]:
        codings.append({"system": SYS_ICD10, "code": row["icd10"], "display": e["label"]})
    elif e.get("code") and e.get("code_system") == "ICD-10":
        codings.append({"system": SYS_ICD10, "code": e["code"], "display": e["label"]})
    if row and row["snomed"]:
        codings.append({"system": SYS_SNOMED, "code": str(row["snomed"]), "display": e["label"]})
    code = {"text": e["label"]}
    if codings:
        code["coding"] = codings
    return {"resourceType": "Condition", "id": _uid(note_id, "dx", i), "code": code,
            "subject": {"reference": patient_url}, "recordedDate": when}


def _vital(note_id, i, e, patient_url, when):
    base = {"resourceType": "Observation", "id": _uid(note_id, "vit", i), "status": "final",
            "category": [{"coding": [{"system": SYS_OBS_CAT, "code": "vital-signs", "display": "Vital Signs"}]}],
            "subject": {"reference": patient_url}, "effectiveDateTime": when}
    if e["kind"] == "bp":
        sys_v, dia_v = e["value"].split("/") if "/" in e["value"] else (None, None)
        if sys_v is None:
            return None  # a lone BP number can't be mapped honestly
        mm = {"unit": "mmHg", "system": SYS_UCUM, "code": "mm[Hg]"}
        base["code"] = {"coding": [{"system": SYS_LOINC, "code": BP_PANEL[0], "display": BP_PANEL[1]}], "text": "Blood pressure"}
        base["component"] = [
            {"code": {"coding": [{"system": SYS_LOINC, "code": c[0], "display": c[1]}]},
             "valueQuantity": {"value": _num(v), **mm}} for c, v in ((BP_SYS, sys_v), (BP_DIA, dia_v))]
        return base
    loinc = VITAL_LOINC.get(e["kind"])
    if not loinc:
        return None
    base["code"] = {"coding": [{"system": SYS_LOINC, "code": loinc[0], "display": loinc[1]}], "text": e.get("label") or loinc[1]}
    base["valueQuantity"] = {"value": _num(e["value"]), "unit": loinc[3], "system": SYS_UCUM, "code": loinc[2]}
    return base


def note_to_bundle(note, entities, kb=None):
    """note: sqlite Row (id, patient_ref, created_at); entities: parsed confirmed_json list."""
    nid = note["id"]
    when = _iso(note["created_at"])
    pat_id = _uid(nid, "patient")
    pat_url = f"urn:uuid:{pat_id}"
    entries = [{"fullUrl": pat_url, "resource": {
        "resourceType": "Patient", "id": pat_id,
        "identifier": [{"system": SYS_PATIENT_REF, "value": note["patient_ref"]}]}}]
    for i, e in enumerate(entities):
        t = e.get("type")
        if t == "drug":
            res = _medication(nid, i, e, pat_url, when)
        elif t == "dx":
            res = _condition(nid, i, e, kb, pat_url, when)
        elif t == "vital":
            res = _vital(nid, i, e, pat_url, when)
        else:
            res = None
        if res:
            entries.append({"fullUrl": f"urn:uuid:{res['id']}", "resource": res})
    return {"resourceType": "Bundle", "id": _uid(nid, "bundle"), "type": "collection", "timestamp": when,
            "identifier": {"system": SYS_NOTE, "value": str(nid)}, "entry": entries}


SYS_SBAR = "urn:nursevoice:sbar"
LOINC_EVAL_NOTE = ("51848-0", "Evaluation note")      # checked on loinc.org: long name "Evaluation note"
LOINC_PLAN_NOTE = ("18776-5", "Plan of care note")


def sbar_to_bundle(report, sbar):
    """Export a confirmed SBAR hand-over (Bundle type "collection").

    Mapping (design decision, to be reviewed with the clinical/terminology team):
      Assessment     -> one Observation per statement (category "exam", LOINC 51848-0, valueString = the nurse's text),
                        plus an Observation per structured vital sign found in Situation/Assessment (KB-free, LOINC).
      Recommendation -> ONE CarePlan (status draft, intent proposal: a nurse's suggestion, not an order),
                        one activity.detail per statement.
    Situation and Background are NOT exported. The patient is identified by file number only.
    Assessment/Recommendation are free text the nurse typed: they may contain a name, which cannot be detected here.
    The nurse must confirm that they do not before saving.
    report: row/dict with id, patient_ref, created_at. sbar: {"A": [str], "R": [str], "vitals": [cleaned vital entities]}.
    """
    rid = f"sbar{report['id']}"
    when = _iso(report["created_at"])
    pat_id = _uid(rid, "patient")
    pat_url = f"urn:uuid:{pat_id}"
    entries = [{"fullUrl": pat_url, "resource": {
        "resourceType": "Patient", "id": pat_id,
        "identifier": [{"system": SYS_PATIENT_REF, "value": report["patient_ref"]}]}}]

    def put(res):
        entries.append({"fullUrl": f"urn:uuid:{res['id']}", "resource": res})

    for i, text in enumerate(sbar.get("A", [])):
        put({"resourceType": "Observation", "id": _uid(rid, "assess", i), "status": "final",
             "category": [{"coding": [{"system": SYS_OBS_CAT, "code": "exam", "display": "Exam"}]}],
             "code": {"coding": [{"system": SYS_LOINC, "code": LOINC_EVAL_NOTE[0], "display": LOINC_EVAL_NOTE[1]}],
                      "text": "SBAR Assessment (nursing)"},
             "subject": {"reference": pat_url}, "effectiveDateTime": when, "valueString": text})
    for i, v in enumerate(sbar.get("vitals", [])):
        res = _vital(rid, i, v, pat_url, when)
        if res:
            put(res)
    recs = sbar.get("R", [])
    if recs:
        put({"resourceType": "CarePlan", "id": _uid(rid, "careplan"), "status": "draft", "intent": "proposal",
             "category": [{"coding": [{"system": SYS_LOINC, "code": LOINC_PLAN_NOTE[0], "display": LOINC_PLAN_NOTE[1]}]}],
             "title": "SBAR Recommendation (nursing hand-over)", "subject": {"reference": pat_url}, "created": when,
             "activity": [{"detail": {"status": "not-started", "description": t}} for t in recs]})
    return {"resourceType": "Bundle", "id": _uid(rid, "bundle"), "type": "collection", "timestamp": when,
            "identifier": {"system": SYS_SBAR, "value": str(report["id"])}, "entry": entries}


def dumps(bundle):
    return json.dumps(bundle, ensure_ascii=False, indent=2)
