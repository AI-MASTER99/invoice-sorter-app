"""
JWT minting for the per-request user-scoped Supabase client.

Phase B switch: every authenticated request mints a short-lived
HS256 JWT signed with SUPABASE_JWT_SECRET. The JWT is consumed
inside the request scope only — never returned to the user, never
logged. See migrations/PHASE_B_PLAN.md for the full design rationale.

The PostgREST `role` claim is HARDCODED to "authenticated" (the
Postgres connection role). The application-level role
(user/admin/super_admin) goes under `app_role` to avoid the
PostgREST `SET LOCAL ROLE` collision documented in migration 003a.
"""
import os
import time
from typing import Any

import jwt as pyjwt  # pyjwt>=2.8,<3 — HS256 is in core, no [crypto] extra needed

_secret = os.environ.get("SUPABASE_JWT_SECRET", "")
if not _secret:
    raise RuntimeError(
        "SUPABASE_JWT_SECRET environment variable is required for Phase B"
    )

# 30 minutes. JWT never leaves the server — constructed in `authed`,
# used by the per-request Supabase client over HTTPS to Supabase, and
# discarded when the request ends. The TTL only needs to be long enough
# for the longest-running handler (`/memory/refresh-tariff` with
# only_stale=False can take a few minutes at 200 rows × ~1s each).
JWT_TTL_SECONDS = 1800


def mint_user_jwt(ctx: dict[str, Any]) -> str:
    """Mint a Supabase-compatible HS256 JWT for the current request's user.

    `ctx` must contain non-empty: user_id, company_id, role.
    `username` is optional (used for convenience claims only).
    """
    now = int(time.time())
    payload = {
        "iss":        "invoiceflow",
        "sub":        ctx["user_id"],
        "aud":        "authenticated",
        # PostgREST connection role — drives SET LOCAL ROLE.
        # Always "authenticated"; never the app-level role.
        "role":       "authenticated",
        # App-level claims read by RLS policies (see migration 003a).
        "user_id":    ctx["user_id"],
        "username":   ctx.get("username", ""),
        "company_id": ctx["company_id"],
        "app_role":   ctx["role"],   # user / admin / super_admin
        "iat":        now,
        "exp":        now + JWT_TTL_SECONDS,
    }
    return pyjwt.encode(payload, _secret, algorithm="HS256")
