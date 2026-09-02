# Session handoff — 2026-07-10 (production recovery in progress)

Context for continuing (new session: read this + `git log --oneline -25`).

## Situation

Production (app.invoice-sorter.com, Render) is down-ish because the
**Supabase project is over its free-tier storage quota (23 GB used / 5 GB
allowed)** — old invoice uploads + Excel exports in the `invoice-uploads`
and `invoice-exports` buckets. The operator is also **locked out of the
app** (admin password unknown). Everything else is healthy.

Supabase project ref: `pbahlprxmlxvntfvcytd` (see scripts/apply_003.py).

## What is already shipped (all on `main`, Render auto-deploys)

- Boot no longer crash-loops when the DB is unreachable; admin bootstrap
  + stale-job sweep retry lazily at login.
- `FORCE_ADMIN_RESET=1` is currently set in render.yaml (**TEMPORARY** —
  remove once the operator can log in; it resets the default admin's
  password to APP_PASSWORD on every boot).
- Daily storage retention purge (STORAGE_RETENTION_DAYS=7) + super-admin
  endpoints `GET /api/admin/storage/usage`, `POST /api/admin/storage/purge`
  + admin UI (Storage card).
- `scripts/storage_cleanup.py` — bulk cleanup, dry-run by default.
- Full external audit fixes (security/XSS/pipeline/deps), tariff rule
  engine (`tariff_rules.py`, Y929 food-only + N853 flag), in-app client
  list editor (Clients page). 46 tests green.

## Recovery plan (waiting on operator)

The operator is granting this environment network access to
`*.supabase.co` and pasting the project's **service_role key** in chat.
Once available:

1. Verify reach: `curl https://pbahlprxmlxvntfvcytd.supabase.co/rest/v1/`
   with `apikey: <key>` header.
2. Free storage (target < 5 GB): run
   `SUPABASE_URL=https://pbahlprxmlxvntfvcytd.supabase.co SUPABASE_KEY=<key> python scripts/storage_cleanup.py`
   (dry-run first, then `--older-than 0 --apply`).
3. Fix login without Render: bcrypt-hash a password agreed with the
   operator (passlib, `$2b$12`) and PATCH the default company's `admin`
   row's `password_hash` via PostgREST with the service key
   (`users` table; default company = the one the seed admin belongs to —
   look it up via `GET /rest/v1/users?username=eq.admin`).
4. Have the operator log in; then REMOVE the FORCE_ADMIN_RESET entry from
   render.yaml (and tell them to rotate the service_role key:
   Supabase → Settings → API → rotate).

## Open items after recovery

- N853/BTOM nuance: confirm the flag-only behaviour with the customs
  specialist (tariff_rules.py), then decide if N853 should auto-fill.
- Rules-engine phases: API-driven authoring (migration 005 tables are
  still unused and NOT applied to the DB).
- passlib → bcrypt migration (requirements.txt note).
- Supabase free tier keeps ~5 GB: retention (7d) should keep it down.
