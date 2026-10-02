"""Split a nurse's dictated hand-over into the four SBAR parts (Situation, Background, Assessment, Recommendation).

Rule-based, no generative AI (same approach as extract.py):
  1. Spoken headers. If the nurse says the section names («الموقف … الخلفية … التقييم … التوصية», or the English
     words) the text is cut at those words. Needs at least two different headers to count as header mode.
  2. Otherwise each clause is scored against keyword lists for the four sections, with a small preference for the
     usual S→B→A→R order. Anything not decided with confidence is marked `review` and the nurse moves it by hand.

Accuracy on real nurse speech has NOT been measured. Negation is not handled («ما عنده ضيق تنفس» still looks like
a complaint). Web Speech dictation usually returns no punctuation, which is why clauses are also cut at strong
trigger words, not only at full stops.
"""
import re

from .textnorm import normalize

SECTIONS = ("S", "B", "A", "R")
IDX = {s: i for i, s in enumerate(SECTIONS)}

# Section headers spoken by the nurse (matched on normalised text: no tashkeel, أ/إ→ا, ة→ه, ى→ي).
_W = r"(?<![\w])(?:%s)(?![\w])"
HEADERS = {
    "S": re.compile(_W % r"الموقف|الوضع\s+الحالي|الحاله\s+الحاليه|situation", re.I),
    "B": re.compile(_W % r"الخلفيه(?:\s+المرضيه)?|خلفيه\s+المريض|background", re.I),
    "A": re.compile(_W % r"التقييم|تقييمي|تقييم\s+الحاله|assessment", re.I),
    "R": re.compile(_W % r"التوصيه|التوصيات|توصيتي|توصياتي|recommendations?", re.I),
}
_AFTER_HEADER = re.compile(r"^[\s:：\-–—،,.]+|^(?:هو|هي|هوه)\s+")

# Keyword scores per section (normalised text). Weight 2 = strong marker, 1–1.5 = supportive.
_RULES = {
    "S": [
        (2.0, r"تعاني|يعاني|تشتكي|يشتكي|شكوي|complain|c/o|presents?\s+with|presenting"),
        (1.5, r"ضيق\s+(?:في\s+)?(?:ال)?تنفس|(?<!\w)الم(?!\w)|وجع|دوخه|اغماء|نزيف|قيء|تقيؤ|اسهال|صداع|سعال|هبوط|ارتفاع|انخفاض|فجاه|"
              r"(?<!\w)الان(?!\w)|حاليا|منذ\s+(?:ساعه|ساعتين|قليل|\d+\s*ساع)|shortness|chest\s+pain|dyspnea|fever|bleeding|vomit|seizure"),
        (1.0, r"تدهور|الحاله\s+الحاليه|الوضع\s+الحالي"),
    ],
    "B": [
        (2.0, r"تاريخ(?:ه|ها)?\s*(?:ال)?مرضي|history|(?<!\w)hx(?!\w)|known\s+case|(?<!\w)k/c(?!\w)|معروف\s+(?:ب|لديه|عنده)|مصاب\s+ب|مزمن"),
        (1.5, r"ادخل|ادخلت|دخلت|دخل\s+(?:المستشفي|العنايه|امس)|admitted|اقامه|حساسيه|allerg|سابق|سابقه|عمليه|جراحه|"
              r"منذ\s+\d+\s*(?:سنه|سنوات|شهر|اشهر)|عمره|عمرها|\d+\s*سنه|\d+\s*(?:years?|y/o|yo)"),
        (0.8, r"(?<![\w])(?:copd|htn|dm2?|ckd|chf|cad|asthma|السكري|السكر|الضغط|الربو)(?![\w])"),
    ],
    "A": [
        (2.0, r"تم\s+(?:ال)?(?:اعطاء|اخذ|قياس|تركيب|اجراء|وضع|سحب|تغيير)|اعطيت|اعطينا|اعطي(?:ت)?\s|قمت|قمنا|اجريت|"
              r"gave|given|administered|started|applied|assessed|checked"),
        (1.5, r"الحاله\s+(?:حرجه|مستقره|غير\s+مستقره|سيئه|جيده|متدهوره)|(?<!\w)حرج|مستقر|غير\s+مستقر|يبدو|اعتقد|اشتبه|ملاحظ|لاحظت|تحسن|"
              r"appears?|looks?|stable|unstable|critical|deteriorat"),
    ],
    "R": [
        (2.0, r"اوصي|نوصي|نحتاج|تحتاج\s+(?:الي|ل)|يحتاج\s+(?:الي|ل)|يجب|يلزم|يرجي|ارجو|المطلوب|استدعاء|ابلاغ|اطلب|تحويل|"
              r"اعاده\s+تقييم|recommend|please|(?<!\w)need|should|must|notify|call\s+(?:the\s+)?(?:doctor|md|physician)|reassess|follow[\s-]?up"),
        (1.5, r"متابعه|مراقبه|راقب|تابع|monitor|consider"),
    ],
}
_RULES = {s: [(w, re.compile(p, re.I)) for w, p in lst] for s, lst in _RULES.items()}

# Strong starters used to cut an unpunctuated dictation into clauses (only when the part before has >= 4 words).
_CUT = re.compile(
    r"(?<![\w])(?:اوصي|نوصي|نحتاج|يرجي|ارجو|تم\s+(?:ال)?اعطاء|اعطيت|اعطينا|الحاله\s+(?:حرجه|مستقره|غير\s+مستقره)|"
    r"تاريخ(?:ه|ها)?\s*(?:ال)?مرضي|مصاب\s+ب|admitted|please|recommend|gave|history\s+of|known\s+case)(?![\w])", re.I)
_SENT = re.compile(r"(?<=[.!?؟؛])\s+|\n+")
_VITAL_FALLBACK = re.compile(r"spo2|sat\b|اكسجين|تشبع|حراره|نبض|ضغط|\b\d{2,3}\s*/\s*\d{2,3}\b|\b3[5-9](?:\.\d)?\s*(?:°|c\b|درجه)", re.I)


def _sentences(text):
    return [s.strip() for s in _SENT.split(text) if s and s.strip()]


def _words(s):
    return len(re.findall(r"\w+", s))


def _cut_clauses(sentence):
    """Cut a long unpunctuated sentence before strong section starters."""
    norm = normalize(sentence)
    cuts = [m.start() for m in _CUT.finditer(norm) if _words(sentence[:m.start()]) >= 4]
    if not cuts:
        return [sentence]
    parts, prev = [], 0
    for c in cuts:
        parts.append(sentence[prev:c].strip()); prev = c
    parts.append(sentence[prev:].strip())
    return [p for p in parts if p]


def _score(norm, n_vitals):
    sc = {s: 0.0 for s in SECTIONS}
    for s, rules in _RULES.items():
        for w, rx in rules:
            if rx.search(norm):
                sc[s] += w
    if n_vitals:
        sc["S"] += 1.0
        sc["A"] += 1.0
    return sc


def _has_header(norm):
    return {s for s, rx in HEADERS.items() if rx.search(norm)}


def _by_headers(text, norm):
    """-> [(section | None, chunk_text)] cut at spoken headers; header words are removed from the chunk."""
    hits = sorted((m.start(), m.end(), s) for s, rx in HEADERS.items() for m in rx.finditer(norm))
    # drop overlapping hits (keep the earliest)
    clean, last_end = [], -1
    for st, en, s in hits:
        if st >= last_end:
            clean.append((st, en, s)); last_end = en
    out = []
    if clean and text[:clean[0][0]].strip():
        out.append((None, text[:clean[0][0]].strip()))
    for i, (st, en, s) in enumerate(clean):
        end = clean[i + 1][0] if i + 1 < len(clean) else len(text)
        body = _AFTER_HEADER.sub("", text[en:end]).strip()
        if body:
            out.append((s, body))
    return out


def split_sbar(text, count_vitals=None):
    """Split `text` into SBAR clauses.

    count_vitals: optional callable(clause_text) -> number of vital signs found in it (the app passes extract.analyze);
    without it a small regex stands in.
    Returns {"mode": "headers"|"rules", "items": [{"text","section","how","review"}], "sections": {S:[...],...}}.
    """
    text = (text or "").strip()
    norm = normalize(text)
    nv = count_vitals or (lambda t: 1 if _VITAL_FALLBACK.search(normalize(t)) else 0)
    items = []
    prev = None  # last decided section

    def add(clause, section, how, review):
        nonlocal prev
        items.append({"text": clause, "section": section, "how": how, "review": review})
        prev = section

    def classify(clause):
        n = normalize(clause)
        sc = _score(n, nv(clause))
        for s in SECTIONS:                       # prefer the usual S->B->A->R flow a little
            if prev is None:
                sc[s] += 0.3 if s == "S" else 0.0
            elif IDX[s] >= IDX[prev]:
                sc[s] += 0.4 + (0.2 if s == prev else 0.0)
        ranked = sorted(sc.items(), key=lambda kv: -kv[1])
        (best, top), (_, second) = ranked[0], ranked[1]
        if top < 1.0:                            # nothing recognised: carry the previous section, flag it
            return (prev or "S"), "carry", True
        return best, "rule", (top - second) < 1.0 or top < 2.0

    headers = _has_header(norm)
    if len(headers) >= 2:
        mode = "headers"
        for sec, chunk in _by_headers(text, norm):
            for sent in _sentences(chunk):
                if sec is None:
                    s, how, rv = classify(sent); add(sent, s, how, True)
                else:
                    add(sent, sec, "header", False)
    else:
        mode = "rules"
        for sent in _sentences(text):
            for clause in _cut_clauses(sent):
                s, how, rv = classify(clause)
                add(clause, s, how, rv)
    sections = {s: [i["text"] for i in items if i["section"] == s] for s in SECTIONS}
    return {"mode": mode, "items": items, "sections": sections}
