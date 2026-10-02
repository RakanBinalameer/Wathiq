"""Clinical-note analysis: entity linking against the KB + attributes + safety checks.

Design choices (important for a clinical setting):
 * Retrieval, not generation: every entity must exist in the knowledge base; nothing is invented.
 * Exact alias hit -> 'matched'. Any fuzzy (spelling/transliteration) hit -> 'review' and the nurse must confirm.
 * If two different drugs score within a few points of each other (look-alike/sound-alike risk) the
   alternatives are returned so the nurse picks one explicitly.
 * Thresholds below are provisional and must be validated by the clinical team.
"""
import re

from .mixed import find_translit
from .textnorm import TOKEN_RE, is_arabic, normalize, norm_phrase, strip_marks, tokenize

MAX_NGRAM = 4
FUZZY_CUTOFF = 84
FUZZY_CUTOFF_MULTI = 90  # multi-word fuzzy matches are riskier, so stricter
LASA_MARGIN = 4  # alternatives within this many points of the best hit are shown

# words that must never be fuzzy-matched to a drug/diagnosis (units, routes, frequencies, fillers)
NON_TERMS = set("""
mg mcg ml gm gram grams unit units iv im po sc sl prn stat bid tid qid daily
ملغ ملجم مجم جرام غرام مل وحده وحدات وريدي عضلي فموي مرتين يوميا مره مرات ساعات ساعه حبه حبتين حبوب
المريض مريض مريضه المريضه الممرضه ممرض الطبيب طبيب اعطيت اعطينا اعطاء اعطي تم بدات وصفت عند اللزوم
""".split())

# ---- attribute patterns (applied on normalised text) ----------------------------------------
_NUMWORDS = {"نصف": 0.5, "نص": 0.5, "ربع": 0.25, "واحد": 1, "اثنين": 2, "اثنان": 2, "ثلاث": 3, "ثلاثه": 3,
             "اربع": 4, "اربعه": 4, "خمس": 5, "خمسه": 5, "عشر": 10, "عشره": 10}
# Spoken English numbers ("five hundred", "six", "half a"): matched inside the patterns below instead
# of rewriting the text, because every entity offset must keep pointing into the nurse's original text.
_EN_ONES = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9}
_EN_TEENS = {"ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
             "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19}
_EN_TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90}
_EN_ONES_RE = "|".join(_EN_ONES)
_EN_BELOW100 = (r"(?:(?:" + "|".join(_EN_TENS) + r")(?:[\s-](?:" + _EN_ONES_RE + r"))?|"
                + "|".join(_EN_TEENS) + "|" + _EN_ONES_RE + ")")
_EN_NUM = (r"(?<![a-z])(?:(?:" + _EN_ONES_RE + r")\s+hundred(?:\s+(?:and\s+)?" + _EN_BELOW100 + r")?|"
           + _EN_BELOW100 + r"(?:\s+thousand)?|half(?:\s+an?)?)(?![a-z])")


def en_number(s):
    """'five hundred' -> 500, 'twenty-five' -> 25, 'half a' -> 0.5 (words from the patterns above only)."""
    total = cur = 0
    for w in re.split(r"[\s-]+", s.strip()):
        if w == "half":
            return 0.5
        if w in _EN_ONES:
            cur += _EN_ONES[w]
        elif w in _EN_TEENS:
            cur += _EN_TEENS[w]
        elif w in _EN_TENS:
            cur += _EN_TENS[w]
        elif w == "hundred":
            cur = max(cur, 1) * 100
        elif w == "thousand":
            total, cur = total + max(cur, 1) * 1000, 0
    return total + cur


_UNIT_CANON = [
    (r"mcg|ug|micrograms?|مكجم|ميكروجرام|ميكروغرام", "mcg"),
    (r"mg|milligrams?|ملغ\w*|ملجم|مجم|ملي\s*جرام|ملي\s*غرام", "mg"),
    (r"gm|gr|grams?|g|جرام|غرام|جم", "g"),
    (r"ml|milliliters?|millilitres?|cc|مل|ملي", "ml"),
    (r"iu|units?|وحد[هات]+", "unit"),
    (r"meq", "meq"),
    (r"tabs?|tablets?|حبه|حبات|حبوب|حبتين", "tab"),
    (r"caps?|capsules?|كبسوله|كبسولات", "cap"),
    (r"amps?|ampoules?|امبول\w*", "amp"),
]
_UNIT_RE = "|".join(f"(?P<u{i}>{p})" for i, (p, _c) in enumerate(_UNIT_CANON))
_NUM = r"(?P<num>\d+(?:\.\d+)?|" + _EN_NUM + "|" + "|".join(sorted(_NUMWORDS, key=len, reverse=True)) + r")"
DOSE_RE = re.compile(_NUM + r"\s*(?:" + _UNIT_RE + r")(?![a-z؀-ۿ])")
_TO_MG = {"mg": 1.0, "g": 1000.0, "mcg": 0.001}

ROUTES = [
    ("IV", r"\biv\b|i\.v|intravenous|وريدي|بالوريد|وريد\b"),
    ("IM", r"\bim\b|intramuscular|عضلي|بالعضل|عضل\b"),
    ("SC", r"\bsc\b|\bsubq\b|s\.c|subcut\w*|تحت الجلد"),
    ("SL", r"\bsl\b|sublingual|تحت اللسان"),
    ("INH", r"\binh\b|inhal\w*|nebuli[sz]\w*|نيبولايزر|نبولايزر|استنشاق|بخاخ"),
    ("PR", r"\bpr\b|rectal\w*|شرجي|تحميله"),
    ("TOP", r"topical|موضعي"),
    ("PO", r"\bpo\b|p\.o|\boral(?:ly)?\b|by mouth|فموي|بالفم|عن طريق الفم"),
]
FREQS = [
    ("q{n}h", r"\bq\s*(\d{1,2})\s*h\b|every\s+(\d{1,2}|" + _EN_BELOW100 + r")\s*(?:hours?|hrs?|h)\b|كل\s+(\d{1,2})\s*ساع\w*"),
    ("q1h", r"كل\s+ساعه"),
    ("BID", r"\bbid\b|b\.i\.d|twice\s+(?:a\s+)?(?:day|daily)|مرتين"),
    ("TID", r"\btid\b|t\.i\.d|three\s+times|ثلاث\s+مرات|3\s+مرات"),
    ("QID", r"\bqid\b|q\.i\.d|four\s+times|اربع\s+مرات|4\s+مرات"),
    ("QHS", r"\bqhs\b|at\s+bed\s*time|at\s+night|عند\s+النوم|قبل\s+النوم"),
    ("STAT", r"\bstat\b|فورا|فوري|حالا"),
    ("QD", r"\bod\b|\bqd\b|once\s+(?:a\s+)?day|once\s+daily|\bdaily\b|مره\s+(?:في\s+اليوم|يوميا)|يوميا"),
]
PRN_RE = re.compile(r"\bprn\b|as\s+needed|when\s+needed|عند\s+اللزوم|عند\s+الحاجه|اذا\s+لزم")
SENT_BREAK = re.compile(r"[؟?!;؛\n]|\.(?!\d)")

# ---- vitals ------------------------------------------------------------------------------------
# Spoken Arabic attaches a pronoun to the measure (حرارته، نبضه، سكره) and often puts a filler word
# before the number (حرارة المريض 39). English forms need none of this.
_PRON = r"(?:ه|ها|هم)?"
_FILL = r"(?:\s+(?:المريض|المريضه|عنده|عندها|تبلغ|يبلغ|حوالي|تقريبا|الان|هو|هي|يعادل|وصل|وصلت|صار|كان))*"
_SEP = _FILL + r"\s*(?:is|هو|:|=|-)?\s*"
VITALS = [
    ("bp", "ضغط الدم", "mmHg",
     # "160/100", spoken English "160 over 100", spoken Gulf Arabic "150 على 95" (normalised: ى -> ي)
     re.compile(r"(?:\bbp\b|blood\s+pressure|ضغط" + _PRON + r"(?:\s+الدم)?)" + _SEP + r"(\d{2,3})\s*(?:/|over|علي)\s*(\d{2,3})(?:\s*mmhg)?")),
    ("hr", "النبض", "bpm",
     re.compile(r"(?:\bhr\b|heart\s+rate|pulse|نبض" + _PRON + r"|معدل\s+النبض|ضربات\s+القلب)" + _SEP + r"(\d{2,3})(?![\d/])")),
    ("rr", "معدل التنفس", "/min",
     re.compile(r"(?:\brr\b|resp(?:iratory)?\s+rate|معدل\s+التنفس|تنفس" + _PRON + r")" + _SEP + r"(\d{1,2})(?![\d/.])")),
    ("temp", "الحرارة", "°C",
     re.compile(r"(?:\btemp(?:erature)?\b|حرار(?:ه|ت" + _PRON + r")|درجه\s+الحراره)" + _SEP + r"(\d{2,3}(?:\.\d)?)\s*(?:°|c\b|f\b|درجه)?")),
    ("spo2", "تشبع الأكسجين", "%",
     re.compile(r"(?:spo2|o2\s*sat|\bsat\b|saturation|اشباع(?:\s+الاكسجين)?|اكسجين)" + _SEP + r"(\d{2,3})\s*%?(?!\s*(?:لتر|l\b|lpm|liters?))")),
    ("glucose", "سكر الدم", "mg/dL",
     re.compile(r"(?:\brbs\b|\bfbs\b|\bbgl\b|glucose|blood\s+sugar|\bsugar\b|سكر" + _PRON + r"|جلوكوز)" + _SEP + r"(\d{1,3}(?:\.\d)?)\s*(mg/dl|mmol(?:/l)?)?")),
]
# plausible = physiologically possible (else "check spelling"); alert = clinically notable (informational)
PLAUSIBLE = {"hr": (20, 250), "rr": (4, 60), "temp": (30, 43), "spo2": (50, 100), "glucose": (10, 900)}
ALERT_TXT = {
    "hr": (lambda v: v < 50 or v > 120, "النبض خارج 50–120"),
    "rr": (lambda v: v < 10 or v > 24, "معدل التنفس خارج 10–24"),
    "temp": (lambda v: v >= 38.0 or v < 36.0, "الحرارة خارج 36–38"),
    "spo2": (lambda v: v < 92, "تشبع الأكسجين أقل من 92%"),
    "glucose": (lambda v: v < 70 or v > 250, "سكر الدم خارج 70–250 mg/dL"),
}

CLASS_AR = {"penicillins": "البنسلينات", "NSAIDs": "مضادات الالتهاب غير الستيرويدية",
            "opioids": "الأفيونات", "cephalosporins": "السيفالوسبورينات"}

# ---- allergy classes ---------------------------------------------------------------------------
ALLERGY_CLASSES = {
    "penicillins": (["penicillin", "penicillins", "بنسلين", "بنسيلين", "بنسلينات"],
                    ["amoxicillin", "amoxicillin clavulanate", "piperacillin tazobactam"]),
    "NSAIDs": (["nsaid", "nsaids", "مضادات الالتهاب غير الستيرويديه", "مسكنات الالتهاب"],
               ["ibuprofen", "diclofenac", "ketorolac", "aspirin"]),
    "opioids": (["opioid", "opioids", "مشتقات المورفين", "اوبيويد"], ["morphine", "tramadol", "fentanyl"]),
    "cephalosporins": (["cephalosporin", "cephalosporins", "سيفالوسبورين"], ["ceftriaxone"]),
}


def _canon_unit(m):
    for i, (_p, canon) in enumerate(_UNIT_CANON):
        if m.group(f"u{i}"):
            return canon


def parse_dose(seg: str):
    m = DOSE_RE.search(seg)
    if not m:
        return None
    raw = m.group("num")
    if raw[0].isdigit():
        num = float(raw)
    elif raw in _NUMWORDS:
        num = float(_NUMWORDS[raw])
    else:
        num = float(en_number(raw))
    unit = _canon_unit(m)
    if m.group(0).strip().endswith("حبتين"):
        num, unit = 2.0, "tab"
    return {"value": num, "unit": unit, "text": m.group(0).strip(),
            "mg": num * _TO_MG[unit] if unit in _TO_MG else None}


# A bare number straight after the drug name ("فيوروسيمايد 40 وريدي") is a dose without a unit, unless it is
# followed by a time/frequency word ("3 مرات", "5 ايام"), a percent, a ratio or a decimal continuation.
_NOT_DOSE = (r"ساع|مر[هة]|مرات|يوم|ايام|اسبوع|اسابيع|شهر|اشهر|دقيق|دقائق|ثانيه|سن[هة]|سنوات|"
             r"h\b|hrs?\b|hours?|times?|days?|weeks?|months?|x\b")
_BARE_DOSE_RE = re.compile(r"^\s*(?:بجرعه|جرعه|dose(?:\s+of)?)?\s*(\d+(?:\.\d+)?)(?![\d/:%]|\.\d)(?!\s*(?:" + _NOT_DOSE + r"))")
_BARE_FACTORS = (("mcg", 0.001), ("mg", 1.0), ("g", 1000.0))
_PLAUSIBLE_LOW = 50  # a usual dose is rarely below max_single_dose / 50; PROVISIONAL, for the clinical team to tune


def _suggest_unit(kb, did, value):
    """Unit that keeps `value` inside (max_single_dose/50 .. max_single_dose], only if exactly one does.
    Uses the KB's own dose limit as a plausibility check. Returns None when unknown or ambiguous."""
    mx = kb.drugs[did]["max_single_dose_mg"]
    if not mx:
        return None
    ok = [u for u, f in _BARE_FACTORS if mx / _PLAUSIBLE_LOW <= value * f <= mx]
    return ok[0] if len(ok) == 1 else None


def parse_bare_dose(kb, did, seg):
    m = _BARE_DOSE_RE.search(seg)
    if not m:
        return None
    v = float(m.group(1))
    unit = _suggest_unit(kb, did, v)
    return {"value": v, "unit": unit, "text": m.group(1), "unit_missing": True,
            "mg": v * _TO_MG[unit] if unit else None}


def parse_route(seg):
    for code, pat in ROUTES:
        if re.search(pat, seg):
            return code


def parse_freq(seg):
    for code, pat in FREQS:
        m = re.search(pat, seg)
        if m:
            if code == "q{n}h":
                n = next(g for g in m.groups() if g)
                return f"q{int(n) if n.isdigit() else en_number(n)}h"
            return code


# ---- term finding ------------------------------------------------------------------------------
def _is_short_latin_ok(orig):  # 'HTN', 'NS' accepted only when typed in capitals
    return orig.isupper()


# The alias 'فاليوم' (Valium) loses its 'فال' prefix when normalised and becomes 'يوم', which is also what
# 'اليوم' (today) normalises to. Without this guard every note containing 'اليوم' matched diazepam.
# Only the real spelling (optionally with a leading 'و') may hit these stems.
_STEM_GUARD = {"يوم": "فاليوم"}


def _passes_stem_guard(phrase, orig):
    required = _STEM_GUARD.get(phrase)
    if required is None:
        return True
    r = normalize(orig)
    return (r[1:] if r.startswith("و") else r) == required


def _exact(kb, kind, phrase, orig, n):
    ids = kb.exact(kind, phrase)
    if not ids:
        return None
    if n == 1 and not _passes_stem_guard(phrase, orig):
        return None
    if len(phrase) <= 3 and not is_arabic(phrase) and n == 1 and not _is_short_latin_ok(orig):
        return None
    return ids, 100, []


def _fuzzy(kb, kind, phrase, n):
    if any(c.isdigit() for c in phrase) or phrase in NON_TERMS:
        return None
    if len(phrase) < (5 if n == 1 else 8):
        return None
    fz = kb.fuzzy(kind, phrase, cutoff=FUZZY_CUTOFF if n == 1 else FUZZY_CUTOFF_MULTI)
    if not fz:
        return None
    best = fz[0][2]
    close = [(eid, s) for eid, _a, s in fz if best - s <= LASA_MARGIN]
    return [fz[0][0]], best, close  # best hit; look-alikes exposed via `close`


def _best(cands):
    cands = [c for c in cands if c]
    return max(cands, key=lambda c: c[1][1]) if cands else None


def find_terms(kb, tokens, text, blocked):
    """Pass 1: exact alias hits (longest n-gram first). Pass 2: fuzzy hits on what is still uncovered.
    Doing exact first stops a loose fuzzy multi-word match from swallowing a perfect shorter one."""
    covered, hits = set(blocked), []

    def take(i, n, kind, cand, start=None):
        ids, score, close = cand
        s = tokens[i][1] if start is None else start
        hits.append({"kind": kind, "ids": ids, "score": score, "close": close, "start": s, "end": tokens[i + n - 1][2]})
        covered.update(range(i, i + n))

    for pass_no in (1, 2):
        i = 0
        while i < len(tokens):
            if i in covered:
                i += 1
                continue
            placed = False
            for n in range(min(MAX_NGRAM if pass_no == 1 else 3, len(tokens) - i), 0, -1):
                if any(j in covered for j in range(i, i + n)):
                    continue
                phrase = " ".join(t[0] for t in tokens[i:i + n])
                orig = text[tokens[i][1]:tokens[i + n - 1][2]]
                cands = []
                for kind in ("drug", "dx"):
                    c = _exact(kb, kind, phrase, orig, n) if pass_no == 1 else _fuzzy(kb, kind, phrase, n)
                    cands.append((kind, c) if c else None)
                best = _best([(k, c) if c else None for k, c in [x for x in cands if x]])
                if best:
                    take(i, n, best[0], best[1])
                    placed = True
                    break
                # Arabic conjunction 'و' glued to the first word (وسكري، والم في الصدر): retry without it.
                # Only accepted if the remainder is itself a KB hit, so words that legitimately start
                # with 'و' (وريدي، وارفارين) are never mangled.
                first = tokens[i][0]
                if first.startswith("و") and len(first) >= 4 and is_arabic(first) and (pass_no == 1 or n == 1):
                    rest = " ".join([first[1:]] + [t[0] for t in tokens[i + 1:i + n]])
                    cands = []
                    for kind in ("drug", "dx"):
                        c = _exact(kb, kind, rest, rest, n) if pass_no == 1 else _fuzzy(kb, kind, rest, 1)
                        cands.append((kind, c) if c else None)
                    best = _best([x for x in cands if x])
                    if best:
                        take(i, n, best[0], best[1], start=tokens[i][1] + 1)
                        placed = True
                        break
            i += 1
    hits.sort(key=lambda h: h["start"])
    return hits


def find_vitals(nt):
    out = []
    for kind, label, unit, rx in VITALS:
        for m in rx.finditer(nt):
            out.append((kind, label, unit, m))
    # resolve overlaps: keep earliest/longest
    out.sort(key=lambda x: (x[3].start(), -(x[3].end() - x[3].start())))
    kept, last_end = [], -1
    for v in out:
        if v[3].start() >= last_end:
            kept.append(v)
            last_end = v[3].end()
    return kept


def vital_entity(kind, label, unit, m):
    warns, status = [], "matched"
    if kind == "bp":
        sys_, dia = int(m.group(1)), int(m.group(2))
        value = f"{sys_}/{dia}"
        if not (50 <= sys_ <= 260 and 30 <= dia <= 160 and sys_ > dia):
            status, warns = "check", [("warn", "قيمة ضغط الدم غير معقولة — راجع الإملاء")]
        elif sys_ >= 180 or dia >= 110 or sys_ < 90:
            warns.append(("info", "ضغط الدم خارج النطاق المعتاد (90–180 / <110)"))
    else:
        v = float(m.group(1))
        if kind == "temp" and 90 <= v <= 110:  # Fahrenheit
            v, unit = round((v - 32) * 5 / 9, 1), "°C"
        if kind == "glucose" and len(m.groups()) > 1 and m.group(2) and m.group(2).startswith("mmol"):
            unit = "mmol/L"
            v_mgdl = v * 18.0
        else:
            v_mgdl = v
        value = str(int(v)) if v == int(v) else str(v)
        lo, hi = PLAUSIBLE[kind]
        check_v = v_mgdl if kind == "glucose" else v
        if not (lo <= check_v <= hi):
            status, warns = "check", [("warn", f"قيمة {label} غير معقولة ({value}) — راجع الإملاء")]
        elif kind in ALERT_TXT and ALERT_TXT[kind][0](check_v):
            warns.append(("info", ALERT_TXT[kind][1]))
    return {"type": "vital", "kind": kind, "label": label, "value": value, "unit": unit,
            "status": status, "score": 100, "warnings": [{"level": l, "msg": t} for l, t in warns]}


def allergy_drug_ids(kb, allergies_text):
    """Drug ids (and class names) implied by the free-text allergy field."""
    text = strip_marks(allergies_text or "")
    ids, classes = {}, {}
    for h in find_terms(kb, tokenize(text), text, set()):
        if h["kind"] == "drug":
            for i in h["ids"]:
                ids[i] = kb.drugs[i]["generic_en"]
    nt = norm_phrase(text)
    by_key = {d["key"]: i for i, d in kb.drugs.items()}
    for cname, (aliases, members) in ALLERGY_CLASSES.items():
        if any(norm_phrase(a) in nt for a in aliases):
            for mname in members:
                if mname in by_key:
                    classes[by_key[mname]] = cname
    return ids, classes


def analyze(kb, text, allergies=""):
    text = strip_marks(text)[:5000]
    nt = normalize(text)
    tokens = tokenize(text)

    vit = find_vitals(nt)
    v_spans = [(m.start(), m.end()) for _k, _l, _u, m in vit]
    blocked = {j for j, (_t, s, e) in enumerate(tokens) if any(s >= a and e <= b for a, b in v_spans)}
    hits = find_terms(kb, tokens, text, blocked)

    allergy_ids, allergy_classes = allergy_drug_ids(kb, allergies)
    entities = []
    hit_starts = sorted(h["start"] for h in hits)
    for idx, h in enumerate(hits):
        ent = {"type": h["kind"], "text": text[h["start"]:h["end"]], "start": h["start"], "end": h["end"],
               "score": round(h["score"]), "warnings": [],
               "status": "matched" if h["score"] >= 100 and len(h["ids"]) == 1 else "review"}
        view = kb.drug_view if h["kind"] == "drug" else kb.dx_view
        ent["match"] = view(h["ids"][0])
        alts = [{**view(i), "score": round(s)} for i, s in h["close"]] if h["close"] else []
        if len(h["ids"]) > 1:  # one alias maps to several entities (exact)
            alts = [{**view(i), "score": 100} for i in h["ids"]]
        if len(alts) > 1:
            ent["alternatives"] = alts
            ent["status"] = "review"
            ent["warnings"].append({"level": "warn", "msg": "أسماء متشابهة — اختر الصحيح صراحةً"})
        if h["score"] < 100:
            ent["warnings"].append({"level": "warn", "msg": f"مطابقة تقريبية ({round(h['score'])}%) — تأكد من الاسم"})
        if h["kind"] == "drug":
            _drug_attrs(kb, ent, text, nt, h, hits, idx, hit_starts, allergy_ids, allergy_classes)
        elif not ent["match"]["code"]:
            ent["warnings"].append({"level": "info", "msg": "لا يوجد ربط ICD-10 لهذا التشخيص — يحدَّد من قِبل الترميز الطبي"})
        entities.append(ent)
    for kind, label, unit, m in vit:
        ent = vital_entity(kind, label, unit, m)
        ent.update(text=text[m.start():m.end()], start=m.start(), end=m.end())
        entities.append(ent)
    entities.sort(key=lambda e: e["start"])
    for n, e in enumerate(entities):
        e["id"] = f"e{n}"
    summary = {"drugs": sum(e["type"] == "drug" for e in entities), "dx": sum(e["type"] == "dx" for e in entities),
               "vitals": sum(e["type"] == "vital" for e in entities),
               "review": sum(e["status"] in ("review", "check") for e in entities),
               "danger": sum(w["level"] == "danger" for e in entities for w in e["warnings"])}
    hints = find_translit(text, [(e["start"], e["end"]) for e in entities])
    return {"text": text, "entities": entities, "summary": summary, "hints": hints}


def _drug_attrs(kb, ent, text, nt, h, hits, idx, hit_starts, allergy_ids, allergy_classes):
    end = h["end"]
    nxt = next((s for s in hit_starts if s > h["start"]), len(text))
    brk = SENT_BREAK.search(nt, end)
    w_end = min(nxt, brk.start() if brk else len(text), end + 90)
    seg = nt[end:w_end]
    dose, route, freq = parse_dose(seg), parse_route(seg), parse_freq(seg)
    if not (dose and route and freq):
        # look just before the drug name ('1 gram paracetamol IV') - but only when the previous
        # mention is not itself a drug, otherwise we would steal that drug's dose/route/frequency
        prev = hits[idx - 1] if idx else None
        b = 0 if prev is None else prev["end"]
        if prev is not None and prev["kind"] == "drug":
            b = h["start"]
        b = max(b, h["start"] - 28)
        for m in SENT_BREAK.finditer(nt, 0, h["start"]):
            b = max(b, m.end())
        pre = nt[b:h["start"]]
        dose = dose or parse_dose(pre)
        route = route or parse_route(pre)
        freq = freq or parse_freq(pre)
    if not dose:
        dose = parse_bare_dose(kb, h["ids"][0], seg)
    ent["attrs"] = {"dose": dose, "route": route, "freq": freq, "prn": bool(PRN_RE.search(seg))}
    if dose and dose.get("unit_missing"):
        # never auto-confirmed: a wrong unit is a 1000x error. The server also refuses to save a dose without a unit.
        ent["status"] = "review"
        if dose["unit"]:
            ent["warnings"].append({"level": "warn", "code": "unit_assumed",
                                    "msg": f"الوحدة غير مذكورة — افترضنا {dose['unit']} من الحد المعتاد للدواء. تأكد"})
        else:
            ent["warnings"].append({"level": "warn", "code": "no_unit",
                                    "msg": f"الرقم {dose['text']} بدون وحدة — اختر الوحدة (mg / g / mcg …)"})
    if not dose:
        # a drug without a dose must never be auto-confirmed: the nurse either types it or confirms it was not stated
        ent["status"] = "review"
        ent["warnings"].append({"level": "warn", "code": "no_dose",
                                "msg": "لم تُستخرج جرعة لهذا الدواء — أدخلها أو أكّد أنها غير مذكورة"})
    if ent["match"]["ambiguous"]:
        ent["status"] = "review"
        ent["warnings"].append({"level": "warn", "msg": "نوع الأنسولين غير محدد — حدّده (لانتوس/عادي/…)"})
    ent["warnings"].extend(drug_warnings(kb, h["ids"][0], dose, allergy_ids, allergy_classes))


def drug_warnings(kb, did, dose, allergy_ids, allergy_classes):
    """Safety checks for one confirmed drug. Re-run server-side on save (never trust the client)."""
    out = []
    max_mg = kb.drugs[did]["max_single_dose_mg"]
    if dose and dose.get("mg") is not None and max_mg and dose["mg"] > max_mg:
        out.append({"level": "danger", "msg": f"الجرعة {dose['text']} أعلى من الحد المعتاد للجرعة الواحدة ({max_mg:g} mg) — راجع الوصفة"})
    if did in allergy_ids:
        out.append({"level": "danger", "msg": f"حساسية مسجلة لدى المريض تجاه {allergy_ids[did]}"})
    elif did in allergy_classes:
        out.append({"level": "danger", "msg": f"حساسية مسجلة من فئة {CLASS_AR.get(allergy_classes[did], allergy_classes[did])} — هذا الدواء ضمن الفئة"})
    return out
