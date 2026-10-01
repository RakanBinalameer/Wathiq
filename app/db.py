import sqlite3
from flask import g, current_app

SCHEMA = """
CREATE TABLE IF NOT EXISTS drugs(
  id INTEGER PRIMARY KEY, key TEXT UNIQUE NOT NULL,
  generic_en TEXT NOT NULL, generic_ar TEXT, brands TEXT DEFAULT '',
  source TEXT NOT NULL,              -- curated_sample | synthea_rxnorm | sfda_import
  sfda_verified INTEGER DEFAULT 0,   -- 1 only for rows imported from an official SFDA file
  max_single_dose_mg REAL, ambiguous INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS drug_products(
  id INTEGER PRIMARY KEY, drug_id INTEGER NOT NULL REFERENCES drugs(id),
  rxnorm TEXT, display TEXT NOT NULL, strength TEXT, form TEXT);
CREATE INDEX IF NOT EXISTS ix_products_drug ON drug_products(drug_id);
CREATE TABLE IF NOT EXISTS drug_aliases(alias TEXT NOT NULL, drug_id INTEGER NOT NULL REFERENCES drugs(id),
  PRIMARY KEY(alias, drug_id));
CREATE TABLE IF NOT EXISTS diagnoses(
  id INTEGER PRIMARY KEY, icd10 TEXT, name_en TEXT NOT NULL, name_ar TEXT,
  snomed TEXT, snomed_verified INTEGER DEFAULT 0, source TEXT NOT NULL);
CREATE UNIQUE INDEX IF NOT EXISTS ux_dx ON diagnoses(name_en, COALESCE(icd10,''));
CREATE TABLE IF NOT EXISTS diagnosis_aliases(alias TEXT NOT NULL, dx_id INTEGER NOT NULL REFERENCES diagnoses(id),
  PRIMARY KEY(alias, dx_id));
CREATE TABLE IF NOT EXISTS users(
  id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL, password_hash TEXT NOT NULL,
  display_name TEXT NOT NULL, role TEXT NOT NULL DEFAULT 'nurse');
CREATE TABLE IF NOT EXISTS notes(
  id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id),
  patient_ref TEXT, allergies TEXT, transcript TEXT NOT NULL,
  confirmed_json TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE INDEX IF NOT EXISTS ix_notes_user ON notes(user_id, id DESC);
CREATE TABLE IF NOT EXISTS audit(
  id INTEGER PRIMARY KEY, ts TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  user_id INTEGER, action TEXT NOT NULL, detail TEXT);
CREATE TABLE IF NOT EXISTS kb_meta(key TEXT PRIMARY KEY, value TEXT);
"""


def connect(path):
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("PRAGMA journal_mode=WAL")
    return con


def init_schema(con):
    con.executescript(SCHEMA)
    con.commit()


def get_db():
    if "db" not in g:
        g.db = connect(current_app.config["DATABASE"])
    return g.db


def close_db(_exc=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def audit(action, detail="", user_id=None):
    db = get_db()
    db.execute("INSERT INTO audit(user_id, action, detail) VALUES (?,?,?)", (user_id, action, detail[:500]))
    db.commit()
