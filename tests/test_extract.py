from app.extract import analyze, parse_dose, parse_freq, parse_route
from app.textnorm import norm_phrase, normalize, tokenize


def ents(kb, text, allergies="", type_=None):
    r = analyze(kb, text, allergies)
    return [e for e in r["entities"] if type_ is None or e["type"] == type_]


def test_normalisation():
    assert normalize("أعطيتُ ١٢٠/٨٠") == "اعطيت 120/80"
    assert norm_phrase("الباراسيتامول") == "باراسيتامول"
    assert [t[0] for t in tokenize("ألم، الصدر؟")] == ["الم", "صدر"]  # Arabic punctuation is not a letter


def test_arabic_drug_with_full_attributes(kb):
    d = ents(kb, "أعطيت المريض باراسيتامول ١ جرام وريدي كل ٨ ساعات", type_="drug")[0]
    assert d["match"]["label"] == "paracetamol" and d["status"] == "matched"
    a = d["attrs"]
    assert (a["dose"]["value"], a["dose"]["unit"], a["route"], a["freq"]) == (1.0, "g", "IV", "q8h")


def test_brand_and_synonym_resolve_to_same_drug(kb):
    ids = {ents(kb, t, type_="drug")[0]["match"]["id"] for t in ("بنادول ١ جرام", "Panadol 1 g", "acetaminophen 1 g", "باراسيتامول")}
    assert len(ids) == 1


def test_english_and_mixed(kb):
    r = ents(kb, "Pt known case of HTN and DM2. Gave metformin 500 mg PO BID")
    labels = {e["match"]["label"] for e in r if e["type"] != "vital"}
    assert {"Essential hypertension", "Type 2 diabetes mellitus", "metformin"} <= labels


def test_exact_beats_loose_fuzzy_multiword(kb):
    e = ents(kb, "المريض عنده ارتفاع ضغط الدم", type_="dx")
    assert len(e) == 1 and e[0]["status"] == "matched" and e[0]["match"]["code"] == "I10"


def test_misspelling_needs_review_not_auto_accept(kb):
    d = ents(kb, "اعطيت ميتفرمين ٥٠٠ ملغ", type_="drug")[0]
    assert d["match"]["label"] == "metformin" and d["status"] == "review" and d["score"] < 100


def test_attributes_do_not_leak_between_drugs(kb):
    d = ents(kb, "ميتفورمين ٥٠٠ ملغ مرتين يوميا وامبلوديبين ١٠ ملغ", type_="drug")
    assert d[0]["attrs"]["freq"] == "BID" and d[1]["attrs"]["freq"] is None


def test_word_starting_with_waw_not_mangled(kb):
    d = ents(kb, "اعطيت وارفارين ٥ ملغ", type_="drug")
    assert d and d[0]["match"]["label"] == "warfarin"


def test_insulin_unspecified_forces_review(kb):
    d = ents(kb, "انسولين ١٠ وحدة تحت الجلد", type_="drug")[0]
    assert d["status"] == "review" and d["attrs"]["route"] == "SC"


def test_overdose_flagged(kb):
    d = ents(kb, "اعطيت بنادول ١٠ جرام وريدي", type_="drug")[0]
    assert any(w["level"] == "danger" for w in d["warnings"])


def test_normal_dose_not_flagged(kb):
    d = ents(kb, "paracetamol 1 g IV", type_="drug")[0]
    assert not [w for w in d["warnings"] if w["level"] == "danger"]


def test_allergy_class_and_direct(kb):
    d = ents(kb, "اعطيت اموكسيسيلين ٥٠٠ ملجم", "بنسلين", "drug")[0]
    assert any("البنسلينات" in w["msg"] for w in d["warnings"])
    d = ents(kb, "gave ibuprofen 400 mg", "ibuprofen", "drug")[0]
    assert any(w["level"] == "danger" for w in d["warnings"])
    d = ents(kb, "gave paracetamol 1 g", "بنسلين", "drug")[0]
    assert not d["warnings"]


def test_vitals_arabic_and_english(kb):
    v = {e["kind"]: e for e in ents(kb, "الضغط ١٢٠/٨٠ والنبض ٨٨ والحرارة ٣٨.٥ SpO2 93% RBS 210", type_="vital")}
    assert v["bp"]["value"] == "120/80" and v["hr"]["value"] == "88" and v["temp"]["value"] == "38.5"
    assert v["spo2"]["value"] == "93" and v["glucose"]["value"] == "210"


def test_implausible_vital_marked_check(kb):
    v = ents(kb, "SpO2 190%", type_="vital")
    assert v and v[0]["status"] == "check"
    b = ents(kb, "BP 80/120", type_="vital")[0]
    assert b["status"] == "check"


def test_oxygen_litres_not_read_as_saturation(kb):
    assert not ents(kb, "اكسجين ٤ لتر", type_="vital")
    assert not ents(kb, "oxygen sat", type_="vital")


def test_fahrenheit_converted(kb):
    v = ents(kb, "temp 101.3", type_="vital")[0]
    assert v["value"] == "38.5" and v["unit"] == "°C"


def test_no_false_positives_on_plain_text(kb):
    assert ents(kb, "The patient is stable and resting comfortably. تم تغيير الضمادة وتوعية المريض") == []


def test_spans_point_into_original_text(kb):
    text = "أعطيت باراسيتامول ١ جرام. الضغط ١٢٠/٨٠"
    for e in analyze(kb, text)["entities"]:
        assert text[e["start"]:e["end"]] == e["text"]


def test_short_latin_abbreviation_needs_capitals(kb):
    assert ents(kb, "he was at pe class", type_="dx") == []
    assert ents(kb, "known HTN", type_="dx")


def test_parsers():
    assert parse_dose("نص جرام")["value"] == 0.5
    assert parse_dose("2 حبتين") and parse_dose("500mg")["mg"] == 500
    assert parse_route("subcutaneous") == "SC" and parse_route("تحت اللسان") == "SL"
    assert parse_freq("q6h") == "q6h" and parse_freq("مرتين يوميا") == "BID" and parse_freq("عند النوم") == "QHS"


def test_spoken_arabic_vitals_with_pronouns_and_fillers(kb):
    def vit(t):
        return {e["kind"]: e["value"] for e in ents(kb, t, type_="vital")}
    assert vit("وليد في حراره درجه حرارته 30 تعبان")["temp"] == "30"
    v = vit("المريض حرارته 38.5 ونبضه 110 وضغطه 150/95 وسكره 240")
    assert v == {"temp": "38.5", "hr": "110", "bp": "150/95", "glucose": "240"}
    assert vit("درجة حرارة المريض 39")["temp"] == "39"
    assert vit("حرارتها ٣٨")["temp"] == "38"


def test_diabetes_word_is_not_read_as_glucose_value(kb):
    assert not ents(kb, "المريض سكري 15 سنة", type_="vital")
    assert not ents(kb, "سكري نوع ثاني", type_="vital")
