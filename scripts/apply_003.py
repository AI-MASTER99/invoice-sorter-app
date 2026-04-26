"""Apply migration 003 (change_own_password SECURITY DEFINER RPC).

Sends the migration body as a single SQL statement to the Supabase
Management API, then runs the 2 VERIFY queries from the file footer
that don't need a JWT context.

Usage: SUPABASE_PAT=sbp_... python apply_003.py
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

MIGRATION = Path(r"C:\InvoiceFlow\invoiceflow\migrations\003_change_own_password_rpc.sql")
sql_text = MIGRATION.read_text(encoding="utf-8")

# Strip the commented VERIFY queries from the migration body — we run
# those as separate uncommented queries below.
body_end = sql_text.find("-- VERIFY (run manually after COMMIT)")
migration_body = sql_text[:body_end] if body_end != -1 else sql_text


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


# Step 1: apply the migration (CREATE FUNCTION + REVOKE/GRANT, in BEGIN/COMMIT)
run_sql(migration_body, "APPLY 003 (CREATE change_own_password + GRANT)")

# Step 2: VERIFY 1 — function exists, prosecdef=true, search_path lockdown
run_sql(
    """SELECT proname, prosecdef, proconfig
       FROM pg_proc
       WHERE proname = 'change_own_password'
         AND pronamespace = 'public'::regnamespace;""",
    "VERIFY 1 — function metadata (expect prosecdef=true, proconfig=[search_path=public, pg_temp])",
)

# Step 3: VERIFY 2 — only `authenticated` has EXECUTE
run_sql(
    """SELECT grantee, privilege_type
       FROM information_schema.routine_privileges
       WHERE routine_schema = 'public'
         AND routine_name = 'change_own_password';""",
    "VERIFY 2 — grants (expect 1 row: grantee=authenticated, EXECUTE)",
)

print("\n[done]")
