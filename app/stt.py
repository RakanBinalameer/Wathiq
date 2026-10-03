"""Speech-to-text with Whisper (the open model), run on this server through sherpa-onnx (CPU, int8).

Audio never leaves the server. The browser records, converts to 16 kHz mono WAV and posts it to
/api/stt; this module decodes it and returns text.

Model files (see scripts/get_whisper.py):  models/whisper-<size>/<size>-encoder.int8.onnx,
<size>-decoder.int8.onnx and <size>-tokens.txt.

Environment:
  WHISPER_SIZE       tiny | base | small (default small; larger = more accurate but slower)
  WHISPER_MODEL_DIR  explicit model folder (overrides models/whisper-<size>)
  WHISPER_THREADS    CPU threads (default 2)

The language is detected automatically for every speech segment (a pause of half a second starts a new
segment), so Arabic and English parts of one recording are each decoded in their own language. A segment
detected as anything other than Arabic/English is decoded again as Arabic. Words mixed inside ONE sentence
are not handled: Whisper picks one language per segment.

Optional cloud engine (Google Gemini `gemini-3.5-transcribe`), used when GEMINI_API_KEY is set
(STT_ENGINE=local turns it off):
  GEMINI_API_KEY     the key. NEVER put it in code or in git; read it from the environment or a private file.
  GEMINI_STT_MODEL   default gemini-3.5-transcribe
It receives the whole recording with automatic language detection and drug names as custom vocabulary; Google
documents that it switches between languages when the speaker mixes them. The audio LEAVES this server (demo/fake
data only; Google's free tier may use content to improve its products). If the call fails, the local model is
used when it is installed. Not yet tested against the real API.

Accuracy on real nurse speech has NOT been measured.
"""
import base64
import io
import json
import logging
import os
import re
import threading
import urllib.error
import urllib.request
import wave
from pathlib import Path

CHUNK_SECONDS = 25         # Whisper reads at most 30 s at a time; longer audio is cut at quiet points
SILENT_RMS = 0.003         # chunks quieter than this are skipped (Whisper invents text on silence)
MAX_SECONDS = 120          # longer recordings are refused (keeps one request from hogging the CPU)
LANGUAGES = {"ar", "en"}   # languages the app expects; anything else is re-decoded as FALLBACK_LANGUAGE
FALLBACK_LANGUAGE = "ar"
PAUSE_SECONDS = 0.5        # silence this long starts a new segment (language is detected per segment)
MIN_SEGMENT_SECONDS = 2.0  # shorter pieces are merged into a neighbour: language ID is unreliable on <2 s

_ROOT = Path(__file__).resolve().parent.parent
_lock = threading.Lock()
_recognizers = {}          # language ("" = auto-detect) -> OfflineRecognizer, loaded on first use
_inference = threading.Lock()   # one decode at a time: the model is CPU-heavy


class SttUnavailable(RuntimeError):
    pass


class BadAudio(ValueError):
    pass


class RemoteError(RuntimeError):
    """The cloud engine failed and there is no local model to fall back on."""


def size() -> str:
    s = (os.environ.get("WHISPER_SIZE") or "small").strip().lower()
    return s if s in ("tiny", "base", "small") else "small"


def model_dir() -> Path:
    custom = os.environ.get("WHISPER_MODEL_DIR")
    return Path(custom) if custom else _ROOT / "models" / f"whisper-{size()}"


def _files():
    d, s = model_dir(), size()
    enc, dec, tok = d / f"{s}-encoder.int8.onnx", d / f"{s}-decoder.int8.onnx", d / f"{s}-tokens.txt"
    return (enc, dec, tok) if all(p.is_file() for p in (enc, dec, tok)) else None


def local_available() -> bool:
    """True when sherpa-onnx is installed and the model files are present."""
    if _files() is None:
        return False
    try:
        import sherpa_onnx  # noqa: F401
        import numpy  # noqa: F401
    except ImportError:
        return False
    return True


def _recognizer(language: str):
    with _lock:
        r = _recognizers.get(language)
        if r is None:
            files = _files()
            if files is None:
                raise SttUnavailable("model files missing")
            import sherpa_onnx
            enc, dec, tok = files
            r = sherpa_onnx.OfflineRecognizer.from_whisper(
                encoder=str(enc), decoder=str(dec), tokens=str(tok), language=language,
                task="transcribe", num_threads=int(os.environ.get("WHISPER_THREADS", "2")))
            _recognizers[language] = r
        return r


def read_wav(data: bytes):
    """Decode a 16-bit PCM WAV into (float32 mono samples at 16 kHz). Raises BadAudio otherwise."""
    import numpy as np
    try:
        with wave.open(io.BytesIO(data)) as w:
            ch, width, rate, n = w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()
            if width != 2 or ch not in (1, 2):
                raise BadAudio("unsupported wav format")
            if n / max(rate, 1) > MAX_SECONDS:
                raise BadAudio("recording too long")
            raw = w.readframes(n)
    except (wave.Error, EOFError) as e:
        raise BadAudio("not a wav file") from e
    x = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    if len(x) < ch:
        raise BadAudio("empty recording")
    if ch == 2:
        x = x.reshape(-1, 2).mean(axis=1)
    if rate != 16000:
        pos = np.linspace(0, len(x) - 1, max(int(len(x) * 16000 / rate), 1))
        x = np.interp(pos, np.arange(len(x)), x).astype(np.float32)
    return x


def split_chunks(x, rate=16000):
    """Cut samples into pieces of at most CHUNK_SECONDS, preferring the quietest 100 ms near each cut."""
    import numpy as np
    size_, frame = CHUNK_SECONDS * rate, rate // 10
    out, start = [], 0
    while len(x) - start > size_:
        lo, hi = start + size_ - 3 * rate, start + size_
        seg = np.abs(x[lo:hi])
        n = len(seg) // frame
        cut = lo + int(np.argmin(seg[: n * frame].reshape(n, frame).mean(axis=1))) * frame + frame // 2
        out.append(x[start:cut])
        start = cut
    out.append(x[start:])
    return out


_REPEAT = re.compile(r"(\S+)(?:\s+\1){3,}")


def clean(text: str) -> str:
    """Collapse a word looped 4+ times in a row (a known Whisper failure mode) into one."""
    return _REPEAT.sub(r"\1", text).strip()


def split_speech(x, rate=16000):
    """Cut at pauses of PAUSE_SECONDS (keeping pieces >= MIN_SEGMENT_SECONDS), then at CHUNK_SECONDS at most."""
    import numpy as np
    frame = rate * 30 // 1000
    n = len(x) // frame
    if n == 0:
        return [x]
    e = np.sqrt((x[: n * frame].reshape(n, frame) ** 2).mean(axis=1))
    quiet = e < max(SILENT_RMS, 0.08 * float(np.percentile(e, 95)))
    need, cuts, run = int(PAUSE_SECONDS * 1000 / 30), [], 0
    for i, q in enumerate(list(quiet) + [False]):
        if q:
            run += 1
        else:
            if run >= need and i - run > 0 and i < n:        # a pause strictly inside the recording
                cuts.append((i - run // 2) * frame)
            run = 0
    pieces, start = [], 0
    for c in cuts + [len(x)]:
        if pieces and c - start < MIN_SEGMENT_SECONDS * rate:    # too short: merge with the previous piece
            pieces[-1] = (pieces[-1][0], c)
        else:
            pieces.append((start, c))
        start = c
    if len(pieces) > 1 and pieces[0][1] - pieces[0][0] < MIN_SEGMENT_SECONDS * rate:   # short first piece
        pieces[1] = (pieces[0][0], pieces[1][1]); pieces.pop(0)
    out = []
    for a, b in pieces:
        out.extend(split_chunks(x[a:b], rate))
    return out


def _decode(rec, chunk):
    stream = rec.create_stream()
    stream.accept_waveform(16000, chunk)
    rec.decode_stream(stream)
    return clean(stream.result.text), (stream.result.lang or "").strip()


def _transcribe_local(samples) -> dict:
    import numpy as np
    parts, langs = [], []
    for chunk in split_speech(samples):
        if len(chunk) < 1600 or float(np.sqrt(np.mean(chunk ** 2))) < SILENT_RMS:
            continue
        with _inference:
            text, lang = _decode(_recognizer(""), chunk)
            if lang not in LANGUAGES:                    # e.g. dialect mis-detected as Persian/Urdu
                text, lang = _decode(_recognizer(FALLBACK_LANGUAGE), chunk)[0], FALLBACK_LANGUAGE
        if text:
            parts.append(text)
            if lang not in langs:
                langs.append(lang)
    return {"text": " ".join(parts).strip(), "languages": langs}


# ---- optional cloud engine ------------------------------------------------------------------------
log = logging.getLogger("wathiq.stt")
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/interactions"
GEMINI_REVISION = "2026-05-20"      # the Api-Revision shown in Google's Interactions API examples
GEMINI_TIMEOUT = 60
KEYWORD_LIMIT = 40                  # Google: up to 100 vocabulary terms work best


def remote_configured() -> bool:
    return bool(os.environ.get("GEMINI_API_KEY")) and os.environ.get("STT_ENGINE", "auto") != "local"


def available() -> bool:
    return remote_configured() or local_available()


def engine() -> str:
    """Which engine the next request tries first: "gemini", "local" or "" (none)."""
    if remote_configured():
        return "gemini"
    return "local" if local_available() else ""


def keywords(kb):
    """Drug names (English generic names from the curated list) so they are spelled as in the knowledge base."""
    if kb is None:
        return []
    names = [d["generic_en"] for d in kb.drugs.values() if d["source"] == "curated_sample" and d.get("generic_en")]
    return [n for n in names if not re.search(r"[<>\r\n]", n)][:KEYWORD_LIMIT]


def _gemini_text(res: dict) -> str:
    """Transcript out of an Interactions API response (top-level output_text, else the text steps)."""
    if isinstance(res.get("output_text"), str):
        return res["output_text"]
    texts = []
    for step in res.get("steps") or []:
        for c in (step.get("content") or []) if isinstance(step, dict) else []:
            if isinstance(c, dict) and c.get("type") == "text" and c.get("text"):
                texts.append(str(c["text"]))
    return " ".join(texts)


def _gemini_post(data: bytes, kws) -> str:
    """Send the recording to Gemini and return the transcript. Raises RemoteError (the key is never logged)."""
    body = {"model": os.environ.get("GEMINI_STT_MODEL") or "gemini-3.5-transcribe",
            "input": [{"type": "audio", "data": base64.b64encode(data).decode("ascii"), "mime_type": "audio/wav"}],
            "generation_config": {"transcription_config": {"language_codes": [], "custom_vocabulary": list(kws)}}}
    req = urllib.request.Request(GEMINI_URL, data=json.dumps(body).encode("utf-8"), method="POST", headers={
        "x-goog-api-key": os.environ["GEMINI_API_KEY"], "Content-Type": "application/json",
        "Api-Revision": GEMINI_REVISION})
    try:
        with urllib.request.urlopen(req, timeout=GEMINI_TIMEOUT) as resp:
            return _gemini_text(json.loads(resp.read().decode("utf-8")))
    except urllib.error.HTTPError as e:
        try:
            detail = (json.loads(e.read().decode("utf-8")).get("error") or {}).get("message", "")[:200]
        except Exception:
            detail = ""
        log.warning("gemini http %s %s", e.code, detail)
        raise RemoteError(f"gemini http {e.code}") from None
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
        log.warning("gemini unreachable (%s)", type(e).__name__)
        raise RemoteError("gemini unreachable") from None


def transcribe(data: bytes, kb=None) -> dict:
    """Return {"text", "languages": [codes], "engine": "gemini"|"local"}. Language is detected automatically."""
    if not available():
        raise SttUnavailable("stt unavailable")
    import numpy as np
    samples = read_wav(data)                             # validates the file and its length
    if float(np.sqrt(np.mean(samples ** 2))) < SILENT_RMS:
        return {"text": "", "languages": [], "engine": engine()}     # nothing to transcribe (and nothing to pay for)
    if remote_configured():
        try:
            return {"text": clean(_gemini_post(data, keywords(kb))), "languages": [], "engine": "gemini"}
        except RemoteError:
            if not local_available():
                raise
    out = _transcribe_local(samples)
    out["engine"] = "local"
    return out
