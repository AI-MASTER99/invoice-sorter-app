# Invoice Sorter

AI-powered customs invoice processing for EU→UK imports.
Multi-tenant SaaS built on FastAPI + Claude + Supabase.

## Live

- App: https://app.invoice-sorter.com
- Landing: https://www.invoice-sorter.com

## Tech stack

- **Backend**: FastAPI, Python 3.12
- **AI**: Anthropic Claude — Opus (primary: extraction, verification, sub-code
  matching) + Sonnet (light: totals extraction)
- **Database + Storage**: Supabase (PostgreSQL + object storage)
- **Hosting**: Render (free tier, EU region)
- **Frontend**: Vanilla HTML/CSS/JS — no framework

## Local development

```bash
cd invoiceflow
pip install -r requirements.txt

# Copy .env.example to .env and fill in every REQUIRED key —
# the app refuses to start if any of them is missing:
#   SECRET_KEY, APP_PASSWORD, ANTHROPIC_API_KEY,
#   SUPABASE_URL, SUPABASE_KEY, SUPABASE_ANON_KEY, SUPABASE_JWT_SECRET
# Recommended for local parity with production:
#   USE_CLIENT_LIST=1, DEV_MODE=1

uvicorn main:app --reload --port 8000
```

Open http://localhost:8000/login (user `admin` + your `APP_PASSWORD`).

Tests: `python -m pytest tests_review.py tests_rate_limit.py -q`

## Environment variables

See `invoiceflow/.env.example` for the authoritative, commented list.

| Var | Purpose |
|-----|---------|
| `ANTHROPIC_API_KEY` | Claude API key |
| `SUPABASE_URL` | Supabase project URL |
| `SUPABASE_KEY` | Supabase `service_role` key |
| `SUPABASE_ANON_KEY` | Supabase public anon key (legacy JWT form, `eyJ…`) |
| `SUPABASE_JWT_SECRET` | Supabase JWT secret (signs per-request user JWTs for RLS) |
| `SECRET_KEY` | Random string (≥32 chars) for session cookies |
| `APP_PASSWORD` | Default admin password on first run |
| `USE_CLIENT_LIST` | `1` = per-client commodity list (the "V-lookup"); production runs with this ON |
| `AI_MODEL_PRIMARY` | Primary Claude model (default: `claude-opus-4-8`) |
| `AI_MODEL_LIGHT` | Light Claude model (default: `claude-sonnet-4-6`) |
| `AI_MODEL` | Legacy single-model override — sets both of the above |
| `STORAGE_RETENTION_DAYS` | Auto-purge uploads/exports older than N days (default 7, 0 = off) |
| `FORCE_ADMIN_RESET` | `1` = reset the admin password to `APP_PASSWORD` on boot (break-glass; remove after use) |
| `DEV_MODE` | `1` = relax cookie security + allow localhost CORS (local dev only) |
