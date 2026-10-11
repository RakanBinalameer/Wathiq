# Wathiq (وثّق)

English · [العربية](README.ar.md)

A hackathon prototype that turns a nurse's **spoken dictation** (Arabic, English, or mixed) into a structured note:
medications with dose / route / frequency, diagnoses (ICD-10), and vital signs, matched against a medical knowledge base.
**The nurse then reviews and confirms** the note before anything is saved.

**Demo video:** https://drive.google.com/file/d/1q0BAvxiXYxhFO5Tej8vwGRxMts7qUcjp/view?usp=sharing

**Published demo:** https://wathiq.pythonanywhere.com. Fake data only; do not enter real patient data. Analysis accuracy on real nurses' speech has not been measured yet.

## Quick start
```bash
pip install -r requirements.txt
python wsgi.py            # first run creates the database and prints the password for user nurse1 once
# http://127.0.0.1:5000
python -m pytest -q       # 126 tests
```
Alternatives: `DEMO_PASSWORD=... python scripts/seed_kb.py` to rebuild the database, or `docker build -t nursevoice . && docker run -p 8000:8000 -e SECRET_KEY=... nursevoice`.
The UI has "try an example" buttons that work without a microphone (Arabic, English, mixed with an allergy, overdose).

## How it works, in plain words
1. The nurse speaks or types their note.
2. The system normalizes the text (Arabic letter variants, digits) and looks up every name in a **fixed knowledge base**. "بنادول", "باراسيتامول" and "acetaminophen" resolve to the same drug.
3. It picks up dose, route, frequency, and vital signs.
4. It warns about: a dose above the usual, a patient allergy, two look-alike names, an impossible value.
5. Anything not 100% certain is marked "needs review", and the note is saved only after the nurse confirms. The system **searches** the knowledge base and never **generates** drugs of its own.

## Structure
| Path | Purpose |
|---|---|
| `app/textnorm.py` | Arabic/English normalization (keeps character positions for highlighting) |
| `app/extract.py` | Matching (exact first, then fuzzy), doses, vital signs, safety checks |
| `app/seed.py` + `scripts/seed_kb.py` | Builds the knowledge base: curated sample + Synthea + optional SFDA import |
| `app/views.py` | Pages and API endpoints (the server re-validates everything that is saved) |
| `app/stt.py` | Speech-to-text: Gemini (cloud) first if `GEMINI_API_KEY` is set, with local Whisper (sherpa-onnx, CPU) as the fallback; Whisper is the default without the key |
| `training/gen_corpus.py` | Generates synthetic dictation sentences (recording script + LLM fine-tuning data) |
| `training/finetune_whisper.py` | Whisper fine-tuning recipe (not run here) |
| `tests/` | Tests for extraction, security, saving, and seeding |

## What was actually done and what was not (honestly)
**Implemented and tested**
- Knowledge base: 266 drugs and 257 diagnoses. From Synthea: 461 RxNorm products (after excluding 56 that could not be parsed, such as contraceptive packs) and 204 SNOMED diagnoses. `data/synthea_codes.json` is extracted from Synthea modules (Apache-2.0).
- 53 hand-curated diagnoses with ICD-10 (WHO) codes and Arabic synonyms. Of the SNOMED codes attached to these 53, **only 26 were checked against Synthea**; the other 27 are flagged `snomed_verified=0`.
- Extraction, alerts, the confirmation gate, saving with server-side re-validation, and per-patient note views for all nurses (every open of another author's note or of a patient file is written to the audit log; there is no ward or care-team restriction yet). Analysis time is about 2 ms.
- Security: CSRF, strict CSP with no inline code, protective headers, 30-minute sessions, hashed passwords, rate limiting, note text is never written to the audit log, escaped Jinja/DOM output.

**Not implemented / needs work before any clinical use**
- **The official SFDA list is not integrated**: the build environment had no network access. The "curated" drugs are a common sample with well-known trade names, not a check against SFDA registration (`sfda_verified=0` for all). To import the official file: `python scripts/seed_kb.py --sfda-csv FILE.csv` (the column names `generic_name,trade_name` are adjustable in `import_sfda_csv`; tested on a sample file only).
- **Whisper/LLM fine-tuning was not done**: it needs a GPU and real recordings from nurses with several dialects. What exists: a recording-sentence generator and the fine-tuning script (only its wording was checked). The local model is the stock Whisper (not fine-tuned) and is the default without a Gemini key, and the fallback when it is set. Running a fine-tuned model requires converting it to ONNX for sherpa-onnx (not tried). Current extraction is rules plus fuzzy matching, not a language model.
- The 100% figure in `gen_corpus.py` is **circular** (the sentences are generated from the knowledge base itself), so it is a regression test, not real accuracy. A real evaluation on actual recordings is needed, using WER and drug-name accuracy.
- Voice dictation runs on an engine on the server (there is no browser alternative): Gemini first if the key is set, otherwise local Whisper, with Whisper as the fallback if the connection to Google fails. The Whisper model is not in the repository: install it with `python scripts/get_whisper.py small`. `WHISPER_SIZE` picks the size (tiny/base/small); larger is more accurate and slower. **Accuracy on real nurses' speech has not been measured**, and speed depends on the server CPU (small on two cores is roughly real time). Without the model files, voice dictation stops and typing still works.
  - **Language is detected automatically** (there is no language picker): the recording is split at half-second pauses and each segment's language (Arabic/English) is detected, so an Arabic segment and an English segment in one recording are each transcribed in their own language.
  - **Optional cloud engine:** if `GEMINI_API_KEY` is set, the whole recording is sent to Google Gemini (`gemini-3.5-transcribe`, with automatic language detection and drug names as custom vocabulary); Google's documentation says it switches between languages when a speaker mixes them. **The audio leaves the server**, and on the free tier the content may be used to improve Google products, so it must not be used with real patient data. If the connection fails, the local model is used if installed. The key goes in an environment variable or a private file, never in the code or on GitHub. It is enabled on the published demo (visible at `/api/stt/status`), **and its accuracy has not been measured**. Free-tier limits are unconfirmed (user reports mention about 100 requests a day for this model). Connections to `.googleapis.com` are allowed on the PythonAnywhere free plan.
  - Two limits of the local model: English words inside a single Arabic sentence (no pause) may be lost or written in Arabic, and language-detection accuracy has not been measured on real dialects.
- Maximum dose limits (`max_single_dose_mg`) and vital-sign thresholds are provisional and must be approved by the pharmacy/medical team. The extra Synthea diagnoses have no ICD-10 (they are marked as such) and are left to medical coding.
- No integration with a hospital information system (HIS/EMR) or a payment system; they were not required by the given specification. There is an **export** in HL7 FHIR R4 format for every saved note (`GET /api/notes/<id>/fhir`: `MedicationStatement`, `Condition`, `Observation`), which is a file ready for exchange, not a live connection to any system. Its structure was checked with `fhir.resources` (R4B) models only, not with the official FHIR validator or against any server or Saudi implementation guide.
- For production: HTTPS (`FORCE_HTTPS=1`), a fixed `SECRET_KEY`, a managed database instead of SQLite, a shared rate-limit store (Redis), encryption at rest, and cybersecurity and data-protection approval (PDPL / NCA).

## Author
Rakan
