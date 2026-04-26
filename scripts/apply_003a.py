"""Apply migration 003a (rename role -> app_role in 8 RLS policies).

Sends the migration body as a single SQL statement to the Supabase
Management API, then runs the 3 VERIFY queries from the file footer
and prints results.

Usage: SUPABASE_PAT=sbp_... python apply_003a.py
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

MIGRATION = Path(r"C:\InvoiceFlow\invoiceflow\migrations\003a_rename_role_to_app_role.sql")

# The migration body is everything before the VERIFY block (commented out).
# We'll send the full file as-is — Postgres ignores -- comments.
sql_text = MIGRATION.read_text(encoding="utf-8")

# Strip the commented VERIFY queries from the migration body, since we
# run those as separate uncommented queries below.
body_end = sql_text.find("-- VERIFY (run manually after COMMIT)")
migration_body = sql_text[:body_end] if body_end != -1 else sql_text


def run_sql(query: str, label: str) -> dict:
    """POST a SQL query to the Management API and return the parsed JSON."""
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


# Step 1: apply the migration (DROP + CREATE 8 policies, wrapped in BEGIN/COMMIT)
run_sql(migration_body, "APPLY 003a (DROP + CREATE 8 policies)")

# Step 2: VERIFY query A — expect 0 rows (no policies still on old `role` claim)
run_sql(
    """SELECT schemaname, tablename, policyname, cmd
       FROM pg_policies
       WHERE schemaname='public'
         AND (qual ~* 'auth\\.jwt\\(\\)\\s*->>\\s*''role'''
           OR with_check ~* 'auth\\.jwt\\(\\)\\s*->>\\s*''role''')
       ORDER BY tablename, policyname;""",
    "VERIFY A — old 'role' claim references (expect 0 rows)",
)

# Step 3: VERIFY query B — expect 8 rows, all referencing app_role
run_sql(
    """SELECT schemaname, tablename, policyname, cmd
       FROM pg_policies
       WHERE schemaname='public'
         AND (qual ~* 'auth\\.jwt\\(\\)\\s*->>\\s*''app_role'''
           OR with_check ~* 'auth\\.jwt\\(\\)\\s*->>\\s*''app_role''')
       ORDER BY tablename, policyname;""",
    "VERIFY B — new 'app_role' claim references (expect 8 rows)",
)

# Step 4: VERIFY query C — total policy count unchanged
run_sql(
    "SELECT count(*) AS total FROM pg_policies WHERE schemaname='public';",
    "VERIFY C — total policy count (expect 13)",
)

print("\n[done]")
