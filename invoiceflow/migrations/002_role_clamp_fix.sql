-- ============================================================
-- Migration 002 — Hotfix role-clamp on users_admin_update / _delete
-- ============================================================
-- Applied 2026-04-26 after the external review of Fase A flagged
-- MEDIUM-5: a tenant admin could demote or delete a super_admin in
-- their own tenant (a denial-of-service against the operator role).
--
-- Original USING clauses on users_admin_update / users_admin_delete
-- only required (company match) AND (caller is admin/super_admin).
-- They did not check the OLD row's role, so an admin in tenant X
-- could UPDATE a super_admin's row (also in tenant X) to demote
-- them, or DELETE them outright.
--
-- This migration tightens USING with `AND role IN ('user', 'admin')`,
-- meaning admins cannot act on super_admin rows. super_admins still
-- have full power because users_super_admin (PERMISSIVE) is OR'd in.
--
-- Run via: Supabase Dashboard → SQL Editor → paste → Run
-- Verify after: see VERIFY section at the bottom
-- Rollback:    run 002_role_clamp_fix_rollback.sql
-- ============================================================

BEGIN;

DROP POLICY IF EXISTS users_admin_update ON users;
CREATE POLICY users_admin_update ON users
  FOR UPDATE TO authenticated
  USING (
    company_id = (auth.jwt() ->> 'company_id')::uuid
    AND (auth.jwt() ->> 'role') IN ('admin', 'super_admin')
    -- An admin cannot UPDATE a super_admin row in their own tenant.
    -- super_admins themselves bypass via the users_super_admin policy
    -- below (PERMISSIVE policies are OR'd at evaluation time).
    AND role IN ('user', 'admin')
  )
  WITH CHECK (
    company_id = (auth.jwt() ->> 'company_id')::uuid
    -- Role clamp on the NEW row prevents promotion to super_admin.
    AND role IN ('user', 'admin')
  );

DROP POLICY IF EXISTS users_admin_delete ON users;
CREATE POLICY users_admin_delete ON users
  FOR DELETE TO authenticated
  USING (
    company_id = (auth.jwt() ->> 'company_id')::uuid
    AND (auth.jwt() ->> 'role') IN ('admin', 'super_admin')
    -- An admin cannot DELETE a super_admin row in their own tenant.
    AND role IN ('user', 'admin')
  );

COMMIT;

-- ─────────────────────────────────────────────────────────
-- VERIFY (run manually after COMMIT)
-- ─────────────────────────────────────────────────────────
-- Expected: 2 rows, both columns = true
--   SELECT
--     policyname,
--     cmd,
--     qual LIKE '%role IN%' AS using_has_role_clamp,
--     COALESCE(with_check LIKE '%role IN%', true) AS check_has_role_clamp_or_no_check_needed
--   FROM pg_policies
--   WHERE schemaname='public' AND tablename='users'
--     AND policyname IN ('users_admin_update', 'users_admin_delete')
--   ORDER BY policyname;
