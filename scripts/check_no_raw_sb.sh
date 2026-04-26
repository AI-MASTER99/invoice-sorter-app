#!/usr/bin/env bash
# Phase B safety lint — keeps the user-scoped Supabase client invariant.
#
# Three checks, each fails the script with a non-zero exit code:
#
#   1. No raw `db.sb.*` references in main.py.
#      Such references would bypass RLS by hitting the service-role
#      client. Everything that touches the DB during a request must go
#      through a DAL function (which uses _client()) or call db._client()
#      directly. Storage operations are exempt because they explicitly
#      live on _sb_service inside the DAL.
#
#   2. No `require_auth` / `require_admin` / `require_super_admin` symbols
#      anywhere in invoiceflow/. Phase B replaced these with `authed`,
#      `admin_authed`, `super_admin_authed`. A reference to the old name
#      means a missed sweep — the request would never bind a user-scoped
#      client and silently fall back to service-role.
#
#   3. No inline `request.session` reads outside `api_logout`. Logout is
#      the only handler permitted to read the session directly (it just
#      calls `.clear()` so a user with a stale/invalid cookie can still
#      log out cleanly). Every other handler must depend on `authed` so
#      RLS is in force.
#
# Wire this into CI (.github/workflows/ci.yml) and/or pre-commit.

set -euo pipefail

ROOT="${1:-$(cd "$(dirname "$0")/.." && pwd)}"
fail=0

echo "→ Phase B lint: scanning $ROOT"

# 1. raw db.sb.*
if grep -nE 'db\.sb\.' "$ROOT/invoiceflow/main.py" >/dev/null 2>&1; then
  echo "ERROR: raw db.sb.* in main.py — would bypass RLS. Use a DAL function or db._client()." >&2
  grep -nE 'db\.sb\.' "$ROOT/invoiceflow/main.py" >&2
  fail=1
fi

# 2. require_auth / require_admin / require_super_admin
if grep -nrE '\b(require_auth|require_admin|require_super_admin)\b' \
     "$ROOT/invoiceflow"/*.py >/dev/null 2>&1; then
  echo "ERROR: deleted dependency referenced (require_auth/admin/super_admin)." >&2
  echo "       Use authed / admin_authed / super_admin_authed instead." >&2
  grep -nrE '\b(require_auth|require_admin|require_super_admin)\b' \
       "$ROOT/invoiceflow"/*.py >&2
  fail=1
fi

# 3. inline request.session outside api_logout (which uses .clear())
# Three legitimate patterns are allowlisted:
#   a) `request.session.clear()` — api_logout (and a comment in api_me).
#   b) `request.session["k"] = v` (assignment) — login flow only. The
#      regex requires `]` followed by `=` so READS via subscript
#      (`x = request.session["k"]`) still trip the lint.
#   c) `sess = request.session` — the canonical reader inside `authed`.
matches=$(grep -nE 'request\.session\b' "$ROOT/invoiceflow/main.py" \
          | grep -vE 'request\.session\.(clear|setdefault)\b' \
          | grep -vE 'request\.session\[[^]]+\]\s*=[^=]' \
          | grep -vE 'sess = request\.session' \
          || true)
if [ -n "$matches" ]; then
  echo "ERROR: inline request.session read outside api_logout / authed." >&2
  echo "$matches" >&2
  fail=1
fi

if [ $fail -eq 0 ]; then
  echo "✓ Phase B lint passed."
else
  exit 1
fi
