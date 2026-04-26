-- ============================================================
-- Migration 001 — ROLLBACK
-- ============================================================
-- Reverses 001_enable_rls.sql. Run only if Fase A or Fase B causes
-- production breakage and we need to return to the pre-migration state.
--
-- Run via: Supabase Dashboard → SQL Editor → paste → Run
-- ============================================================

BEGIN;

-- ─────────────────────────────────────────────────────────
-- 1. Drop policies (must be done before DISABLE for clarity)
-- ─────────────────────────────────────────────────────────
DROP POLICY IF EXISTS companies_tenant_select   ON companies;
DROP POLICY IF EXISTS companies_super_admin     ON companies;

DROP POLICY IF EXISTS users_tenant_select       ON users;
DROP POLICY IF EXISTS users_admin_insert        ON users;
DROP POLICY IF EXISTS users_admin_update        ON users;
DROP POLICY IF EXISTS users_admin_delete        ON users;
DROP POLICY IF EXISTS users_super_admin         ON users;

DROP POLICY IF EXISTS invoices_tenant_all       ON invoices;
DROP POLICY IF EXISTS invoices_super_admin      ON invoices;

DROP POLICY IF EXISTS memory_tenant_all         ON product_memory;
DROP POLICY IF EXISTS memory_super_admin        ON product_memory;

DROP POLICY IF EXISTS jobs_tenant_all           ON jobs;
DROP POLICY IF EXISTS jobs_super_admin          ON jobs;

-- ─────────────────────────────────────────────────────────
-- 2. Disable RLS on all tables
-- ─────────────────────────────────────────────────────────
ALTER TABLE companies      DISABLE ROW LEVEL SECURITY;
ALTER TABLE users          DISABLE ROW LEVEL SECURITY;
ALTER TABLE invoices       DISABLE ROW LEVEL SECURITY;
ALTER TABLE product_memory DISABLE ROW LEVEL SECURITY;
ALTER TABLE jobs           DISABLE ROW LEVEL SECURITY;

-- ─────────────────────────────────────────────────────────
-- 3. Drop CHECK constraint on users.role
-- ─────────────────────────────────────────────────────────
ALTER TABLE users DROP CONSTRAINT IF EXISTS users_role_chk;

COMMIT;

-- ─────────────────────────────────────────────────────────
-- VERIFY rollback (run manually after COMMIT)
-- ─────────────────────────────────────────────────────────
-- Expected: rowsecurity = false on all 5 tables
--   SELECT schemaname, tablename, rowsecurity FROM pg_tables
--   WHERE schemaname = 'public' ORDER BY tablename;
--
-- Expected: 0 policies in public schema
--   SELECT count(*) FROM pg_policies WHERE schemaname = 'public';
--
-- Expected: 0 CHECK constraints on users
--   SELECT count(*) FROM pg_constraint
--   WHERE conrelid = 'public.users'::regclass AND contype = 'c';
