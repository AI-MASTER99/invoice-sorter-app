-- ============================================================
-- Migration 006 — Company-wide commodity-code list
-- ============================================================
-- Most clients use the same (or almost the same) commodity codes, so
-- the per-client lists (client_products) are replaced by ONE shared
-- list per company:
--   commodity_codes — the company-wide V-lookup (general code ->
--                     full code + description + CDS fields)
--
-- The table is seeded from client_products, folded to one row per
-- (company_id, full_code) — the most recently updated row wins when
-- two clients carried the same code with different descriptions.
-- Review collisions BEFORE running with:
--
--   SELECT company_id, full_code, count(*),
--          array_agg(DISTINCT description) AS descriptions
--   FROM client_products
--   GROUP BY company_id, full_code
--   HAVING count(*) > 1;
--
-- client_products is NOT dropped here: the currently-deployed code
-- still reads it, so this migration stays risk-free for the running
-- app (cf. 004). Drop it in a later migration (007) once the new
-- code has shipped and soaked.
--
-- The seed is idempotent (ON CONFLICT DO NOTHING) — safe to re-run
-- after the code deploy to pick up any list edits made in between.
--
-- House style: ENABLE (not FORCE) RLS, a tenant policy on company_id,
-- and a super_admin bypass on the 'app_role' JWT claim (cf. 001/003a).
--
-- Run via: Supabase Dashboard -> SQL Editor -> paste -> Run
-- Verify after:  006_verify.sql
-- Rollback:      006_add_commodity_codes_rollback.sql
-- ============================================================

BEGIN;

-- ─────────────────────────────────────────────────────────
-- 1. commodity_codes — one shared list per company
-- ─────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS commodity_codes (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  company_id    uuid NOT NULL,
  general_code  text NOT NULL,        -- VLOOKUP key, e.g. 07049010
  full_code     text NOT NULL,        -- complete output code, e.g. 0704901000
  taric_code    text,
  description   text,                 -- canonical description (output)
  -- The rich CDS fields (all optional; only filled when the list has them):
  procedure     text,
  pref          text,
  mop           text,
  val_method    text,
  coo           text,
  cop           text,
  nat_add_code  text,
  documents     jsonb NOT NULL DEFAULT '[]'::jsonb,  -- [{code,id,status,reason}, …]
  created_at    timestamptz NOT NULL DEFAULT now(),
  updated_at    timestamptz NOT NULL DEFAULT now()
);

-- One row per (company, full commodity code); upsert target for loading.
CREATE UNIQUE INDEX IF NOT EXISTS commodity_codes_unique_code
  ON commodity_codes (company_id, full_code);

-- The hot path: VLOOKUP a general code within the company's list.
CREATE INDEX IF NOT EXISTS commodity_codes_lookup_idx
  ON commodity_codes (company_id, general_code);

-- ─────────────────────────────────────────────────────────
-- 2. Row-Level Security (ENABLE, not FORCE — service-role bypasses)
-- ─────────────────────────────────────────────────────────
ALTER TABLE commodity_codes ENABLE ROW LEVEL SECURITY;

-- commodity_codes: tenant isolation + super_admin override
CREATE POLICY commodity_codes_tenant_all ON commodity_codes
  FOR ALL TO authenticated
  USING      (company_id = (auth.jwt() ->> 'company_id')::uuid)
  WITH CHECK (company_id = (auth.jwt() ->> 'company_id')::uuid);

CREATE POLICY commodity_codes_super_admin ON commodity_codes
  FOR ALL TO authenticated
  USING      ((auth.jwt() ->> 'app_role') = 'super_admin')
  WITH CHECK ((auth.jwt() ->> 'app_role') = 'super_admin');

-- ─────────────────────────────────────────────────────────
-- 3. Seed from the per-client lists, one row per (company, full code)
-- ─────────────────────────────────────────────────────────
INSERT INTO commodity_codes (company_id, general_code, full_code, taric_code,
                             description, procedure, pref, mop, val_method,
                             coo, cop, nat_add_code, documents)
SELECT DISTINCT ON (company_id, full_code)
       company_id, general_code, full_code, taric_code,
       description, procedure, pref, mop, val_method,
       coo, cop, nat_add_code, documents
FROM client_products
ORDER BY company_id, full_code, updated_at DESC
ON CONFLICT (company_id, full_code) DO NOTHING;

COMMIT;
