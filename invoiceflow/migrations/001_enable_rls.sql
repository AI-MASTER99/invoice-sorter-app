-- ============================================================
-- Migration 001 — Enable Row-Level Security (RLS)
-- ============================================================
-- Batch 2, Fase A: schema-only migration. Adds:
--   1. CHECK constraint on users.role (defense-in-depth for R8)
--   2. ENABLE RLS on all 5 tenant tables
--   3. Tenant-isolation policies (auth.jwt() ->> 'company_id')
--   4. Super-admin bypass policies (auth.jwt() ->> 'role' = 'super_admin')
--
-- IMPORTANT: This migration is risk-free for the running app because
-- the Python code uses the service-role key (sb_secret_) which has
-- BYPASSRLS privilege. RLS becomes effective in Fase B when we mint
-- per-request user JWTs and switch DAL calls to a user-scoped client.
--
-- Run via: Supabase Dashboard → SQL Editor → paste → Run
-- Verify after:  see "VERIFY" section at the bottom
-- Rollback:      run 001_enable_rls_rollback.sql
-- ============================================================

BEGIN;

-- ─────────────────────────────────────────────────────────
-- 0. SANITY CHECK: detect legacy rows with invalid roles
-- ─────────────────────────────────────────────────────────
-- If this returns any row, the CHECK below will fail. Investigate
-- before continuing (e.g. UPDATE users SET role='user' WHERE …).
DO $$
DECLARE
  bad_count integer;
BEGIN
  SELECT count(*) INTO bad_count
  FROM users
  WHERE role NOT IN ('user', 'admin', 'super_admin');

  IF bad_count > 0 THEN
    RAISE EXCEPTION
      'Found % users with invalid role values. Fix before adding CHECK.',
      bad_count;
  END IF;
END $$;

-- ─────────────────────────────────────────────────────────
-- 1. CHECK CONSTRAINT on users.role
-- ─────────────────────────────────────────────────────────
-- NOT VALID first, then VALIDATE — standard pattern for adding
-- a constraint to an existing table without long table-locks.
ALTER TABLE users
  ADD CONSTRAINT users_role_chk
  CHECK (role IN ('user', 'admin', 'super_admin')) NOT VALID;

ALTER TABLE users VALIDATE CONSTRAINT users_role_chk;

-- Document the canonical role list at the column level so it's
-- discoverable via psql \d+ users (schema.sql comment was stale).
COMMENT ON COLUMN users.role IS
  'One of: user, admin, super_admin (enforced by users_role_chk)';

-- ─────────────────────────────────────────────────────────
-- 2. ENABLE RLS on all 5 tenant tables
-- ─────────────────────────────────────────────────────────
-- We do NOT use FORCE ROW LEVEL SECURITY: service-role (BYPASSRLS)
-- must continue to work for the login flow, Supabase Studio, and
-- any operational scripts. Only the `authenticated` and `anon`
-- Postgres roles will be subject to these policies.
ALTER TABLE companies      ENABLE ROW LEVEL SECURITY;
ALTER TABLE users          ENABLE ROW LEVEL SECURITY;
ALTER TABLE invoices       ENABLE ROW LEVEL SECURITY;
ALTER TABLE product_memory ENABLE ROW LEVEL SECURITY;
ALTER TABLE jobs           ENABLE ROW LEVEL SECURITY;

-- ─────────────────────────────────────────────────────────
-- 3. COMPANIES policies
-- ─────────────────────────────────────────────────────────
-- A regular authenticated user can only SELECT their own company row.
-- INSERT/UPDATE/DELETE: super_admin only (via the super_admin policy).
CREATE POLICY companies_tenant_select ON companies
  FOR SELECT TO authenticated
  USING (id = (auth.jwt() ->> 'company_id')::uuid);

CREATE POLICY companies_super_admin ON companies
  FOR ALL TO authenticated
  USING ((auth.jwt() ->> 'role') = 'super_admin')
  WITH CHECK ((auth.jwt() ->> 'role') = 'super_admin');

-- ─────────────────────────────────────────────────────────
-- 4. USERS policies
-- ─────────────────────────────────────────────────────────
-- SELECT  : any authenticated user can list users in their own company
-- INSERT  : admin/super_admin only, must stay in own company
-- UPDATE  : admin/super_admin only, must stay in own company
-- DELETE  : admin/super_admin only, must stay in own company
-- super_admin override: full access across tenants
--
-- Note: self-password-change is intentionally NOT covered here.
-- We will route it through a SECURITY DEFINER RPC in Fase B so we
-- can restrict UPDATE to the password_hash column only — RLS itself
-- is row-level and cannot enforce column-level restrictions.
--
-- ⚠️  PHASE B DEPENDENCY ⚠️
-- When Fase B switches Python to user-scoped JWTs (anon key + JWT)
-- instead of the service-role key, the SECURITY DEFINER RPC
--     change_own_password(text)
-- MUST already exist in the DB before the cutover. Without it, every
-- non-admin user will get 403 on /api/users/{username}/password
-- because none of the policies below grant a normal user UPDATE on
-- their own row. This is a hard prerequisite for Fase B — do NOT
-- ship the Python switch without the RPC.

CREATE POLICY users_tenant_select ON users
  FOR SELECT TO authenticated
  USING (company_id = (auth.jwt() ->> 'company_id')::uuid);

CREATE POLICY users_admin_insert ON users
  FOR INSERT TO authenticated
  WITH CHECK (
    company_id = (auth.jwt() ->> 'company_id')::uuid
    AND (auth.jwt() ->> 'role') IN ('admin', 'super_admin')
    -- Role clamp: a regular admin must not mint super_admin accounts.
    -- super_admins themselves bypass via the users_super_admin policy
    -- below (PERMISSIVE policies are OR'd at evaluation time).
    AND role IN ('user', 'admin')
  );

CREATE POLICY users_admin_update ON users
  FOR UPDATE TO authenticated
  USING (
    company_id = (auth.jwt() ->> 'company_id')::uuid
    AND (auth.jwt() ->> 'role') IN ('admin', 'super_admin')
  )
  WITH CHECK (
    company_id = (auth.jwt() ->> 'company_id')::uuid
    -- Role clamp on the NEW row: a regular admin must not be able to
    -- promote anyone (including themselves) to super_admin via UPDATE.
    -- super_admins bypass via the users_super_admin policy below.
    AND role IN ('user', 'admin')
  );

CREATE POLICY users_admin_delete ON users
  FOR DELETE TO authenticated
  USING (
    company_id = (auth.jwt() ->> 'company_id')::uuid
    AND (auth.jwt() ->> 'role') IN ('admin', 'super_admin')
  );

CREATE POLICY users_super_admin ON users
  FOR ALL TO authenticated
  USING ((auth.jwt() ->> 'role') = 'super_admin')
  WITH CHECK ((auth.jwt() ->> 'role') = 'super_admin');

-- ─────────────────────────────────────────────────────────
-- 5. INVOICES policies
-- ─────────────────────────────────────────────────────────
-- Full CRUD for any authenticated user within their own company.
CREATE POLICY invoices_tenant_all ON invoices
  FOR ALL TO authenticated
  USING (company_id = (auth.jwt() ->> 'company_id')::uuid)
  WITH CHECK (company_id = (auth.jwt() ->> 'company_id')::uuid);

CREATE POLICY invoices_super_admin ON invoices
  FOR ALL TO authenticated
  USING ((auth.jwt() ->> 'role') = 'super_admin')
  WITH CHECK ((auth.jwt() ->> 'role') = 'super_admin');

-- ─────────────────────────────────────────────────────────
-- 6. PRODUCT_MEMORY policies
-- ─────────────────────────────────────────────────────────
CREATE POLICY memory_tenant_all ON product_memory
  FOR ALL TO authenticated
  USING (company_id = (auth.jwt() ->> 'company_id')::uuid)
  WITH CHECK (company_id = (auth.jwt() ->> 'company_id')::uuid);

CREATE POLICY memory_super_admin ON product_memory
  FOR ALL TO authenticated
  USING ((auth.jwt() ->> 'role') = 'super_admin')
  WITH CHECK ((auth.jwt() ->> 'role') = 'super_admin');

-- ─────────────────────────────────────────────────────────
-- 7. JOBS policies
-- ─────────────────────────────────────────────────────────
CREATE POLICY jobs_tenant_all ON jobs
  FOR ALL TO authenticated
  USING (company_id = (auth.jwt() ->> 'company_id')::uuid)
  WITH CHECK (company_id = (auth.jwt() ->> 'company_id')::uuid);

CREATE POLICY jobs_super_admin ON jobs
  FOR ALL TO authenticated
  USING ((auth.jwt() ->> 'role') = 'super_admin')
  WITH CHECK ((auth.jwt() ->> 'role') = 'super_admin');

COMMIT;

-- ─────────────────────────────────────────────────────────
-- VERIFY  (run these manually after COMMIT)
-- ─────────────────────────────────────────────────────────
-- Expected: rowsecurity = true on all 5 tables
--   SELECT schemaname, tablename, rowsecurity
--   FROM pg_tables
--   WHERE schemaname = 'public'
--   ORDER BY tablename;
--
-- Expected: 13 policies total across 5 tables
--   SELECT schemaname, tablename, policyname, cmd, roles
--   FROM pg_policies
--   WHERE schemaname = 'public'
--   ORDER BY tablename, policyname;
--
-- Expected: 1 row, constraint name = users_role_chk, validated = true
--   SELECT conname, convalidated
--   FROM pg_constraint
--   WHERE conrelid = 'public.users'::regclass
--     AND contype = 'c';
