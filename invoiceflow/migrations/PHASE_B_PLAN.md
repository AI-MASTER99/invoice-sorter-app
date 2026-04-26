# Phase B Implementation Plan — v2 (post re-review)

> Status: **READY FOR IMPLEMENTATION** (post external review + post re-review).
> Companion: `PHASE_B_PREREQUISITES.md` (pre-flight checklist), `PHASE_B_REVIEW.md` (v1 review + v2 re-review).
> v1 → v2 changes summarised in §0; post-re-review tweaks summarised in the same section.

This document specifies the exact code/DB changes for Phase B: making RLS effective by switching the FastAPI backend from a single service-role Supabase client to a per-request user-scoped client. After Phase B ships, the policies created by `001_enable_rls.sql` and tightened by `002_role_clamp_fix.sql` will actually filter queries; today they are inert because the service-role key BYPASSes RLS.

---

## 0. Changelog v1 → v2

The external review (`PHASE_B_REVIEW.md`) returned **RED** on v1 with 4 critical and 5 high findings. v2 resolves all of them:

| Review finding | v2 resolution |
|---|---|
| **C1** — JWT `role` claim collides with PostgREST's `SET LOCAL ROLE` | NEW migration **003a** renames the app-role claim to `app_role` and rewrites all 8 dependent policies. JWT carries `role: "authenticated"` (PostgREST connection role) and `app_role: "user"\|"admin"\|"super_admin"` (RLS predicate). |
| **C2** — Storage breaks under user JWT (no `storage.objects` policies) | All `storage_*` DAL functions explicitly bound to `_sb_service`. Tenant isolation continues via `{company_id}/…` path-prefix. Storage RLS is out of Phase B scope. |
| **C3** — 5 raw `db.sb.table(...)` sites in main.py, plan only addressed 1 | All 5 sites (L2556, L3087, L3143, L3259, L3276) refactored. Backward-compat `sb` shim removed. CI lint added. |
| **C4** — "Forced logout" claim wrong (SECRET_KEY persists across deploys) | Cookie name bumped `is_session` → `is_session_v2` in same commit. SECRET_KEY rotation is no longer load-bearing for cutover. |
| **H1** — JWT 5min TTL too short for `/memory/refresh-stale` (3+ min) | TTL bumped to 30 minutes. JWT never leaves the server (constructed in `authed`, used over HTTPS to Supabase, discarded). |
| **H2** — ContextVar reset semantics with `StreamingResponse` | Documented as a `StreamingResponse` ban in `authed`'s docstring + risk §8. Today's responses are buffered. |
| **H3** — `make_user_client` per-request httpx pool overhead | Staging perf-test added as a pre-deploy gate (§9). Mitigation pre-decided: if measured >50ms p95, swap to a single pooled `Client` with per-request `postgrest.auth(jwt)` re-binding. |
| **H4** — `company_id=""` fallback breaks UUID cast | `authed` raises `HTTPException(401)` if any of `user_id`/`company_id`/`role` missing from session. No silent fallback. |
| **H5** — `pyjwt[crypto]` extra unnecessary for HS256 | `pyjwt>=2.8,<3` plain. Saves ~10MB and Rust toolchain on builds. |
| **M2** — `_session_only_ctx` footgun | Removed entirely. Tests already construct ctx dicts directly. |
| **M5** — Migration 003 not in BEGIN/COMMIT | 003 wrapped, matches house style. |
| **M6** — RPC hash validation too loose | Bcrypt prefix regex (`^\$2[aby]\$\d{2}\$.{53}$`) instead of length floor. |

### Post re-review tweaks (v2 → final)

A second-pass review on v2 returned YELLOW with 4 new issues. All four are addressed in this revision:

| Re-review finding | Resolution |
|---|---|
| **N1** — `api_me` and `api_logout` read `request.session` directly, not in §4.3.3 sweep | §4.3.3 split into Pass 1 (Depends rewires, count corrected to 26) and Pass 2 (inline-session readers). `api_me` → `Depends(authed)`. `api_logout` → intentionally session-only (documented). CI lint extended in §9. |
| **N2** — §4.3.1 cookie snippet silently weakened `same_site`, dropped `max_age`, hardcoded `https_only` | §4.3.1 rewritten to change ONLY the `session_cookie` name; all other settings preserved verbatim from current `main.py:2215-2222`. |
| **N3** — §6 step 1 inline VERIFY query was laxer than 003a.sql's | §6 step 1 now references the three VERIFY queries at the bottom of `003a.sql`. No inline restate. |
| **N4** — H3 fallback relies on `postgrest.auth(jwt)` rebind being a stable `supabase-py` API | §8.4 now requires a 30-min pre-spike against `supabase==2.28.3` to confirm the rebind behaves correctly before treating it as a viable fallback. |
| **H1 caveat** — `/memory/refresh-tariff` (full sweep) may be slower than `/memory/refresh-stale` (only_stale=True) | §8.3 extended to time both endpoints. |

The 6 open decision points from v1 are resolved in §10.

---

## 1. Goal & non-goals

**Goal:** Every HTTP request that maps to a logged-in user runs DAL queries against a Supabase client that carries that user's JWT. Background tasks, login flow, the queue worker, and storage operations continue to use the service-role client.

**Non-goals (Phase B):**
- Adding `company_id` filters to the few DAL functions that are missing them (`get_user_by_id`, `get_job`). RLS will silently filter cross-tenant rows; the Python layer surfaces 404 instead of 403, which we accept for now. → **Phase C scope.**
- Storage RLS policies. Storage stays explicitly on the service-role client; isolation continues via path-prefix (`{company_id}/…`). → Future security batch.
- Auditing super_admin actions. → Future security batch.
- Refactoring the DAL into a class. Stick with module-level functions + contextvar.
- SECRET_KEY rotation as part of the deploy (cookie-name bump replaces it).

---

## 2. Architecture

### 2.1 Auth model (current vs after Phase B)

**Current:**
- Login → server validates password → writes session cookie (`is_session`, `SessionMiddleware`).
- Every subsequent request → `require_auth` reads session → returns `ctx = {user_id, username, company_id, role}`.
- DAL → calls `sb.table(…)` (single global service-role client). RLS bypassed.

**After Phase B:**
- Login → unchanged in flow (still service-role for the user lookup) but cookie name bumped to `is_session_v2`. Old `is_session` cookies are ignored → all users re-login, getting a fresh ctx written to the new cookie name.
- Every authenticated request → `authed()` dep reads session → mints a 30-min Supabase JWT (HS256, signed with `SUPABASE_JWT_SECRET`) → builds a user-scoped Supabase client (anon key + Authorization header) → stores in a `ContextVar` for the request lifetime → returns `ctx`.
- DAL → resolves the current client via `_client()` (= contextvar value, fallback to `_sb_service`). In a request scope, that's the user client. Outside (worker, startup), it's service-role.
- Storage DAL functions explicitly use `_sb_service.storage…`, regardless of context.

### 2.2 Why ContextVar (reaffirmed)

Three patterns considered (see v1 §2.2). **Pattern B (ContextVar) wins** with the caveats from review:
- Endpoint sweep is mechanical (replace every `Depends(require_auth)` etc.).
- Misuse is loud only if the developer forgets `Depends(authed)` entirely → falls through to `_sb_service`. Mitigations: (a) every authenticated route MUST use `authed`/`admin_authed`/`super_admin_authed`, (b) CI grep-lint, (c) integration tests assert RLS is effective.
- The "session-only ctx" footgun (`_session_only_ctx` in v1 §3.3.3) is **dropped** in v2 — no escape hatch.

### 2.3 JWT design (revised)

| Claim | Value | Source / Why |
|---|---|---|
| `iss` | `"invoiceflow"` | Convention, not validated by Supabase but useful for audit |
| `sub` | `ctx["user_id"]` | Postgres `auth.uid()` reads this |
| `aud` | `"authenticated"` | Required by Supabase / PostgREST audience check |
| `role` | `"authenticated"` | **PostgREST claim** — drives `SET LOCAL ROLE authenticated` so RLS is evaluated under the `authenticated` Postgres role. Must be `"authenticated"`, never the app-level role. |
| `user_id` | `ctx["user_id"]` (UUID string) | App-level claim; consumed by RPC `change_own_password` |
| `username` | `ctx["username"]` | Convenience; not read by any policy |
| `company_id` | `ctx["company_id"]` (UUID string) | **Used by every tenant-isolation policy** — `(auth.jwt() ->> 'company_id')::uuid` |
| `app_role` | `ctx["role"]` (`user`/`admin`/`super_admin`) | **Used by super_admin bypass + admin-only policies** — renamed from `role` to avoid PostgREST collision (see migration 003a) |
| `iat` | `now` | Standard |
| `exp` | `now + 1800` (30 min) | Long enough for `/memory/refresh-stale` (200 rows × ~1s each ≈ 3 min); JWT never leaves the server |

**The PostgREST/RLS contract:**
- PostgREST inspects `role` and switches the connection's Postgres role via `SET LOCAL ROLE`. We always set this to `"authenticated"`.
- RLS policies inspect `auth.jwt() ->> 'company_id'` and `auth.jwt() ->> 'app_role'` to make their decisions.
- The two are namespaced separately and there is no longer any collision.

### 2.4 Client construction

```python
# database.py (new section)
from supabase.client import ClientOptions

_anon_key = os.environ["SUPABASE_ANON_KEY"]  # NEW required env var; raise if missing

def make_user_client(jwt: str) -> Client:
    """Build a per-request Supabase client that carries a user JWT.

    PERF NOTE: this constructs a fresh httpx.Client. If staging measurement
    shows >50ms p95 added to request latency, switch to the pooled-client
    pattern: keep a single Client and call client.postgrest.auth(jwt) per
    request to swap the Bearer header. Tracked in §8 (H3)."""
    return create_client(
        _url, _anon_key,
        options=ClientOptions(headers={"Authorization": f"Bearer {jwt}"}),
    )
```

The anon key (not service-role) is what user-scoped clients use; the JWT in the Authorization header is what RLS reads.

---

## 3. Migrations

Two new migrations land **before** the Python deploy. Both run safely under service-role (BYPASSRLS) — same deploy-safety argument as 001/002.

Order: 003a, then 003. Both wrapped in BEGIN/COMMIT for atomicity.

### 3.1 Migration 003a — rename `role` → `app_role` in policies

**Why:** PostgREST consumes the JWT `role` claim via `SET LOCAL ROLE`; we cannot reuse it for the app-level role. Existing 8 policies in `001_enable_rls.sql` (post-002) read `auth.jwt() ->> 'role'` for the app role — every such reference must move to `auth.jwt() ->> 'app_role'`. Migration drops + recreates each affected policy.

**Affected policies (8):**
- `companies_super_admin` (companies)
- `users_admin_insert`, `users_admin_update`, `users_admin_delete`, `users_super_admin` (users)
- `invoices_super_admin` (invoices)
- `memory_super_admin` (product_memory)
- `jobs_super_admin` (jobs)

(The 5 tenant-scoped policies that read `auth.jwt() ->> 'company_id'` only — `users_self_select`, `invoices_tenant_*`, `memory_tenant_*`, `jobs_tenant_*` — are not affected.)

**SQL** — see `migrations/003a_rename_role_to_app_role.sql` (drafted in §3.1.1 alongside this plan).

### 3.2 Migration 003 — `change_own_password` RPC

**Why:** RLS policies don't have column-level granularity, so a normal user has no policy that lets them UPDATE their own `users.password_hash`. Route password self-change through a SECURITY DEFINER RPC.

```sql
-- migrations/003_change_own_password_rpc.sql
BEGIN;

CREATE OR REPLACE FUNCTION public.change_own_password(new_hash text)
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
  caller_id uuid;
BEGIN
  caller_id := (auth.jwt() ->> 'user_id')::uuid;
  IF caller_id IS NULL THEN
    RAISE EXCEPTION 'No authenticated caller';
  END IF;
  -- Bcrypt-prefix sanity check (defense-in-depth: catches "Python passed plaintext").
  -- Allow argon2 for forward compatibility.
  IF new_hash !~ '^(\$2[aby]\$\d{2}\$.{53}|\$argon2(id|i)\$.+)$' THEN
    RAISE EXCEPTION 'Invalid hash format';
  END IF;
  UPDATE public.users
     SET password_hash = new_hash,
         updated_at    = now()
   WHERE id = caller_id;
END;
$$;

REVOKE ALL ON FUNCTION public.change_own_password(text) FROM PUBLIC;
GRANT  EXECUTE ON FUNCTION public.change_own_password(text) TO authenticated;

COMMIT;
```

**Hash validation:** bcrypt `$2[aby]$NN$<53 chars>` OR argon2 prefix. Catches the "future bug passed plaintext" failure mode.

**`SET search_path` is critical** — without it, SECURITY DEFINER is exposed to search-path-based privilege escalation.

### 3.3 Rollback

`migrations/003a_rename_role_to_app_role_rollback.sql` reverts policies to read `'role'` (pairs with code rollback if Phase B is reverted).
`migrations/003_change_own_password_rpc_rollback.sql` drops the RPC. **Must be run BEFORE the code rollback** if Phase B has shipped to avoid 500s on `/api/users/{me}/password`.

---

## 4. File-by-file changes

### 4.1 New file: `auth_jwt.py`

Isolates JWT logic; unit-testable.

```python
import os
import time
from typing import Any
import jwt as pyjwt  # pyjwt>=2.8

_secret = os.environ.get("SUPABASE_JWT_SECRET", "")
if not _secret:
    raise RuntimeError("SUPABASE_JWT_SECRET environment variable is required")

JWT_TTL_SECONDS = 1800  # 30 min — see PHASE_B_PLAN.md §2.3 H1 rationale

def mint_user_jwt(ctx: dict[str, Any]) -> str:
    """Mint a Supabase-compatible HS256 JWT for the current request's user.

    The PostgREST `role` claim is HARDCODED to "authenticated" — never the
    app-level role. App-level role goes under `app_role` (see migration 003a).
    """
    now = int(time.time())
    payload = {
        "iss":        "invoiceflow",
        "sub":        ctx["user_id"],
        "aud":        "authenticated",
        "role":       "authenticated",          # PostgREST connection role
        "user_id":    ctx["user_id"],
        "username":   ctx.get("username", ""),
        "company_id": ctx["company_id"],
        "app_role":   ctx["role"],              # user / admin / super_admin
        "iat":        now,
        "exp":        now + JWT_TTL_SECONDS,
    }
    return pyjwt.encode(payload, _secret, algorithm="HS256")
```

### 4.2 `database.py` changes

```python
# Top of file additions
from contextvars import ContextVar
from typing import Optional
from supabase.client import ClientOptions

_url = os.environ.get("SUPABASE_URL", "")
_service_key = os.environ.get("SUPABASE_KEY", "")
_anon_key = os.environ.get("SUPABASE_ANON_KEY", "")

if not _url or not _service_key or not _anon_key:
    raise RuntimeError(
        "SUPABASE_URL, SUPABASE_KEY, and SUPABASE_ANON_KEY are required"
    )

# Renamed from `sb` for clarity. NEVER imported directly by main.py — use _client().
_sb_service: Client = create_client(_url, _service_key)

_current_client: ContextVar[Optional[Client]] = ContextVar(
    "current_client", default=None
)

def _client() -> Client:
    """Return the request-scoped client, or fall back to service-role.

    NEVER call this from background workers — they explicitly use _sb_service.
    """
    c = _current_client.get()
    return c if c is not None else _sb_service

def make_user_client(jwt: str) -> Client:
    """Build a per-request user-scoped client. See PHASE_B_PLAN.md §2.4."""
    return create_client(
        _url, _anon_key,
        options=ClientOptions(headers={"Authorization": f"Bearer {jwt}"}),
    )
```

**Every DAL function** changes from `sb.table(…)` to `_client().table(…)` — except storage:

```python
# Storage stays explicitly on _sb_service. Tenant isolation comes from
# {company_id}/… path-prefix in app code. Storage RLS is out of Phase B.
def storage_upload(bucket: str, path: str, data: bytes) -> None:
    _sb_service.storage.from_(bucket).upload(path, data, ...)

def storage_download(bucket: str, path: str) -> bytes:
    return _sb_service.storage.from_(bucket).download(path)

def storage_signed_url(bucket: str, path: str, expires: int) -> str:
    return _sb_service.storage.from_(bucket).create_signed_url(path, expires)["signedURL"]

def storage_delete(bucket: str, paths: list[str]) -> None:
    _sb_service.storage.from_(bucket).remove(paths)
```

**No `sb` shim.** Anyone importing `db.sb` directly gets an `AttributeError` at runtime — by design. The 5 sites in main.py (§4.3.5) are all refactored.

**Queue worker DAL calls** are unchanged in code: they go through DAL functions which call `_client()`, which falls through to `_sb_service` because the worker has no contextvar set.

### 4.3 `main.py` changes

#### 4.3.1 Cookie-name bump

**Change ONLY the `session_cookie` name. Preserve everything else exactly as-is.** The current config is hardened (same_site=strict, max_age=12h, https_only conditional on DEV_MODE) and any change to those is out-of-scope for Phase B.

Concrete edit at `main.py:2215-2222`:

```python
app.add_middleware(
    SessionMiddleware,
    secret_key=SECRET_KEY,
    session_cookie="is_session_v2",   # CHANGED: was "is_session"
    max_age=60 * 60 * 12,             # UNCHANGED — 12 h
    https_only=not DEV_MODE,          # UNCHANGED — DEV_MODE allows http://localhost
    same_site="strict",               # UNCHANGED — strict CSRF posture
)
```

Bumping the cookie name invalidates all existing `is_session` cookies on the deploy boundary. Users re-login through the regular login flow → fresh `is_session_v2` cookie with the full ctx fields the new `authed` dep requires. No SECRET_KEY rotation required.

#### 4.3.2 New `authed()` dependency (yield generator)

```python
import database as db
from auth_jwt import mint_user_jwt

async def authed(request: Request):
    """Authenticated dep: binds a per-request user-scoped Supabase client.

    Use on every authenticated handler. NEVER use Depends(require_auth)
    directly — it bypasses the user client binding and falls back to
    service-role (RLS-bypass).

    DO NOT use this with StreamingResponse from a generator: the contextvar
    resets when this dep tears down, which happens BEFORE the generator
    yields. All current responses are buffered (Response/JSONResponse) so
    this is safe today; a future contributor adding StreamingResponse must
    revisit. See PHASE_B_PLAN.md §8 (H2).
    """
    # Hard-fail on missing session fields. No silent fallback to "" — the UUID
    # cast in policies would 500 anyway, fail loud instead. See review H4.
    sess = request.session
    user_id    = sess.get("user_id")
    company_id = sess.get("company_id")
    role       = sess.get("role")
    if not user_id or not company_id or not role:
        raise HTTPException(status_code=401, detail="Not authenticated")

    ctx = {
        "user_id":    user_id,
        "username":   sess.get("username", ""),
        "company_id": company_id,
        "role":       role,
    }

    jwt = mint_user_jwt(ctx)
    client = db.make_user_client(jwt)
    token = db._current_client.set(client)
    # Audit line — never log the JWT or the secret. Just the ctx fields,
    # which are also already in the session cookie. Cheap ops win for
    # tracing "who did what when" without per-handler instrumentation.
    log.info(
        "authed user_id=%s company_id=%s app_role=%s",
        ctx["user_id"], ctx["company_id"], ctx["role"],
    )
    try:
        yield ctx
    finally:
        db._current_client.reset(token)


async def admin_authed(ctx: dict = Depends(authed)) -> dict:
    if ctx["role"] not in ("admin", "super_admin"):
        raise HTTPException(status_code=403, detail="Admin required")
    return ctx


async def super_admin_authed(ctx: dict = Depends(authed)) -> dict:
    if ctx["role"] != "super_admin":
        raise HTTPException(status_code=403, detail="Super admin required")
    return ctx
```

The generator-with-`yield` pattern is FastAPI's documented "dependencies with yield" — teardown after handler returns. ContextVar binding is set before `admin_authed`/`super_admin_authed` evaluate (FastAPI resolves deps depth-first), reset after the handler returns the response.

**The old `require_auth`, `require_admin`, `require_super_admin` are deleted** — no compatibility shim. Anyone importing them gets `ImportError` at module load.

#### 4.3.3 Endpoint sweep

**Two passes required.**

**Pass 1 — `Depends(...)` rewires.** For every `Depends(require_auth | require_admin | require_super_admin)` in main.py:
- `require_auth`        → `Depends(authed)`
- `require_admin`       → `Depends(admin_authed)`
- `require_super_admin` → `Depends(super_admin_authed)`

Counted in current main.py: 20× `require_auth`, 3× `require_admin`, 3× `require_super_admin` = **26 call sites**. Mechanical change. Verify via grep that 0 occurrences of `require_auth|require_admin|require_super_admin` remain post-sweep.

**Pass 2 — endpoints that read `request.session` directly.** Two endpoints today bypass the `Depends` system and read the session inline:

| Endpoint | File:line | Touches DB? | Action |
|---|---|---|---|
| `GET /api/me` (`api_me`) | `main.py:2566` | YES (`db.get_user_by_id`, `db.list_companies`) | **Refactor to `Depends(authed)`**. The handler signature becomes `async def api_me(ctx: dict = Depends(authed))`. Remove the inline session check (already handled by `authed`'s 401). The DB calls then run under user JWT and are filtered by RLS — `db.list_companies()` will only return the user's own company under `companies_tenant_select`, making the `next()` filter in the handler redundant but safe. |
| `POST /api/logout` (`api_logout`) | `main.py:2560` | NO (only `request.session.clear()`) | **Leave as-is, intentionally session-only.** Adding `Depends(authed)` here would mean a user with a stale/invalid session can't log out cleanly. Document this as intentional. |

**CI lint addition (§9):** add a check that no other handler reads `request.session` directly except `api_logout`. Pattern: `grep -nE 'request\.session\b' main.py` should return ≤ 1 line (the logout). If a future contributor adds another inline-session reader, the lint catches it.

#### 4.3.4 `api_change_password`

```python
@app.put("/api/users/{username}/password")
async def api_change_password(
    username: str,
    body: dict = {},
    ctx: dict = Depends(authed),
):
    new_pw = body.get("password") or ""
    if not new_pw:
        raise HTTPException(400, "Password required")
    new_hash = _pwd_ctx.hash(new_pw)

    if username == ctx["username"]:
        # Self-change via SECURITY DEFINER RPC — RLS doesn't allow
        # column-restricted UPDATEs on users.password_hash directly.
        db._client().rpc("change_own_password", {"new_hash": new_hash}).execute()
        return {"ok": True}

    # Admin/super_admin changing someone else
    if ctx["role"] not in ("admin", "super_admin"):
        raise HTTPException(403, "Forbidden")

    target = db.get_user(username, ctx["company_id"])
    if not target:
        raise HTTPException(404, "User not found")

    # Migration 002's USING role-clamp: an admin cannot UPDATE a super_admin
    # row (returns 0 rows). Surface that as a clear 403 rather than a silent
    # no-op success. (M4 from review.)
    if target["role"] == "super_admin" and ctx["role"] != "super_admin":
        raise HTTPException(403, "Cannot reset super_admin password")

    db.update_user_password(target["id"], new_hash)
    return {"ok": True}
```

The `not in ("admin","super_admin")` fix from the spawned chip-task is included here.

#### 4.3.5 Five `db.sb.table(...)` raw sites — all refactored

Per review C3:

| Site | Current | After Phase B |
|---|---|---|
| **L2556** (delete company, super_admin) | `db.sb.table("companies").delete().eq("id",cid).execute()` | Wrap in DAL: `db.delete_company(cid)`. Endpoint uses `Depends(super_admin_authed)`. RLS policy `companies_super_admin` permits this — verify in staging. |
| **L3087** (delete failed job during retry) | `db.sb.table("jobs").delete().eq("id",jid).execute()` | Wrap in DAL: `db.delete_job(jid)` filtering by `company_id` AND id. Endpoint uses `Depends(authed)`. RLS policy `jobs_tenant_delete` permits. |
| **L3143** (delete job) | `db.sb.table("jobs").delete().eq("id",jid).execute()` | Same `db.delete_job(jid)` wrapper as above. |
| **L3259** (delete memory entry) | `db.sb.table("product_memory").delete().eq("id",mid).execute()` | Wrap: `db.delete_memory_entry(mid)`. RLS policy `memory_tenant_delete` permits. |
| **L3276** (cleanup invalid memory entries) | `db.sb.table("product_memory").delete().eq("company_id",cid).is_("...",None).execute()` | Wrap: `db.cleanup_invalid_memory(cid)`. |

**CI lint** in repo root (e.g., `scripts/check_no_raw_sb.sh` or `pre-commit` hook):

```bash
#!/usr/bin/env bash
# Fail if main.py contains raw db.sb.* references (would bypass RLS).
if grep -nE 'db\.sb\.' invoiceflow/main.py > /dev/null; then
  echo "ERROR: raw db.sb.* found in main.py — must go through DAL or db._client()"
  grep -nE 'db\.sb\.' invoiceflow/main.py
  exit 1
fi
```

Wired into the existing CI pipeline (or added as a pre-commit hook).

### 4.4 `render.yaml` changes

Add two env vars (`sync: false`, set in Render dashboard):
- `SUPABASE_ANON_KEY`
- `SUPABASE_JWT_SECRET`

### 4.5 `requirements.txt` changes

Add: `pyjwt>=2.8,<3` (no `[crypto]` extra — HS256 is in pyjwt's core).

---

## 5. Test strategy

### 5.1 Unit tests

- **`tests_jwt.py` (new):** mint a JWT, decode it, verify all claims (incl. `role=authenticated`, `app_role=<actual>`, `aud=authenticated`). Verify TTL behaviour: token at exp-1 valid, exp+1 invalid.
- **`tests_user_admin.py` (existing):** retrofit the new `authed` dep so privilege-gate tests still pass. Tests pass ctx dicts directly today (no need for the dropped `_session_only_ctx`).
- **`tests_rate_limit.py` (existing):** no changes expected; login flow unchanged.

### 5.2 Integration tests (`tests_rls_integration.py`)

Hits a **separate** Supabase project (test project, not prod). Gated with `pytest.mark.integration`; default-skip in CI; runs nightly + manual. `conftest.py` fixture truncates and seeds two tenants with known users.

Per policy class, one positive + one negative case:
- Tenant-A user reads tenant-A invoice → success
- Tenant-A user reads tenant-B invoice → 0 rows
- Admin in tenant A creates user with role=user → success
- Admin in tenant A creates user with role=super_admin → fails (RLS WITH CHECK)
- Admin in tenant A updates super_admin user in tenant A → fails (USING role-clamp from 002)
- Super_admin reads anything → success
- Anonymous (no JWT) → 401 from FastAPI dep
- **NEW:** assert `_current_client.get()` is non-None inside a handler decorated with `Depends(admin_authed)` (M7 from review)

### 5.3 Smoke tests (manual, post-deploy to staging)

The list in `PHASE_B_PREREQUISITES.md` §5, plus:
- **NEW:** `change_own_password` RPC end-to-end. Log in as regular user, hit `PUT /api/users/<self>/password` with a new password, verify hash updated. Verify the same JWT cannot directly UPDATE another user's `password_hash` via raw `_client().table("users").update(...)`.
- **NEW:** invalid-hash RPC call: `db._client().rpc("change_own_password", {"new_hash": "plaintext"}).execute()` must raise.
- **NEW:** storage upload/download round-trip via `/upload` and export retrieval, confirming storage path stayed on service-role.

---

## 6. Deploy plan

> **Pre-deploy gate:** the staging perf-test from §9 (H3) must pass before production deploy.

Order matters. Each step ≤ 5 min.

1. **Run migration 003a** in Supabase SQL Editor (rename `role` → `app_role` in 8 policies). **VERIFY: run the three queries in the VERIFY block at the bottom of `003a_rename_role_to_app_role.sql`** (do NOT use a one-liner — the inline regex must distinguish `'role'` from `'app_role'`, which a naive `qual ~ 'role'` does not). Expected: query 1 returns 0 rows, query 2 returns 8 rows, query 3 returns 13.
2. **Run migration 003** (RPC). VERIFY: function exists, GRANT to `authenticated` is set, REVOKE from PUBLIC is set.
3. **Configure secrets** in Render dashboard: add `SUPABASE_ANON_KEY` and `SUPABASE_JWT_SECRET`. Confirm via `render env list` (or dashboard) that both are non-empty.
4. **Deploy code** (single git push). Render auto-deploys. The cookie-name bump in this commit invalidates all existing sessions on the deploy boundary.
5. **Smoke tests:** run §5.3 list against staging within 5 min of deploy. Includes `/api/me`, list invoices, list jobs, change own password, upload+download storage.
6. **Monitor:** Render logs for 401/403 spikes for 1 hour. Threshold: **>5 sustained 401s/min for 5 minutes → rollback**. Threshold: **any 500 referencing UUID cast or RLS denial → investigate immediately, rollback if not explainable.**

### Rollback

If Phase B causes prod breakage:

1. **`git revert` the deploy commit.** Render redeploys. RLS stays enabled because the service-role key (now back in Python's hands) bypasses it. Cookie name stays bumped (no harm; users just re-login on the rolled-back code).
2. **If migration 003 (RPC) was used at runtime**: drop it BEFORE the code rollback finishes (the rolled-back code calls the old `update_user_password` directly, not the RPC, but if any in-flight requests are mid-execution they may 500). In practice: just leave 003 in place — it's harmless if unused.
3. **Don't roll back 003a.** Renaming the claim back to `role` requires updating 8 policies again; not worth the churn. The new `app_role`-based policies still work correctly under service-role (RLS is bypassed anyway). Only run 003a-rollback if there's a separate reason.
4. **Don't roll back 002 or 001.** Those stay regardless.

---

## 7. Risk assessment

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Migration 003a leaves a policy missed → query under app_role-claim fails open under wrong policy | Low | High | Verify post-migration: every policy reading any role/app_role claim references `app_role` exclusively. SELECT from `pg_policies` and grep. |
| One handler missing `authed` dep → bypasses RLS silently | Low (mechanical sweep) | Medium | Endpoint sweep at deploy; CI lint as safety net (every router endpoint must have one of the three authed deps unless explicitly listed). |
| Storage operations break despite service-role binding (unlikely but tested) | Low | Medium (uploads/downloads broken) | Storage round-trip in §5.3 smoke tests. |
| JWT TTL mid-request expiry (>30 min handler) | Very Low | Low (would 401 mid-handler) | 30 min is generous; if a handler runs longer something else is wrong. Tariff lookups can be slow but are bounded by the trade-tariff API timeout. |
| Queue worker forgotten somewhere | Low | High (background jobs break) | Worker runs in a thread started at module load (`main.py:216`); no contextvar set; falls back to `_sb_service`. Documented contract: no DB-touching threads spawned from request handlers. |
| `make_user_client` per-request httpx pool overhead (H3) | Medium | Medium (latency regression) | Staging perf-test pre-gate (§9). If >50ms p95, swap to pooled-client pattern. |
| ContextVar reset before StreamingResponse generator yields (H2) | Low (today) | High (silent service-role fallback) | All current responses buffered. `authed` docstring bans StreamingResponse from generators that touch DB. Add to onboarding docs. |
| `auth.jwt() ->> 'user_id'` doesn't work inside SECURITY DEFINER | Very Low | High (password change broken) | Smoke test M3 explicitly. (Should work — `auth.jwt()` reads request.jwt.claims GUC, unaffected by SET ROLE.) |

---

## 8. Things to test in staging FIRST (gates before production deploy)

These three tests must pass in staging before any production cutover. Each invalidates a critical assumption if it fails.

### 8.1 PostgREST `role`-claim behaviour (validates C1 fix)

**Test:** mint a JWT with `role: "admin"` (the broken v1 design), call any authenticated endpoint, confirm 500 with PostgREST role error. Then mint a JWT with `role: "authenticated"` and `app_role: "admin"` (v2 design), confirm endpoint returns expected data and RLS evaluates correctly.

**Pass criteria:** v1-shape JWT errors out as predicted; v2-shape JWT works with the rewritten policies. If v1-shape works without error, the review's reading of PostgREST is wrong and v2's policy rename was unnecessary — but rolling forward with v2 is still safe (the `app_role` claim is more explicit).

### 8.2 Storage round-trip (validates C2 fix)

**Test:** mint a user JWT, build `_user_client(jwt)`, call `_user_client.storage.from_("invoice-uploads").upload(...)`. Expect RLS denial. Then call `_sb_service.storage.from_("invoice-uploads").upload(...)` (the v2 path). Expect success. Confirm the full upload→queue→export cycle works end-to-end.

**Pass criteria:** user-client storage call denied; service-role storage call works; full upload→export cycle succeeds with the v2 code.

### 8.3 Long-running handler under 30-min JWT TTL (validates H1 fix)

**Test:** seed staging tenant with 200 memory rows. Hit BOTH:
- `POST /memory/refresh-tariff` with `only_stale=True` (the `/memory/refresh-stale` shortcut)
- `POST /memory/refresh-tariff` with `only_stale=False` (the **full sweep** — re-reads every entry, may be slower)

Time both handlers end-to-end.

**Pass criteria:** both complete within 30 minutes (much less expected) with no 401-storm at the end. If either crosses 30 min, bump TTL further or refactor to re-mint mid-loop. The full sweep is the worst case — if it fits, refresh-stale fits.

### 8.4 Performance gate (validates H3 mitigation)

**Test:** load-test 50 req/s against `/api/invoices` for 60s, measure p95 of `db.make_user_client` call (`time.perf_counter()` instrumentation around the call site in `authed`).

**Pass criteria:** p95 < 50ms. If exceeded, switch to pooled-client pattern before production deploy.

**⚠️ Pre-spike before relying on the fallback:** the fallback assumes `client.postgrest.auth(jwt)` rebinds the Authorization header on a single pooled `Client` without recreating connections or invalidating cached schema fetches. This API surface has been inconsistent across `supabase-py` versions. Confirm against the pinned version (`supabase==2.28.3`) with a 30-min spike: build one `Client`, call `client.postgrest.auth(jwt_a)` then a query, then `client.postgrest.auth(jwt_b)` then a query — assert each query carried the right Bearer token (verify via Supabase logs or a request interceptor). If the rebind is not clean, the fallback needs a different shape (e.g. monkey-patch the headers dict directly) and the staging gate becomes a hard prerequisite, not a fallback.

---

## 9. CI lint additions

Add to the existing CI pipeline:

```yaml
# .github/workflows/ci.yml (or wherever)
- name: Forbid raw db.sb.* in main.py (must go through DAL or _client())
  run: |
    if grep -nE 'db\.sb\.' invoiceflow/main.py; then
      echo "ERROR: raw db.sb.* references found"
      exit 1
    fi

- name: Forbid require_auth/require_admin/require_super_admin (deleted in Phase B)
  run: |
    if grep -nE '\b(require_auth|require_admin|require_super_admin)\b' invoiceflow/*.py; then
      echo "ERROR: deleted dependency referenced"
      exit 1
    fi

- name: Forbid inline request.session reads outside api_logout
  run: |
    # Only api_logout is allowed to read request.session directly (it just
    # clears the session and exits). Everything else MUST go through
    # Depends(authed | admin_authed | super_admin_authed).
    matches=$(grep -nE 'request\.session\b' invoiceflow/main.py | grep -v 'request\.session\.clear()' || true)
    if [ -n "$matches" ]; then
      echo "ERROR: inline request.session read outside api_logout:"
      echo "$matches"
      exit 1
    fi
```

---

## 10. Resolved decision points (from v1 §8)

All 6 v1 open questions are now closed:

1. **JWT claim name (`role` vs `app_role`):** **`app_role`.** PostgREST's `SET LOCAL ROLE` consumes the `role` claim — collision is unavoidable. Migration 003a renames the claim and rewrites 8 policies. JWT carries `role: "authenticated"` (PostgREST) and `app_role: <actual>` (RLS).
2. **DAL pattern:** **ContextVar (B).** Reaffirmed by review. No `_session_only_ctx` footgun.
3. **Storage:** **`_sb_service` (no migration in Phase B).** Storage RLS is out of scope. Tenant isolation continues via `{company_id}/…` path-prefix in app code.
4. **JWT lib:** **`pyjwt>=2.8,<3` plain.** No `[crypto]` extra (HS256 is in pyjwt core).
5. **Hash validation in RPC:** **Bcrypt prefix regex** (with argon2 fallback for forward compat). Length floor was too loose.
6. **Backward-compat shim for `db.sb`:** **Removed.** All 5 raw call sites in main.py refactored to either DAL wrappers or `db._client()`. CI lint enforces.

---

_Last updated: 2026-04-26 — v2 post external review AND post re-review. Both reviews verdict-trail in `PHASE_B_REVIEW.md`. Ready for implementation pending §8 staging validations._
