# Phase B Prerequisites

> Status: **NOT YET MET** — do not deploy the Python JWT switch until every box on this checklist is ticked off.

Phase B switches the FastAPI backend from a single service-role Supabase client to a **per-request user-scoped client** built around freshly-minted user JWTs. RLS becomes effective at that moment. Anything that runs without an HTTP request — or runs *before* the request has a JWT — must explicitly continue to use a service-role client, otherwise it will hit RLS and fail.

This file enumerates the prerequisites identified during the Fase A external review (verdict: YELLOW; concrete prerequisites below). Update this file as items get implemented.

---

## 1. Pre-cutover database objects

These DB objects MUST exist in production before the Python cutover ships.

- [ ] **`change_own_password(text)` SECURITY DEFINER RPC**
  RLS cannot enforce column-level restrictions, so a normal user cannot UPDATE their own `password_hash` via direct DAL — there is no policy that lets a non-admin do `UPDATE users SET password_hash=… WHERE id=self`. We route password change through this RPC, which runs with the function-owner's privileges and is hardened to only mutate `password_hash` on `WHERE id = (auth.jwt() ->> 'user_id')::uuid`. Without this RPC, every non-admin who tries to change their own password gets 403.

- [ ] **JWT signing key configured**
  `SUPABASE_JWT_SECRET` (HS256) added to Render env. Same value as Supabase project's JWT secret.

---

## 2. Code paths that MUST keep the service-role client

These paths run without an authenticated user (or before authentication) and will break under RLS if they go through the user-scoped client.

| Path | File / function | Why service-role required |
|---|---|---|
| **Login flow** | `main.py` → `api_login` (~L2463) | Looks up user via `db.get_user(username, company_id)` *before* any JWT exists. Must read `users` as anon-equivalent. The bcrypt-burn timing-equalisation also depends on the dummy-hash path working when the user doesn't exist. |
| **App startup** | `main.py` → `ensure_default_admin()` | Bootstraps the seed admin on a fresh DB. Runs before any HTTP requests. |
| **Background queue worker** | `main.py` → `_queue_worker` (~L195) | Sequential job processor; no per-request scope. Calls `db.update_job`, `db.list_memory`, `db.update_memory`, `db.upsert_memory`, `db.create_invoice`, `db.storage_upload`. JWT issued at HTTP-request time would be expired by the time the job runs. |
| **Orphan-table access (if ever revived)** | none today | The existing `service_role`-only policies on `processed_invoices` / `tariff_cache` make these unreachable from a user JWT by design. Document if they're ever wired back into Python. |

**Implementation pattern:** keep the existing `db.sb` global as the service-role client (rename to `db._sb_service` for clarity). Add a new `db.sb_user_client(jwt: str) -> Client` factory that returns a per-request client built from the anon key + Authorization header. Refactor DAL functions to take an optional `client` parameter that defaults to the service-role client. Inject the user client from FastAPI dependency.

---

## 3. Code paths that MUST switch to the user-scoped client

Every HTTP handler that today goes through `db.*` (which uses the service-role client and BYPASSES RLS) must be reviewed and switched. RLS becoming effective is the entire point of Phase B — these handlers gain real tenant-isolation enforcement at the DB layer.

- [ ] All `/api/invoices/*` endpoints
- [ ] All `/api/jobs/*` endpoints
- [ ] All `/api/users/*` endpoints (except password-change → RPC)
- [ ] All `/api/companies/*` endpoints (super_admin only by app gate; also gated at RLS)
- [ ] All `/api/memory/*` endpoints

For each: confirm the DAL function takes the user-scoped client, and that the FastAPI dependency injects it correctly.

---

## 4. DAL functions to harden in Phase C (separate from Phase B cutover)

Identified during the Fase A review — these will be safe under RLS (cross-tenant rows return zero) but the Python error messages may reveal "not found" where "forbidden" would be more accurate. Not blocking the cutover, but track for Phase C:

- `database.py:89` — `get_user_by_id` has no `company_id` filter
- `database.py:243` — `get_job` has no `company_id` filter

Under Phase B, RLS makes these queries return zero rows for cross-tenant lookups — the Python code will translate that into a 404. That's an information-disclosure trade-off worth documenting (404 vs 403 reveals slightly different things to a probing attacker), but acceptable for now.

---

## 5. Pre-flight validation steps

Before the Phase B deploy hits production:

- [ ] Smoke test 1 — log in as a regular user, confirm `/api/me`, list invoices, list jobs all return correct rows.
- [ ] Smoke test 2 — log in as user from tenant A, attempt to fetch an invoice ID from tenant B (must 404).
- [ ] Smoke test 3 — log in as admin, create user with role=user (must succeed).
- [ ] Smoke test 4 — log in as admin, attempt to create user with role=super_admin (must 403, both at app layer AND silently denied by RLS WITH CHECK).
- [ ] Smoke test 5 — log in as admin, attempt UPDATE on a super_admin row in own tenant (must 0 rows affected — blocked by USING role-clamp from migration 002).
- [ ] Smoke test 6 — change own password via the RPC (must succeed).
- [ ] Smoke test 7 — kick off a job upload, verify queue worker still progresses through stages (must succeed; service-role client is what the worker uses).
- [ ] Smoke test 8 — restart the app, verify `ensure_default_admin()` still runs cleanly on first boot.

---

## 6. Rollback plan

If Phase B causes production breakage:

1. Revert the Python deploy (Render rollback) — this restores the service-role-only DAL. RLS stays enabled but service-role bypasses it, so the app keeps working.
2. If RLS itself causes problems (extremely unlikely given service-role bypasses): run `migrations/001_enable_rls_rollback.sql` in Supabase SQL Editor.
3. The `users_role_chk` CHECK constraint is independent of RLS and should stay; don't roll back item (3) of the rollback file unless there's a separate reason.

The forced-logout policy from Batch 1 means all currently-issued JWTs invalidate on the next deploy, so there's no "stale JWT under new code" risk.

---

## 7. Open issues found in Fase A review (track separately)

These are not Phase B blockers but are flagged for follow-up:

- `api_change_password` (main.py:2631) excludes `super_admin` from changing other users' passwords — a tenant-X super_admin can't reset a tenant-X user's password. Pre-existing bug, fix in a separate PR.
- `schema.sql` is stale: `users.role` comment says "admin or user", missing `super_admin`. Also doesn't reflect the `processed_invoices` / `tariff_cache` orphan tables.
- Audit logging for super_admin cross-tenant actions — out of scope for Batch 2; consider for a future security batch.

---

_Last updated: 2026-04-26 (post-Fase A external review)_
