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

### Tests

```bash
cd invoiceflow
pip install -r requirements-dev.txt
python -m pytest            # pytest.ini picks up every tests_*.py
python -m pyflakes *.py ../scripts/*.py
bash ../scripts/check_no_raw_sb.sh   # Phase B invariant: no service-role client in request handlers
```

Every test file imports `main`, so the `.env` must be valid (dummy values are
fine for the unit tests). `tests_user_admin.py` talks to the real Supabase
project in `.env` and fails without network access to it; the other files
run offline.

## Commodity-code list (the "V-lookup")

ONE company-wide list, shared by every client, lives in `commodity_codes`
(the per-client `client_products` lists it replaced are kept only until
migration 007 drops them). It is edited in the app (Commodity codes tool)
or loaded from a MultiFreight CDS "Items" export — the goods lines as they
were accepted at the border. `invoiceflow/data/commodity_codes.csv` is that
export folded to one row per (exporter REX, full code) by
`invoiceflow/cds_list.py`; the loader merges those to one row per code.

Clients themselves live in the `clients` registry (Clients / REX tool):
name, REX and EORI, used to match invoices to a client and to fill the
export's REX when the invoice doesn't carry one.

```bash
# what is in the list file, and which REX numbers already have a client
python scripts/load_commodity_list.py --list

# refresh the company list from the committed CSV (upsert, never deletes)
python scripts/load_commodity_list.py

# after a new export from MultiFreight
python scripts/load_commodity_list.py --source "<CDS items export.csv>" --derive
```

`--help` covers wiping the list first (`--replace`) and `--dry-run`.

**The CSV is not the database.** `commodity_codes.csv` is the list as
derived from the export; the app only shows what has been loaded into the
`commodity_codes` table. If the Commodity codes tool shows far fewer codes
than `--list` reports, the loader has not been run against that database —
run `python scripts/load_commodity_list.py` (it upserts, so it is safe to
re-run and never deletes a code).

### Importing a plain code list

A sheet of codes — one row per code, no CDS provenance — can be added
either in the app (Commodity codes → **Import list**, accepting .xlsx or
.csv) or from the command line:

```bash
# what the sheet would add (writes nothing)
python scripts/add_commodity_codes.py "Commodity_Codes_2026.xlsx" --dry-run

# merge it into the committed CSV, then load it
python scripts/add_commodity_codes.py "Commodity_Codes_2026.xlsx"
python scripts/load_commodity_list.py
```

Either route adds only the codes the list does not already hold: a code
already present keeps its description (which came from a real declaration,
as a sheet's wording did not), so an overlapping sheet never duplicates a
row. Both refuse a code shorter than 8 digits rather than padding it into
one that was never declared.

## Database migrations

`invoiceflow/migrations/` holds numbered SQL files, each with a `_rollback`
and (usually) a `_verify` companion whose header states the expected
result. Apply them through the Supabase SQL editor, or with a personal
access token:

```bash
SUPABASE_PAT=sbp_... python scripts/apply_migration.py 006 --dry-run   # show what would be sent
SUPABASE_PAT=sbp_... python scripts/apply_migration.py 006             # apply, then run 006_verify.sql
```

Migration 005 (tariff-engine tables) is written but not applied anywhere;
nothing in the app reads those tables yet.

## Repository layout

| Path | What |
|------|------|
| `invoiceflow/` | The FastAPI app (`main.py`), data layer (`database.py`), review/tariff helpers, tests, migrations, static frontend |
| `scripts/` | Operator tooling: commodity-list loading, storage cleanup, migration apply, the Phase B lint |
| `docs/` | The rules-engine plan (open work) and `docs/archive/` for historical hand-overs and the Phase B design |
| `website/` | The static landing page (www.invoice-sorter.com) |
| `render.yaml`, `Procfile` | Render deployment (Blueprint + fallback start command) |

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
| `USE_CLIENT_LIST` | `1` = company-wide commodity list (the "V-lookup", shared by all clients); production runs with this ON |
| `AI_MODEL_PRIMARY` | Primary Claude model (default: `claude-opus-4-8`) |
| `AI_MODEL_LIGHT` | Light Claude model (default: `claude-sonnet-4-6`) |
| `AI_MODEL` | Legacy single-model override — sets both of the above |
| `STORAGE_RETENTION_DAYS` | Auto-purge uploads/exports older than N days (default 7, 0 = off) |
| `FORCE_ADMIN_RESET` | `1` = reset the admin password to `APP_PASSWORD` on boot (break-glass; remove after use) |
| `DEV_MODE` | `1` = relax cookie security + allow localhost CORS (local dev only) |
