import json
import re
import pytest
from app.seed import build_kb, import_sfda_csv, parse_rxnorm_display


def payload(c, text="اعطيت باراسيتامول ١ جرام وريدي. الضغط 120/80", allergies=""):
    return c.post("/api/analyze", json={"text": text, "allergies": allergies}, headers={"X-CSRFToken": c.csrf}).get_json()


def to_entities(a):
    out = []
    for e in a["entities"]:
        if e["type"] == "vital":
            out.append({"type": "vital", "text": e["text"], "kind": e["kind"], "label": e["label"], "value": e["value"], "unit": e["unit"]})
        else:
            d = {"type": e["type"], "text": e["text"], "match_id": e["match"]["id"]}
            if e["type"] == "drug":
                dose = e["attrs"]["dose"]
                d.update(dose=dose and {"value": dose["value"], "unit": dose["unit"]}, route=e["attrs"]["route"], freq=e["attrs"]["freq"])
            out.append(d)
    return out


def save(c, a, **kw):
    body = {"patient_ref": "100234", "allergies": "", "transcript": a["text"], "entities": to_entities(a)}
    body.update(kw)
    return c.post("/api/notes", json=body, headers={"X-CSRFToken": c.csrf})


# ---- auth / security -------------------------------------------------------------------
def test_anonymous_redirected_and_api_401(app):
    c = app.test_client()
    assert c.get("/").status_code == 302
    assert c.post("/api/analyze", json={"text": "x"}).status_code in (400, 401)
    assert c.get("/api/kb/search?q=para").status_code == 401


def test_login_requires_csrf_and_rejects_bad_password(app):
    c = app.test_client()
    assert c.post("/login", data={"username": "nurse1", "password": "Test#Pass123"}).status_code == 400
    tok = re.search(r'name="csrf_token" value="([^"]+)"', c.get("/login").text).group(1)
    r = c.post("/login", data={"username": "nurse1", "password": "bad", "csrf_token": tok})
    assert r.status_code == 401 and "غير صحيحة" in r.text
    r = c.post("/login", data={"username": "ghost", "password": "bad", "csrf_token": tok})
    assert r.status_code == 401  # same response for unknown user


def test_no_open_redirect_after_login(app):
    c = app.test_client()
    tok = re.search(r'name="csrf_token" value="([^"]+)"', c.get("/login").text).group(1)
    r = c.post("/login?next=//evil.example", data={"username": "nurse1", "password": "Test#Pass123", "csrf_token": tok})
    assert r.headers["Location"] == "/"


def test_security_headers(app):
    h = app.test_client().get("/login").headers
    assert "script-src 'self'" in h["Content-Security-Policy"] and "'unsafe-inline'" not in h["Content-Security-Policy"]
    assert h["X-Frame-Options"] == "DENY" and h["X-Content-Type-Options"] == "nosniff"
    assert "no-store" in h["Cache-Control"]


def test_csrf_enforced_on_api(client):
    assert client.post("/api/analyze", json={"text": "x"}).status_code == 400


def test_pages_render_without_inline_handlers(client):
    for path in ("/", "/kb", "/about"):
        html = client.get(path).text
        assert " style=" not in html and "onclick=" not in html and "<script>" not in html


# ---- analyze / save --------------------------------------------------------------------
def test_analyze_validation(client):
    H = {"X-CSRFToken": client.csrf}
    assert client.post("/api/analyze", json={"text": ""}, headers=H).status_code == 400
    assert client.post("/api/analyze", json={"text": "x" * 5001}, headers=H).status_code == 400
    assert client.post("/api/analyze", data="nope", headers=H).status_code == 400


def test_save_and_view_note(client):
    a = payload(client)
    r = save(client, a)
    assert r.status_code == 201
    page = client.get(r.get_json()["url"]).text
    assert "paracetamol" in page and "100234" in page


def test_danger_requires_acknowledgement(client):
    a = payload(client, "اعطيت بنادول ١٠ جرام وريدي")
    r = save(client, a)
    assert r.status_code == 409 and r.get_json()["warnings"]
    assert save(client, a, acknowledged=True).status_code == 201


def test_server_recomputes_warnings_after_client_edit(client):
    a = payload(client, "اعطيت باراسيتامول ١ جرام")
    ents = to_entities(a)
    ents[0]["dose"] = {"value": 50, "unit": "g"}  # nurse edits dose upward
    r = client.post("/api/notes", json={"patient_ref": "1", "transcript": a["text"], "entities": ents}, headers={"X-CSRFToken": client.csrf})
    assert r.status_code == 409


def test_allergy_danger_blocks_unacked_save(client):
    a = payload(client, "اعطيت اموكسيسيلين ٥٠٠ ملجم فموي", "بنسلين")
    assert save(client, a, allergies="بنسلين").status_code == 409


@pytest.mark.parametrize("mutate,expected", [
    (lambda b: b["entities"].__setitem__(0, {"type": "drug", "text": "x", "match_id": 99999999}), 400),
    (lambda b: b.__setitem__("patient_ref", "محمد أحمد"), 400),
    (lambda b: b.__setitem__("patient_ref", ""), 400),
    (lambda b: b.__setitem__("entities", "x"), 400),
    (lambda b: b["entities"].append({"type": "vital", "kind": "bp", "value": "<b>", "text": "x"}), 400),
    (lambda b: b["entities"].append({"type": "weird"}), 400),
])
def test_tampered_payloads_rejected(client, mutate, expected):
    a = payload(client)
    body = {"patient_ref": "100234", "transcript": a["text"], "entities": to_entities(a)}
    mutate(body)
    assert client.post("/api/notes", json=body, headers={"X-CSRFToken": client.csrf}).status_code == expected


def test_client_labels_are_ignored(client):
    a = payload(client)
    ents = to_entities(a)
    ents[0]["label"] = "FAKE DRUG"
    r = save(client, a, entities=ents)
    assert "FAKE DRUG" not in client.get(r.get_json()["url"]).text


def test_xss_is_escaped(client):
    a = payload(client)
    ents = to_entities(a)
    ents[0]["text"] = "<img src=x onerror=alert(1)>"
    r = save(client, a, entities=ents, transcript="<script>alert(1)</script>")
    page = client.get(r.get_json()["url"]).text
    assert "<script>alert(1)</script>" not in page and "<img src=x" not in page


def test_notes_are_shared_by_patient_and_cross_access_is_audited(app, client, client2):
    ref = "SHARED-77"
    url = save(client, payload(client), patient_ref=ref).get_json()["url"]
    save(client2, payload(client2, "اعطيت بنادول 500 ملغ"), patient_ref=ref)
    save(client, payload(client), patient_ref="OTHER-1")
    page = client2.get(url)
    assert page.status_code == 200 and "كتبها" in page.text  # another nurse can read it, author is shown
    pt = client2.get(f"/patients/{ref}").text
    assert pt.count("<li>") == 2 and "(أنت)" in pt and "OTHER-1" not in pt  # both nurses' notes, not other patients'
    assert client2.get(f"/patients?ref={ref}").status_code == 302
    from app.db import connect
    con = connect(app.config["DATABASE"])
    acts = [r["action"] for r in con.execute("SELECT action FROM audit")]
    con.close()
    assert "note_view_other" in acts and "patient_view" in acts


def test_patient_page_validates_ref_and_requires_login(app, client):
    assert client.get("/patients/<script>").status_code == 404
    assert client.get("/patients?ref=محمد").status_code == 302  # bad ref -> back home, no lookup
    assert client.get("/patients/NO-SUCH-1").status_code == 200
    assert app.test_client().get("/patients/ANY-1").status_code in (302, 401)


def test_audit_log_never_stores_note_text(app, client):
    payload(client, "اعطيت باراسيتامول SECRETWORD")
    from app.db import connect
    con = connect(app.config["DATABASE"])
    rows = " ".join(r["detail"] or "" for r in con.execute("SELECT detail FROM audit"))
    assert "SECRETWORD" not in rows and "chars=" in rows


# ---- KB / STT --------------------------------------------------------------------------
def test_kb_search(client):
    res = client.get("/api/kb/search?q=augmentin").get_json()["results"]
    assert res and res[0]["label"] == "amoxicillin clavulanate"
    assert client.get("/api/kb/search?q=a").get_json()["results"] == []
    res = client.get("/api/kb/search?q=ضغط").get_json()["results"]
    assert any(r["code"] == "I10" for r in res)


def test_stt_gracefully_unavailable(client):
    assert client.get("/api/stt/status").get_json() == {"server": False}
    r = client.post("/api/stt", data={"audio": (__import__("io").BytesIO(b"abc"), "a.webm", "audio/webm")}, headers={"X-CSRFToken": client.csrf})
    assert r.status_code == 501
    assert client.post("/api/stt", data={}, headers={"X-CSRFToken": client.csrf}).status_code == 400


def test_healthz_and_404(app, client):
    assert app.test_client().get("/healthz").get_json() == {"ok": True}
    assert client.get("/nope").status_code == 404


# ---- seeding ---------------------------------------------------------------------------
def test_parse_rxnorm_display():
    p = parse_rxnorm_display("Acetaminophen 325 MG / Oxycodone Hydrochloride 5 MG Oral Tablet [Percocet]")
    assert p["ingredients"] == ["acetaminophen", "oxycodone hydrochloride"] and p["brand"] == "Percocet" and p["form"] == "Oral Tablet"
    assert parse_rxnorm_display("1 ML medroxyPROGESTERone acetate 150 MG/ML Injection")["ingredients"] == ["medroxyprogesterone acetate"]


def test_kb_seed_quality(kb):
    names = [d["generic_en"] for d in kb.drugs.values()]
    assert not [n for n in names if not n or "{" in n or n.startswith("nda")]
    assert all(d["sfda_verified"] == 0 for d in kb.drugs.values())  # nothing claims SFDA verification without an import
    assert len(kb.drugs) > 200 and len(kb.dx) > 200


def test_sfda_import(tmp_path):
    from pathlib import Path
    data = Path(__file__).resolve().parent.parent / "data"
    csv = tmp_path / "sfda.csv"
    csv.write_text("generic_name,trade_name,registration_no\nPARACETAMOL,Panadol Extra,123\nNEWDRUGINE HYDROCHLORIDE,Newdro,456\n", encoding="utf-8")
    con, _ = build_kb(str(tmp_path / "s.db"), data, str(csv))
    assert con.execute("SELECT sfda_verified FROM drugs WHERE key='paracetamol'").fetchone()[0] == 1
    assert con.execute("SELECT source FROM drugs WHERE key='newdrugine'").fetchone()[0] == "sfda_import"
