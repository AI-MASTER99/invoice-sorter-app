"""Fetch the two values that need to go into Render:
   - SUPABASE_ANON_KEY  (the public anon JWT)
   - SUPABASE_JWT_SECRET (HS256 secret for signing per-request user JWTs)

Tries the Management API endpoints; falls back to listing what's
available if exact endpoints differ.
"""
import json
import os
import sys
import urllib.request
import urllib.error

PROJECT_REF = "pbahlprxmlxvntfvcytd"
PAT = os.environ.get("SUPABASE_PAT")
if not PAT:
    print("ERROR: SUPABASE_PAT env var not set", file=sys.stderr)
    sys.exit(1)

HEADERS = {
    "Authorization": f"Bearer {PAT}",
    "Accept": "application/json",
    "User-Agent": "curl/8.5.0",
}


def get(path: str, label: str):
    url = f"https://api.supabase.com{path}"
    req = urllib.request.Request(url, headers=HEADERS)
    print(f"\n--- {label}: GET {path} ---")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = resp.read().decode("utf-8")
            try:
                parsed = json.loads(data)
                print(json.dumps(parsed, indent=2))
                return parsed
            except json.JSONDecodeError:
                print(data)
                return data
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        print(f"HTTP {e.code}: {body}")
        return None


# Try the api-keys endpoint
get(f"/v1/projects/{PROJECT_REF}/api-keys", "API keys")

# Try with reveal flag
get(f"/v1/projects/{PROJECT_REF}/api-keys?reveal=true", "API keys (reveal)")

# Postgres / postgrest config sometimes contains JWT info
get(f"/v1/projects/{PROJECT_REF}/postgrest", "PostgREST config")

# Auth config might contain JWT secret
get(f"/v1/projects/{PROJECT_REF}/config/auth", "Auth config")

# Secrets (per-project secrets)
get(f"/v1/projects/{PROJECT_REF}/secrets", "Secrets")
