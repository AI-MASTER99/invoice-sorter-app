"""Apply migration 004 (clients + client_products tables) to Supabase.

Sends the migration body to the Supabase Management API, then runs the
verification queries from 004_verify.sql. Mirrors apply_003.py.

Usage: SUPABASE_PAT=sbp_... python scripts/apply_004.py
"""
import json
import os
import sys
import urllib.request
import urllib.error
from pathlib import Path

PROJECT_REF = "pbahlprxmlxvntfvcytd"
API = f"https://api.supabase.com/v1/projects/{PROJECT_REF}/database/query"
PAT = os.environ.get("SUPABASE_PAT")
if not PAT:
    print("ERROR: SUPABASE_PAT env var not set", file=sys.stderr)
    sys.exit(1)

HERE = Path(__file__).resolve().parent
MIG_DIR = HERE.parent / "invoiceflow" / "migrations"
migration_body = (MIG_DIR / "004_add_clients_and_client_products.sql").read_text(encoding="utf-8")


def run_sql(query: str, label: str) -> dict:
    payload = json.dumps({"query": query}).encode("utf-8")
    req = urllib.request.Request(
        API,
        data=payload,
        headers={
            "Authorization": f"Bearer {PAT}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "curl/8.5.0",
        },
        method="POST",
    )
    print(f"\n=== {label} ===")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            print(json.dumps(data, indent=2))
            return data
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        print(f"HTTP {e.code}: {body}", file=sys.stderr)
        raise


# Step 1: apply the migration (CREATE TABLEs + indexes + RLS, in BEGIN/COMMIT)
run_sql(migration_body, "APPLY 004 (create clients + client_products)")

# Step 2: VERIFY — tables exist
run_sql(
    """SELECT tablename FROM pg_tables
       WHERE schemaname='public' AND tablename IN ('clients','client_products')
       ORDER BY tablename;""",
    "VERIFY 1 — tables (expect clients, client_products)",
)

# Step 3: VERIFY — RLS enabled
run_sql(
    """SELECT relname, relrowsecurity FROM pg_class
       WHERE relname IN ('clients','client_products') ORDER BY relname;""",
    "VERIFY 2 — RLS enabled (expect relrowsecurity=true for both)",
)

# Step 4: VERIFY — policies present
run_sql(
    """SELECT tablename, policyname FROM pg_policies
       WHERE tablename IN ('clients','client_products')
       ORDER BY tablename, policyname;""",
    "VERIFY 3 — policies (expect 4: tenant_all + super_admin for each)",
)

print("\n[done]")
