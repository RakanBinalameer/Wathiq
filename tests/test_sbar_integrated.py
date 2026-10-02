"""SBAR inside the dictation page: one save for the note and (optionally) its SBAR hand-over."""
import json
import re
import sqlite3
import pytest

TEXT = "أعطيت باراسيتامول 1 جرام وريدي. الحالة حرجة، SpO2 88%. أوصي بمتابعة دقيقة."
SBAR = {"sections": {"S": ["ضيق تنفس"], "B": [], "A": ["الحالة حرجة"], "R": ["متابعة دقيقة"]},
        "vitals": [{"type": "vital", "text": "SpO2 88%", "kind": "spo2", "label": "SpO2", "value": "88", "unit": "%"}],
        "confirmed": True, "no_names": True}


def entities(c):
    a = c.post("/api/analyze", json={"text": TEXT}, headers={"X-CSRFToken": c.csrf}).get_json()
    out = []
    for e in a["entities"]:
        if e["type"] == "vital":
            out.append({"type": "vital", "text": e["text"], "kind": e["kind"], "label": e["label"], "value": e["value"], "unit": e["unit"]})
        elif e["type"] == "drug":
            d = e["attrs"]["dose"]
            out.append({"type": "drug", "text": e["text"], "match_id": e["match"]["id"], "dose": d and {"value": d["value"], "unit": d["unit"]},
                        "route": e["attrs"]["route"], "freq": e["attrs"]["freq"]})
    return out


def save(c, ref="700100", **kw):
    body = {"patient_ref": ref, "transcript": TEXT, "entities": entities(c), "acknowledged": True}
    body.update(kw)
    return c.post("/api/notes", json=body, headers={"X-CSRFToken": c.csrf})


def count(db_path, table, ref):
    return sqlite3.connect(db_path).execute(f"SELECT COUNT(*) FROM {table} WHERE patient_ref=?", (ref,)).fetchone()[0]


def test_note_without_sbar_is_unchanged(client, db_path):
    r = save(client, ref="700101")
    assert r.status_code == 201 and r.get_json()["sbar_id"] is None and "sbar=" not in r.get_json()["url"]
    assert count(db_path, "sbar_reports", "700101") == 0


def test_note_and_sbar_saved_together(client, client2, db_path):
    r = save(client, ref="700102", sbar=SBAR)
    assert r.status_code == 201
    d = r.get_json()
    assert d["sbar_id"] and f"sbar={d['sbar_id']}" in d["url"]
    assert count(db_path, "notes", "700102") == 1 and count(db_path, "sbar_reports", "700102") == 1
    stored = json.loads(sqlite3.connect(db_path).execute("SELECT sbar_json FROM sbar_reports WHERE id=?", (d["sbar_id"],)).fetchone()[0])
    assert stored["A"] == ["الحالة حرجة"] and stored["vitals"][0]["kind"] == "spo2"
    page = client.get(d["url"]).text                                       # note page links to the report saved with it
    assert f"/sbar/{d['sbar_id']}" in page
    assert f"/sbar/{d['sbar_id']}" not in client2.get(f"/notes/{d['id']}?sbar={d['sbar_id']}").text   # not shown to another nurse
    assert f"/sbar/{d['sbar_id']}" in client.get(f"/patients/700102").text
    acts = [row[0] for row in sqlite3.connect(db_path).execute("SELECT action FROM audit")]
    assert "note_saved" in acts and "sbar_saved" in acts
    detail = " ".join(row[0] or "" for row in sqlite3.connect(db_path).execute("SELECT detail FROM audit WHERE action='sbar_saved'"))
    assert "حرجة" not in detail


def test_sbar_banner_only_for_own_report_of_same_file(client, db_path):
    a = save(client, ref="700103", sbar=SBAR).get_json()
    b = save(client, ref="700104").get_json()
    assert f"/sbar/{a['sbar_id']}" not in client.get(f"/notes/{b['id']}?sbar={a['sbar_id']}").text   # other file number
    assert client.get(f"/notes/{b['id']}?sbar=abc").status_code == 200


@pytest.mark.parametrize("kw,status", [
    ({"confirmed": False}, 409), ({"no_names": False}, 409), ({"confirmed": "yes"}, 409),
    ({"sections": {"S": [], "B": [], "A": [], "R": []}}, 400), ({"sections": "x"}, 400),
    ({"vitals": [{"type": "drug", "match_id": 1}]}, 400),
    ({"vitals": [{"type": "vital", "kind": "spo2", "value": "abc", "label": "x", "unit": "%"}]}, 400),
])
def test_refused_sbar_saves_nothing(client, db_path, kw, status):
    ref = "700105"
    before_n, before_s = count(db_path, "notes", ref), count(db_path, "sbar_reports", ref)
    r = save(client, ref=ref, sbar={**SBAR, **kw})
    assert r.status_code == status and r.get_json()["sbar"] is True          # flagged so the page does not treat it as a drug alert
    assert count(db_path, "notes", ref) == before_n and count(db_path, "sbar_reports", ref) == before_s


def test_sbar_must_be_an_object(client, db_path):
    r = save(client, ref="700106", sbar="oops")
    assert r.status_code == 400 and count(db_path, "notes", "700106") == 0


def test_dictation_page_has_the_sbar_section(client):
    html = client.get("/").text
    assert 'role="tab"' not in html                                          # one view: no tabs to switch between
    for needle in ('id="tabp-sbar"', 'id="sb-cards"', 'id="sb-include"', 'id="sb-ok-review"', 'id="sb-ok-names"', "sbar_core.js"):
        assert needle in html
    assert html.index("sbar_core.js") < html.index("app.js")                  # the panel must load before app.js uses it
