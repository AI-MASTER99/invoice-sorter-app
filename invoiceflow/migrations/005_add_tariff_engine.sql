-- ============================================================
-- Migration 005 — Tariff rule-engine support tables
-- ============================================================
-- Adds the storage for the MultiFreight "Items" rule engine that
-- auto-fills per-commodity-code documents / additional codes /
-- supplementary units from the UK Trade Tariff API
-- (www.trade-tariff.service.gov.uk/api/v2). The API itself carries the
-- conditional logic, so the DB stays small. See
-- docs/multifreight_rules_engine_plan.md.
--
-- Three tables:
--   1. tariff_cache             — raw API responses, ~24h TTL (GLOBAL: tariff
--                                 data is public + identical for every tenant)
--   2. tariff_reference         — decode dictionaries (measure types, condition
--                                 codes, certificates, units…) (GLOBAL)
--   3. client_tariff_overrides  — per-client human decisions for the choice
--                                 cases the API can't resolve alone (TENANT-scoped)
--
-- RISK-FREE for the running app: these are NEW tables that no
-- currently-deployed code references. The live app keeps working
-- unchanged until the new Python is shipped (after "last-call").
--
-- House style: ENABLE (not FORCE) RLS so the service role bypasses.
-- The tenant table gets a company_id policy + super_admin override (cf.
-- 001/004). The two GLOBAL tariff tables hold public, non-tenant reference
-- data, so they get a single permissive authenticated policy; the
-- service-role worker is what normally populates them.
--
-- Run via: Supabase Dashboard -> SQL Editor -> paste -> Run
-- Verify after:  005_verify.sql
-- Rollback:      005_add_tariff_engine_rollback.sql
-- ============================================================

BEGIN;

-- ─────────────────────────────────────────────────────────
-- 1. tariff_cache — cached UK Trade Tariff API responses (GLOBAL)
-- ─────────────────────────────────────────────────────────
-- Keyed by (commodity_code, geographical_area_id). The tariff updates
-- daily, so each row carries an expires_at the app sets to ~24h ahead.
-- commodity_code is the API's goods_nomenclature_item_id (NOT data.id).
CREATE TABLE IF NOT EXISTS tariff_cache (
  id                   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  commodity_code       text NOT NULL,            -- 10-digit goods_nomenclature_item_id
  geographical_area_id text NOT NULL DEFAULT '', -- origin filter used; '' = unfiltered
  payload              jsonb NOT NULL,            -- raw JSON:API response
  fetched_at           timestamptz NOT NULL DEFAULT now(),
  expires_at           timestamptz NOT NULL       -- app sets to fetched_at + ~24h
);

CREATE UNIQUE INDEX IF NOT EXISTS tariff_cache_unique_key
  ON tariff_cache (commodity_code, geographical_area_id);
CREATE INDEX IF NOT EXISTS tariff_cache_expires_idx
  ON tariff_cache (expires_at);

-- ─────────────────────────────────────────────────────────
-- 2. tariff_reference — decode dictionaries (GLOBAL)
-- ─────────────────────────────────────────────────────────
-- Small, slowly-changing code lists from the API reference endpoints
-- (measure_types, measure_condition_codes, measure_actions,
-- additional_code_types, certificates, measurement_units). Refreshed
-- weekly. 'kind' names the dictionary; 'extra' holds e.g. series ids.
CREATE TABLE IF NOT EXISTS tariff_reference (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  kind         text NOT NULL,   -- e.g. 'measure_type', 'certificate', 'measurement_unit'
  code         text NOT NULL,   -- the code within that dictionary
  description  text,
  extra        jsonb NOT NULL DEFAULT '{}'::jsonb,
  refreshed_at timestamptz NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS tariff_reference_unique_key
  ON tariff_reference (kind, code);

-- ─────────────────────────────────────────────────────────
-- 3. client_tariff_overrides — per-client stable decisions (TENANT-scoped)
-- ─────────────────────────────────────────────────────────
-- The small hand-authored part: where the API offers a choice the invoice
-- can't resolve (e.g. organic exemption Y929 vs certificate C644, or which
-- anti-dumping additional code), a human records the stable choice per
-- client + product here. NULL origin_country / measure_type = applies
-- generally; a value scopes the override. 'decision' holds the chosen
-- output, e.g.
--   {"action":"use","document_code":"Y930","status":"JE","reason":"…","id_template":"{invoice}"}
--   {"action":"additional_code","code":"B009"}
--   {"action":"flag","note":"needs human check"}
CREATE TABLE IF NOT EXISTS client_tariff_overrides (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  company_id      uuid NOT NULL,
  client_id       uuid NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
  commodity_code  text NOT NULL,    -- code (or prefix) this override applies to
  origin_country  text,             -- NULL = any origin; else 2-letter ISO
  measure_type    text,             -- NULL = general; else API measure_type id (e.g. '750')
  decision        jsonb NOT NULL,   -- the chosen output (see header)
  note            text,
  created_at      timestamptz NOT NULL DEFAULT now(),
  updated_at      timestamptz NOT NULL DEFAULT now()
);

-- One override per (client, code, origin, measure_type); COALESCE so NULL
-- scopes collapse to a single row (Postgres treats raw NULLs as distinct).
CREATE UNIQUE INDEX IF NOT EXISTS client_tariff_overrides_unique_key
  ON client_tariff_overrides
     (company_id, client_id, commodity_code,
      COALESCE(origin_country, ''), COALESCE(measure_type, ''));

CREATE INDEX IF NOT EXISTS client_tariff_overrides_lookup_idx
  ON client_tariff_overrides (company_id, client_id, commodity_code);

-- ─────────────────────────────────────────────────────────
-- 4. Row-Level Security (ENABLE, not FORCE — service-role bypasses)
-- ─────────────────────────────────────────────────────────
ALTER TABLE tariff_cache            ENABLE ROW LEVEL SECURITY;
ALTER TABLE tariff_reference        ENABLE ROW LEVEL SECURITY;
ALTER TABLE client_tariff_overrides ENABLE ROW LEVEL SECURITY;

-- Global tariff tables: public, non-tenant reference data → one permissive
-- policy for any authenticated user (the service-role worker populates them).
CREATE POLICY tariff_cache_authenticated_all ON tariff_cache
  FOR ALL TO authenticated
  USING (true) WITH CHECK (true);

CREATE POLICY tariff_reference_authenticated_all ON tariff_reference
  FOR ALL TO authenticated
  USING (true) WITH CHECK (true);

-- client_tariff_overrides: tenant isolation + super_admin override (cf. 004)
CREATE POLICY client_tariff_overrides_tenant_all ON client_tariff_overrides
  FOR ALL TO authenticated
  USING      (company_id = (auth.jwt() ->> 'company_id')::uuid)
  WITH CHECK (company_id = (auth.jwt() ->> 'company_id')::uuid);

CREATE POLICY client_tariff_overrides_super_admin ON client_tariff_overrides
  FOR ALL TO authenticated
  USING      ((auth.jwt() ->> 'app_role') = 'super_admin')
  WITH CHECK ((auth.jwt() ->> 'app_role') = 'super_admin');

COMMIT;
