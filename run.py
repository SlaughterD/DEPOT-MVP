"""Start the Depot Ledger MVP.   python3 run.py [--db depot.db] [--port 8000] [--empty]"""
import argparse

from depot import seed
from depot.db import connect, new_id
from depot.server import serve
from depot.service import Ledger

ap = argparse.ArgumentParser()
ap.add_argument("--db", default="depot.db")
ap.add_argument("--port", type=int, default=8000)
ap.add_argument("--host", default="127.0.0.1")
ap.add_argument("--empty", action="store_true", help="start with an empty database (no demo data)")
a = ap.parse_args()
if a.empty:
    conn = connect(a.db)
    row = conn.execute("SELECT id FROM organizations").fetchone()
    if not row:
        org = new_id()
        conn.execute("INSERT INTO organizations(id,name) VALUES (?,?)", (org, "My Depot"))
    L = Ledger(conn, row[0] if row else org)
else:
    L = seed.build(a.db)
serve(L, a.host, a.port)
