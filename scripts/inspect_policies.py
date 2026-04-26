"""List all policies on public schema to investigate the +2 discrepancy."""
import json
import os
import sys
import urllib.request
import urllib.error

PROJECT_REF = "pbahlprxmlxvntfvcytd"
API = f"https://api.supabase.com/v1/projects/{PROJECT_REF}/database/query"
PAT = os.environ.get("SUPABASE_PAT")
if not PAT:
    print("ERROR: SUPABASE_PAT env var not set", file=sys.stderr)
    sys.exit(1)


def run_sql(query: str, label: str):
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


run_sql(
    """SELECT tablename, policyname, cmd, roles, qual, with_check
       FROM pg_policies
       WHERE schemaname='public'
       ORDER BY tablename, policyname;""",
    "ALL policies (full detail)",
)
