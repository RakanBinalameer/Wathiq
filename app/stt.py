"""Speech-to-text with OpenAI Whisper, run on this server through sherpa-onnx (CPU, int8).

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

Accuracy on real nurse speech has NOT been measured.
"""
import io
import os
import re
import threading
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


def available() -> bool:
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


def transcribe(data: bytes) -> dict:
    """Return {"text": str, "languages": [codes in order of appearance]}. Language is auto-detected."""
    if not available():
        raise SttUnavailable("stt unavailable")
    import numpy as np
    samples = read_wav(data)
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
