"""Arabic/English text normalisation used for every KB lookup and for note analysis."""
import re
import unicodedata

_DIGITS = {**{chr(0x0660 + i): str(i) for i in range(10)}, **{chr(0x06F0 + i): str(i) for i in range(10)}, "٫": "."}
_CHAR_MAP = str.maketrans({
    "أ": "ا", "إ": "ا", "آ": "ا", "ٱ": "ا",
    "ى": "ي", "ی": "ي", "ئ": "ي",
    "ة": "ه", "ؤ": "و",
    "ک": "ك", "گ": "ك",
    "ڤ": "ف", "پ": "ب", "چ": "ج",
    **_DIGITS,
})
_MARKS = re.compile("[ً-ٰٟـ]")  # tashkeel + tatweel
# Token = word, optionally joined by . / : % - (e.g. 120/80, 98.6, covid-19, mg/dl)
# \w already covers Arabic letters/digits; an explicit Arabic block range would also swallow « ، ؛ ؟ ».
TOKEN_RE = re.compile(r"\w+(?:[./:%\-]\w+)*%?")
_PREFIXES = ("وبال", "وال", "بال", "كال", "فال", "لل", "ال")


def strip_marks(text: str) -> str:
    """Remove diacritics/tatweel once, up-front, so later normalisation keeps character offsets."""
    return _MARKS.sub("", unicodedata.normalize("NFC", text or ""))


def _lower1(c: str) -> str:
    lc = c.lower()
    return lc if len(lc) == 1 else c


def normalize(text: str) -> str:
    """Character-level normalisation. Length-preserving once strip_marks() was applied."""
    return "".join(_lower1(c) for c in strip_marks(text).translate(_CHAR_MAP))


def strip_prefix(tok: str) -> str:
    """Remove Arabic definite article / common clitics (الباراسيتامول -> باراسيتامول)."""
    if len(tok) > 4 and "؀" <= tok[0] <= "ۿ":
        for p in _PREFIXES:
            if tok.startswith(p) and len(tok) - len(p) >= 3:
                return tok[len(p):]
    return tok


def norm_token(tok: str) -> str:
    return strip_prefix(normalize(tok))


def norm_phrase(text: str) -> str:
    """Normalise a multi-word alias the same way tokens are normalised at match time."""
    return " ".join(norm_token(t) for t in TOKEN_RE.findall(strip_marks(text)))


def tokenize(text: str):
    """Return [(normalised_token, start, end)] with offsets into `text` (already mark-stripped)."""
    return [(norm_token(m.group()), m.start(), m.end()) for m in TOKEN_RE.finditer(text or "")]


def is_arabic(s: str) -> bool:
    return any("؀" <= c <= "ۿ" for c in s)
