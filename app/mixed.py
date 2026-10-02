"""English ward words that Arabic dictation wrote in Arabic letters ('بيشنت' = patient).

Design (clinical setting, so deliberately conservative):
 * SUGGESTIONS ONLY. The nurse's text is never rewritten automatically; the UI offers a replace button.
 * Lookup in a small curated lexicon. Nothing is guessed or generated; a word that is not listed is left alone.
 * The lexicon is PROVISIONAL: spellings were written by hand, not measured on real nurse dictation.
   Speech engines spell the same English word in many different ways, so coverage is expected to be low
   until the lexicon is grown from real recordings.
 * Tokens that already became a drug / diagnosis / vital are skipped, so drug names are never touched.
"""
from .textnorm import TOKEN_RE, normalize, strip_marks

# English word -> Arabic-letter spellings seen (or expected) from Arabic speech recognition.
LEXICON = {
    "patient": ["بيشنت", "بيشينت", "بيشانت", "باشنت"],
    "support": ["سبورت", "سابورت"],
    "because": ["بيكوز", "بيكاوز"],
    "understand": ["اندرستاند", "اندرستند"],
    "history": ["هستوري", "هيستوري"],
    "allergy": ["الرجي", "اليرجي", "الرجى"],
    "pressure": ["بريشر", "بريشير"],
    "sugar": ["شوقر", "شوغر", "شوكر"],
    "saturation": ["ساتشوريشن", "ساتوريشن", "ساتورايشن"],
    "temperature": ["تمبريتشر", "تيمبريتشر", "تمبرتشر"],
    "pulse": ["بالس"],
    "drip": ["دريب"],
    "discharge": ["دسشارج", "ديسشارج", "دشارج"],
    "admit": ["ادميت", "ادمت"],
    "consult": ["كونسلت", "كنسلت"],
    "report": ["ريبورت", "ريبوت"],
    "result": ["ريزلت", "ريزالت"],
    "stable": ["ستيبل", "ستابل"],
    "unstable": ["انستيبل", "انستابل"],
    "critical": ["كريتيكال", "كريتكل"],
    "monitor": ["مونيتر", "مونتر"],
    "order": ["اوردر"],
    "sample": ["سامبل"],
    "lab": ["لاب"],
}

# Arabic clitics that glue onto the word: ال (the), وال (and the), و (and)
_PREFIXES = ("وال", "ال", "و")

_MAP = {}
for _en, _spellings in LEXICON.items():
    for _s in _spellings:
        _MAP[normalize(_s)] = _en


def find_translit(text, skip_spans=()):
    """Return [{text, start, end, term, suggest}] for lexicon words written in Arabic letters.

    `text` must already be mark-stripped (offsets point into it). `skip_spans` are (start, end) ranges
    already claimed by drugs / diagnoses / vitals. `suggest` is the exact replacement for [start, end):
    the English word, keeping a separate Arabic 'و' ("and") when the token started with it.
    """
    text = strip_marks(text)
    out = []
    for m in TOKEN_RE.finditer(text):
        s, e = m.start(), m.end()
        if any(s < b and e > a for a, b in skip_spans):
            continue
        tok = normalize(m.group())
        term, waw = _MAP.get(tok), False
        if term is None:
            for p in _PREFIXES:
                if tok.startswith(p) and tok[len(p):] in _MAP:
                    term, waw = _MAP[tok[len(p):]], p.startswith("و")
                    break
        if term:
            out.append({"text": m.group(), "start": s, "end": e, "term": term,
                        "suggest": ("و " if waw else "") + term})
    return out
