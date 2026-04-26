-- ============================================================
-- Migration 003a — Rename app-role JWT claim from `role` to `app_role`
-- ============================================================
-- Drafted 2026-04-26 after the external review of PHASE_B_PLAN v1
-- flagged CRITICAL-1: the JWT `role` claim is consumed by PostgREST's
-- SET LOCAL ROLE machinery to switch the connection's Postgres role.
-- We cannot reuse it for the application-level role
-- (user/admin/super_admin). Phase B JWTs will carry:
--     role:     "authenticated"   (PostgREST connection role)
--     app_role: "user"|"admin"|"super_admin"   (RLS predicate)
--
-- This migration drops + recreates all 8 policies that read
-- auth.jwt() ->> 'role' so they read auth.jwt() ->> 'app_role' instead.
-- The 5 tenant-scoped policies that only read 'company_id' are
-- unaffected.
--
-- ⚠️  ORDERING: this migration MUST run BEFORE the Phase B Python
-- deploy. After it ships, but before the Python switch, app behaviour
-- is unchanged because the service-role key bypasses RLS. The first
-- moment 'app_role' is read is after the Python deploy lands.
--
-- ⚠️  PAIR WITH: migration 003 (change_own_password RPC). Both must
-- be in place before the Phase B code deploy.
--
-- Run via: Supabase Dashboard → SQL Editor → paste → Run
-- Verify after: see VERIFY section at the bottom
-- Rollback:    run 003a_rename_role_to_app_role_rollback.sql
-- ============================================================

BEGIN;

-- ─────────────────────────────────────────────────────────
-- COMPANIES
-- ─────────────────────────────────────────────────────────
DROP POLICY IF EXISTS companies_super_admin ON companies;
CREATE POLICY companies_super_admin ON companies
  FOR ALL TO authenticated
  USING ((auth.jwt() ->> 'app_role') = 'super_admin')
  WITH CHECK ((auth.jwt() ->> 'app_role') = 'super_admin');

-- ─────────────────────────────────────────────────────────
-- USERS — admin INSERT
-- ─────────────────────────────────────────────────────────
-- Note: the `role IN ('user', 'admin')` clause in WITH CHECK reads
-- the row's `role` column (not the JWT). That stays as-is.
DROP POLICY IF EXISTS users_admin_insert ON users;
CREATE POLICY users_admin_insert ON users
  FOR INSERT TO authenticated
  WITH CHECK (
    company_id = (auth.jwt() ->> 'company_id')::uuid
    AND (auth.jwt() ->> 'app_role') IN ('admin', 'super_admin')
    AND role IN ('user', 'admin')
  );

-- ─────────────────────────────────────────────────────────
-- USERS — admin UPDATE  (post-migration-002 shape, with role-clamp)
-- ─────────────────────────────────────────────────────────
DROP POLICY IF EXISTS users_admin_update ON users;
CREATE POLICY users_admin_update ON users
  FOR UPDATE TO authenticated
  USING (
    company_id = (auth.jwt() ->> 'company_id')::uuid
    AND (auth.jwt() ->> 'app_role') IN ('admin', 'super_admin')
    AND role IN ('user', 'admin')
  )
  WITH CHECK (
    company_id = (auth.jwt() ->> 'company_id')::uuid
    AND role IN ('user', 'admin')
  );

-- ─────────────────────────────────────────────────────────
-- USERS — admin DELETE  (post-migration-002 shape, with role-clamp)
-- ─────────────────────────────────────────────────────────
DROP POLICY IF EXISTS users_admin_delete ON users;
CREATE POLICY users_admin_delete ON users
  FOR DELETE TO authenticated
  USING (
    company_id = (auth.jwt() ->> 'company_id')::uuid
    AND (auth.jwt() ->> 'app_role') IN ('admin', 'super_admin')
    AND role IN ('user', 'admin')
  );

-- ─────────────────────────────────────────────────────────
-- USERS — super_admin bypass
-- ─────────────────────────────────────────────────────────
DROP POLICY IF EXISTS users_super_admin ON users;
CREATE POLICY users_super_admin ON users
  FOR ALL TO authenticated
  USING ((auth.jwt() ->> 'app_role') = 'super_admin')
  WITH CHECK ((auth.jwt() ->> 'app_role') = 'super_admin');

-- ─────────────────────────────────────────────────────────
-- INVOICES — super_admin bypass
-- ─────────────────────────────────────────────────────────
DROP POLICY IF EXISTS invoices_super_admin ON invoices;
CREATE POLICY invoices_super_admin ON invoices
  FOR ALL TO authenticated
  USING ((auth.jwt() ->> 'app_role') = 'super_admin')
  WITH CHECK ((auth.jwt() ->> 'app_role') = 'super_admin');

-- ─────────────────────────────────────────────────────────
-- PRODUCT_MEMORY — super_admin bypass
-- ─────────────────────────────────────────────────────────
DROP POLICY IF EXISTS memory_super_admin ON product_memory;
CREATE POLICY memory_super_admin ON product_memory
  FOR ALL TO authenticated
  USING ((auth.jwt() ->> 'app_role') = 'super_admin')
  WITH CHECK ((auth.jwt() ->> 'app_role') = 'super_admin');

-- ─────────────────────────────────────────────────────────
-- JOBS — super_admin bypass
-- ─────────────────────────────────────────────────────────
DROP POLICY IF EXISTS jobs_super_admin ON jobs;
CREATE POLICY jobs_super_admin ON jobs
  FOR ALL TO authenticated
  USING ((auth.jwt() ->> 'app_role') = 'super_admin')
  WITH CHECK ((auth.jwt() ->> 'app_role') = 'super_admin');

COMMIT;

-- ─────────────────────────────────────────────────────────
-- VERIFY (run manually after COMMIT)
-- ─────────────────────────────────────────────────────────
-- Expected: 0 rows. If any row returns, a policy still references the
-- old `role` claim and Phase B will break.
--   SELECT schemaname, tablename, policyname, cmd
--   FROM pg_policies
--   WHERE schemaname='public'
--     AND (qual ~* 'auth\.jwt\(\)\s*->>\s*''role'''
--       OR with_check ~* 'auth\.jwt\(\)\s*->>\s*''role''')
--   ORDER BY tablename, policyname;
--
-- Expected: 8 rows, all referencing app_role in qual or with_check
--   SELECT schemaname, tablename, policyname, cmd
--   FROM pg_policies
--   WHERE schemaname='public'
--     AND (qual ~* 'auth\.jwt\(\)\s*->>\s*''app_role'''
--       OR with_check ~* 'auth\.jwt\(\)\s*->>\s*''app_role''')
--   ORDER BY tablename, policyname;
--
-- Expected sanity: total policy count is still 13 (2 companies + 5
-- users + 2 invoices + 2 memory + 2 jobs).
--   SELECT count(*) FROM pg_policies WHERE schemaname='public';
