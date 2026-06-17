-- ============================================================
-- Migration 004 — Clients + per-client commodity-code lists
-- ============================================================
-- Adds two new tenant-scoped tables that power the redesign where
-- commodity codes come from a per-client list instead of the UK
-- trade-tariff website:
--   1. clients          — one row per supplier/exporter (the "client")
--   2. client_products  — that client's commodity-code lookup list
--
-- RISK-FREE for the running app: these are NEW tables that no
-- currently-deployed code references. The live app keeps working
-- unchanged until the new Python is shipped (after "last-call").
--
-- House style: ENABLE (not FORCE) RLS, a tenant policy on company_id,
-- and a super_admin bypass on the 'app_role' JWT claim (cf. 001/003a).
--
-- Run via: Supabase Dashboard -> SQL Editor -> paste -> Run
-- Verify after:  004_verify.sql
-- Rollback:      004_add_clients_and_client_products_rollback.sql
-- ============================================================

BEGIN;

-- ─────────────────────────────────────────────────────────
-- 1. clients — one per supplier/exporter
-- ─────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS clients (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  company_id  uuid NOT NULL,
  name        text NOT NULL,
  rex         text,                 -- e.g. ITREXIT06167560157 (stable id)
  eori        text,                 -- e.g. IT 06167560157
  aliases     jsonb NOT NULL DEFAULT '[]'::jsonb,  -- alternate names for matching
  created_at  timestamptz NOT NULL DEFAULT now(),
  updated_at  timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS clients_company_idx ON clients (company_id);
CREATE INDEX IF NOT EXISTS clients_company_rex_idx ON clients (company_id, rex);

-- ─────────────────────────────────────────────────────────
-- 2. client_products — the lookup list (matches COMMODITY CODE SPREADSHEET)
-- ─────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS client_products (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  company_id    uuid NOT NULL,
  client_id     uuid NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
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

-- One row per (client, full commodity code); upsert target for loading.
CREATE UNIQUE INDEX IF NOT EXISTS client_products_unique_code
  ON client_products (company_id, client_id, full_code);

-- The hot path: VLOOKUP a general code within a client's list.
CREATE INDEX IF NOT EXISTS client_products_lookup_idx
  ON client_products (company_id, client_id, general_code);

-- ─────────────────────────────────────────────────────────
-- 3. Row-Level Security (ENABLE, not FORCE — service-role bypasses)
-- ─────────────────────────────────────────────────────────
ALTER TABLE clients         ENABLE ROW LEVEL SECURITY;
ALTER TABLE client_products ENABLE ROW LEVEL SECURITY;

-- clients: tenant isolation + super_admin override
CREATE POLICY clients_tenant_all ON clients
  FOR ALL TO authenticated
  USING      (company_id = (auth.jwt() ->> 'company_id')::uuid)
  WITH CHECK (company_id = (auth.jwt() ->> 'company_id')::uuid);

CREATE POLICY clients_super_admin ON clients
  FOR ALL TO authenticated
  USING      ((auth.jwt() ->> 'app_role') = 'super_admin')
  WITH CHECK ((auth.jwt() ->> 'app_role') = 'super_admin');

-- client_products: tenant isolation + super_admin override
CREATE POLICY client_products_tenant_all ON client_products
  FOR ALL TO authenticated
  USING      (company_id = (auth.jwt() ->> 'company_id')::uuid)
  WITH CHECK (company_id = (auth.jwt() ->> 'company_id')::uuid);

CREATE POLICY client_products_super_admin ON client_products
  FOR ALL TO authenticated
  USING      ((auth.jwt() ->> 'app_role') = 'super_admin')
  WITH CHECK ((auth.jwt() ->> 'app_role') = 'super_admin');

COMMIT;
