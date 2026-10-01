#!/usr/bin/env python3
"""Generate synthetic nurse-dictation sentences (Arabic / English / code-switched) from the KB.

Outputs (in training/out/):
  recording_script.txt  one sentence per line -> have nurses read these aloud to record Whisper training audio
  llm_sft.jsonl         {"text":..., "entities":[...]} gold labels by construction -> LLM / extractor fine-tuning
It also runs the current extractor over the corpus and prints its recall, which is a cheap regression
metric for the KB + rules (it does NOT replace evaluation on real nurse recordings).
"""
import argparse, json, random, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from app.db import connect          # noqa: E402
from app.extract import analyze     # noqa: E402
from app.kb import KnowledgeBase    # noqa: E402

ROUTE = {"IV": ("وريدي", "IV"), "PO": ("فموي", "PO"), "IM": ("عضلي", "IM"), "SC": ("تحت الجلد", "SC")}
FREQ = {"BID": ("مرتين يوميا", "BID"), "TID": ("ثلاث مرات يوميا", "TID"), "QD": ("مرة يوميا", "daily"),
        "q8h": ("كل 8 ساعات", "q8h"), "q12h": ("كل 12 ساعة", "q12h")}
DOSES = {"paracetamol": [(500, "mg"), (1, "g")], "ibuprofen": [(400, "mg")], "metformin": [(500, "mg"), (850, "mg")],
         "amlodipine": [(5, "mg"), (10, "mg")], "amoxicillin clavulanate": [(1, "g")], "ceftriaxone": [(1, "g"), (2, "g")],
         "omeprazole": [(20, "mg"), (40, "mg")], "atorvastatin": [(20, "mg"), (40, "mg")], "furosemide": [(40, "mg")]}
UNIT_AR = {"mg": "ملغ", "g": "جرام"}


def build(kb, n, rng):
    drugs = [(i, d) for i, d in kb.drugs.items() if d["generic_en"] in DOSES]
    dxs = [(i, d) for i, d in kb.dx.items() if d["source"] == "curated_sample" and d["name_ar"]]
    rows = []
    for _ in range(n):
        did, d = rng.choice(drugs)
        dose, unit = rng.choice(DOSES[d["generic_en"]])
        rcode = rng.choice(list(ROUTE)); fcode = rng.choice(list(FREQ))
        dxid, dx = rng.choice(dxs)
        style = rng.choice(["ar", "en", "mix"])
        brand = (d["brands"].split("|")[0] if d["brands"] else d["generic_en"])
        if style == "ar":
            name = d["generic_ar"] or d["generic_en"]
            text = f"أعطيت المريض {name} {dose} {UNIT_AR[unit]} {ROUTE[rcode][0]} {FREQ[fcode][0]} بسبب {dx['name_ar']}"
        elif style == "en":
            text = f"Gave {rng.choice([d['generic_en'], brand])} {dose} {unit} {ROUTE[rcode][1]} {FREQ[fcode][1]} for {dx['name_en'].lower()}"
        else:
            text = f"gave {rng.choice([d['generic_en'], brand])} {dose} {unit} {ROUTE[rcode][1]} {FREQ[fcode][1]}، المريض عنده {dx['name_ar']}"
        if rng.random() < .5:
            text += f". BP {rng.randint(100, 170)}/{rng.randint(60, 105)}, HR {rng.randint(60, 120)}"
        rows.append({"text": text, "entities": [
            {"type": "drug", "id": did, "label": d["generic_en"], "dose": dose, "unit": unit, "route": rcode, "freq": fcode},
            {"type": "dx", "id": dxid, "label": dx["name_en"], "icd10": dx["icd10"]}]})
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(ROOT / "instance" / "nursevoice.db"))
    ap.add_argument("-n", type=int, default=2000)
    ap.add_argument("--seed", type=int, default=7)
    a = ap.parse_args()
    kb = KnowledgeBase(connect(a.db))
    rows = build(kb, a.n, random.Random(a.seed))
    out = ROOT / "training" / "out"; out.mkdir(exist_ok=True)
    (out / "recording_script.txt").write_text("\n".join(r["text"] for r in rows), encoding="utf-8")
    with open(out / "llm_sft.jsonl", "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    hit = dx_hit = attr_ok = 0
    for r in rows:
        res = analyze(kb, r["text"])
        d = next((e for e in res["entities"] if e["type"] == "drug"), None)
        g = r["entities"][0]
        if d and d["match"]["id"] == g["id"]:
            hit += 1
            at = d["attrs"]
            attr_ok += bool(at["dose"] and at["dose"]["value"] == g["dose"] and at["route"] == g["route"] and at["freq"] == g["freq"])
        dx_hit += any(e["type"] == "dx" and e["match"]["id"] == r["entities"][1]["id"] for e in res["entities"])
    n = len(rows)
    print(f"{n} sentences -> {out}\nextractor recall on synthetic set: drug {hit/n:.1%} | dose+route+freq exact {attr_ok/n:.1%} | diagnosis {dx_hit/n:.1%}")


if __name__ == "__main__":
    main()
