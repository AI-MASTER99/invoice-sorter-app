-- ============================================================
-- Migration 001 — VERIFY (run these 3 queries one by one)
-- ============================================================

-- VERIFY 1: RLS enabled op alle 5 tenant-tabellen
-- Verwacht: 5 rijen, allemaal rowsecurity = true
SELECT schemaname, tablename, rowsecurity
FROM pg_tables
WHERE schemaname = 'public'
  AND tablename IN ('companies', 'users', 'invoices', 'product_memory', 'jobs')
ORDER BY tablename;


-- VERIFY 2: 13 policies in totaal
-- Verwacht: 13 rijen, gegroepeerd per tabel
SELECT tablename, policyname, cmd, roles
FROM pg_policies
WHERE schemaname = 'public'
ORDER BY tablename, policyname;


-- VERIFY 3: CHECK constraint op users.role
-- Verwacht: 1 rij, conname = 'users_role_chk', convalidated = true
SELECT conname, convalidated
FROM pg_constraint
WHERE conrelid = 'public.users'::regclass
  AND contype = 'c';
