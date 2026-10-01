"""Optional server-side speech-to-text (faster-whisper). Falls back gracefully when not installed.

For production, point WHISPER_MODEL at your fine-tuned, CTranslate2-converted model directory
(see training/finetune_whisper.py). Audio never leaves your server in this mode.
"""
import os
import tempfile

_model = None
_tried = False


def available() -> bool:
    return _load() is not None


def _load():
    global _model, _tried
    if _tried:
        return _model
    _tried = True
    name = os.environ.get("WHISPER_MODEL")
    if not name:
        return None
    try:
        from faster_whisper import WhisperModel
        _model = WhisperModel(name, device=os.environ.get("WHISPER_DEVICE", "cpu"), compute_type="int8")
    except Exception:  # library or model missing
        _model = None
    return _model


def build_prompt(kb, limit=60):
    """Whisper 'initial_prompt' biasing: list frequent drug names so they are spelled as in the KB."""
    names = [d["generic_en"] for d in kb.drugs.values() if d["source"] == "curated_sample"][:limit]
    return "ملاحظة تمريضية: " + ", ".join(names)


def transcribe(file_storage, kb):
    model = _load()
    if model is None:
        raise RuntimeError("stt_unavailable")
    with tempfile.NamedTemporaryFile(suffix=".webm") as tmp:
        file_storage.save(tmp.name)
        segments, _info = model.transcribe(tmp.name, initial_prompt=build_prompt(kb), vad_filter=True)
        return " ".join(s.text.strip() for s in segments).strip()
