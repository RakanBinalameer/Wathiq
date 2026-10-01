"""Knowledge-base seeding: curated CSVs + Synthea (RxNorm/SNOMED) + optional official SFDA export."""
import csv
import json
import re
import sqlite3
from pathlib import Path

from werkzeug.security import generate_password_hash

from .db import init_schema
from .textnorm import norm_phrase

SALT_WORDS = {
    "hydrochloride", "sodium", "potassium", "sulfate", "sulphate", "succinate", "acetate", "maleate",
    "tartrate", "hydrobromide", "phosphate", "citrate", "mesylate", "besylate", "fumarate", "calcium",
    "hcl", "bromide", "chloride", "nitrate", "oxalate", "hydrate", "anhydrous",
}
STRENGTH_RE = re.compile(r"\d[\d.,]*\s*(?:MG|MCG|UNT|ML|MEQ|G|%|IU|ACTUAT)(?:/(?:ML|HR|ACTUAT|MG|G))?", re.I)


def parse_rxnorm_display(display: str) -> dict:
    """'Acetaminophen 325 MG / Oxycodone Hydrochloride 5 MG Oral Tablet [Percocet]' -> parts."""
    brand_m = re.search(r"\[([^\]]+)\]\s*$", display)
    brand = brand_m.group(1) if brand_m else ""
    d = re.sub(r"\s*\[[^\]]+\]\s*$", "", display)
    d = re.sub(r"^\d+(?:\.\d+)?\s*(?:ML|MG|G)\s+", "", d, flags=re.I)  # leading '1 ML '
    ingredients = []
    for part in d.split(" / "):
        name = []
        for w in part.split():
            if w[0].isdigit():
                break
            name.append(w)
        ingredients.append(" ".join(name).lower())
    strengths = STRENGTH_RE.findall(d)
    last = None
    for last in STRENGTH_RE.finditer(d):
        pass
    form = d[last.end():].strip() if last else ""
    return {"ingredients": ingredients, "brand": brand, "strength": " / ".join(s.strip() for s in strengths), "form": form}


def strip_salts(name: str) -> str:
    return " ".join(w for w in name.split() if w not in SALT_WORDS).strip()


def _split(s):
    return [x.strip() for x in (s or "").split("|") if x.strip()]


def _add_drug_alias(con, drug_id, alias):
    a = norm_phrase(alias)
    if a:
        con.execute("INSERT OR IGNORE INTO drug_aliases(alias, drug_id) VALUES (?,?)", (a, drug_id))


def _add_dx_alias(con, dx_id, alias):
    a = norm_phrase(alias)
    if a:
        con.execute("INSERT OR IGNORE INTO diagnosis_aliases(alias, dx_id) VALUES (?,?)", (a, dx_id))


def load_curated_drugs(con, path):
    n = 0
    with open(path, encoding="utf-8", newline="") as f:
        for r in csv.DictReader(f):
            cur = con.execute(
                "INSERT INTO drugs(key, generic_en, generic_ar, brands, source, sfda_verified, max_single_dose_mg, ambiguous)"
                " VALUES (?,?,?,?,?,0,?,?)",
                (r["generic_en"].lower(), r["generic_en"], r["generic_ar"], r["brands_en"],
                 "curated_sample", float(r["max_single_dose_mg"]) if r["max_single_dose_mg"] else None,
                 int(r["ambiguous"] or 0)))
            did = cur.lastrowid
            for a in [r["generic_en"], r["generic_ar"], *_split(r["brands_en"]), *_split(r["aliases_ar"])]:
                _add_drug_alias(con, did, a)
            n += 1
    return n


def load_curated_dx(con, path, synthea_snomed):
    n = 0
    with open(path, encoding="utf-8", newline="") as f:
        for r in csv.DictReader(f):
            verified = 1 if r["snomed"] in synthea_snomed else 0
            cur = con.execute(
                "INSERT INTO diagnoses(icd10, name_en, name_ar, snomed, snomed_verified, source) VALUES (?,?,?,?,?,?)",
                (r["icd10"], r["name_en"], r["name_ar"], r["snomed"] or None, verified, "curated_sample"))
            did = cur.lastrowid
            for a in [r["name_en"], r["name_ar"], *_split(r["aliases"])]:
                _add_dx_alias(con, did, a)
            n += 1
    return n


def load_synthea(con, codes_json):
    """Merge Synthea RxNorm medications and SNOMED disorders into the KB."""
    data = json.loads(Path(codes_json).read_text(encoding="utf-8"))
    alias_to_drug = {}
    for row in con.execute("SELECT alias, drug_id FROM drug_aliases"):
        alias_to_drug.setdefault(row["alias"], row["drug_id"])
    seen_rx, attached, created, skipped = set(), 0, 0, 0
    for item in data:
        if item["type"] != "MedicationOrder" or item["system"] != "RxNorm" or item["code"] in seen_rx:
            continue
        seen_rx.add(item["code"])
        if item["display"].lstrip().startswith("{"):  # RxNorm kit/pack notation (e.g. contraceptive packs)
            skipped += 1
            continue
        p = parse_rxnorm_display(item["display"])
        ings = [re.sub(r"\s*\((?:usp|nf)\)", "", strip_salts(i)).strip() for i in p["ingredients"] if i]
        if not ings or any(re.fullmatch(r"nda\d+|", i) for i in ings):  # unparseable ingredient
            skipped += 1
            continue
        drug_id = alias_to_drug.get(norm_phrase(ings[0])) if len(ings) == 1 else None
        if drug_id:
            attached += 1
        else:
            key = " + ".join(sorted(ings))
            row = con.execute("SELECT id FROM drugs WHERE key=?", (key,)).fetchone()
            if row:
                drug_id = row["id"]
            else:
                cur = con.execute(
                    "INSERT INTO drugs(key, generic_en, generic_ar, brands, source) VALUES (?,?,?,?,?)",
                    (key, " + ".join(ings), None, p["brand"], "synthea_rxnorm"))
                drug_id = cur.lastrowid
                created += 1
                _add_drug_alias(con, drug_id, " ".join(ings))
                alias_to_drug[norm_phrase(" ".join(ings))] = drug_id
            if p["brand"]:
                _add_drug_alias(con, drug_id, p["brand"])
        con.execute("INSERT INTO drug_products(drug_id, rxnorm, display, strength, form) VALUES (?,?,?,?,?)",
                    (drug_id, item["code"], item["display"], p["strength"], p["form"]))
    known_snomed = {r["snomed"] for r in con.execute("SELECT snomed FROM diagnoses WHERE snomed IS NOT NULL")}
    known_alias = {r["alias"] for r in con.execute("SELECT alias FROM diagnosis_aliases")}
    dx_new = 0
    for item in data:
        if item["type"] != "ConditionOnset" or item["system"] != "SNOMED-CT":
            continue
        disp = item["display"]
        if not disp.endswith("(disorder)") or item["code"] in known_snomed:
            continue
        name = disp[: -len("(disorder)")].strip()
        if norm_phrase(name) in known_alias:
            continue
        cur = con.execute(
            "INSERT OR IGNORE INTO diagnoses(icd10, name_en, name_ar, snomed, snomed_verified, source) VALUES (NULL,?,?,?,1,?)",
            (name, None, item["code"], "synthea_snomed"))
        if cur.rowcount:
            _add_dx_alias(con, cur.lastrowid, name)
            known_snomed.add(item["code"])
            known_alias.add(norm_phrase(name))
            dx_new += 1
    return {"rxnorm_products": len(seen_rx), "attached_to_curated": attached, "new_drugs": created,
            "skipped_unparseable": skipped, "new_dx": dx_new}


def import_sfda_csv(con, path, col_generic="generic_name", col_trade="trade_name", col_reg="registration_no"):
    """Import an official SFDA drug-list export (CSV). Column names are configurable because the
    portal's export format may change. Matching rows are marked sfda_verified=1."""
    alias_to_drug = {r["alias"]: r["drug_id"] for r in con.execute("SELECT alias, drug_id FROM drug_aliases")}
    matched = created = 0
    with open(path, encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            generic = strip_salts((r.get(col_generic) or "").lower())
            trade = (r.get(col_trade) or "").strip()
            if not generic:
                continue
            did = alias_to_drug.get(norm_phrase(generic))
            if did:
                con.execute("UPDATE drugs SET sfda_verified=1 WHERE id=?", (did,))
                matched += 1
            else:
                cur = con.execute("INSERT OR IGNORE INTO drugs(key, generic_en, brands, source, sfda_verified)"
                                  " VALUES (?,?,?,?,1)", (generic, generic, trade, "sfda_import"))
                did = cur.lastrowid or con.execute("SELECT id FROM drugs WHERE key=?", (generic,)).fetchone()["id"]
                _add_drug_alias(con, did, generic)
                alias_to_drug[norm_phrase(generic)] = did
                created += 1
            if trade:
                _add_drug_alias(con, did, trade)
                alias_to_drug[norm_phrase(trade)] = did
    return {"sfda_matched": matched, "sfda_created": created}


def create_user(con, username, password, display_name, role="nurse"):
    con.execute("INSERT OR REPLACE INTO users(username, password_hash, display_name, role) VALUES (?,?,?,?)",
                (username, generate_password_hash(password), display_name, role))


def build_kb(db_path, data_dir, sfda_csv=None):
    data_dir = Path(data_dir)
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    for t in ("drug_aliases", "drug_products", "drugs", "diagnosis_aliases", "diagnoses", "kb_meta"):
        con.execute(f"DROP TABLE IF EXISTS {t}")  # keep users/notes/audit across re-seeds
    init_schema(con)
    codes = data_dir / "synthea_codes.json"
    synthea_snomed = {i["code"] for i in json.loads(codes.read_text(encoding="utf-8")) if i["system"] == "SNOMED-CT"} if codes.exists() else set()
    stats = {"curated_drugs": load_curated_drugs(con, data_dir / "drugs_curated.csv"),
             "curated_dx": load_curated_dx(con, data_dir / "icd10_curated.csv", synthea_snomed)}
    if codes.exists():
        stats.update(load_synthea(con, codes))
    if sfda_csv:
        stats.update(import_sfda_csv(con, sfda_csv))
    stats["drugs_total"] = con.execute("SELECT COUNT(*) FROM drugs").fetchone()[0]
    stats["dx_total"] = con.execute("SELECT COUNT(*) FROM diagnoses").fetchone()[0]
    con.execute("INSERT INTO kb_meta(key, value) VALUES ('stats', ?)", (json.dumps(stats),))
    con.commit()
    return con, stats
