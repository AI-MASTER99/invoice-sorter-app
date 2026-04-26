-- ============================================================
-- Migration 003 — change_own_password SECURITY DEFINER RPC
-- ============================================================
-- Drafted 2026-04-26 as part of Phase B. RLS policies are row-level
-- and cannot enforce column-level restrictions: a normal authenticated
-- user has no policy that lets them UPDATE their own users row, even
-- if the only column changing is password_hash. We route password
-- self-change through this RPC, which runs with the function-owner's
-- privileges (typically `postgres`, BYPASSRLS) and gates access by
-- reading `auth.jwt() ->> 'user_id'` itself.
--
-- ⚠️  ORDERING: ship this migration BEFORE the Phase B Python deploy.
-- Without it, every non-admin who tries to change their own password
-- gets 403 because none of the migration-001/003a policies grant a
-- normal user UPDATE on their own row.
--
-- ⚠️  PAIR WITH: migration 003a (rename role → app_role).
--
-- Run via: Supabase Dashboard → SQL Editor → paste → Run
-- Verify after: see VERIFY section at the bottom
-- Rollback:    run 003_change_own_password_rpc_rollback.sql
-- ============================================================

BEGIN;

-- The function reads auth.jwt() inside SECURITY DEFINER. This works
-- in Supabase because auth.jwt() reads the request.jwt.claims GUC
-- which is set per-request by GoTrue/PostgREST — independent of the
-- SET ROLE that SECURITY DEFINER does. (Smoke-tested in §5.3 of
-- PHASE_B_PLAN.md.)
--
-- SET search_path is critical: without it SECURITY DEFINER functions
-- are exposed to search-path-based privilege escalation.
CREATE OR REPLACE FUNCTION public.change_own_password(new_hash text)
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
  caller_id uuid;
BEGIN
  caller_id := (auth.jwt() ->> 'user_id')::uuid;
  IF caller_id IS NULL THEN
    RAISE EXCEPTION 'No authenticated caller';
  END IF;

  -- Bcrypt prefix check: $2a$NN$<53 chars>, $2b$NN$<53 chars>,
  -- $2y$NN$<53 chars>. Accept argon2 prefixes for forward compat.
  -- Catches the "Python passed plaintext" defense-in-depth case.
  IF new_hash !~ '^(\$2[aby]\$\d{2}\$.{53}|\$argon2(id|i)\$.+)$' THEN
    RAISE EXCEPTION 'Invalid hash format';
  END IF;

  UPDATE public.users
     SET password_hash = new_hash,
         updated_at    = now()
   WHERE id = caller_id;
END;
$$;

-- Lock down: only the `authenticated` Postgres role can call this.
-- (Service-role bypasses GRANTs anyway because it has BYPASSRLS and
-- is a Postgres superuser-equivalent for these purposes.)
--
-- ⚠️  Supabase auto-grants EXECUTE on new functions to {anon,
-- authenticated, service_role} via default privileges. REVOKE FROM
-- PUBLIC does NOT undo those role-specific grants — we have to
-- REVOKE FROM anon explicitly. Defense-in-depth: the internal
-- `auth.jwt() ->> 'user_id'` check would already error out on an
-- anon call, but principle-of-least-privilege wins.
REVOKE ALL ON FUNCTION public.change_own_password(text) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION public.change_own_password(text) FROM anon;
GRANT  EXECUTE ON FUNCTION public.change_own_password(text) TO authenticated;

COMMIT;

-- ─────────────────────────────────────────────────────────
-- VERIFY (run manually after COMMIT)
-- ─────────────────────────────────────────────────────────
-- 1) Function exists, is SECURITY DEFINER, has the search_path lockdown
--   SELECT proname, prosecdef, proconfig
--   FROM pg_proc
--   WHERE proname = 'change_own_password'
--     AND pronamespace = 'public'::regnamespace;
--   -- Expected: 1 row, prosecdef=true, proconfig contains
--   -- 'search_path=public, pg_temp'.
--
-- 2) GRANTs are correct: anon has NO EXECUTE; authenticated does.
--   SELECT grantee, privilege_type
--   FROM information_schema.routine_privileges
--   WHERE routine_schema = 'public'
--     AND routine_name = 'change_own_password'
--   ORDER BY grantee;
--   -- Expected: 3 rows — authenticated, postgres (function owner),
--   -- service_role. NO row for `anon` (we revoked it). All three
--   -- privilege_type='EXECUTE'.
--
-- 3) Plaintext hash is rejected (manual smoke from psql or SQL Editor;
--    note this requires a JWT with user_id claim, so easier to test
--    via the application after Phase B ships).
--   SELECT change_own_password('plaintext');
--   -- Expected: ERROR: Invalid hash format
