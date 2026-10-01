"""In-memory index over the SQLite knowledge base (exact + fuzzy lookup)."""
import json
from collections import defaultdict

from rapidfuzz import fuzz, process

from .textnorm import is_arabic, norm_phrase


class KnowledgeBase:
    def __init__(self, con):
        self.drugs, self.dx = {}, {}
        for r in con.execute("SELECT * FROM drugs"):
            self.drugs[r["id"]] = dict(r, products=[])
        for r in con.execute("SELECT drug_id, rxnorm, display, strength, form FROM drug_products"):
            self.drugs[r["drug_id"]]["products"].append(dict(r))
        for r in con.execute("SELECT * FROM diagnoses"):
            self.dx[r["id"]] = dict(r)
        self.drug_alias = defaultdict(set)
        self.dx_alias = defaultdict(set)
        for r in con.execute("SELECT alias, drug_id FROM drug_aliases"):
            self.drug_alias[r["alias"]].add(r["drug_id"])
        for r in con.execute("SELECT alias, dx_id FROM diagnosis_aliases"):
            self.dx_alias[r["alias"]].add(r["dx_id"])
        self._choices = {}
        for kind, amap in (("drug", self.drug_alias), ("dx", self.dx_alias)):
            ar = [a for a in amap if is_arabic(a)]
            lat = [a for a in amap if not is_arabic(a)]
            self._choices[kind] = {True: ar, False: lat}
        row = con.execute("SELECT value FROM kb_meta WHERE key='stats'").fetchone()
        self.stats = json.loads(row["value"]) if row else {}

    # ---- lookups -------------------------------------------------------------------------
    def _amap(self, kind):
        return self.drug_alias if kind == "drug" else self.dx_alias

    def exact(self, kind, phrase):
        return sorted(self._amap(kind).get(phrase, ()))

    def fuzzy(self, kind, phrase, cutoff=84, limit=6):
        """Return [(entity_id, best_alias, score)] de-duplicated per entity, best first."""
        pool = self._choices[kind][is_arabic(phrase)]
        amap = self._amap(kind)
        best = {}
        for alias, score, _ in process.extract(phrase, pool, scorer=fuzz.ratio, limit=limit * 2, score_cutoff=cutoff):
            for eid in amap[alias]:
                if eid not in best or score > best[eid][1]:
                    best[eid] = (alias, score)
        return sorted(((eid, a, s) for eid, (a, s) in best.items()), key=lambda x: -x[2])[:limit]

    # ---- presentation --------------------------------------------------------------------
    def drug_view(self, did):
        d = self.drugs[did]
        codes = sorted({p["rxnorm"] for p in d["products"] if p["rxnorm"]})
        return {"kind": "drug", "id": did, "label": d["generic_en"], "label_ar": d["generic_ar"],
                "brands": d["brands"], "code": codes[0] if codes else None, "code_system": "RxNorm" if codes else None,
                "products": len(d["products"]), "sfda_verified": bool(d["sfda_verified"]), "source": d["source"],
                "ambiguous": bool(d["ambiguous"])}

    def dx_view(self, did):
        d = self.dx[did]
        return {"kind": "dx", "id": did, "label": d["name_en"], "label_ar": d["name_ar"], "code": d["icd10"],
                "code_system": "ICD-10" if d["icd10"] else None, "snomed": d["snomed"],
                "snomed_verified": bool(d["snomed_verified"]), "source": d["source"]}

    def search(self, q, limit=8):
        """Free-text KB search for the browser page: exact hits first, then fuzzy."""
        phrase = norm_phrase(q)
        out = []
        if not phrase:
            return out
        for kind, view in (("drug", self.drug_view), ("dx", self.dx_view)):
            ids = [(i, 100) for i in self.exact(kind, phrase)]
            if len(phrase) >= 4:
                ids += [(i, s) for i, _a, s in self.fuzzy(kind, phrase, cutoff=70, limit=limit) if i not in {x for x, _ in ids}]
            # substring hits (typing "para" should find paracetamol)
            if len(phrase) >= 3:
                for alias, eids in self._amap(kind).items():
                    if phrase in alias:
                        ids += [(i, 90) for i in eids if i not in {x for x, _ in ids}]
            for i, s in sorted(ids, key=lambda x: -x[1])[:limit]:
                v = view(i)
                v["score"] = round(s)
                out.append(v)
        return out[: limit * 2]
