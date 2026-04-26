-- ============================================================
-- Migration 002 — ROLLBACK
-- ============================================================
-- Reverts 002_role_clamp_fix.sql to the post-001 state (the
-- pre-review version of users_admin_update / users_admin_delete
-- with no OLD-row role clamp).
--
-- ⚠️  NOTE: rolling back to the pre-review state RE-INTRODUCES the
-- DoS vulnerability (admin can demote/delete a super_admin in
-- their own tenant). Only roll back if the new policies cause an
-- unexpected production break.
--
-- Run via: Supabase Dashboard → SQL Editor → paste → Run
-- ============================================================

BEGIN;

DROP POLICY IF EXISTS users_admin_update ON users;
CREATE POLICY users_admin_update ON users
  FOR UPDATE TO authenticated
  USING (
    company_id = (auth.jwt() ->> 'company_id')::uuid
    AND (auth.jwt() ->> 'role') IN ('admin', 'super_admin')
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
  );

COMMIT;
