"""Spoken English forms, missing-dose safety net, and Arabic-letter English words (suggestions only)."""
from app.extract import analyze, en_number, parse_dose, parse_freq
from app.mixed import find_translit


def ents(kb, text, allergies="", type_=None):
    return [e for e in analyze(kb, text, allergies)["entities"] if type_ is None or e["type"] == type_]


def test_en_number_words():
    assert en_number("five hundred") == 500 and en_number("twenty-five") == 25
    assert en_number("two hundred and fifty") == 250 and en_number("half a") == 0.5
    assert en_number("one thousand") == 1000


def test_dose_spoken_english_units_and_numbers():
    d = parse_dose("500 milligrams")
    assert (d["value"], d["unit"], d["mg"]) == (500.0, "mg", 500.0)
    assert parse_dose("five hundred milligrams")["mg"] == 500
    assert parse_dose("half a gram")["mg"] == 500
    assert parse_dose("250 micrograms")["unit"] == "mcg" and parse_dose("10 milliliters")["unit"] == "ml"
    assert parse_dose("two tablets")["unit"] == "tab" and parse_dose("one tablet")["value"] == 1


def test_number_words_inside_other_words_are_not_doses():
    assert parse_dose("often") is None and parse_dose("someone mg") is None
    assert parse_dose("phone 5 mg")["value"] == 5  # the digit is the dose, 'phone' contributes nothing


def test_frequency_spoken_english():
    assert parse_freq("every six hours") == "q6h" and parse_freq("every twelve hours") == "q12h"
    assert parse_freq("every 8 hrs") == "q8h" and parse_freq("every 6 hours") == "q6h"
    assert parse_freq("every other day") is None


def test_bp_over_and_arabic_ala(kb):
    assert ents(kb, "BP 160 over 100", type_="vital")[0]["value"] == "160/100"
    assert ents(kb, "ضغطه 150 على 95", type_="vital")[0]["value"] == "150/95"
    assert ents(kb, "BP 80 over 120", type_="vital")[0]["status"] == "check"  # implausible still flagged


def test_screenshot_sentence(kb):
    r = analyze(kb, "Gave paracetamol 500 milligrams IV every six hours. BP 160 over 100")
    d = [e for e in r["entities"] if e["type"] == "drug"][0]
    a = d["attrs"]
    assert (a["dose"]["value"], a["dose"]["unit"], a["route"], a["freq"]) == (500.0, "mg", "IV", "q6h")
    assert d["status"] == "matched" and not d["warnings"]
    assert [e["value"] for e in r["entities"] if e["type"] == "vital"] == ["160/100"]


def test_spoken_overdose_still_flagged(kb):
    d = ents(kb, "gave paracetamol five grams IV", type_="drug")[0]
    assert any(w["level"] == "danger" for w in d["warnings"])


def test_drug_without_dose_is_never_auto_confirmed(kb):
    d = ents(kb, "gave paracetamol IV", type_="drug")[0]
    assert d["status"] == "review"
    assert any(w.get("code") == "no_dose" for w in d["warnings"])
    d = ents(kb, "gave paracetamol 1 g IV", type_="drug")[0]
    assert d["status"] == "matched" and not any(w.get("code") == "no_dose" for w in d["warnings"])


TEXT = "اليوم البيشنت كان عنده سكر وطلب مني اتواصل مع الاهل لانه يحتاج سبورت وبعد قال والاندرستاند"


def test_arabic_letter_english_words_are_suggested_not_rewritten(kb):
    r = analyze(kb, TEXT)
    assert r["text"] == TEXT  # the nurse's text is never altered
    got = {h["term"]: h for h in r["hints"]}
    assert {"patient", "support", "understand"} <= set(got)
    for h in r["hints"]:
        assert TEXT[h["start"]:h["end"]] == h["text"]
    assert got["understand"]["suggest"] == "و understand"  # keeps the Arabic 'and' separate
    assert got["patient"]["suggest"] == "patient"


def test_hints_skip_drug_names_and_unknown_words(kb):
    assert find_translit("اعطيت بنادول وباراسيتامول") == []
    assert find_translit("كلمة غير معروفة بالمرة") == []
    r = analyze(kb, "اعطيت البيشنت باراسيتامول 1 جرام وريدي")
    assert [h["term"] for h in r["hints"]] == ["patient"]
    assert [e["match"]["label"] for e in r["entities"] if e["type"] == "drug"] == ["paracetamol"]


def test_word_today_is_not_diazepam(kb):
    # 'فاليوم' (Valium) normalises to the stem 'يوم', the same as 'اليوم' (today)
    for t in ("اليوم المريض بخير", "واليوم تم اعطاء العلاج", "باليوم الثاني", "يوم الاحد", "اليوم البيشنت كان عنده سكر"):
        assert ents(kb, t, type_="drug") == [], t


def test_real_valium_spellings_still_match(kb):
    for t in ("اعطيت فاليوم 5 ملغ", "اعطيت ڤاليوم 5 ملغ", "اعطيت ديازيبام 5 ملغ", "gave diazepam 5 mg"):
        d = ents(kb, t, type_="drug")
        assert d and d[0]["match"]["label"] == "diazepam", t


# ---- a number straight after the drug name, with no unit ("فيوروسيمايد 40 وريدي") ------------------

def _drug(kb, text):
    return ents(kb, text, type_="drug")[0]


def test_bare_number_is_read_as_dose_with_unit_inferred_from_kb_limit(kb):
    d = _drug(kb, "اعطيت فيوروسيمايد 40 وريدي")
    dose = d["attrs"]["dose"]
    assert (dose["value"], dose["unit"], dose["unit_missing"]) == (40.0, "mg", True)
    assert d["status"] == "review" and any(w.get("code") == "unit_assumed" for w in d["warnings"])
    # 1 with a 1000 mg limit can only be grams; 500 can only be mg
    assert _drug(kb, "اعطيت بنادول 1 وريدي")["attrs"]["dose"]["unit"] == "g"
    assert _drug(kb, "اعطيت بنادول 500")["attrs"]["dose"]["unit"] == "mg"


def test_ambiguous_or_unknown_unit_is_left_blank_and_flagged(kb):
    d = _drug(kb, "اعطيت بنادول 10 وريدي")  # 10 mg is implausibly low, 10 g far above the limit
    assert d["attrs"]["dose"]["unit"] is None and d["status"] == "review"
    assert any(w.get("code") == "no_unit" for w in d["warnings"])
    d = _drug(kb, "اعطيت هيبارين 5000 وريدي")  # no dose limit in the KB for heparin
    assert d["attrs"]["dose"]["unit"] is None


def test_numbers_that_are_not_doses_are_ignored(kb):
    for t in ("اعطيت بنادول 3 مرات يوميا", "اعطيت اموكسيسيلين 5 ايام", "اعطيت بنادول 40%", "paracetamol 3 times daily"):
        d = _drug(kb, t)
        assert d["attrs"]["dose"] is None and any(w.get("code") == "no_dose" for w in d["warnings"]), t


def test_explicit_unit_is_unchanged(kb):
    d = _drug(kb, "اعطيت فيوروسيمايد 40 ملغ وريدي")
    assert "unit_missing" not in d["attrs"]["dose"] and d["attrs"]["dose"]["unit"] == "mg"


def test_server_refuses_to_save_a_dose_without_unit(client, kb):
    did = kb.exact("drug", "باراسيتامول")[0]
    body = {"patient_ref": "100234", "transcript": "اعطيت بنادول 10", "allergies": "", "acknowledged": False,
            "entities": [{"type": "drug", "text": "بنادول", "match_id": did, "dose": {"value": 10, "unit": None}}]}
    r = client.post("/api/notes", json=body, headers={"X-CSRFToken": client.csrf})
    assert r.status_code == 400 and "جرعة غير صالحة" in r.get_json()["error"]
    body["entities"][0]["dose"] = {"value": 10, "unit": "mg"}
    assert client.post("/api/notes", json=body, headers={"X-CSRFToken": client.csrf}).status_code == 201
