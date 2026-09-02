# Phase B Plan — External Review

> Reviewer: external (skeptical mode). Reviewed against `PHASE_B_PLAN.md`,
> `PHASE_B_PREREQUISITES.md`, `001_enable_rls.sql`, `002_role_clamp_fix.sql`,
> the live `database.py` and `main.py`. Cross-checked against PostgREST and
> Supabase docs. Date: 2026-04-26.

---

## Verdict

**RED.** As written, the plan will break production on first deploy. The
single biggest reason — JWT claim collision on `role` between PostgREST's
`SET LOCAL ROLE` machinery and the existing RLS policies — is acknowledged
in the plan but **not yet resolved**, and resolving it is a non-trivial DB
migration. There are also 4–5 unaddressed call-site issues (raw `db.sb.table`
usage in main.py, storage RLS, queue-worker JWT lifetime, SECRET_KEY rotation
claim) that will manifest as 500s or RLS bypasses.

This is fixable, but the plan needs another iteration before any code is
written.

---

## Critical findings

### C1. JWT `role` claim is consumed by PostgREST itself; the existing policies cannot use the same name

**Severity:** CRITICAL
**Evidence:** `001_enable_rls.sql` lines 82, 117, 128, 135, 147, 161, 174,
187 all read `auth.jwt() ->> 'role'` and compare to `'super_admin'` /
`'admin'`. PostgREST's documented behaviour
(https://postgrest.org/en/stable/references/auth.html): *"When a request
contains a valid JWT with a role claim PostgREST will switch to the database
role with that name for the duration of the HTTP request"* via
`SET LOCAL ROLE`. So a JWT with `role: "admin"` makes PostgREST run
`SET LOCAL ROLE admin`, which fails because no Postgres role named `admin`
exists. Connection errors out → 500 response, RLS never even evaluated.
The plan's §2.3 already flags this as risky and proposes putting
`role: "authenticated"` at the top level alongside an *app-level* `role`
of `user`/`admin`/`super_admin` — but that's the same JSON key; you can't
have two values for the same claim. The plan's two-row table for §2.3
muddles this.
**Recommended fix:** rename the app-role claim to `app_role` (or
`x_role`/`tenant_role`). Ship a migration `003a_rename_role_claim.sql` that
drops and recreates **all** policies that reference `auth.jwt() ->> 'role'`
to use `auth.jwt() ->> 'app_role'` instead — that's 8 policies across 5
tables (companies_super_admin, users_admin_insert, users_admin_update,
users_admin_delete, users_super_admin, invoices_super_admin,
memory_super_admin, jobs_super_admin). Then put **only**
`role: "authenticated"` in the JWT. **This must land BEFORE the Python
cutover.** Running the rename migration is risk-free under service-role
(BYPASSRLS) — same deploy-safety argument as 001/002.

### C2. Storage will break: no RLS policies on `storage.objects`

**Severity:** CRITICAL
**Evidence:** Grep of `migrations/` shows zero references to
`storage.objects`, `storage.buckets`, `invoice-uploads`, or
`invoice-exports`. Supabase Storage default posture is deny-all without
explicit RLS policies. The service-role key bypasses storage RLS today, so
nothing has broken. The plan §3.2 flags storage as "decision point" but
doesn't specify a path. Once any `db.storage_*` call goes through the user
client (anon key + JWT), it will hit `storage.objects` RLS and fail with
the equivalent of "new row violates row-level security policy" — every
upload/download/signed-URL/delete in the app breaks.
**Affected call sites:** `main.py` L2123, L2124 (export uploads inside
queue worker → SAFE, worker stays service-role), L2936 (request-time upload
during `/upload` → BREAKS), L3015 (download during export → BREAKS), L3063,
L3106 (download during retry → BREAKS), L3078 (re-upload during retry →
BREAKS), L3139, L3162 (delete during cleanup → BREAKS).
**Recommended fix:** keep all `storage_*` DAL functions on `_sb_service`
(simplest, also matches "storage doesn't participate in our RLS anyway"
per §7 risk table). Make `storage_upload`/`download`/`signed_url`/`delete`
explicitly call `_sb_service.storage…`, not `_client().storage…`. Add a
comment explaining that storage isolation comes from path-prefixing on
`{company_id}/…` in application code, not from RLS. Optional Phase C:
add storage RLS policies that mirror the `company_id`-prefix-checks.

### C3. main.py has 5 `db.sb.table(...)` raw call sites the plan only acknowledges 1 of

**Severity:** CRITICAL
**Evidence:** Grep `db\.sb\b` in `main.py` shows:
- L2556 — delete company (super_admin endpoint)
- L3087 — delete failed job during retry
- L3143 — delete job
- L3259 — delete memory entry
- L3276 — cleanup invalid memory entries
The plan §3.2 mentions only L2556 and proposes "either the shim, or replace
with a DAL call". The other four are not mentioned. Under the plan as
written, the backward-compat shim `sb = _sb_service` keeps these working
but they bypass RLS entirely (silent privilege escalation past the per-
request user client). A user calling `DELETE /jobs/{id}` would delete *any*
job by id regardless of company_id — the user's session ctx restricts what
they can target, but only via the explicit `.eq("company_id",
ctx["company_id"])` chained on, which is a *Python* filter, not RLS.
**Recommended fix:** treat removal of all 5 `db.sb.table` call sites as
in-scope for Phase B. Add a CI lint (grep test) that `main.py` contains
zero matches for `db\.sb\.` post-Phase-B. Either route through DAL
functions (`db.delete_job_for_company(...)`, `db.delete_memory_entry(...)`)
or use `db._client().table(...)` directly so they participate in RLS.
The plan's "(a) keep shim" recommendation is too forgiving; pick (b).

### C4. The plan's §6 step 4 "deploy already invalidates all sessions" claim is wrong

**Severity:** CRITICAL (operational)
**Evidence:** `render.yaml` shows `SECRET_KEY` is `sync: false` — its value
is set in the Render dashboard and persists across deploys unless an
operator manually rotates it. The Starlette `SessionMiddleware`
(`main.py:2216`) signs cookies with `SECRET_KEY`; cookies remain valid
across code redeploys as long as that key is unchanged. So existing user
sessions *will* carry through the Phase B deploy, hit the new `authed` dep,
and immediately try to mint JWTs from session ctx that may be missing
fields (e.g., `company_id` — see M1) — or, worse, they keep working but
under stale role assumptions.
**Recommended fix:** either (a) make SECRET_KEY rotation an explicit
checklist item in §6 ("Step 0: rotate SECRET_KEY in Render dashboard
before pushing the deploy commit"), or (b) bump session cookie name
(e.g. `is_session` → `is_session_v2`) in the same commit so old cookies
are ignored. Option (b) is more reliable because it doesn't depend on
operator discipline.

---

## High-severity findings

### H1. JWT TTL of 5 minutes is too short for the long-running endpoints

**Severity:** HIGH
**Evidence:** `/memory/refresh-stale` (`main.py:2890`) and
`/memory/refresh` (`main.py:2849`) iterate over every memory entry and
`await lookup_tariff(code)` for each — calls trade-tariff.service.gov.uk
sequentially. For a tenant with 200 memory rows × ~1s/req that's already
3+ minutes; the upload+refresh sweep can plausibly cross 5 minutes during
a cold cache. Once the JWT expires mid-handler, the next DAL call returns
401/RLS-denied and the user sees a partial update + opaque error.
**Recommended fix:** mint the JWT with a longer TTL inside the request
scope (e.g. 30 min) — the security argument for 5 min was "leaked JWT
blast radius", but the JWT never leaves the server in this design (it's
constructed in `authed`, used by the Supabase client over HTTPS to
Supabase, and discarded). 30 min is fine. Alternative: re-mint inside the
loop, but that's invasive. Document the chosen TTL with rationale.

### H2. `_scoped_user_client` reset semantics: contextvar reset happens AFTER body sent for non-streaming, BEFORE for StreamingResponse

**Severity:** HIGH (latent — only bites if someone introduces a stream)
**Evidence:** Plan §3.3.1 wraps `async with _scoped_user_client(ctx)` in
the dep with `yield ctx`. FastAPI runs the teardown after the response
body is fully sent for buffered responses (today's exports use
`Response(content=...)` at `main.py:3018` — buffered → safe). But a future
contributor adding a `StreamingResponse(generator)` would see the dep's
`finally`/`__aexit__` run *before* the generator yields data — so the
generator's DAL calls would race against `_current_client.reset(token)`
and silently fall through to `_sb_service`.
**Recommended fix:** add an explicit lint/test or a docstring on `authed`
warning never to use `StreamingResponse` from generators that touch the
DB, OR rewrite the streaming pattern to fully buffer + use `Response`.
Add this as a §7 risk row.

### H3. `make_user_client` opens an httpx connection pool per request

**Severity:** HIGH (performance)
**Evidence:** `supabase==2.28.3`'s `create_client(...)` constructs a
`PostgrestClient` which constructs an `httpx.Client` (sync) or
`AsyncClient` (async) under the hood. That's a fresh TCP/TLS handshake to
the Supabase URL on first call of every request unless the SDK pools
across instances (it doesn't — pools are per-`Client`). At 10 req/s this
adds ~50–200 ms of TLS handshake to every request and saturates Supabase's
edge connections.
**Recommended fix:** verify in staging with a `time.perf_counter()` around
`db.make_user_client(...)`. If the cost is meaningful, switch the design
to: keep a single `_user_postgrest` pool and use
`postgrest_client.auth(jwt)` per request to attach the Bearer header. Or
keep one `Client` pool per anon key and only swap headers per request via
a request-scoped session (note: the supabase-py SDK doesn't expose this
cleanly — confirm with a spike).

### H4. `request.session.get("company_id", "")` empty-string fallback breaks UUID cast in policies

**Severity:** HIGH
**Evidence:** Plan §3.3.1 line 171: `"company_id": request.session.get(
"company_id", "")`. Every RLS policy does
`(auth.jwt() ->> 'company_id')::uuid` — a cast of `""` to uuid raises
`invalid input syntax for type uuid: ""` and the entire query 500s. Any
session with a missing `company_id` (corrupted, legacy pre-tenant, or post-
SECRET_KEY-rotation-with-stale-cookie) hits this.
**Recommended fix:** in `authed`, raise `HTTPException(401, "Session
missing company_id")` if the session lacks it. Don't fall back to empty
string — fail loud. Same hardening for `user_id` and `role`.

### H5. JWT lib: PyJWT's HS256 sign requires `bytes` for the key in some versions; `python-jose` is also fine

**Severity:** HIGH (correctness)
**Evidence:** Plan §3.5 says "PyJWT for simplicity". PyJWT >= 2.0 accepts
`str` for HS256, but the install must include the `[crypto]` extra ONLY
if you also use RS256/ES256 etc. For pure HS256 you don't need
`[crypto]`. The plan says `pyjwt[crypto]` which pulls in `cryptography`
unnecessarily and adds ~10MB to the wheel and a Rust-toolchain build step
on some platforms.
**Recommended fix:** use plain `pyjwt>=2.8` for HS256-only. Drop the
`[crypto]` extra unless you plan to add asymmetric signing later.

---

## Medium-severity findings

### M1. `authed` calls `make_user_client` synchronously inside an async dep

**Severity:** MEDIUM
**Evidence:** `create_client(...)` from supabase-py is synchronous and
performs filesystem/env reads + httpx Client construction. Calling it
inside `async def authed` blocks the event loop briefly. With the current
~30-handler sweep this fires per request → maybe 1–5 ms blocked per
request, low but non-zero.
**Recommended fix:** if H3's optimization (pool reuse) lands, this goes
away. Otherwise wrap in `asyncio.to_thread` if measurements warrant it.

### M2. `_session_only_ctx` (renamed `require_auth`) is a foot-gun

**Severity:** MEDIUM
**Evidence:** Plan §3.3.3 keeps `require_auth` for "tests, internal
calls" and notes "DAL falls through to service-role → bypasses RLS". A
future contributor wiring a new endpoint to `Depends(_session_only_ctx)`
gets full service-role access and no RLS — a privilege regression invisible
in code review.
**Recommended fix:** delete `_session_only_ctx` entirely. Tests can
construct a ctx dict directly (current `tests_user_admin.py:54` does this
already by passing a literal dict to `api_add_user`). Internal callers
(if any) should use `authed` or be refactored. Don't ship a footgun whose
only use case is "tests" when tests already work without it.

### M3. The `change_own_password` RPC reads `auth.jwt() ->> 'user_id'`, depends on a custom claim AND on `auth.jwt()` working inside SECURITY DEFINER

**Severity:** MEDIUM
**Evidence:** Migration 003 reads `(auth.jwt() ->> 'user_id')::uuid`.
Two assumptions:
1. The minted JWT contains a top-level `user_id` claim (plan §2.3 row
   "user_id" — OK, will be there).
2. `auth.jwt()` returns the caller's claims even inside SECURITY DEFINER.
   This is true in Supabase's implementation because `auth.jwt()` reads
   the session GUC `request.jwt.claims`, set by GoTrue/PostgREST per
   request, which is unaffected by SET ROLE. **But** it's worth a
   smoke test — staging only.
**Recommended fix:** add a §5.3 smoke test specifically for the RPC: log
in as a regular user, call the RPC, verify `password_hash` updated AND
that the same JWT cannot UPDATE another user's row directly. Belt-and-
braces: also verify `auth.jwt() ->> 'user_id'` is non-null inside the
function with a manual `SELECT change_own_password('not-a-real-hash')`
that should ERROR with "Invalid hash" not "No authenticated caller".

### M4. `users_admin_update` USING+CHECK and the `update_user_password` admin-changes-other-user path

**Severity:** MEDIUM (you asked specifically — answer: works, with one caveat)
**Evidence:** Policy at `001_enable_rls.sql:124` (post-002) requires
caller's `role` IN (admin, super_admin) AND target's OLD `role` IN (user,
admin) AND target's NEW `role` IN (user, admin). When admin updates
another user's password, only `password_hash` changes → existing `role`
flows through both USING and WITH CHECK unchanged. As long as target's
existing role is `user` or `admin`, the update succeeds — `password_hash`
is not column-restricted by the policy, so it's fine. **Caveat:** if an
admin tries to reset a super_admin's password in their own tenant, USING's
`role IN ('user','admin')` blocks it — admin can't reset super_admin's
password (same DoS-protection rule from migration 002). Probably fine, but
unstated in the plan.
**Recommended fix:** document in §3.3.5 that admins-resetting-super_admin
will return 0 rows / 404. Update `api_change_password` to return a
specific 403 in that case (look up target.role first; if it's super_admin
and caller is only admin, 403 explicitly).

### M5. Migration 003 not wrapped in BEGIN/COMMIT

**Severity:** MEDIUM
**Evidence:** `PHASE_B_PLAN.md:233-258` shows the SQL with no BEGIN/COMMIT.
Migration 001 and 002 both use BEGIN/COMMIT (`001_enable_rls.sql:20,190`,
`002_role_clamp_fix.sql:23,52`).
**Recommended fix:** wrap 003 in `BEGIN; … COMMIT;` to match house style
and so a partial failure (e.g. GRANT fails after CREATE OR REPLACE)
rolls back cleanly. Idempotency is fine via CREATE OR REPLACE; the GRANT
is also re-runnable.

### M6. Hash length validation in RPC is too loose for a defense-in-depth check

**Severity:** MEDIUM
**Evidence:** Plan migration 003 has `length(new_hash) < 20` as the
floor. A 20-char "hash" is e.g. `a` × 20 — passes. The function trusts the
Python layer to have actually bcrypt'd the password. If a future bug in
Python passes a plaintext password by accident, the RPC dutifully writes
it.
**Recommended fix:** check bcrypt prefix at the RPC level:
`new_hash !~ '^\$2[aby]\$\d{2}\$.{53}$'` raises. If you anticipate
switching to argon2, allow `^\$argon2(id|i)\$` in the same regex. This
is paranoid defense-in-depth; cheap to add.

### M7. The proposed `super_admin_authed` dep reuses `authed` — but how?

**Severity:** MEDIUM
**Evidence:** Plan §3.3.2 says `require_admin`/`require_super_admin`
"compose `authed` instead". Code is not shown. The natural shape is:
```python
async def admin_authed(ctx: dict = Depends(authed)) -> dict:
    if ctx["role"] not in ("admin", "super_admin"):
        raise HTTPException(403, "Admin required")
    return ctx
```
This works only if `authed` is structured as `async def` with `yield`
(generator dep) — the contextvar binding is a *side effect* of the
authed dep and persists for the duration of the *outer* dep chain. FastAPI
handles teardown of generator deps in reverse order, so the contextvar
is bound before `admin_authed` runs and reset after the handler returns.
**Recommended fix:** show this composition explicitly in the plan and
add a unit test that asserts `_current_client.get()` is non-None inside
a handler decorated with `Depends(admin_authed)`.

---

## Low / nits

### L1. `aud=authenticated` claim is supported but the plan should be explicit about whether it's required

The Supabase JWT spec uses `aud` to scope tokens to the project. PostgREST
checks it against `jwt-aud` config. Set it correctly to avoid surprise
401s. Not strictly a bug in the plan but worth making explicit.

### L2. `_url` and `_anon_key` checked at import time but only `_url` and `_service_key` validated today

`database.py:17` raises `RuntimeError` if `SUPABASE_URL`/`SUPABASE_KEY`
are missing. Add the same guard for `SUPABASE_ANON_KEY` — silent fall-
through to "" gives confusing 401s later.

### L3. `tests_rls_integration.py` description: "actually hit the live Supabase DB"

Brittle for CI. Two suggestions: (a) point at a separate Supabase project
for tests with a fixed schema seeded via a `conftest.py` truncate-and-
seed; (b) gate with `pytest.mark.integration` and skip by default in CI;
run nightly. Hitting the prod project from CI is a recipe for "intern
truncates production".

### L4. Logging: no mention of structured log lines for "user X minted JWT for company Y" audit trail

Add a single `logger.info("authed user_id=%s company_id=%s role=%s",
ctx["user_id"], ctx["company_id"], ctx["role"])` in `authed`. Every JWT
mint becomes auditable in Render logs without revealing the JWT itself.

### L5. `verify_password(password, _DUMMY_BCRYPT_HASH)` timing in login: untouched by Phase B

Just confirming: login keeps service-role; this timing-equalization stays
intact. Not a finding, just verified.

---

## Answers to the plan's 6 open decision points

### 1. JWT claim name (role vs app_role)

**`app_role` (rename mandatory).** Evidence in C1 above: PostgREST does
`SET LOCAL ROLE` from the JWT `role` claim. The current 8 RLS policies
read `auth.jwt() ->> 'role'` for the application role; reusing `role` for
both is a name collision that crashes connections. The fix is a small
DDL migration (drop+recreate 8 policies) to read `auth.jwt() ->>
'app_role'` and a one-line change in `auth_jwt.py` to put the app role
under `app_role` in the JWT body. This must land **before** the Python
cutover, in the same window as migration 003.

### 2. DAL pattern (contextvar vs explicit vs class)

**Contextvar (B), as proposed.** Reasoning matches the plan's table.
Caveats: see C3 (raw `db.sb.table(...)` in main.py needs to go) and M2
(don't ship a `_session_only_ctx` footgun). The "1-line opt-in via
`Depends(authed)`" is the right call-site cost; explicit-param
refactors of 30 functions are not worth the safety margin given that
the contextvar default falls back to a clearly-named `_sb_service`
(loud during code review).

### 3. Storage (keep _sb_service vs migrate)

**Keep on `_sb_service` (no migration in Phase B).** Evidence in C2:
no storage RLS policies exist. Storage isolation comes from `{company_id}/
…` path prefix in app code. The DAL's `storage_*` functions should
explicitly call `_sb_service.storage…`, not `_client().storage…`. Add
storage RLS policies as a Phase D or later batch if needed; not blocking
Phase B.

### 4. JWT lib (PyJWT vs python-jose)

**PyJWT, plain (no `[crypto]` extra).** HS256 is built into PyJWT's core
without requiring `cryptography`. python-jose has known maintenance
issues (slower release cadence, occasional security advisories). PyJWT
is well-maintained and 1MB lighter. Pin: `pyjwt>=2.8.0,<3` (2.x stable
API).

### 5. Hash validation in RPC (length vs format)

**Format regex.** See M6: a length floor of 20 is too loose. Use
`new_hash ~ '^\$2[aby]\$\d{2}\$.{53}$'` for bcrypt; if argon2 is on the
roadmap, use `'^(\$2[aby]\$|\$argon2(id|i)\$)'`. This catches the
"someone passed plaintext" bug and costs nothing.

### 6. Backward-compat shim for `db.sb`

**Remove the shim, fix all 5 call sites.** See C3. Keeping the shim
preserves an RLS-bypass surface that's invisible to the per-endpoint
sweep. The plan's recommendation of "(a) keep shim and update L2556"
silently keeps the other 4 raw `db.sb.table(...)` references in
production. Replacing with `db._client().table(...)` (or proper DAL
wrappers) takes the Python layer fully under RLS. Add a CI grep test:
`! grep -E 'db\.sb\.' invoiceflow/main.py`.

---

## Things the plan doesn't cover that it should

1. **Storage RLS posture** — already C2 above. The plan flags it as a
   "decision point" but doesn't actually decide. Decide before merging.
2. **Migration order/atomicity** — the plan ships migration 003 in step 1
   but leaves the proposed C1-fix (rename `role` claim) unspecified.
   Bundle 003a+003 in a single SQL transaction that runs before the code
   deploy. Document the exact order: SECRET_KEY rotation → DB migrations
   → env-var update in Render → Python deploy.
3. **Migration 003 rollback affects sessions** — if 003 is rolled back
   while Python is using the RPC, every password change 500s. The
   rollback file should include "must be paired with code rollback".
4. **CI lint for `db.sb.` references** — recommended in C3; should be a
   shipped artifact, not a one-time check.
5. **Server-side rate limiting on JWT minting** — not strictly
   exploitable (login already gates session creation), but worth
   measuring whether a session-cookie holder spamming `/api/me` (a non-
   authed endpoint that touches DB) generates load. Verify there's no
   path where a logged-in user can drive JWT minting at >1 req/s.
6. **Worker → service-role contract** — `_queue_worker` runs in a
   *thread*, not an asyncio task. Contextvars are inherited from the
   parent task at thread spawn time; if `_queue_worker` is started at
   module load (`main.py:216`), no contextvar is set → falls back to
   `_sb_service`. Good. But if any future code spawns a thread *from*
   a request handler (e.g. `BackgroundTasks` is a coroutine pool, but
   `threading.Thread(...)` could be added) and that thread does DAL
   calls, behavior is undefined (contextvars cross thread boundaries
   in CPython 3.7+ via copy_context, but not via raw threading).
   Document explicitly: "no DB-touching threads spawned from request
   handlers; use BackgroundTasks (which inherits contextvar) instead".
7. **Observability for forced 401 spikes** — §6 says "watch Render logs
   for 401/403 spikes for 1 hour". Define the threshold ("more than 5
   401s/min sustained for 5 min → rollback") so the on-call has a
   binary decision rule.
8. **Test for JWT TTL behavior** — the plan's `tests_jwt.py` covers
   "token at exp+1 invalid", but the failure mode at the *Supabase*
   side (PostgREST's clock skew tolerance) is not tested. Add a manual
   smoke test: use a JWT minted with `exp = now + 1` and confirm
   PostgREST returns the expected 401 within ~2 s.

---

## Things to test in staging FIRST that will break the plan if wrong

(Top 3, ordered by likelihood of breaking the deploy.)

### 1. JWT `role` claim collision (C1)

**Test:** mint a JWT with `role: "admin"` (current plan), call any
authenticated endpoint, expect 500. Then mint a JWT with `role:
"authenticated"` and `app_role: "admin"`, observe RLS policies still
need rewriting. **This test alone determines whether you need to ship
003a (the policy-rename migration) before the code deploy. If it
passes with `role: "admin"`, my reading of PostgREST docs is wrong —
test it before you trust me.**

### 2. Storage operations under user JWT (C2)

**Test:** mint a user JWT, build `_user_client(jwt)`, call
`_user_client.storage.from_("invoice-uploads").upload(...)`. Expect
an RLS denial. Confirm the workaround (keep storage on
`_sb_service`) preserves all upload/download behavior end-to-end —
upload an invoice via `/upload`, watch it process via the queue
worker (which is service-role), download the export.

### 3. Long-running tariff refresh under JWT TTL (H1)

**Test:** seed a tenant with 200 memory rows. Hit
`/memory/refresh-stale`. Time the full handler. If it crosses 5
minutes, the JWT expires mid-loop and you get partial-update
followed by 401-storm at the DAL. Adjust TTL to 30 min before
deploy.

---

_Reviewer note: I tried to be skeptical, not aggressive. C1 is a real
blocker; C2 and C3 are real blockers in slightly less obvious ways.
H1–H5 should land in the same iteration. Everything in M and L is
cleanup. The plan has good bones — the auth model is sound, the
contextvar choice is right, the RPC pattern is right. It needs one
more revision before any code is touched._

---

# Re-review of v2

> Reviewer: external (skeptical mode, second pass). Reviewed against
> `PHASE_B_PLAN.md` v2, the two new migrations (003a + 003) and their
> rollback files, plus a fresh re-grep of the live `main.py` and
> `database.py`. Date: 2026-04-26.

## Verdict

**YELLOW.** v2 is a serious, well-engineered response — every C/H
finding from v1 has been addressed in some form, the new migrations
are tight, and the staging gate concept is correct. But v2 introduced
two real new issues (the `/api/me` and `api_change_password` paths
that read `request.session` directly are not in the endpoint sweep,
and the cookie-bump snippet in §4.3.1 silently changes
`same_site`/`max_age`) plus the bcrypt regex needs verification of the
`!~` semantics in plpgsql. Ship after the small tweaks below; do NOT
ship the snippet in §4.3.1 verbatim.

## v1 finding closure status

**Critical:**
- **C1 (JWT `role` claim collision)** — **ADDRESSED.** Migration
  003a renames the app-role claim to `app_role` and rewrites all 8
  affected policies (counted: 8 in 001 / post-002, 8 in 003a — match).
  JWT mint in `auth_jwt.py` snippet sets `role: "authenticated"` and
  `app_role: ctx["role"]`. Clean.
- **C2 (Storage RLS)** — **ADDRESSED.** §4.2 binds all 4 storage
  functions (`storage_upload`, `storage_download`, `storage_signed_url`,
  `storage_delete`) to `_sb_service`. Spot-check of `database.py`
  confirms only those 4 storage functions exist — no `storage_list` or
  similar that the plan missed. §8.2 staging gate covers the
  user-client storage denial assertion.
- **C3 (5 raw `db.sb.table` sites)** — **ADDRESSED.** Re-grep
  `db\.sb\.` against current `main.py` returns the same 5 lines
  (L2556, L3087, L3143, L3259, L3276) — line numbers have NOT
  drifted since v1, so §4.3.5's table is accurate. CI lint specified
  in §9. Good.
- **C4 (SECRET_KEY rotation claim)** — **ADDRESSED** in concept.
  Cookie name bumped `is_session` → `is_session_v2`. But see new
  issue N2: the snippet also silently changes `same_site` from
  `strict` → `lax` and drops `max_age` entirely. That is NOT in the
  changelog and is a new security regression. Fix before ship.

**High:**
- **H1 (JWT TTL too short)** — **ADDRESSED.** Bumped to 30 min
  (`JWT_TTL_SECONDS = 1800`). §8.3 has a staging gate that times the
  long-running endpoint. **Caveat:** the plan repeatedly cites
  `/memory/refresh-stale` as the long-runner, but `main.py:2847`
  shows `/memory/refresh-tariff` (with `only_stale=False`) iterates
  EVERY memory entry unconditionally. That can be slower than
  `refresh-stale`. Mention both endpoints in §8.3 — or add an
  explicit assertion that `refresh-tariff` (full sweep) also fits
  inside 30 min.
- **H2 (StreamingResponse)** — **ADDRESSED** as a documented ban
  + risk row §7. Re-grep `StreamingResponse` in current `main.py`:
  zero matches today, so the assumption holds. The docstring on
  `authed` includes the ban. Acceptable.
- **H3 (per-request httpx pool)** — **PARTIALLY**. Deferred to
  staging perf gate §8.4 with a 50 ms p95 threshold and a pre-decided
  fallback (pooled `Client` + `postgrest.auth(jwt)` rebind per
  request). The threshold is reasonable: a fresh TCP+TLS handshake
  to Supabase typically costs 50–200 ms; pooled clients drop that to
  <5 ms. The test as described does measure the right thing
  (instrument `db.make_user_client` with `time.perf_counter` and load
  to 50 req/s). One residual concern: the `supabase-py` SDK does NOT
  expose a clean `postgrest.auth(jwt)` rebind on the same Client
  instance as a public API — the fallback may require a
  spike before it's known to work. Note this in §8.4 so the on-call
  doesn't discover it during the cutover.
- **H4 (empty-string company_id fallback)** — **ADDRESSED.**
  `authed` (§4.3.2) explicitly raises `HTTPException(401)` if any
  of `user_id`/`company_id`/`role` is missing. No `or ""` fallback
  remains. Verified: the `sess.get("user_id")`,
  `sess.get("company_id")`, `sess.get("role")` calls have NO default,
  and the `if not user_id or not company_id or not role` check fires
  before any UUID cast.
- **H5 (pyjwt[crypto] extra)** — **ADDRESSED.** §4.5 specifies
  `pyjwt>=2.8,<3` plain.

**Medium:**
- **M1 (sync `create_client` in async dep)** — **NOT EXPLICITLY
  ADDRESSED.** The plan does not mention `asyncio.to_thread`. v1
  review framed M1 as "if H3 fix lands, this goes away". v2's H3
  approach (pooled-client fallback) would also resolve M1. If the
  H3 perf gate passes with per-request creation, M1 stays open as
  ~1–5 ms event-loop block per request. Acceptable to ship.
- **M2 (`_session_only_ctx` footgun)** — **ADDRESSED.** §0
  changelog confirms removal. §2.2 confirms "no escape hatch".
- **M3 (`auth.jwt()` inside SECURITY DEFINER)** — **ADDRESSED.**
  §5.3 includes a smoke test for `change_own_password` end-to-end
  AND an invalid-hash test. Migration 003 itself has a comment
  explaining the GUC mechanism. Acceptable.
- **M4 (`users_admin_update` for super_admin reset)** — **ADDRESSED**
  in `api_change_password` (§4.3.4). The handler now checks
  `target["role"] == "super_admin" and ctx["role"] != "super_admin"`
  and returns 403 explicitly. The policy logic is correct: USING's
  `role IN ('user','admin')` clause from migration 002 blocks the
  admin's UPDATE on a super_admin row → returns 0 rows → Python
  returns 0-rows; the new explicit `if` check converts that to a
  clear 403 BEFORE hitting the DB. **One small bug: §4.3.4 also
  fixes the `!= "admin"` → `not in ("admin","super_admin")` typo
  from v1's review M4 spawn-task ("super_admin can't change another
  user's password"). Verified the new version correctly allows
  super_admin to reset others' passwords.**
- **M5 (Migration 003 BEGIN/COMMIT)** — **ADDRESSED.** 003 is
  wrapped in `BEGIN; … COMMIT;`. 003a is also wrapped. Good.
- **M6 (RPC hash validation)** — **ADDRESSED.** Regex
  `^(\$2[aby]\$\d{2}\$.{53}|\$argon2(id|i)\$.+)$`. **I tested this
  regex against real bcrypt outputs (`$2a$/$2b$/$2y$`, rounds 04
  through 31, exactly 53 chars after the third `$`) and it accepts
  all valid forms; rejects plaintext, short hashes, single-digit
  rounds, and argon2d (only argon2id/argon2i allowed). Postgres's
  regex engine supports `\d` as digit class, `{53}` quantifiers,
  and alternation `|` — the SQL is syntactically valid.** One nit:
  the regex anchors `^` and `$` are inside a single regex group (the
  alternation parens) — correct. Verified.
- **M7 (`super_admin_authed` composition)** — **ADDRESSED.** §4.3.2
  shows the explicit composition. §5.2 includes the contextvar
  presence assertion as a new test.

**Low:**
- **L1 (`aud` claim explicit)** — **ADDRESSED.** §2.3 table sets
  `aud: "authenticated"` with an explanation.
- **L2 (`SUPABASE_ANON_KEY` env validation)** — **ADDRESSED.**
  §4.2 raises `RuntimeError` if any of URL/key/anon-key is missing.
- **L3 (separate test Supabase project)** — **ADDRESSED.** §5.2
  explicitly mentions a separate project, `pytest.mark.integration`
  gate, default-skip, conftest truncate-and-seed. Good.
- **L4 (audit log line in `authed`)** — **NOT ADDRESSED.** v2's
  `auth_jwt.py` and `authed()` snippets have NO log line. This is
  not a blocker, but a missed cheap win for ops. Add a one-line
  `logger.info("authed user_id=%s company_id=%s role=%s", ...)` in
  `authed` post-mint. Importantly, do NOT log the JWT itself or
  the `_secret`. Verified the plan never logs either — clean.
- **L5 (login bcrypt timing)** — **ADDRESSED** by being unchanged
  (login keeps service-role; the dummy-hash path stays).

**Total v1 finding closure: 4/4 critical, 4/5 high (H3 partial), 6/7
medium (M1 deferred), 4/5 low (L4 deferred). Acceptable spread; no
critical blockers regress.**

## New issues introduced by v2

### N1 (HIGH). `/api/me` and `api_logout` read `request.session` directly — not in the endpoint sweep

`main.py:2566-2585` (`api_me`) and `main.py:2560-2563` (`api_logout`)
do not use `Depends(require_auth)` today; they read
`request.session.get("user_id")` directly. The plan's §4.3.3 sweep
table only replaces `Depends(require_auth | require_admin |
require_super_admin)` and explicitly says "approximately 30+ call
sites" — counted: **20 + 3 + 3 = 26 today**, not 30+. The plan misses
**`api_me`** and **`api_logout`**, which are authenticated endpoints
that touch DB (`get_user_by_id`, `list_companies`).

After Phase B ships:
- `api_me` runs with NO contextvar set → falls through to
  `_sb_service` → bypasses RLS. Today's behaviour is preserved
  silently. That's not a security regression because
  `_sb_service` was the only client before, but it's a contract
  violation of "every authenticated request runs under user JWT".
  And `db.list_companies()` in `api_me` is a particular concern:
  under user JWT it would only return the user's own company
  (per the `companies_tenant_select` policy), but under
  `_sb_service` it returns ALL companies. The Python code filters
  by `c["id"] == user["company_id"]` so the visible behaviour is
  unchanged — but the policy is moot.
- `api_logout` doesn't touch DB, so it's harmless to leave.

**Recommendation:** add an explicit row in §4.3.3 covering `api_me`
(refactor to `Depends(authed)`) and call out `api_logout` as
intentionally session-only (no DB). The CI lint in §9 is also too
narrow: it greps for `db.sb.` but not for "endpoint reads
`request.session` directly without `Depends(authed)`". Add a note
that authenticated endpoints reading session directly are a smell.

### N2 (HIGH). Cookie middleware snippet in §4.3.1 silently weakens `same_site` and drops `max_age`

Current `main.py:2215-2222`:
```python
SessionMiddleware(secret_key=SECRET_KEY, session_cookie="is_session",
                  max_age=60*60*12, https_only=not DEV_MODE,
                  same_site="strict")
```

Plan §4.3.1 snippet:
```python
SessionMiddleware(secret_key=SECRET_KEY, session_cookie="is_session_v2",
                  https_only=True, same_site="lax")
```

Three silent changes:
1. **`same_site="strict"` → `"lax"`** — weakens CSRF posture for
   top-level GETs. May or may not be intentional, but is NOT in the
   §0 changelog. The original was strict-by-design (see comment
   block above the middleware in `main.py:2210-2214`).
2. **`max_age=60*60*12` (12 h) DROPPED** — without `max_age`,
   Starlette's SessionMiddleware emits a session-only cookie that
   dies at browser close. That's a UX regression (users re-login
   every browser restart) and an undocumented change in posture.
3. **`https_only=not DEV_MODE` → `True`** — fine for prod but
   breaks local DEV_MODE testing.

**Recommendation:** §4.3.1 should specify the cookie line as
"change ONLY `session_cookie="is_session"` to
`session_cookie="is_session_v2"`" and leave the rest as-is, OR
explicitly call out the change in the §0 changelog with rationale.
As written, an implementer copying the snippet introduces three
unintended changes.

### N3 (MEDIUM). Plan's deploy-step §6 verify query is laxer than 003a's verify

§6 step 1 says verify with:
```sql
SELECT policyname, qual FROM pg_policies
WHERE schemaname='public' AND qual ~ 'role'
```

That regex matches `'role'`, `'app_role'`, AND any policy with the
column-level `role IN ('user','admin')` clause (which includes
`users_admin_*`). A passing run of that query returns 5+ rows with
no failure signal. The 003a.sql VERIFY block at the bottom is
stricter (`~* 'auth\.jwt\(\)\s*->>\s*''role'''`) and is the one to
trust.

**Recommendation:** §6 step 1 should reference "run the VERIFY
queries at the bottom of `003a_rename_role_to_app_role.sql`". Don't
restate a weaker query inline; it's a footgun for the on-call.

### N4 (LOW). H3 fallback assumes `postgrest.auth(jwt)` is a public API

The plan's §2.4 perf note and §7 risk row both pre-decide the
fallback as "swap to a single pooled `Client` and call
`client.postgrest.auth(jwt)` per request". `supabase-py`'s
`Client.postgrest` exposes `auth(token: str)` in the underlying
`PostgrestClient`, but it's been part of the public surface
inconsistently across versions. If the perf gate fails and the
fallback is needed, an implementer may discover the rebind doesn't
behave as expected (e.g., it may set the header but not invalidate
a cached schema fetch). Worth a 30-min spike against
`supabase==2.28.3` before treating §8.4 as a low-risk fallback path.

**Recommendation:** add a one-line note to §8.4 / risk H3:
"Confirm `client.postgrest.auth(jwt)` rebinds the Authorization
header on the next request without recreating connections. Test
in staging before relying on as fallback."

## Migration 003a correctness

All 8 policies present and renamed correctly:

| Policy | In 003a? | USING preserves post-002 role-clamp? | WITH CHECK preserves? |
|---|---|---|---|
| `companies_super_admin` | yes (L35-39) | n/a (super_admin gate) | yes |
| `users_admin_insert` | yes (L46-53) | n/a (INSERT only) | yes (`role IN ('user','admin')`) |
| `users_admin_update` | **yes (L58-69) — KEY ITEM** | yes (`role IN ('user','admin')` from 002) | yes (`role IN ('user','admin')`) |
| `users_admin_delete` | **yes (L74-81) — KEY ITEM** | yes (`role IN ('user','admin')` from 002) | n/a (DELETE only) |
| `users_super_admin` | yes (L86-90) | super_admin gate | yes |
| `invoices_super_admin` | yes (L95-99) | super_admin gate | yes |
| `memory_super_admin` | yes (L104-108) | super_admin gate | yes |
| `jobs_super_admin` | yes (L113-117) | super_admin gate | yes |

**The two critical post-002 role-clamp preservations are correct.**
Cross-checked `users_admin_update` 003a L62-65 against 002 L28-34:
- USING: `company_id = (auth.jwt() ->> 'company_id')::uuid AND
  (auth.jwt() ->> 'app_role') IN ('admin', 'super_admin') AND
  role IN ('user', 'admin')` — matches 002, only the JWT key
  changed.
- WITH CHECK: `company_id = (auth.jwt() ->> 'company_id')::uuid AND
  role IN ('user', 'admin')` — matches 002.

Cross-checked `users_admin_delete` 003a L77-81 against 002 L43-50:
identical except for the JWT key rename. Good.

No policy is accidentally weakened. None of the 5 tenant-only
policies (which read just `company_id`) are touched, which is
correct.

**VERIFY queries at the bottom of 003a:**
- Query 1 (looking for any policy still referencing
  `auth\.jwt\(\)\s*->>\s*'role'`): correctly written. The SQL
  string-quoting `'auth\.jwt\(\)\s*->>\s*''role'''` decodes to the
  regex `auth\.jwt\(\)\s*->>\s*'role'`. The literal `'` before
  `role` in the regex distinguishes it from `'app_role'` (which
  starts with `'app`). Test confirmed: regex matches
  `'role'::text` rendering, does NOT match `'app_role'::text`.
- Query 2 (looking for `app_role`): mirror-image, correctly
  written. Should return 8 rows post-migration.
- The third sanity check (count of policies = 13) is good. (5
  tenant-scoped policies + 8 renamed = 13. Original 13 from 001 was
  2+5+2+2+2; 003a touches 8 of them, leaves 5 alone. Total still
  13.)

**003a rollback file:** restores all 8 policies to the original
`'role'` claim, wrapped in BEGIN/COMMIT, syntactically clean. Good.

**No "forgotten policy" risk:** I cross-checked
`grep "auth.jwt() ->> 'role'" 001_enable_rls.sql` mentally:
- Line 82 (companies_super_admin) — covered
- Line 117 (users_admin_insert) — covered
- Line 128 (users_admin_update) — covered (also re-tightened by 002)
- Line 135 (users_admin_update WITH CHECK — column-level role, NOT
  JWT, so unchanged)
- Line 142 (users_admin_delete) — covered (also re-tightened by 002)
- Line 147 (users_super_admin) — covered
- Line 161 (invoices_super_admin) — covered
- Line 174 (memory_super_admin) — covered
- Line 187 (jobs_super_admin) — covered

That's 8 USING/CHECK occurrences of `auth.jwt() ->> 'role'`, all 8
in 003a. Match.

## Migration 003 correctness

- **`SECURITY DEFINER`** — present (L37).
- **`SET search_path = public, pg_temp`** — present (L38). Critical
  for security; protects against search-path-based privilege
  escalation as flagged in v1.
- **Bcrypt regex** —
  `^(\$2[aby]\$\d{2}\$.{53}|\$argon2(id|i)\$.+)$` (L51).
  - Accepts: `$2a$`, `$2b$`, `$2y$` with rounds 04-31 (always 2
    digits) followed by exactly 53 chars (22-char salt + 31-char
    hash). Tested against real bcrypt outputs from
    `passlib.hash.bcrypt`: passes.
  - Accepts: argon2id and argon2i with `.+` body. Forward-compat
    only; no length check — but the prefix is enough to catch the
    "passed plaintext" case.
  - Rejects: plaintext, single-digit rounds, wrong-length bcrypt,
    argon2d, empty string. Good.
  - Postgres regex engine supports `\d`, `{N}` quantifiers, and
    `|` alternation. The `!~` operator does case-sensitive POSIX
    regex match; the regex compiles cleanly under Postgres's
    parser.
- **GRANT/REVOKE pair** —
  `REVOKE ALL ON FUNCTION ... FROM PUBLIC` then
  `GRANT EXECUTE ... TO authenticated` (L65-66). Correct ordering;
  PUBLIC default is removed before the per-role grant. Service-role
  keeps access via BYPASSRLS / superuser-equivalent privilege.
- **BEGIN/COMMIT** — present (L24, L68). Matches house style.
- **`RAISE EXCEPTION 'No authenticated caller'` on null user_id** —
  good. The `(auth.jwt() ->> 'user_id')::uuid` cast on a null
  intermediate would itself raise, so the explicit IF is
  belt-and-braces but cheap. Acceptable.
- **`UPDATE … WHERE id = caller_id`** — single-row update on a
  primary key. Idempotent; safe.
- **VERIFY block** — three queries (function exists +
  `prosecdef=true` + `proconfig` contains `search_path`; GRANTs
  show only `authenticated/EXECUTE`; manual plaintext rejection).
  Sufficient.

**003 rollback file:** simple `DROP FUNCTION IF EXISTS`. Comment
correctly warns that running rollback while Phase B code is live
breaks every `change_own_password` call. Operational guidance is
sound.

## Final go/no-go

**Recommendation: (b) ship after small tweaks.**

Required tweaks before the production deploy:
1. **Fix §4.3.1 cookie snippet (N2, HIGH).** Either keep
   `same_site="strict"`, restore `max_age=60*60*12`, and keep
   `https_only=not DEV_MODE`, OR add an explicit changelog row +
   rationale for each of the three changes. As written, the snippet
   silently ships three undocumented behaviour shifts.
2. **Cover `api_me` in the §4.3.3 sweep (N1, HIGH).** Either
   refactor it to `Depends(authed)` (preferred) or document
   explicitly why it stays on direct `request.session` access and
   that it will not have the user JWT context bound. (`api_logout`
   is fine to leave session-only.)
3. **Replace the §6 step 1 verify query with "run the VERIFY block
   at the bottom of 003a.sql" (N3, MEDIUM).** The inline query is
   too lax.
4. **Add a 30-min spike note to §8.4 / risk H3 (N4, LOW).** Confirm
   `client.postgrest.auth(jwt)` rebind behaviour against
   `supabase==2.28.3` before relying on it as the H3 fallback.

Optional (not blocking):
- Add a one-line `logger.info` audit line in `authed` (L4 was
  deferred). Cheap ops win.
- §8.3 should also benchmark `/memory/refresh-tariff` (full sweep)
  not just `/memory/refresh-stale`.
- M1 (`asyncio.to_thread` wrap) deferred is fine, but mention it
  in the post-deploy follow-up list.

The migrations themselves (003a + 003) are correct as-is and can
ship verbatim. The Python plan needs the 4 tweaks above before the
Phase B deploy commit is opened.

_Re-reviewer note: v2 is a substantial improvement over v1. The
auth model is correct, the JWT claim namespace is fixed, the
storage carve-out is clean, and the migrations are tight. The
issues above are all in the "implementer copies a snippet
verbatim" category — fixable with one more pass over §4.3.1 and
the endpoint sweep table. The bcrypt regex is correct, the policy
preservation is correct, and the staging gates are well-designed.
After the four tweaks, this is GREEN._

---

## Final code review — 2026-04-26

> Reviewer: external (independent, third pass). Reviewed against the
> committed code in `auth_jwt.py`, `database.py`, `main.py`,
> `render.yaml`, `requirements.txt`, `scripts/check_no_raw_sb.sh`, the
> four migration files (003 + 003a, with rollbacks), and cross-checked
> against `001_enable_rls.sql` + `002_role_clamp_fix.sql`. Live grep
> counts run on the actual files. Date: 2026-04-26.

### Verdict

**GREEN.** The Phase B code is correctly implemented against v2's
specification, the four tweaks called out in the v2 re-review have all
landed, and there are no critical or high regressions. Two low-severity
items below (one missed docstring contract, one slightly broad lint
allowlist) are nits — they do not gate the deploy. Ship after running
the operational steps in §6 of the plan.

### Findings table

| Severity | Summary | File:line | What's wrong | Suggested fix |
|---|---|---|---|---|
| Low | `authed` docstring drops the StreamingResponse ban | `main.py:158-173` | Plan §4.3.2 explicitly required a "DO NOT use this with StreamingResponse from a generator" warning in the `authed` docstring (review H2's safety contract). The docstring currently lists numbered behaviour but no streaming ban. The risk is dormant today (no `StreamingResponse` in main.py), but the safety contract review H2 and plan §8/§7 promised in the source-of-truth location is missing. | Add a 1-2 line warning in the `authed` docstring: "DO NOT use this with `StreamingResponse(generator)`: the contextvar reset fires when this dep tears down, which happens BEFORE the generator yields. All current responses are buffered (Response/JSONResponse). See PHASE_B_PLAN.md §8 (H2)." |
| Low | Lint allowlist for `request.session[` is broader than necessary | `scripts/check_no_raw_sb.sh:56` | The allowlist accepts ANY `request.session[…]` form, both writes (`session["k"] = v`, the legitimate login pattern) and reads (`x = session["k"]`, which Phase B forbids — handlers should go through `Depends(authed)`). A future contributor reading the session inline via subscript syntax slips past the lint. Today the only subscript usage is the four login writes at L2550-2553, so this is dormant. | Tighten the third grep stage to require `=` after `request.session[…]` (assignment), e.g. `grep -vE 'request\.session\[[^]]+\]\s*='`, so reads are still flagged. Optional: add a comment note explaining the assignment-only scope. |
| Nit | Plan-prompt expectation of "26 Depends sites" undercounts by 1 | n/a (the prompt, not the code) | The review prompt says "26 total Depends sites (20 authed + 3 admin + 3 super_admin). Confirm." Counting the actual code, the correct expected total is **27** (21 + 3 + 3): the v2 re-review N1 fix correctly added `api_me` to the sweep, bringing `Depends(authed)` to 21 endpoint sites. The prompt's 26 reflects the pre-N1-fix count. The code is RIGHT; the prompt's expected total just needs +1 for `api_me`. | No code change. Sanity-grep summary below shows 27 actual sites; this is the correct post-N1 number. |
| Nit | `db._client()` is module-private but accessed by `main.py` | `main.py:2696`, `main.py:190`, `main.py:198` | Three sites in main.py reach into `database.py` to use module-private symbols (`db._client()`, `db._current_client.set/reset`). Convention violation, intentional per plan §4.3.4. Documented in `database.py:48` ("never imported directly by main.py"). Not a bug — just worth noting that the lint enforces only `db.sb.` and not the broader "no underscore-prefixed access" rule. | Optional: add `_client` and `_current_client` to a short documented "blessed cross-module accessors" list in `database.py`, or rename them without leading underscore to make the contract explicit. |

No Critical, High, or Medium findings. The four low/nit items are all
non-blocking.

### Resolution check vs prior reviews

**v1 critical/high findings:**

| ID | Status | Notes |
|---|---|---|
| C1 | Resolved cleanly | Migration 003a renames the JWT app-role claim to `app_role` and rewrites all 8 affected policies. `auth_jwt.py:47` hardcodes `role: "authenticated"` (PostgREST connection role). `auth_jwt.py:52` carries `app_role: ctx["role"]` (RLS predicate). Decoded JWT confirms via test mint: `role=authenticated`, `app_role=admin`, `aud=authenticated`, `sub=<user_id>`, `exp - iat = 1800`. |
| C2 | Resolved cleanly | All 4 storage functions in `database.py:103-126` (`storage_upload`, `storage_download`, `storage_signed_url`, `storage_delete`) bind explicitly to `_sb_service.storage`. No `_client().storage` references anywhere. Spot-checked all 9 storage call sites in `main.py` (L2156-2157, L3011, L3090, L3138, L3153, L3182, L3215, L3239) — every path string starts with `{company_id}/…`, preserving the path-prefix tenant isolation contract. |
| C3 | Resolved cleanly | All 5 raw `db.sb.table(…)` sites refactored: L2598 → `db.delete_company`, L3163 → `db.delete_job`, L3220 → `db.delete_job`, L3338 → `db.delete_memory_entry`, L3352 → `db.delete_memory_entry`. New DAL wrappers exist at `database.py:147` (`delete_company`), `:301` (`delete_memory_entry`), `:338` (`delete_job`). Live grep `db\.sb\.` in `main.py` returns 0 matches. CI lint at `scripts/check_no_raw_sb.sh` enforces. |
| C4 | Resolved cleanly | Cookie name bumped to `is_session_v2` at `main.py:2258`. The N2 concern from v2 re-review is fixed: `same_site="strict"` (L2261), `max_age=60*60*12` (L2259), `https_only=not DEV_MODE` (L2260) are all preserved verbatim. SECRET_KEY rotation no longer required. |
| H1 | Resolved cleanly | `auth_jwt.py:31` sets `JWT_TTL_SECONDS = 1800` (30 min). Test mint confirms `exp - iat = 1800`. |
| H2 | Resolved but… | The risk is documented in the plan §7/§8, but the `authed` docstring in `main.py:158-173` does NOT include the StreamingResponse ban that plan §4.3.2 promised. See finding #1 above (Low). No `StreamingResponse` exists in main.py today, so no live exposure. |
| H3 | Resolved (deferred) | `database.py:69-86` builds a fresh `Client` per request as planned. Plan §8.4 deferred verification to staging perf-test. The fallback (pooled-client + `postgrest.auth(jwt)` rebind) requires the 30-min spike noted in N4 — operational item, not a code regression. |
| H4 | Resolved cleanly | `main.py:178` raises `HTTPException(status_code=401, detail="Not authenticated")` if any of `user_id`/`company_id`/`role` is falsy. No `or ""` fallback. UUID cast in policies cannot fail because the values are guaranteed non-empty by this gate. |
| H5 | Resolved cleanly | `requirements.txt:17` is `pyjwt>=2.8,<3` plain. No `[crypto]` extra. Confirmed via `python -c "import jwt; print(jwt.__version__)"` → `2.12.1` works without `cryptography`. |

**v1 medium findings (selective check):**

- M2 (`_session_only_ctx` footgun): grep across `invoiceflow/` returns 0 matches in code. Resolved.
- M3 (`auth.jwt()` inside SECURITY DEFINER): plan §5.3 includes the smoke test. Resolved (operational verification gate).
- M4 (`api_change_password` admin-resets-super_admin): explicit 403 at `main.py:2711-2712`. Resolved.
- M5 (Migration 003 BEGIN/COMMIT): present at L24/L68. Resolved.
- M6 (RPC hash regex): `^(\$2[aby]\$\d{2}\$.{53}|\$argon2(id|i)\$.+)$` at `003_change_own_password_rpc.sql:51`. Resolved.
- M7 (`super_admin_authed` composition): present at `main.py:201-213`. Both `admin_authed` and `super_admin_authed` chain off `Depends(authed)`. Resolved.

**v2 re-review findings:**

| ID | Status | Notes |
|---|---|---|
| N1 | Resolved cleanly | `api_me` at `main.py:2609` now uses `Depends(authed)` (was previously inline `request.session.get`). `api_logout` at L2602-2605 left intentionally session-only (only `request.session.clear()`). The CI lint at `scripts/check_no_raw_sb.sh:54-64` enforces "no inline `request.session` reads outside login writes / authed / logout". |
| N2 | Resolved cleanly | Cookie middleware at `main.py:2248-2262` changes ONLY `session_cookie` to `is_session_v2`. Preserves `same_site="strict"`, `max_age=60*60*12`, `https_only=not DEV_MODE`. The §0 concern about silent regressions is no longer present. |
| N3 | Operational item | The lax inline VERIFY query is no longer in the plan; plan §6 step 1 references the strict VERIFY block at the bottom of `003a_rename_role_to_app_role.sql`. The 003a VERIFY queries (L122-143) are tight: query 1 uses `~* 'auth\.jwt\(\)\s*->>\s*''role'''` (matches `'role'` exclusively, not `'app_role'`), query 2 mirrors for `'app_role'`, query 3 verifies total policy count = 13. Decoded the SQL string-quoting manually — correct. |
| N4 | Operational item | Plan §8.4 contains the pre-spike note about confirming `client.postgrest.auth(jwt)` rebinds correctly against `supabase==2.28.3` before relying on the pooled-client fallback. Not a code item. |

### Migration cross-checks

**Migration 003a** (`003a_rename_role_to_app_role.sql`):
- 8 policies dropped + recreated, in the order from the plan (companies_super_admin, users_admin_insert, users_admin_update, users_admin_delete, users_super_admin, invoices_super_admin, memory_super_admin, jobs_super_admin). Match.
- All 8 read `auth.jwt() ->> 'app_role'` (not `'role'`). The column-level `role IN ('user','admin')` on `users_admin_insert/update/delete` is unchanged — that's a row-column read, not a JWT claim.
- USING role-clamp from migration 002 preserved on `users_admin_update` (L64) and `users_admin_delete` (L80). Spot-checked: identical to 002 except the JWT claim key.
- VERIFY block (L121-143): 3 queries as expected, regex correct.
- Rollback file (`003a_…_rollback.sql`) wraps in BEGIN/COMMIT and recreates all 8 with `'role'`. Mirror image. Good.

**Migration 003** (`003_change_own_password_rpc.sql`):
- `SECURITY DEFINER` ✓ (L37)
- `SET search_path = public, pg_temp` ✓ (L38) — critical for safety
- Bcrypt regex ✓ (L51) — `^(\$2[aby]\$\d{2}\$.{53}|\$argon2(id|i)\$.+)$`
- `REVOKE ALL FROM PUBLIC` then `GRANT EXECUTE TO authenticated` ✓ (L65-66)
- Wrapped in BEGIN/COMMIT ✓ (L24, L68)
- Caller_id read via `(auth.jwt() ->> 'user_id')::uuid` ✓ (L43); explicit IF-NULL check at L44; UPDATE on `WHERE id = caller_id` ✓ (L58)
- Rollback file: simple `DROP FUNCTION IF EXISTS`, with operational warning to run AFTER code rollback ✓

### Sanity grep summary (live counts)

```
$ grep -nE 'Depends\(require_' invoiceflow/main.py | wc -l
0
$ grep -nE 'db\.sb\.' invoiceflow/main.py | wc -l
0
$ grep -nE 'request\.session' invoiceflow/main.py | wc -l
7
  → 1 read in `authed` (L174, allowlisted: `sess = request.session`)
  → 4 writes in `api_login` (L2550-2553, allowlisted: `request.session["…"] = …`)
  → 1 write in `api_logout` (L2604, allowlisted: `request.session.clear()`)
  → 1 comment in `api_me` (L2617, allowlisted: contains `request.session.clear`)
$ grep -nE '\b_sb\b' invoiceflow/main.py | wc -l
0   (main.py never names the service-role client)
$ grep -c 'Depends(authed)' invoiceflow/main.py
25  (= 21 endpoint sites + 1 docstring at L171 + 1 docstring at L2610 + 2 dep definitions at L201,L209)
$ grep -c 'Depends(admin_authed)' invoiceflow/main.py
3   (api_list_users, api_add_user, api_delete_user)
$ grep -c 'Depends(super_admin_authed)' invoiceflow/main.py
4   (= 3 endpoint sites + 1 in-comment reference at L2597)
```

**Endpoint-site breakdown** (after stripping non-call-site matches):
- `Depends(authed)`: 21 endpoints (api_me, api_change_password, tariff_search, refresh_memory_tariff, refresh_stale_tariff, upload_invoice, list_jobs, get_stats, list_invoices, invoice_debug, export_full, export_raw, retry_job, retry_invoice, delete_job, delete_invoice_endpoint, resolve_invoice, list_memory, confirm_memory, delete_memory_entry, cleanup_invalid_memory)
- `Depends(admin_authed)`: 3
- `Depends(super_admin_authed)`: 3
- **Total = 27.** The plan-prompt's 26 was the pre-N1-fix count; the real total post-N1 is 27, which is what the code shows. Match.

`scripts/check_no_raw_sb.sh` runs clean: `→ Phase B lint: scanning … ✓ Phase B lint passed.` (exit 0).

**JWT structural verification** (programmatic decode of a freshly-minted token):
```
type: str (pyjwt 2.x default)
parts: 3 (header.payload.signature)
payload keys: ['app_role', 'aud', 'company_id', 'exp', 'iat', 'iss',
               'role', 'sub', 'user_id', 'username']
role:       authenticated   ← PostgREST connection role (CORRECT)
app_role:   admin            ← RLS predicate (CORRECT)
aud:        authenticated   ← Supabase audience check
sub:        <user_id>       ← auth.uid() reads this
user_id:    <user_id>       ← RPC reads (auth.jwt() ->> 'user_id')
company_id: <company_id>    ← every tenant policy reads this
exp - iat:  1800             ← 30 min, matches H1 fix
```

### Things to do before deploying to prod

Code is ready. The remaining items are operational, mostly from plan §6:

1. **Apply migration 003a** in Supabase SQL Editor for the production project. After COMMIT, run the three VERIFY queries at the bottom of `003a_rename_role_to_app_role.sql`:
   - Query 1 (policies referencing old `'role'` claim) → expect 0 rows.
   - Query 2 (policies referencing new `'app_role'` claim) → expect 8 rows.
   - Query 3 (total policy count) → expect 13.
2. **Apply migration 003** in Supabase SQL Editor. Verify per the comments at the bottom of `003_change_own_password_rpc.sql`: function exists, `prosecdef=true`, `proconfig` contains `search_path=public, pg_temp`, GRANT only to `authenticated`.
3. **Configure Render secrets** (both `sync: false` in `render.yaml`):
   - `SUPABASE_ANON_KEY` — paste the project anon JWT from Supabase Settings → API.
   - `SUPABASE_JWT_SECRET` — paste the JWT secret from Supabase Settings → API → JWT Settings (must match exactly; HS256 will fail otherwise).
4. **Run staging perf gate (§8.4):** load-test 50 req/s against `/api/invoices` for 60s with `time.perf_counter()` instrumentation around `db.make_user_client`. Pass criteria: p95 < 50ms. If it fails, do the H3 fallback spike (§8.4 paragraph 3) against `supabase==2.28.3` before relying on `client.postgrest.auth(jwt)` rebinding.
5. **Run staging long-handler gate (§8.3):** seed 200 memory rows; time both `POST /memory/refresh-stale` and `POST /memory/refresh-tariff` with `only_stale=False`. Pass criteria: both finish in <30 min with no end-of-handler 401-storm.
6. **Run staging JWT-shape gate (§8.1):** confirm a `role: "admin"` JWT (the broken v1 design) errors out as predicted by C1, and a `role: "authenticated"` + `app_role: "admin"` JWT works with the rewritten policies.
7. **Run staging storage gate (§8.2):** confirm a `_user_client.storage…` call is denied (storage RLS is deny-all) and the application path `_sb_service.storage…` works end-to-end (upload → queue → export).
8. **Deploy code** (single commit). Render auto-deploys; cookie name bump invalidates all in-flight `is_session` cookies on the boundary.
9. **Smoke test** (plan §5.3 + prerequisites §5): self-password change via RPC, invalid-hash RPC rejection, storage upload+download round-trip.
10. **Monitor for 1 hour:** rollback on >5 sustained 401s/min for 5 min, OR any 500 mentioning UUID cast / RLS denial that's not explainable.

Optional follow-ups (not blocking):
- Tighten the lint allowlist on subscript-form session reads (finding #2 above).
- Add the StreamingResponse ban into the `authed` docstring (finding #1 above).

_Reviewer note: I came in skeptical of "GREEN" verdicts and tried to find regressions. The code matches v2 + the four post-re-review tweaks closely. The two remaining nits (docstring drift, lint scope) are below the line that gates a deploy — they're worth fixing in a follow-up but not worth blocking for. The auth model, JWT shape, RLS policy rewrite, migration ordering, and DAL refactor are all correct. Ship it after running the operational steps._
