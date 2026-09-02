# Session handoff — 2026-06-18 (continue at home)

This branch (`continue-2026-06-18`) carries the work-in-progress from a session on
another machine so it can be continued at home. **It is NOT deployed** (only `main`
auto-deploys to Render). Merge into `main` when you want it live.

> ⚠️ The Claude chat and the assistant's memory do NOT travel between machines.
> This file + the repo are the context. The detailed plan lives in
> `docs/multifreight_rules_engine_plan.md`.

---

## Already LIVE on `main` (pushed + Render-deployed today)
- `3b39e9f` MultiFreight Items: central defaults (Procedure 4000, Valuation 1,
  Packages PK), docs N935 + Y929, **origin-based Preference 300 (EU) / 100 (non-EU)
  + Country of Preferential Origin**, xlsx layout fix.
- `963d915` Login rate-limiter: dedup attempts by unique `(timestamp, seq)`.
- `f7faca8` MultiFreight Items: **Packages Shipping Marks (01) = `N/M`**.
- `b210f83` MultiFreight Items: **U116** statement-on-origin doc on EU lines
  (id = invoice number).

## On THIS branch only (NOT on main, NOT deployed)
1. **`invoiceflow/main.py`**
   - **Merge NOT-IN-LIST lines** that share the same commodity code (summed
     weights/value/packages); distinct product names kept behind the
     `*** NOT IN LIST ***` marker so a human can still resolve them.
   - **U116 reference = the supplier's REX (ITREXIT…) when it's on the invoice,
     else the invoice number.** Carried via `totals['supplier_rex']` (captured at
     processing time) → only shows the REX on **newly processed** invoices;
     re-exporting an old invoice falls back to the invoice number.
     ⚠️ **Deliberate divergence from gov.uk** (which says use the invoice number;
     the REX belongs in the statement-on-origin text and, as a data element, under
     code C100 — not U116). Chosen on the advice of the user's specialist colleague;
     every line is human-reviewed. Verified research is in the assistant memory
     `cds-tca-import-codes`.
2. **`docs/multifreight_rules_engine_plan.md`** — the per-code rule-engine plan
   (Phase 1 research + Phase 2 migration done on paper).
3. **`invoiceflow/migrations/005_add_tariff_engine.sql` (+ `_rollback` + `_verify`)**
   — 3 new tables (`tariff_cache`, `tariff_reference`, `client_tariff_overrides`).
   **NOT applied to any database yet.** Apply via Supabase SQL Editor when ready.
   Risk-free until the new Python that uses them is shipped.

## How to continue at home
1. `git fetch origin && git checkout continue-2026-06-18`
2. **`.env` is gitignored — it does NOT travel.** Recreate `invoiceflow/.env` from
   `invoiceflow/.env.example`: `ANTHROPIC_API_KEY`, `SUPABASE_URL`, `SUPABASE_KEY`,
   `SECRET_KEY`, `APP_PASSWORD`, plus `USE_CLIENT_LIST=1`,
   `AI_MODEL_PRIMARY=claude-opus-4-8`, `DEV_MODE=1`.
   (NB: the API key used locally was a *personal* account; production on Render uses
   its own `ANTHROPIC_API_KEY` — separate credit balance.)
3. `cd invoiceflow && pip install -r requirements.txt`
4. `uvicorn main:app --reload --port 8000` → http://localhost:8000/login
   (user `admin` + your `APP_PASSWORD`).
5. Tests: `python -m pytest tests_review.py tests_rate_limit.py -q` (expect 41 pass;
   note: the 2 rate-limit tests only fail on Windows due to clock resolution — fixed,
   they pass now).
6. To deploy later: merge this branch into `main` and push (Render auto-deploys).
   Apply migration 005 to the production Supabase first if the engine needs it.

## Open items / decisions in flight
- **Per-supplier list management:** build an **in-app editor** (user chose
  editor-only, not Excel import) for clients + their product rows. Note the now-dead
  `client_products` columns (Procedure/Valuation/Preference/COP are central/origin-
  based now).
- **N853 rule (user's operational "trucje"):** N853 is always required when the
  commodity code's **first 3 digits** ∈ {020,021,030,040,041,050,150,152,160,210,
  230,350,410,510,670} (animal-origin chapters). Organic = **Y929 exemption**, not
  C644. Confirm vs the BTOM low-risk-dairy nuance before encoding.
- **3-slot DE 2/3 limit:** EU lines already use N935 + Y929 + U116. Adding more docs
  (e.g. N853, C085) needs deterministic prioritization + a flag (never silently drop).
- **Render memory:** free tier (512 MB) hit its limit once → auto-restart + brief
  downtime. Not caused by today's code (Items export peaks ~5 MB). Likely a
  per-request peak; upgrade the instance if it recurs.
