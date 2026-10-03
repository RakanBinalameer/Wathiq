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


def _wav(seconds=1.0, rate=16000, amp=0.3, channels=1):
    import io, math, struct, wave
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(channels); w.setsampwidth(2); w.setframerate(rate)
        n = int(seconds * rate)
        w.writeframes(b"".join(struct.pack("<h", int(amp * 32767 * math.sin(i / 20))) * channels for i in range(n)))
    return buf.getvalue()


def test_stt_gracefully_unavailable(client, monkeypatch, tmp_path):
    monkeypatch.setenv("WHISPER_MODEL_DIR", str(tmp_path / "nothing"))
    assert client.get("/api/stt/status").get_json() == {"server": False}
    r = client.post("/api/stt", data={"audio": (__import__("io").BytesIO(_wav()), "a.wav", "audio/wav")}, headers={"X-CSRFToken": client.csrf})
    assert r.status_code == 501
    assert client.post("/api/stt", data={}, headers={"X-CSRFToken": client.csrf}).status_code == 400


def test_stt_with_fake_model(client, monkeypatch):
    from app import stt
    seen = []

    class FakeStream:
        def __init__(self): self.result = type("R", (), {"text": ""})()
        def accept_waveform(self, rate, x): self.n = len(x); self.rate = rate

    class FakeRec:
        def create_stream(self): return FakeStream()
        def decode_stream(self, st): st.result.text = " اعطيت باراسيتامول "; seen.append((st.rate, st.n))

    monkeypatch.setattr(stt, "available", lambda: True)
    monkeypatch.setattr(stt, "_recognizer", lambda lang: FakeRec())
    hdr = {"X-CSRFToken": client.csrf}
    # 48 kHz stereo is converted to 16 kHz mono
    r = client.post("/api/stt", data={"audio": (__import__("io").BytesIO(_wav(2, 48000, channels=2)), "a.wav", "audio/wav"), "lang": "ar"}, headers=hdr)
    assert r.status_code == 200 and r.get_json()["text"] == "اعطيت باراسيتامول"
    assert seen[-1][0] == 16000 and abs(seen[-1][1] - 32000) <= 1
    # silence is never sent to the model (Whisper invents text on silence)
    n = len(seen)
    r = client.post("/api/stt", data={"audio": (__import__("io").BytesIO(_wav(2, amp=0.0)), "a.wav", "audio/wav")}, headers=hdr)
    assert r.get_json()["text"] == "" and len(seen) == n
    # not a wav -> 400, never a crash
    r = client.post("/api/stt", data={"audio": (__import__("io").BytesIO(b"not audio"), "a.wav", "audio/wav")}, headers=hdr)
    assert r.status_code == 400
    # the call is audited by length only, never by content
    from app.db import connect
    con = connect(client.application.config["DATABASE"])
    rows = [r["detail"] for r in con.execute("SELECT detail FROM audit WHERE action='stt'")]
    assert rows and all(d.startswith("chars=") and "باراسيتامول" not in d for d in rows)


def test_stt_chunking_and_cleaning():
    import numpy as np
    from app import stt
    x = np.ones(16000 * 60, dtype=np.float32) * 0.2
    x[16000 * 23: 16000 * 23 + 1600] = 0.0         # a quiet spot near the first cut
    parts = stt.split_chunks(x)
    assert all(len(p) <= 25 * 16000 for p in parts) and sum(len(p) for p in parts) == len(x) and len(parts) == 3
    assert len(parts[0]) == 23 * 16000 + 800         # cut lands in the quiet spot
    assert stt.clean("اه اه اه اه اه اه") == "اه"
    assert stt.clean("5 5 5 mg") == "5 5 5 mg"      # real repetition below the threshold is kept


def test_stt_refuses_overlong_recording():
    import pytest
    from app import stt
    with pytest.raises(stt.BadAudio):
        stt.read_wav(_wav(seconds=130))
    with pytest.raises(stt.BadAudio):
        stt.read_wav(b"RIFF....")


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
