#!/usr/bin/env python3
"""Build the knowledge base (and a demo nurse account).

Usage:
  python scripts/seed_kb.py [--sfda-csv FILE] [--db instance/nursevoice.db]
Demo password comes from $DEMO_PASSWORD, otherwise a random one is generated and printed ONCE.
"""
import argparse, os, secrets, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from app.seed import build_kb, create_user  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--db", default=str(ROOT / "instance" / "nursevoice.db"))
ap.add_argument("--sfda-csv", help="official SFDA drug list exported as CSV")
ap.add_argument("--demo-user", default="nurse1")
args = ap.parse_args()

con, stats = build_kb(args.db, ROOT / "data", args.sfda_csv)
pw = os.environ.get("DEMO_PASSWORD")
generated = pw is None
pw = pw or secrets.token_urlsafe(9)
create_user(con, args.demo_user, pw, "ممرضة تجريبية", "nurse")
con.commit()
print("KB stats:", stats)
print(f"Demo user: {args.demo_user}" + (f" / password: {pw}   (generated - shown once)" if generated else " (password from DEMO_PASSWORD)"))
