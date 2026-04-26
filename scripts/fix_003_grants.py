"""Tighten EXECUTE on change_own_password to authenticated only.

Supabase auto-grants EXECUTE to {anon, authenticated, service_role} on
every new function via default privileges. Migration 003 only did REVOKE
FROM PUBLIC, which leaves the role-specific grants intact.

Internal `auth.jwt() ->> 'user_id'` check already protects against anon
calls, but principle-of-least-privilege says revoke explicitly. We keep
service_role's grant because Supabase admin tools / DAL fallbacks use
service_role and it bypasses RLS anyway.
"""
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


# Revoke from anon — defense-in-depth. Internal jwt user_id check
# already blocks unauthenticated calls, but tightening the GRANT keeps
# the surface small.
run_sql(
    "REVOKE EXECUTE ON FUNCTION public.change_own_password(text) FROM anon;",
    "REVOKE EXECUTE FROM anon",
)

# Verify the resulting grant set
run_sql(
    """SELECT grantee, privilege_type
       FROM information_schema.routine_privileges
       WHERE routine_schema = 'public'
         AND routine_name = 'change_own_password'
       ORDER BY grantee;""",
    "VERIFY — grants after revoke (expect: postgres, authenticated, service_role)",
)

print("\n[done]")
