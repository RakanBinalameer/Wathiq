"""Extract conditions (SNOMED-CT) and medications (RxNorm) from Synthea's module JSON files."""
import json, sys
from pathlib import Path

def walk(obj, found):
    if isinstance(obj, dict):
        t = obj.get("type")
        if t in ("ConditionOnset", "MedicationOrder", "Procedure", "AllergyOnset") and "codes" in obj:
            for c in obj["codes"]:
                found.append((t, str(c.get("system")), str(c.get("code")), str(c.get("display"))))
        for v in obj.values():
            walk(v, found)
    elif isinstance(obj, list):
        for v in obj:
            walk(v, found)

def extract(modules_dir):
    found = []
    for p in Path(modules_dir).rglob("*.json"):
        try:
            walk(json.loads(p.read_text(encoding="utf-8")), found)
        except Exception as e:
            print("skip", p.name, e, file=sys.stderr)
    return found

if __name__ == "__main__":
    f = extract(sys.argv[1])
    uniq = sorted(set(f))
    out = Path(sys.argv[2]); out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps([dict(type=a, system=b, code=c, display=d) for a, b, c, d in uniq], ensure_ascii=False, indent=1))
    from collections import Counter
    print(Counter((a, b) for a, b, _, _ in uniq))
