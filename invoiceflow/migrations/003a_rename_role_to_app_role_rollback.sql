-- ============================================================
-- Migration 003a — ROLLBACK
-- ============================================================
-- Reverts 003a_rename_role_to_app_role.sql by recreating the 8
-- policies to read auth.jwt() ->> 'role' instead of 'app_role'.
--
-- ⚠️  WHEN TO USE: only if Phase B is fully reverted AND a new direction
-- is chosen for the JWT claim layout. The post-Phase-B steady state is
-- to KEEP this migration applied even after a code rollback (the
-- service-role key bypasses RLS, so the policies' claim-name is
-- irrelevant under the rolled-back code).
--
-- Run via: Supabase Dashboard → SQL Editor → paste → Run
-- ============================================================

BEGIN;

DROP POLICY IF EXISTS companies_super_admin ON companies;
CREATE POLICY companies_super_admin ON companies
  FOR ALL TO authenticated
  USING ((auth.jwt() ->> 'role') = 'super_admin')
  WITH CHECK ((auth.jwt() ->> 'role') = 'super_admin');

DROP POLICY IF EXISTS users_admin_insert ON users;
CREATE POLICY users_admin_insert ON users
  FOR INSERT TO authenticated
  WITH CHECK (
    company_id = (auth.jwt() ->> 'company_id')::uuid
    AND (auth.jwt() ->> 'role') IN ('admin', 'super_admin')
    AND role IN ('user', 'admin')
  );

DROP POLICY IF EXISTS users_admin_update ON users;
CREATE POLICY users_admin_update ON users
  FOR UPDATE TO authenticated
  USING (
    company_id = (auth.jwt() ->> 'company_id')::uuid
    AND (auth.jwt() ->> 'role') IN ('admin', 'super_admin')
    AND role IN ('user', 'admin')
  )
  WITH CHECK (
    company_id = (auth.jwt() ->> 'company_id')::uuid
    AND role IN ('user', 'admin')
  );

DROP POLICY IF EXISTS users_admin_delete ON users;
CREATE POLICY users_admin_delete ON users
  FOR DELETE TO authenticated
  USING (
    company_id = (auth.jwt() ->> 'company_id')::uuid
    AND (auth.jwt() ->> 'role') IN ('admin', 'super_admin')
    AND role IN ('user', 'admin')
  );

DROP POLICY IF EXISTS users_super_admin ON users;
CREATE POLICY users_super_admin ON users
  FOR ALL TO authenticated
  USING ((auth.jwt() ->> 'role') = 'super_admin')
  WITH CHECK ((auth.jwt() ->> 'role') = 'super_admin');

DROP POLICY IF EXISTS invoices_super_admin ON invoices;
CREATE POLICY invoices_super_admin ON invoices
  FOR ALL TO authenticated
  USING ((auth.jwt() ->> 'role') = 'super_admin')
  WITH CHECK ((auth.jwt() ->> 'role') = 'super_admin');

DROP POLICY IF EXISTS memory_super_admin ON product_memory;
CREATE POLICY memory_super_admin ON product_memory
  FOR ALL TO authenticated
  USING ((auth.jwt() ->> 'role') = 'super_admin')
  WITH CHECK ((auth.jwt() ->> 'role') = 'super_admin');

DROP POLICY IF EXISTS jobs_super_admin ON jobs;
CREATE POLICY jobs_super_admin ON jobs
  FOR ALL TO authenticated
  USING ((auth.jwt() ->> 'role') = 'super_admin')
  WITH CHECK ((auth.jwt() ->> 'role') = 'super_admin');

COMMIT;
